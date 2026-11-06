"""Handwritten fakes for the ports in norev.core.ports, so tests run without a model or GPU."""
import copy
from datetime import date, datetime
from typing import Any

from norev.core.models import AuditEvent, ProposedAction, Session, Transcript


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


class FixedClock:
    def __init__(self, at: datetime) -> None:
        self._at = at

    def now(self) -> datetime:
        return self._at


class InMemorySessionStore:
    """Stores and returns deep copies, like a real store, so mutation after save is not silently kept."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}
        self._transcripts: dict[str, Transcript] = {}
        self._actions: dict[str, list[ProposedAction]] = {}

    def save_session(self, session: Session) -> None:
        self._sessions[session.id] = copy.deepcopy(session)

    def get_session(self, session_id: str) -> Session | None:
        return copy.deepcopy(self._sessions.get(session_id))

    def save_transcript(self, session_id: str, transcript: Transcript) -> str:
        ref = f"transcript-{len(self._transcripts) + 1}"
        self._transcripts[ref] = copy.deepcopy(transcript)
        return ref

    def load_transcript(self, transcript_ref: str) -> Transcript:
        if transcript_ref not in self._transcripts:
            raise KeyError(f"no Transcript for ref {transcript_ref!r}")
        return copy.deepcopy(self._transcripts[transcript_ref])

    def save_actions(self, session_id: str, actions: list[ProposedAction]) -> None:
        self._actions[session_id] = copy.deepcopy(actions)

    def load_actions(self, session_id: str) -> list[ProposedAction]:
        return copy.deepcopy(self._actions.get(session_id, []))


class InMemoryAuditLog:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(copy.deepcopy(event))


class FakeActionExtractor:
    """Returns a fixed raw extraction result and records every call as (transcript, session_date)."""

    def __init__(self, result: dict[str, Any]) -> None:
        self._result = result
        self.calls: list[tuple[Transcript, date]] = []

    def extract(self, transcript: Transcript, session_date: date) -> dict[str, Any]:
        self.calls.append((transcript, session_date))
        return copy.deepcopy(self._result)
