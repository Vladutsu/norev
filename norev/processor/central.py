"""Central Processor: the one gate for every model call and every Connector send (ADR 0001/0004, #22).

docs/ARCHITECTURE.md, "One gate for every outbound call": it is the only object that holds the real
LlmClient, Transcriber, Diarizer and Connectors. Use cases and adapters get the processor itself as
their LlmClient (it has the same `generate_json`), so extraction, translation, the Domain stage and
Others all call the model through here.

A send checks that the action is Approved with exactly that payload, writes the attempt to the audit
log, calls the Connector, then writes the result. It does not change the action's status: a Workflow
can have several steps, so the Workflow executor (#23) marks Executed or Failed.
"""
import copy
import json
import logging
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from norev.core.models import ActionStatus, AuditEvent, ProposedAction, SpeakerTurn, Transcript
from norev.core.ports import Clock, Connector, Diarizer, LlmClient, Transcriber
from norev.processor.identity import LocalUser

# Ids, Connector names and error types only: never payload, prompt or Transcript text
log = logging.getLogger(__name__)


class SendRefused(ValueError):
    """The send is not allowed; nothing was written to the audit log and the Connector was not called."""


class AuditFailed(RuntimeError):
    """The attempt could not be written to the audit log, so the Connector was not called."""


class ConnectorFailed(RuntimeError):
    """The Connector raised. The message has only ids and the error type; the original error can quote
    the payload, so it is kept on `cause` and left out of the traceback."""

    def __init__(self, action_id: str, connector: str, cause: Exception) -> None:
        super().__init__(f"Connector {connector} failed for action {action_id} ({type(cause).__name__})")
        self.cause = cause


@dataclass(frozen=True)
class SendResult:
    """What happened to one attempt; written after the Connector answers or raises."""

    at: datetime
    action_id: str
    connector: str
    ok: bool
    error_type: str | None = None  # exception class name only, never its message


@runtime_checkable
class ExecutionAuditLog(Protocol):
    """AuditLog plus the result of each attempt. An attempt with no result is an uncertain send:
    it is shown for reconciliation, never retried automatically (#23).

    # shortcut: defined here, not in core/ports, until the contract is confirmed with #18/#26
    """

    def record(self, event: AuditEvent) -> None: ...

    def record_result(self, result: SendResult) -> None: ...


def _canonical(payload: Any) -> str:
    """Exact comparison: 1, 1.0 and true differ, key order does not. Non-JSON payloads raise."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, allow_nan=False)


class GpuGate:
    """One model on the GPU at a time: Whisper, diarization and the LLM share 6-8 GB of VRAM.

    Calls for the model in use run together (llama-server --parallel). A call for another model waits
    until they end, then the model in use is released, and only then does the new call run. The next
    model is claimed first, so new calls for the current one cannot starve it. Nothing is loaded ahead
    of time: an adapter loads its model on its first call.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._current: str | None = None
        self._release: Callable[[], None] | None = None
        self._active = 0
        self._next: str | None = None

    @contextmanager
    def use(self, name: str, release: Callable[[], None] | None = None) -> Iterator[None]:
        """`release` frees this model's GPU memory; it is called when another model needs the GPU."""
        with self._cond:
            while (self._active and self._current != name) or self._next not in (None, name):
                if self._next is None:
                    self._next = name
                self._cond.wait()
            if self._next == name:
                self._next = None
            if self._current != name:
                try:
                    if self._release is not None:
                        self._release()  # if this fails the new model does not run
                except BaseException:
                    self._cond.notify_all()
                    raise
                self._current, self._release = name, release
            self._active += 1
        try:
            yield
        finally:
            with self._cond:
                self._active -= 1
                self._cond.notify_all()


class CentralProcessor:
    def __init__(
        self,
        llm: LlmClient,
        audit: ExecutionAuditLog,
        *,
        identity: LocalUser,
        clock: Clock,
        connectors: Iterable[Connector] = (),
        transcriber: Transcriber | None = None,
        diarizer: Diarizer | None = None,
    ) -> None:
        self._llm = llm
        self._audit = audit
        self.identity = identity
        self._clock = clock
        self._connectors: dict[str, Connector] = {}
        for connector in connectors:
            if connector.name in self._connectors:
                raise ValueError(f"two Connectors named {connector.name!r}")
            self._connectors[connector.name] = connector
        self._transcriber = transcriber
        self._diarizer = diarizer
        self._gpu = GpuGate()

    # ── model calls ─────────────────────────────────────────────────────────

    def generate_json(self, prompt: str, *, model: str | None = None, json_schema: dict | None = None) -> str:
        """LlmClient for every stage. llama-server runs out of process, so there is no release hook:
        the gate only keeps Whisper and diarization off the GPU while LLM calls run."""
        with self._gpu.use(f"llm:{model or ''}"):
            return self._llm.generate_json(prompt, model=model, json_schema=json_schema)

    def transcribe(self, audio_ref: str, language: str | None = None) -> Transcript:
        if self._transcriber is None:
            raise RuntimeError("no Transcriber configured")
        with self._gpu.use("transcriber", getattr(self._transcriber, "close", None)):
            return self._transcriber.transcribe(audio_ref, language)

    def diarize(self, audio_ref: str) -> list[SpeakerTurn]:
        if self._diarizer is None:
            raise RuntimeError("no Diarizer configured")
        with self._gpu.use("diarizer", getattr(self._diarizer, "close", None)):
            return self._diarizer.diarize(audio_ref)

    def free_gpu(self) -> None:
        """Release Whisper and diarization before an out-of-process model (llama-server) loads."""
        with self._gpu.use("external"):
            pass

    # ── Connector sends ─────────────────────────────────────────────────────

    def has_connector(self, name: str) -> bool:
        return name in self._connectors

    def check_send(
        self, action: ProposedAction, connector: str, payload: dict[str, Any], *, step: str | None = None
    ) -> dict[str, Any]:
        """The checks of send(), with no side effect; returns the approved payload that would be sent.

        `step`: the approved payload is an approval plan (#18, `{"steps": [{"step", "connector",
        "payload"}], ...}`); the step must be in it with this Connector, and `payload` must equal exactly
        that step's payload. Without `step`, `payload` must equal the whole approved payload.
        """
        # shortcut: checks this in-memory copy; the atomic claim of the action in the store comes with #26
        if connector not in self._connectors:
            raise SendRefused(f"unknown Connector {connector!r}")
        if action.status is not ActionStatus.APPROVED or action.approved_payload is None:
            raise SendRefused(f"action {action.id} is {action.status.value}, not approved")
        self._approved_by(action)
        snapshot = action.approved_payload
        if step is not None:
            steps = snapshot.get("steps") if isinstance(snapshot, dict) else None
            matches = [s for s in steps if isinstance(s, dict) and s.get("step") == step] if isinstance(steps, list) else []
            if len(matches) != 1 or matches[0].get("connector") != connector or not isinstance(matches[0].get("payload"), dict):
                raise SendRefused(f"step {step} with Connector {connector} is not in the approved plan of action {action.id}")
            snapshot = matches[0]["payload"]
        try:
            same = _canonical(payload) == _canonical(snapshot)
        except (TypeError, ValueError):
            same = False
        if not same:
            raise SendRefused(f"payload for action {action.id} differs from the approved one")
        return copy.deepcopy(snapshot)  # the Connector cannot change the approved snapshot

    def _approved_by(self, action: ProposedAction) -> str:
        """Who approved it: #18 stores it on the action, checked against the LocalUser contract;
        the processor's own identity for an action approved without it."""
        if action.approved_by is None:
            return self.identity.id
        try:
            return LocalUser(action.approved_by).id
        except ValueError:
            raise SendRefused(f"action {action.id} has an invalid approved_by") from None

    def send(
        self, action: ProposedAction, connector: str, payload: dict[str, Any], *, step: str | None = None
    ) -> dict[str, Any]:
        """Send `payload` through `connector` if it is exactly the payload the User approved (ADR 0002).

        Order: checks -> audit attempt -> Connector -> audit result. Raises SendRefused or AuditFailed
        without calling the Connector, ConnectorFailed if it raised. Returns the Connector's answer.
        """
        sent = self.check_send(action, connector, payload, step=step)
        target = self._connectors[connector]
        event = AuditEvent(self._clock.now(), action.id, connector, copy.deepcopy(sent), self._approved_by(action))
        try:
            self._audit.record(event)
        except Exception as exc:
            log.error("audit failed, not sent: action=%s connector=%s error=%s", action.id, connector, type(exc).__name__)
            raise AuditFailed(f"audit not written for action {action.id}; nothing was sent") from None

        error: Exception | None = None
        try:
            answer = target.send(sent)
        except Exception as exc:  # any Connector error is a failed send
            error = exc
        # outside the except block, so a failing record_result does not chain the Connector error
        self._audit.record_result(
            SendResult(self._clock.now(), action.id, connector, ok=error is None,
                       error_type=type(error).__name__ if error else None)
        )
        if error is not None:
            log.warning("send failed: action=%s connector=%s error=%s", action.id, connector, type(error).__name__)
            raise ConnectorFailed(action.id, connector, error) from None
        log.info("sent: action=%s connector=%s", action.id, connector)
        return answer
