"""The desktop UI on the real Session flow (#43): microphone or audio file -> encrypted store ->
Transcript with Speakers -> analysis (#19) -> review (#18) -> Workflow executor (#23).

Review, edit, preview, approve and reject are DemoBackend's, on the real store. What differs:
- recording captures the microphone into memory; the audio goes into the store sealed, never to a file;
- Stop starts the analysis in the background and `progress` reports its stage; one failed stage
  stops the analysis and keeps what was saved before it;
- llama-server runs only for the LLM stages, after Whisper and diarization released the GPU;
- Execute, Retry and reconcile go through the WorkflowExecutor, so through the Central Processor.
  Nothing here holds a Connector.
"""
import io
import logging
import os
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from norev.adapters.domains.loader import load_others
from norev.adapters.llm.legacy_extractor import LegacyActionExtractor
from norev.adapters.llm.translator import TurnTranslator
from norev.adapters.recorder.recorder import Recorder
from norev.core.models import ActionStatus, Domain, OthersPrompt, ProposedAction, Session, StepState
from norev.core.ports import Clock, PayloadBuilder, SessionStore
from norev.processor.payloads import LocalDocumentPayloadBuilder
from norev.processor.workflow import ExecutionRefused, WorkflowExecutor
from norev.ui._live import LiveFeedback, monitor
from norev.ui.backend import MODES, ROOT, DemoBackend, ModeUnavailable, PromptNotConfirmed, prompt_digest
from norev.usecases import others_prompt
from norev.usecases.session_flow import run_session
from norev.usecases.speaker_roles import set_speaker_role
from norev.usecases.transcribe import transcribe_session

log = logging.getLogger(__name__)

# Stages shown while a Session is analysed, in order
STAGES = ("recording", "transcribing", "starting_llm", "analysing", "ready")


class SessionBusy(ValueError):
    """The Session is recording or being analysed; it cannot be deleted now."""


class MicrophoneUnavailable(ValueError):
    """No input device could be opened. Nothing was recorded or saved."""


def _thread(work: Callable[[], None]) -> None:
    threading.Thread(target=work, daemon=True).start()


def open_with_system(path: Path) -> None:
    """Opens a file with the system's default app for its type (the mail app for .eml)."""
    if hasattr(os, "startfile"):
        os.startfile(path)  # Windows
    else:
        subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])


class SessionBackend(DemoBackend):
    """`processor` is the Central Processor (transcriber, diarizer, LLM and Connectors, #22).
    `llm_session()` wraps the LLM stages and yields the translation model; llama-server is stopped at
    its end (#43, #62).
    `record_source()` gives the microphone's PCM blocks; `pick_file()` the audio file the User chose.
    `builder` makes the previews (default: local documents only); `google` is the User's GoogleAccount
    for connect/disconnect (#45), None when Google Calendar is not set up. `open_file` opens a saved
    email draft in the User's mail app (#61).
    """

    can_execute = True

    def __init__(self, store: SessionStore, *, approved_by: str, processor, export_dir: Path,
                 llm_session: Callable[[], AbstractContextManager[str | None]],
                 record_source: Callable[[], Iterable[bytes]],
                 pick_file: Callable[[], str | None] = lambda: None,
                 workers: int = 1, make_extractor=LegacyActionExtractor, clock: Clock | None = None, domains_dir: Path = ROOT / "domains",
                 monotonic=time.monotonic, run_in_background: Callable[[Callable[[], None]], None] = _thread,
                 builder: PayloadBuilder | None = None, google=None,
                 open_file: Callable[[Path], None] = open_with_system, live_feedback=LiveFeedback) -> None:
        super().__init__(store, approved_by=approved_by, clock=clock, domains_dir=domains_dir, monotonic=monotonic)
        self.processor = processor
        self.export_dir = Path(export_dir)
        self.open_file = open_file
        self.builder = builder or LocalDocumentPayloadBuilder(export_dir)
        self.google = google
        self.llm_session = llm_session
        self.record_source = record_source
        self.pick_file = pick_file
        self.workers = workers
        self.make_extractor = make_extractor  # (llm, workers=) -> ActionExtractor; tests give a fake
        self.live_feedback = live_feedback  # (language) -> LiveFeedback (#47); tests give a fake
        self.run_in_background = run_in_background
        self._lock = threading.Lock()
        self._progress: dict[str, dict[str, Any]] = {}
        self._recordings: dict[str, dict[str, Any]] = {}
        self._others: dict[str, OthersPrompt] = {}  # confirmed prompts by digest, until a Session takes one
        self._executors: dict[str, WorkflowExecutor] = {}  # one per Domain, so a double click runs once

    # --- modes and Others (#21: generated with the local model)

    def _available(self, mode: str) -> bool:
        return (self.domains_dir / mode / "domain.toml").is_file()

    def domain(self, name: str) -> Domain:
        if not self._available(name):
            raise ModeUnavailable(name)
        return super().domain(name) if name != "others" else load_others(self.domains_dir / "others")[0]

    def others_generate(self, context: str) -> dict[str, str]:
        """Instructions for the context, from the local model; the User edits and confirms them."""
        domain, template, schema = load_others(self.domains_dir / "others")
        others_prompt.check_context(context)  # before llama-server starts
        with self.llm_session() as model:
            self.processor.free_gpu()
            return {"prompt": others_prompt.generate_instructions(self.processor, template, schema, context, model=model)}

    def others_confirm(self, context: str, prompt: str) -> dict[str, str]:
        confirmed = others_prompt.confirm(self.domain("others"), context, prompt)
        digest = prompt_digest(confirmed.context, confirmed.instructions)
        self._others[digest] = confirmed
        return {"digest": digest}

    # --- recording and import

    def _new_session(self, mode: str, contact: str, others: dict[str, Any] | None) -> Session:
        if mode not in {m for m, _, _ in MODES}:
            raise ModeUnavailable(str(mode))
        self.domain(mode)
        prompt = None
        if mode == "others":
            others = others or {}
            prompt = self._others.get(prompt_digest(str(others.get("context", "")).strip(), str(others.get("prompt", "")).strip()))
            if prompt is None:
                raise PromptNotConfirmed("others")
        return Session(id=f"s-{uuid.uuid4().hex[:10]}", started_at=self.clock.now(), domain=mode,
                       contact_id=str(contact or "") or None, others_prompt=prompt)

    def start(self, mode: str, language: str, contact: str, others: dict[str, Any] | None = None) -> dict[str, str]:
        session = self._new_session(mode, contact, others)
        paused = threading.Event()
        buffer = io.BytesIO()
        try:
            source = self.record_source()
        except Exception as e:  # a wrong or missing device; its message names the device only, but stays in the log
            log.error("microphone: %s", e)
            raise MicrophoneUnavailable("microphone") from None
        live = self.live_feedback(str(language))
        recorder = Recorder(buffer, source=monitor(_pausable(source, paused), live))
        state = {"recorder": recorder, "buffer": buffer, "paused": paused, "language": str(language),
                 "started": self.monotonic(), "paused_at": None, "paused_total": 0.0, "error": None,
                 "session": session, "live": live}
        thread = threading.Thread(target=self._record, args=(state,), daemon=True)
        state["thread"] = thread
        with self._lock:
            self._recordings[session.id] = state
            self._progress[session.id] = {"stage": "recording", "failed": None, "untranslated": 0}
        thread.start()
        return {"session_id": session.id}

    @staticmethod
    def _record(state: dict[str, Any]) -> None:
        try:
            state["recorder"].record()
        except Exception as e:  # no device, device lost: shown on Stop, never with its message
            state["error"] = type(e).__name__
            log.error("recording failed: %s", type(e).__name__)

    def live(self, session_id: str) -> dict[str, Any]:
        state = self._recording(session_id)
        # Live Transcript: feedback only, kept in memory; the Transcript comes from the whole audio after Stop
        return {"elapsed": int(self._elapsed(state)), "paused": state["paused_at"] is not None,
                "segments": state["live"].read()}

    def pause(self, session_id: str) -> dict[str, Any]:
        state = self._recording(session_id)
        if state["paused_at"] is None:
            state["paused_at"] = self.monotonic()
            state["paused"].set()
        else:
            state["paused_total"] += self.monotonic() - state["paused_at"]
            state["paused_at"] = None
            state["paused"].clear()
        return {"paused": state["paused_at"] is not None}

    def stop(self, session_id: str) -> dict[str, Any]:
        """Ends the recording, seals the audio in the store and starts the analysis. A second Stop
        changes nothing; after a failed analysis it starts the analysis again from what was saved."""
        with self._lock:
            state = self._recordings.pop(session_id, None)
            progress = self._progress.get(session_id)
        if state is not None:
            state["recorder"].stop()  # while paused the source yields b"", so the writer still sees Stop
            state["thread"].join(timeout=10)
            state["live"].close()
            audio = state["buffer"].getvalue()
            if state["error"] or len(audio) <= 44:  # 44 bytes: a WAV header with no audio
                self._progress[session_id] = {"stage": "recording", "failed": "no_audio", "untranslated": 0}
                return {"session_id": session_id}
            self._save_new(state["session"], audio)
            self._analyse_in_background(session_id, state["language"])
        elif progress is None or progress["failed"] not in (None, "no_audio"):  # failed, or the app restarted
            session = self.store.get_session(session_id)
            if session is None:
                raise LookupError("Session does not exist")
            if not session.source_transcript_ref:
                self._analyse_in_background(session_id, (progress or {}).get("language", "auto"))
        return {"session_id": session_id}

    def import_file(self, mode: str, language: str, contact: str, others: dict[str, Any] | None = None) -> dict[str, Any]:
        """An audio or video file the User picks; None when they cancel the dialog."""
        session = self._new_session(mode, contact, others)
        path = self.pick_file()
        if not path:
            return {"session_id": None}
        audio = Path(path).read_bytes()
        if not audio:
            raise ValueError("empty file")
        self._save_new(session, audio)
        self._analyse_in_background(session.id, str(language))
        return {"session_id": session.id}

    def import_example(self, mode: str, language: str, contact: str, others: dict[str, Any] | None = None):
        raise ModeUnavailable("example")  # examples are the demo's (python -m norev.ui --demo)

    def _save_new(self, session: Session, audio: bytes) -> None:
        session.audio_ref = self.store.save_audio(session.id, audio)
        self.store.save_session(session)

    # --- analysis

    def _analyse_in_background(self, session_id: str, language: str) -> None:
        with self._lock:
            self._progress[session_id] = {"stage": "transcribing", "failed": None, "untranslated": 0, "language": language}
        self.run_in_background(lambda: self._analyse(session_id, language))

    def _analyse(self, session_id: str, language: str, regenerate: bool = False) -> None:
        stage = "transcribing"
        try:
            session = self.store.get_session(session_id)
            if not session.transcript_ref:
                transcribe_session(session_id, self.store, self.processor, self.processor,
                                   None if language in ("", "auto") else language)
                session = self.store.get_session(session_id)
            if regenerate or not session.source_transcript_ref or not self.store.load_actions(session_id):
                stage = self._set_stage(session_id, "starting_llm")
                self.processor.free_gpu()
                with self.llm_session() as model:
                    stage = self._set_stage(session_id, "analysing")
                    result = run_session(
                        session_id, store=self.store, domain=self.domain(session.domain),
                        translator=TurnTranslator(self.processor, model=model, workers=self.workers),
                        extractor=self.make_extractor(self.processor, workers=self.workers), llm=self.processor,
                        regenerate=regenerate)
                self._progress[session_id]["untranslated"] = len(result.untranslated)
            self._set_stage(session_id, "ready")
        except Exception as e:  # the stage and the error class only: messages can quote Session content
            log.error("analysis of a Session failed at %s: %s", stage, type(e).__name__)
            with self._lock:
                self._progress[session_id].update(stage=stage, failed=type(e).__name__)

    def _set_stage(self, session_id: str, stage: str) -> str:
        with self._lock:
            self._progress[session_id]["stage"] = stage
        return stage

    def progress(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            progress = self._progress.get(session_id)
        if progress is None:
            session = self.store.get_session(session_id)
            if session is None:
                raise LookupError("Session does not exist")
            ready = bool(session.source_transcript_ref)
            return {"stage": "ready" if ready else "transcribing", "failed": None if ready else "interrupted"}
        return {"stage": progress["stage"], "failed": progress["failed"]}

    # --- review

    def sessions(self) -> list[dict[str, Any]]:
        """Every saved Session (#59), also the ones whose analysis failed or never finished."""
        listed = []
        for s in self.store.list_sessions():
            ready = bool(s.source_transcript_ref)
            listed.append({
                "session_id": s.id, "contact": s.contact_id or "", "domain": s.domain, "started_at": s.started_at.isoformat(),
                "pending": sum(a.status is ActionStatus.PROPOSED for a in self.store.load_actions(s.id)) if ready else 0,
                "ready": ready,
            })
        return listed

    def _summary(self, session_id: str) -> str:
        return self.store.load_summary(session_id)

    def _refuse_if_busy(self, session_id: str) -> None:
        """Nothing else may start while the Session records or is analysed; the caller holds the lock."""
        progress = self._progress.get(session_id)
        if session_id in self._recordings or (progress and progress["stage"] != "ready" and not progress["failed"]):
            raise SessionBusy("session")

    def regenerate(self, session_id: str) -> dict[str, Any]:
        """The User's explicit request after correcting a Role (#60): the Proposed actions are replaced,
        every decided one is kept (run_session's regeneration)."""
        with self._lock:
            self._refuse_if_busy(session_id)
            session = self.store.get_session(session_id)
            if session is None:
                raise LookupError("Session does not exist")
            if not session.source_transcript_ref:
                raise SessionBusy("session")  # never analysed: the first analysis runs from stop or import
            self._progress[session_id] = {"stage": "starting_llm", "failed": None, "untranslated": 0}
        self.run_in_background(lambda: self._analyse(session_id, "", regenerate=True))
        return {"session_id": session_id}

    def delete(self, session_id: str) -> dict[str, Any]:
        """Only while nothing runs on the Session (recording or analysis)."""
        with self._lock:
            self._refuse_if_busy(session_id)
        super().delete(session_id)
        with self._lock:
            self._progress.pop(session_id, None)
        return {"deleted": session_id}

    def _speakers(self, session_id: str, transcript) -> list[dict[str, Any]]:
        return [{"id": s.id, "role": s.role} for s in self.store.load_speakers(session_id)]

    def _partial(self, session_id: str, transcript) -> int:
        return sum(1 for s in transcript.segments if s.untranslated)

    def set_role(self, session_id: str, speaker_id: str, role: str) -> dict[str, Any]:
        set_speaker_role(session_id, self.store, self.domain(self.store.get_session(session_id).domain), str(speaker_id), role)
        return self.review(session_id)

    def _action_view(self, action: ProposedAction, domain: Domain) -> dict[str, Any]:
        view = super()._action_view(action, domain)
        steps = self.store.load_steps(action.id) if action.status is ActionStatus.APPROVED else {}
        view["uncertain_step"] = next((name for name, r in steps.items() if r.state is StepState.STARTED), None)
        view["error"] = action.error or ""
        view["draft"] = self._draft(action) is not None
        return view

    def _draft(self, action: ProposedAction) -> Path | None:
        """The email draft an executed action saved, if it is still the approved file in the export folder."""
        if action.status is not ActionStatus.EXECUTED or not action.approved_payload:
            return None
        steps = self.store.load_steps(action.id)
        for step in action.approved_payload.get("steps", []):
            record = steps.get(step["step"])
            if step["connector"] != "email_draft" or record is None or record.state is not StepState.DONE:
                continue
            path = Path(step["payload"]["destination"])
            content = step["payload"]["content"].encode("utf-8")
            if (path.suffix == ".eml" and path.parent.resolve() == self.export_dir.resolve()
                    and path.is_file() and path.read_bytes() == content):
                return path
        return None

    def open_draft(self, session_id: str, action_id: str) -> dict[str, Any]:
        """"Deschide în mail" (#61): opens the saved draft with the system's mail app, only when the User
        asks. Only the exact approved file, in the export folder; a moved or changed file is refused."""
        action = next((a for a in self.store.load_actions(session_id) if a.id == action_id), None)
        if action is None:
            raise LookupError("Proposed Action does not exist")
        path = self._draft(action)
        if path is None:
            raise ExecutionRefused("no saved email draft for this action")
        self.open_file(path)
        return {"opened": True}

    # --- execution (#23), through the Central Processor

    def _executor(self, session_id: str) -> WorkflowExecutor:
        domain = self.domain(self.store.get_session(session_id).domain)
        if domain.name not in self._executors:
            from norev.processor.compose import LOCAL_STEPS

            self._executors[domain.name] = WorkflowExecutor(self.processor, self.store, self.store, domain, LOCAL_STEPS)
        return self._executors[domain.name]

    def execute(self, session_id: str, action_id: str) -> dict[str, Any]:
        self._executor(session_id).execute(session_id, action_id)
        return self.review(session_id)

    def retry(self, session_id: str, action_id: str) -> dict[str, Any]:
        """OPEN-QUESTIONS #7: manual only; the same approved payload, the steps already done are skipped."""
        executor = self._executor(session_id)
        executor.retry(session_id, action_id)
        executor.execute(session_id, action_id)
        return self.review(session_id)

    def reconcile(self, session_id: str, action_id: str, step: str, happened: bool) -> dict[str, Any]:
        self._executor(session_id).reconcile(session_id, action_id, str(step), bool(happened))
        return self.review(session_id)

    # ---

    def _recording(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            state = self._recordings.get(session_id)
        if state is None:
            raise LookupError("Session is not recording")
        return state


def _pausable(blocks: Iterable[bytes], paused: threading.Event) -> Iterator[bytes]:
    """The blocks, dropped while paused; b"" keeps the writer checking for Stop. Closing it closes the device."""
    try:
        for block in blocks:
            yield b"" if paused.is_set() else block
    finally:
        close = getattr(blocks, "close", None)
        if close is not None:
            close()
