"""Workflow executor (#23): runs the Workflow of an approved Proposed Action, one step at a time.

- Only known steps run. A step with a Connector goes through the Central Processor, which checks the
  approved payload and audits the send. A step without one must be in the explicit registry of local
  steps the app gives the executor. Anything else refuses the whole Workflow before any step runs, so
  a Domain or a model answer cannot name code or commands to run.
- Every Connector step's payload is checked against the approved snapshot before the first step runs:
  an action edited after approval runs nothing.
- Each step's state is saved before and after it runs. A step saved as started, with no end, is
  uncertain (the app stopped mid-step): it is never run again automatically, the User reconciles it.
- One run per action at a time: a second Execute while one runs is refused (double click).
- Retry is manual, keeps the approved payload and skips the steps already done (OPEN-QUESTIONS #7).
"""
import copy
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from norev.core.models import ActionStatus, Domain, ProposedAction, WorkflowStep
from norev.core.ports import SessionStore
from norev.processor.central import CentralProcessor, ConnectorFailed

GENERIC_FIELDS = ("title", "description", "owner", "due_date", "transcript_excerpt")  # domains/README.md


class StepState(str, Enum):
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class StepRecord:
    step: str
    state: StepState
    result: dict[str, Any] | None = None  # what the step answered, e.g. the path of the document
    error_type: str | None = None  # exception class name only, never its message

    def __repr__(self) -> str:
        return f"StepRecord({self.step}, {self.state.value}, result_keys={sorted(self.result or {})}, error={self.error_type})"


@runtime_checkable
class ExecutionStore(Protocol):
    """Step states of each action's Workflow; must survive a restart.

    # shortcut: defined here, not in core/ports, until the contract is confirmed with #26, which stores it
    """

    def load_steps(self, action_id: str) -> dict[str, StepRecord]: ...

    def save_step(self, action_id: str, record: StepRecord) -> None: ...


class ExecutionRefused(ValueError):
    """Nothing ran: the action, its Workflow or its payload does not allow it."""


class AlreadyRunning(ExecutionRefused):
    pass


@dataclass(frozen=True)
class ExecutionOutcome:
    action_id: str
    status: str  # "executed", "failed", or "uncertain": a step must be reconciled by the User first
    steps: dict[str, StepRecord]
    uncertain_step: str | None = None


# A local step: gets a copy of the action, returns its result (or None). Registered by the app, by name.
LocalStep = Callable[[ProposedAction], dict[str, Any] | None]


def step_payload(action: ProposedAction, step: WorkflowStep) -> dict[str, Any]:
    """The payload a Connector step sends, built from the action's current values, in `sends` order."""
    payload = {}
    for name in step.sends:
        if name in action.fields:
            payload[name] = action.fields[name]
        elif name == "due_date":
            payload[name] = action.due_date.isoformat() if action.due_date else None
        elif name in GENERIC_FIELDS:
            payload[name] = getattr(action, name)
        else:
            raise ExecutionRefused(f"step {step.name} sends unknown field {name!r}")
    return payload


class WorkflowExecutor:
    def __init__(
        self,
        processor: CentralProcessor,
        sessions: SessionStore,
        steps: ExecutionStore,
        domain: Domain,
        local_steps: Mapping[str, LocalStep] | None = None,
    ) -> None:
        self._processor = processor
        self._sessions = sessions
        self._steps = steps
        self._domain = domain
        self._local = dict(local_steps or {})
        self._lock = threading.Lock()
        self._running: set[str] = set()

    def preview(self, session_id: str, action_id: str) -> list[dict[str, Any]]:
        """For the approval screen: every Connector step with its exact payload and what the Connector
        will do with it (for local_document: destination and content). No side effect."""
        action = self._action(session_id, action_id)
        previews = []
        for step in self._workflow(action):
            if step.connector:
                payload = step_payload(action, step)
                previews.append(
                    {"step": step.name, "label": step.label, "connector": step.connector, "payload": payload,
                     **self._processor.preview(step.connector, payload)}
                )
        return previews

    def execute(self, session_id: str, action_id: str) -> ExecutionOutcome:
        with self._one_run(action_id):
            action = self._action(session_id, action_id)
            if action.status is not ActionStatus.APPROVED:
                raise ExecutionRefused(f"action {action_id} is {action.status.value}, not approved")
            workflow = self._workflow(action)
            payloads = {}
            for step in workflow:  # before any step runs, so a changed payload runs nothing
                if step.connector:
                    payloads[step.name] = step_payload(action, step)
                    self._processor.check_send(action, step.connector, payloads[step.name], fields=step.sends)

            records = self._steps.load_steps(action_id)
            uncertain = [s.name for s in workflow if s.name in records and records[s.name].state is StepState.STARTED]
            if uncertain:
                return ExecutionOutcome(action_id, "uncertain", records, uncertain[0])

            for step in workflow:
                if step.name in records and records[step.name].state is StepState.DONE:
                    continue  # done in an earlier run: a Retry does not repeat it
                self._save_step(action_id, records, StepRecord(step.name, StepState.STARTED))
                error_type = None
                try:
                    if step.connector:
                        result = self._processor.send(action, step.connector, payloads[step.name], fields=step.sends)
                    else:
                        result = self._local[step.name](copy.deepcopy(action))
                except ConnectorFailed as exc:
                    error_type = type(exc.cause).__name__
                except Exception as exc:  # any step error fails the step; its message may quote Session data
                    error_type = type(exc).__name__
                if error_type is None:
                    self._save_step(action_id, records, StepRecord(step.name, StepState.DONE, result=result))
                    continue
                self._save_step(action_id, records, StepRecord(step.name, StepState.FAILED, error_type=error_type))
                action.mark_failed(f"Step {step.label or step.name} failed ({error_type}).")
                self._save_action(session_id, action)
                return ExecutionOutcome(action_id, "failed", records)

            action.mark_executed()
            self._save_action(session_id, action)
            return ExecutionOutcome(action_id, "executed", records)

    def retry(self, session_id: str, action_id: str) -> None:
        """Manual Retry of a Failed action: back to Approved with the same approved payload."""
        with self._one_run(action_id):
            action = self._action(session_id, action_id)
            action.retry()
            self._save_action(session_id, action)

    def reconcile(self, session_id: str, action_id: str, step: str, happened: bool) -> None:
        """The User's answer for an uncertain step: it did happen (done, not run again) or it did not
        (it runs on the next Execute)."""
        with self._one_run(action_id):
            self._action(session_id, action_id)
            records = self._steps.load_steps(action_id)
            if step not in records or records[step].state is not StepState.STARTED:
                raise ExecutionRefused(f"step {step} of action {action_id} is not uncertain")
            if happened:
                record = StepRecord(step, StepState.DONE, result={"reconciled": True})
            else:
                record = StepRecord(step, StepState.FAILED, error_type="NotSent")
            self._steps.save_step(action_id, record)

    # ── helpers ─────────────────────────────────────────────────────────────

    @contextmanager
    def _one_run(self, action_id: str) -> Iterator[None]:
        # In process: after a restart nothing is running, and a step left started is uncertain instead
        with self._lock:
            if action_id in self._running:
                raise AlreadyRunning(f"action {action_id} is already running")
            self._running.add(action_id)
        try:
            yield
        finally:
            with self._lock:
                self._running.discard(action_id)

    def _action(self, session_id: str, action_id: str) -> ProposedAction:
        action = next((a for a in self._sessions.load_actions(session_id) if a.id == action_id), None)
        if action is None:
            raise LookupError(f"no Proposed Action {action_id!r} in Session {session_id!r}")
        return action

    def _workflow(self, action: ProposedAction) -> tuple[WorkflowStep, ...]:
        action_type = self._domain.action_type(action.action_type) if action.action_type else None
        if action_type is None:
            raise ExecutionRefused(f"action {action.id} has no Action Type of Domain {self._domain.name}")
        for step in action_type.workflow.steps:
            if step.connector:
                if not self._processor.has_connector(step.connector):
                    raise ExecutionRefused(f"step {step.name}: unknown Connector {step.connector!r}")
            elif step.name not in self._local:
                raise ExecutionRefused(f"step {step.name}: no known local implementation")
        return action_type.workflow.steps

    def _save_step(self, action_id: str, records: dict[str, StepRecord], record: StepRecord) -> None:
        self._steps.save_step(action_id, record)
        records[record.step] = record

    def _save_action(self, session_id: str, action: ProposedAction) -> None:
        # shortcut: read-modify-write of the Session's list; the atomic update comes with the store (#26)
        actions = self._sessions.load_actions(session_id)
        self._sessions.save_actions(session_id, [action if a.id == action.id else a for a in actions])
