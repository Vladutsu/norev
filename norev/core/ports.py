"""Ports: what the core needs from the outside world, implemented by adapters (docs/ARCHITECTURE.md).

Only signatures here. Every model call and outbound send goes through the Central Processor,
which is the only module that holds real `LlmClient` and `Connector` implementations.
"""
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from norev.core.models import ActionsSnapshot, Domain, AuditEvent, ProposedAction, Session, SpeakerTurn, Suggestion, Transcript


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
class Translator(Protocol):
    """Transcript in `source_language` -> English Transcript, one Segment per Speaker turn.

    A turn is a run of consecutive Segments with the same `speaker_id`. Each English Segment keeps
    the turn's `speaker_id`, the `start` of its first Segment and the `end` of its last, so later
    stages still know who said what. A turn that could not be translated keeps its original text
    and has `untranslated=True`; no turn is dropped or reordered.
    """

    def translate(self, transcript: Transcript, source_language: str) -> Transcript: ...


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
class PayloadBuilder(Protocol):
    """Prepare a version-1 approval plan without I/O effects or model calls.

    Implementations validate Connector options (including destination and edited
    content), respect Workflow.sends and reject unavailable steps/Connectors with
    UnsupportedWorkflow. The result contains final payloads, not raw Domain fields.
    No filesystem export or network send occurs until execution after approval.
    """

    def build(self, action: ProposedAction, domain: Domain, options: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class Connector(Protocol):
    """Executes one Workflow step with the approved payload; returns what the service answered."""

    name: str

    def send(self, payload: dict[str, Any]) -> dict[str, Any]: ...


@runtime_checkable
class SessionStore(Protocol):
    def save_session(self, session: Session) -> None:
        """Changes invalidate the review revision; after review starts, input changes
        are refused until an explicit atomic regeneration contract is implemented.
        """
        ...

    def get_session(self, session_id: str) -> Session | None: ...

    def save_transcript(self, session_id: str, transcript: Transcript) -> str: ...

    def load_transcript(self, transcript_ref: str) -> Transcript: ...

    def load_actions_snapshot(self, session_id: str) -> ActionsSnapshot:
        """Detached snapshot; revision starts at zero. Unknown Session raises LookupError."""
        ...

    def save_actions(
        self, session_id: str, actions: list[ProposedAction], *, expected_revision: int,
        suggestions: list[Suggestion] | None = None,
    ) -> int:
        """Atomically compare revision, replace actions and optional Suggestions, increment.

        Return the new revision. StaleRevision means NOTHING was saved. Duplicate
        action ids are rejected. None preserves Suggestions; [] clears them.
        Every writer must use this contract; no unchecked overwrite path is allowed.
        """
        ...

    def load_actions(self, session_id: str) -> list[ProposedAction]: ...

    def save_suggestions(
        self, session_id: str, suggestions: list[Suggestion], *, expected_revision: int,
    ) -> int:
        """CAS on the same revision as actions; no partial write on conflict."""
        ...

    def load_suggestions(self, session_id: str) -> list[Suggestion]: ...


@runtime_checkable
class AuditLog(Protocol):
    def record(self, event: AuditEvent) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...
