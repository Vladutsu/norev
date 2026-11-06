"""Handwritten fakes for the ports in norev.core.ports, so tests run without a model or GPU."""
import copy
import dataclasses
from datetime import date, datetime
from threading import RLock
from typing import Any

from norev.core.models import (
    ActionStatus, ActionsSnapshot, AuditEvent, Domain, OthersPrompt, ProposedAction, SendResult, Session, Speaker, SpeakerTurn, StaleRevision,
    StepRecord, Suggestion, Transcript,
)
from norev.core.models import check_decisions_kept
from norev.core.speakers import merge_speakers


class FakeLlmClient:
    """Returns recorded responses in order and records every call as (prompt, model, json_schema)."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str | None, dict | None]] = []

    def generate_json(self, prompt: str, *, model: str | None = None, json_schema: dict | None = None) -> str:
        self.calls.append((prompt, model, json_schema))
        if not self._responses:
            raise AssertionError(f"FakeLlmClient ran out of recorded responses at call {len(self.calls)}")
        return self._responses.pop(0)


class FakeTranscriber:
    def __init__(self, transcripts: dict[str, Transcript]) -> None:
        self._transcripts = dict(transcripts)

    def transcribe(self, audio_ref: str, language: str | None = None) -> Transcript:
        if audio_ref not in self._transcripts:
            raise KeyError(f"FakeTranscriber has no Transcript for audio_ref {audio_ref!r}")
        return self._transcripts[audio_ref]


class FakeDiarizer:
    """Returns recorded diarization turns per audio_ref."""

    def __init__(self, turns: dict[str, list[SpeakerTurn]]) -> None:
        self._turns = {ref: list(t) for ref, t in turns.items()}

    def diarize(self, audio_ref: str) -> list[SpeakerTurn]:
        if audio_ref not in self._turns:
            raise KeyError(f"FakeDiarizer has no turns for audio_ref {audio_ref!r}")
        return list(self._turns[audio_ref])


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at


class InMemorySessionStore:
    """Detached copies and atomic revision checks, with the same contract as a real store."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._revisions: dict[str, int] = {}
        self._sessions: dict[str, Session] = {}
        self._transcripts: dict[str, Transcript] = {}
        self._actions: dict[str, list[ProposedAction]] = {}
        self._suggestions: dict[str, list[Suggestion]] = {}
        self._audio: dict[str, bytes] = {}
        self._speakers: dict[str, list[Speaker]] = {}
        self._steps: dict[str, dict[str, StepRecord]] = {}
        self._summaries: dict[str, str] = {}

    def save_session(self, session: Session) -> None:
        with self._lock:
            previous = self._sessions.get(session.id)
            if previous == session:
                return
            if previous is not None and (session.id in self._actions or session.id in self._suggestions):
                raise ValueError("Session inputs cannot change after review has started")
            saved = copy.deepcopy(session)
            self._sessions[session.id] = saved
            self._revisions[session.id] = 0 if previous is None else self._revisions[session.id] + 1

    def get_session(self, session_id: str) -> Session | None:
        with self._lock:
            return copy.deepcopy(self._sessions.get(session_id))

    def list_sessions(self) -> list[Session]:
        with self._lock:
            return sorted(copy.deepcopy(list(self._sessions.values())), key=lambda s: s.started_at, reverse=True)

    def save_transcript(self, session_id: str, transcript: Transcript) -> str:
        with self._lock:
            ref = f"transcript-{len(self._transcripts) + 1}"
            self._transcripts[ref] = copy.deepcopy(transcript)
            return ref

    def load_transcript(self, transcript_ref: str) -> Transcript:
        with self._lock:
            if transcript_ref not in self._transcripts:
                raise KeyError("Transcript does not exist")
            return copy.deepcopy(self._transcripts[transcript_ref])

    def _revision(self, session_id: str) -> int:
        if session_id not in self._sessions:
            raise LookupError("Session does not exist")
        return self._revisions[session_id]

    def _check_revision(self, session_id: str, expected_revision: int) -> int:
        revision = self._revision(session_id)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("Invalid expected revision")
        if revision != expected_revision:
            raise StaleRevision("Session actions changed; reload before deciding")
        return revision

    def load_actions_snapshot(self, session_id: str) -> ActionsSnapshot:
        with self._lock:
            return ActionsSnapshot(self._revision(session_id),
                                   tuple(copy.deepcopy(self._actions.get(session_id, []))))

    def save_actions(
        self, session_id: str, actions: list[ProposedAction], *, expected_revision: int,
        suggestions: list[Suggestion] | None = None,
    ) -> int:
        with self._lock:
            revision = self._check_revision(session_id, expected_revision)
            saved_actions = copy.deepcopy(actions)
            saved_suggestions = copy.deepcopy(suggestions)
            if len({a.id for a in saved_actions}) != len(saved_actions):
                raise ValueError("Duplicate Proposed Action ids")
            self._actions[session_id] = saved_actions
            if saved_suggestions is not None:
                self._suggestions[session_id] = saved_suggestions
            self._revisions[session_id] = revision + 1
            return revision + 1

    def load_actions(self, session_id: str) -> list[ProposedAction]:
        with self._lock:
            return copy.deepcopy(self._actions.get(session_id, []))

    def regenerate_actions(
        self, session_id: str, actions: list[ProposedAction], *, domain: str, expected_revision: int,
        suggestions: list[Suggestion], others_prompt: OthersPrompt | None = None,
    ) -> int:
        with self._lock:
            revision = self._check_revision(session_id, expected_revision)
            saved_actions, saved_suggestions = copy.deepcopy(actions), copy.deepcopy(suggestions)
            if len({a.id for a in saved_actions}) != len(saved_actions):
                raise ValueError("Duplicate Proposed Action ids")
            check_decisions_kept(self._actions.get(session_id, []), saved_actions)
            session = dataclasses.replace(self._sessions[session_id], domain=domain)
            if others_prompt is not None:
                session = dataclasses.replace(session, others_prompt=others_prompt)
            self._sessions[session_id] = session
            self._actions[session_id] = saved_actions
            self._suggestions[session_id] = saved_suggestions
            self._revisions[session_id] = revision + 1
            return revision + 1

    def save_suggestions(
        self, session_id: str, suggestions: list[Suggestion], *, expected_revision: int,
    ) -> int:
        with self._lock:
            revision = self._check_revision(session_id, expected_revision)
            self._suggestions[session_id] = copy.deepcopy(suggestions)
            self._revisions[session_id] = revision + 1
            return revision + 1

    def load_suggestions(self, session_id: str) -> list[Suggestion]:
        with self._lock:
            return copy.deepcopy(self._suggestions.get(session_id, []))

    def save_audio(self, session_id: str, audio: bytes) -> str:
        with self._lock:
            ref = f"audio-{len(self._audio) + 1}"
            self._audio[ref] = bytes(audio)
            return ref

    def load_audio(self, audio_ref: str) -> bytes:
        with self._lock:
            if audio_ref not in self._audio:
                raise KeyError("audio does not exist")
            return self._audio[audio_ref]

    def save_speakers(self, session_id: str, speakers: list[Speaker]) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise LookupError("Session does not exist")
            self._speakers[session_id] = merge_speakers(self._speakers.get(session_id, []), speakers)

    def load_speakers(self, session_id: str) -> list[Speaker]:
        with self._lock:
            return list(self._speakers.get(session_id, []))

    def save_summary(self, session_id: str, summary: str) -> None:
        with self._lock:
            if session_id not in self._sessions:
                raise LookupError("Session does not exist")
            self._summaries[session_id] = str(summary)

    def load_summary(self, session_id: str) -> str:
        with self._lock:
            return self._summaries.get(session_id, "")

    def delete_session(self, session_id: str) -> None:
        with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                raise LookupError("Session does not exist")
            actions = self._actions.get(session_id, [])
            if any(a.status is ActionStatus.APPROVED for a in actions):
                raise ValueError("an approved Proposed Action is waiting to execute")
            for ref in (session.transcript_ref, session.source_transcript_ref):
                self._transcripts.pop(ref, None)
            self._audio.pop(session.audio_ref, None)
            for a in actions:
                self._steps.pop(a.id, None)
            for table in (self._sessions, self._revisions, self._actions, self._suggestions, self._speakers, self._summaries):
                table.pop(session_id, None)

    # ExecutionStore
    def load_steps(self, action_id: str) -> dict[str, StepRecord]:
        with self._lock:
            return copy.deepcopy(self._steps.get(action_id, {}))

    def save_step(self, action_id: str, record: StepRecord) -> None:
        with self._lock:
            self._steps.setdefault(action_id, {})[record.step] = copy.deepcopy(record)


class InMemoryAuditLog:
    """AuditLog and ExecutionAuditLog."""

    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self.results: list[SendResult] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(copy.deepcopy(event))

    def record_result(self, result: SendResult) -> None:
        self.results.append(result)

    def load_results(self) -> list[SendResult]:
        return list(self.results)


class FakeActionExtractor:
    """Returns a fixed raw extraction result and records every call as (transcript, session_date)."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls: list[tuple[Transcript, date]] = []

    def extract(self, transcript: Transcript, session_date: date) -> dict[str, Any]:
        self.calls.append((transcript, session_date))
        return copy.deepcopy(self._result)


class FakePayloadBuilder:
    """Returns a supplied plan; no Connector/path validation or filesystem effects.

    Real builders in adapters must validate their Connector-specific options.
    This fake intentionally allows malformed plans for boundary tests.
    """

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = copy.deepcopy(plan)
        self.calls: list[tuple[ProposedAction, Domain, dict[str, Any]]] = []

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(copy.deepcopy((action, domain, options)))
        return copy.deepcopy(self.plan)
