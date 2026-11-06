"""Ports: what the core needs from the outside world, implemented by adapters (docs/ARCHITECTURE.md).

Only signatures here. Every model call and outbound send goes through the Central Processor,
which is the only module that holds real `LlmClient` and `Connector` implementations.
"""
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from norev.core.models import AuditEvent, ProposedAction, Session, SpeakerTurn, Transcript


@runtime_checkable
class Transcriber(Protocol):
    def transcribe(self, audio_ref: str, language: str | None = None) -> Transcript: ...


@runtime_checkable
class Diarizer(Protocol):
    def diarize(self, audio_ref: str) -> list[SpeakerTurn]: ...


@runtime_checkable
class LlmClient(Protocol):
    """Same shape as the existing LLM/AI_Model/client.py LLMClient, so it can be wrapped as an adapter."""

    def generate_json(self, prompt: str, *, model: str | None = None, json_schema: dict | None = None) -> str: ...


@runtime_checkable
class ActionExtractor(Protocol):
    """The generic extraction stage (ADR 0005): Transcript -> raw result of the extraction model.

    Returns the parsed JSON object: {"session_summary": str, "proposed_actions": [{"id", "title",
    "description", "owner", "due_date" (YYYY-MM-DD or null), "transcript_excerpt", ...}]}.
    Relative dates ("by Monday") are resolved against `session_date`. Validation and the mapping
    to ProposedAction happen in the use case, not here.
    """

    def extract(self, transcript: Transcript, session_date: date) -> dict[str, Any]: ...


@runtime_checkable
class Connector(Protocol):
    """Executes one Workflow step with the approved payload; returns what the service answered."""

    name: str

    def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class SessionStore(Protocol):
    def save_session(self, session: Session) -> None: ...

    def get_session(self, session_id: str) -> Session | None: ...

    def save_transcript(self, session_id: str, transcript: Transcript) -> str: ...

    def load_transcript(self, transcript_ref: str) -> Transcript: ...

    def save_actions(self, session_id: str, actions: list[ProposedAction]) -> None: ...

    def load_actions(self, session_id: str) -> list[ProposedAction]: ...


@runtime_checkable
class AuditLog(Protocol):
    def record(self, event: AuditEvent) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...
