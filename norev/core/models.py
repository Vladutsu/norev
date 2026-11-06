"""The core model, in the vocabulary of docs/GLOSSARY.md.

Pure: standard library only, no I/O. Objects that hold Session content (Transcript text,
excerpts, payloads) print ids and lengths in their repr, never the content, so patient data
cannot leak into logs or tracebacks.
"""
import copy
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any


class ActionStatus(str, Enum):
    PROPOSED = "proposed"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"
    FAILED = "failed"


# Proposed -> Approved / Rejected -> Executed / Failed (GLOSSARY); Failed -> Approved is the manual Retry
_TRANSITIONS: dict[ActionStatus, frozenset[ActionStatus]] = {
    ActionStatus.PROPOSED: frozenset({ActionStatus.APPROVED, ActionStatus.REJECTED}),
    ActionStatus.APPROVED: frozenset({ActionStatus.EXECUTED, ActionStatus.FAILED}),
    ActionStatus.FAILED: frozenset({ActionStatus.APPROVED}),
}


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class Role:
    """What a Speaker is in the conversation, as defined by the Domain (e.g. Doctor, Patient)."""

    name: str
    description: str = ""


@dataclass(frozen=True)
class Speaker:
    """A distinct voice in a Session; `role` is a Role name once known."""

    id: str
    role: str | None = None


@dataclass(frozen=True)
class SpeakerTurn:
    """A stretch of audio attributed to one Speaker by diarization."""

    start: float
    end: float
    speaker_id: str


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str
    speaker_id: str | None = None

    def __post_init__(self) -> None:
        if self.start < 0 or self.end < self.start:
            raise ValueError(f"invalid segment times: {self.start}..{self.end}")

    def __repr__(self) -> str:
        return f"Segment({self.start}-{self.end}, speaker={self.speaker_id}, text_len={len(self.text)})"


@dataclass(frozen=True)
class Transcript:
    """The authoritative text of a Session, produced from the whole audio after recording stops."""

    segments: tuple[Segment, ...]
    language: str | None = None

    @property
    def text(self) -> str:
        return " ".join(s.text for s in self.segments)

    def __repr__(self) -> str:
        return f"Transcript(language={self.language}, segments={len(self.segments)}, text_len={len(self.text)})"


@dataclass(frozen=True)
class WorkflowStep:
    """One step of a Workflow; `connector` names the Connector it uses, if any."""

    name: str
    connector: str | None = None


@dataclass(frozen=True)
class Workflow:
    steps: tuple[WorkflowStep, ...]


@dataclass(frozen=True)
class ActionType:
    """A kind of action a Domain allows to be proposed, with its fields and its Workflow (ADR 0005)."""

    name: str
    fields: tuple[str, ...]
    workflow: Workflow


@dataclass
class ProposedAction:
    """A concrete action extracted from a Transcript, waiting for the User's decision.

    The core fills the generic fields; the Domain stage sets `action_type` and `fields` (ADR 0005).
    Status changes only through the methods below; approval keeps a copy of the exact payload
    the User approved (ADR 0002), and a Retry reuses it.
    """

    id: str
    title: str
    description: str
    transcript_excerpt: str
    owner: str | None = None
    due_date: date | None = None
    action_type: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    status: ActionStatus = ActionStatus.PROPOSED
    # shortcut: status and approved_payload are plain attributes; the Central Processor must
    # re-check the status and send exactly approved_payload at send time.
    approved_payload: dict[str, Any] | None = None
    error: str | None = None

    def approve(self, payload: dict[str, Any]) -> None:
        """The User's approval of this exact payload. Out of Failed only retry() is allowed."""
        if self.status is not ActionStatus.PROPOSED:
            raise InvalidTransition(f"approve needs status proposed, not {self.status.value}")
        payload_copy = copy.deepcopy(payload)  # before the move, so a failed copy changes nothing
        self._move(ActionStatus.APPROVED)
        self.approved_payload = payload_copy

    def reject(self) -> None:
        self._move(ActionStatus.REJECTED)

    def mark_executed(self) -> None:
        self._move(ActionStatus.EXECUTED)

    def mark_failed(self, error: str) -> None:
        self._move(ActionStatus.FAILED)
        self.error = error

    def retry(self) -> None:
        """Manual Retry after a failure: back to Approved with the same approved payload."""
        if self.status is not ActionStatus.FAILED:
            raise InvalidTransition(f"retry needs status failed, not {self.status.value}")
        self._move(ActionStatus.APPROVED)
        self.error = None

    def _move(self, to: ActionStatus) -> None:
        if to not in _TRANSITIONS.get(self.status, frozenset()):
            raise InvalidTransition(f"{self.status.value} -> {to.value}")
        self.status = to

    def __repr__(self) -> str:
        return (
            f"ProposedAction(id={self.id}, status={self.status.value}, action_type={self.action_type}, "
            f"title_len={len(self.title)}, fields={sorted(self.fields)})"
        )


@dataclass(frozen=True)
class Suggestion:
    """Read-only advice derived from a Transcript; never approved or executed."""

    text: str
    transcript_excerpt: str = ""

    def __repr__(self) -> str:
        return f"Suggestion(text_len={len(self.text)})"


@dataclass
class Session:
    """One recorded conversation. Audio and Transcript are referenced by opaque ids, never paths."""

    id: str
    started_at: datetime
    domain: str
    contact_id: str | None = None
    audio_ref: str | None = None
    transcript_ref: str | None = None


@dataclass(frozen=True)
class AuditEvent:
    """One outbound send: what, where, when, approved by whom (ADR 0004)."""

    at: datetime
    action_id: str
    connector: str
    payload: dict[str, Any]
    approved_by: str

    def __repr__(self) -> str:
        return (
            f"AuditEvent(at={self.at.isoformat()}, action_id={self.action_id}, "
            f"connector={self.connector}, payload_keys={sorted(self.payload)})"
        )
