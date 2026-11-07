"""Ports: what the core needs from the outside world, implemented by adapters (docs/ARCHITECTURE.md).

Only signatures here. Every model call and outbound send goes through the Central Processor,
which is the only module that holds real `LlmClient` and `Connector` implementations.
"""
from datetime import date, datetime
from typing import Any, Protocol, runtime_checkable

from norev.core.models import ActionsSnapshot, Domain, AuditEvent, OthersPrompt, ProposedAction, SendResult, Session, Speaker, SpeakerTurn, StepRecord, Suggestion, Transcript


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
        are refused: a Domain change goes through regenerate_actions.
        """
        ...

    def get_session(self, session_id: str) -> Session | None: ...

    def list_sessions(self) -> list[Session]:
        """Every stored Session, newest first (Recent Sessions, OPEN-QUESTIONS #8)."""
        ...

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

    def regenerate_actions(
        self, session_id: str, actions: list[ProposedAction], *, domain: str, expected_revision: int,
        suggestions: list[Suggestion], others_prompt: OthersPrompt | None = None,
    ) -> int:
        """The User's explicit regeneration (#19): atomically set the Session's Domain and replace its
        actions and Suggestions, with the same compare-and-set as save_actions. A given
        `others_prompt` (#21) replaces the Session's confirmed prompt in the same write; None keeps it.

        Every stored action that is no longer Proposed must be in `actions` unchanged
        (check_decisions_kept), so a decision is never altered or dropped. Any error: nothing saved.
        """
        ...

    def save_suggestions(
        self, session_id: str, suggestions: list[Suggestion], *, expected_revision: int,
    ) -> int:
        """CAS on the same revision as actions; no partial write on conflict."""
        ...

    def load_suggestions(self, session_id: str) -> list[Suggestion]: ...

    def save_audio(self, session_id: str, audio: bytes) -> str:
        """Store the Session's audio (encrypted at rest, ADR 0004); returns its opaque audio_ref."""
        ...

    def load_audio(self, audio_ref: str) -> bytes:
        """Unknown ref raises KeyError. Adapters decode it in memory, never through a plain file."""
        ...

    def save_speakers(self, session_id: str, speakers: list[Speaker]) -> None:
        """Replace the Session's Speakers, except that a stored Speaker with role_confirmed keeps its
        Role unless the new one is also role_confirmed: the User's correction is never overwritten
        automatically. Unknown Session raises LookupError."""
        ...

    def load_speakers(self, session_id: str) -> list[Speaker]: ...

    def save_summary(self, session_id: str, summary: str) -> None:
        """Replace the Session summary of the last analysis (#59). Unknown Session raises LookupError."""
        ...

    def load_summary(self, session_id: str) -> str:
        """"" when the Session has none."""
        ...

    def save_completion(self, session_id: str, completion: str) -> None:
        """Replace the Others result of the last analysis (#83). Unknown Session raises LookupError."""
        ...

    def load_completion(self, session_id: str) -> str:
        """"" when the Session has none."""
        ...

    def delete_session(self, session_id: str) -> None:
        """The User's explicit deletion (#59): the Session, its audio, Transcripts, Proposed Actions,
        Suggestions, Speakers, summary, Others result and Workflow steps, in one write. The audit log is kept.
        Unknown Session raises LookupError; an Approved action still waiting to execute raises
        ValueError and nothing is deleted."""
        ...


@runtime_checkable
class ExecutionStore(Protocol):
    """Step states of each action's Workflow (#23); must survive a restart, so an interrupted step
    stays visible as uncertain. save_step replaces the record of that step only."""

    def load_steps(self, action_id: str) -> dict[str, StepRecord]: ...

    def save_step(self, action_id: str, record: StepRecord) -> None: ...


@runtime_checkable
class AuditLog(Protocol):
    def record(self, event: AuditEvent) -> None: ...


@runtime_checkable
class ExecutionAuditLog(Protocol):
    """AuditLog plus the result of each attempt. An attempt with no result is an uncertain send:
    it is shown for reconciliation, never retried automatically (#23)."""

    def record(self, event: AuditEvent) -> None: ...

    def record_result(self, result: SendResult) -> None: ...


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...
