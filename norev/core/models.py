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


class StaleRevision(ValueError):
    """The Session changed since the caller read its snapshot; no write occurred."""


class ValidationError(ValueError):
    """Safe machine-readable validation failure: no input values in the message."""

    def __init__(self, code: str, field: str):
        self.code = code
        self.field = field
        super().__init__(f"{code}: {field}")


class UnsupportedWorkflow(ValueError):
    """A Workflow cannot be prepared by the available Connector implementations."""


@dataclass(frozen=True)
class Role:
    """What a Speaker is in the conversation, as defined by the Domain (e.g. Doctor, Patient)."""

    name: str
    description: str = ""
    label: str = ""


@dataclass(frozen=True)
class Speaker:
    """A distinct voice in a Session; `role` is a Role name once known.

    `id` (S1, S2...) only tells voices apart: it is never a person's name, Contact or owner.
    `role_confirmed` is True once the User set or confirmed the Role; such a Role is never
    replaced automatically (None with role_confirmed means the User said "unknown").
    """

    id: str
    role: str | None = None
    role_confirmed: bool = False


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
    # Set by a Translator when it could not translate this Segment and kept the original text
    untranslated: bool = False

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
    """One step of a Workflow; `connector` names the Connector it uses, if any, and `sends` the
    payload fields that step sends through the Central Processor (domains/README.md).
    `alternatives`: other Connectors the User may pick for this step at preview (#72); the approved
    plan records the one chosen, and only `connector` or one of these is accepted."""

    name: str
    connector: str | None = None
    sends: tuple[str, ...] = ()
    label: str = ""
    alternatives: tuple[str, ...] = ()

    def connectors(self) -> tuple[str, ...]:
        """The Connectors this step may use: the default first."""
        return ((self.connector,) + self.alternatives) if self.connector else ()


@dataclass(frozen=True)
class Workflow:
    steps: tuple[WorkflowStep, ...]


FIELD_TYPES = ("string", "date", "time")  # date: YYYY-MM-DD, time: HH:MM 24-hour


@dataclass(frozen=True)
class DomainField:
    """A Domain field of an Action Type; a required field left empty blocks approval (OPEN-QUESTIONS #6)."""

    name: str
    type: str
    required: bool
    label: str = ""
    description: str = ""

    def __post_init__(self) -> None:
        if self.type not in FIELD_TYPES:
            raise ValueError(f"unknown field type {self.type!r}")


@dataclass(frozen=True)
class ActionType:
    """A kind of action a Domain allows to be proposed, with its fields and its Workflow (ADR 0005)."""

    name: str
    fields: tuple[DomainField, ...]
    workflow: Workflow
    label: str = ""
    description: str = ""
    # The Domain stage may add a Proposed Action of this type that the generic stage missed (ADR 0005, #66)
    may_add: bool = False


@dataclass(frozen=True)
class Domain:
    """An industry, loaded from domains/<name>/ (format: domains/README.md). The core never reads the folder."""

    name: str
    label: str
    model: str  # GGUF name or path, loaded for the Domain stage
    context: int
    roles: tuple[Role, ...]
    action_types: tuple[ActionType, ...]
    prompt_template: str  # with the {{...}} placeholders of domains/README.md
    answer_schema: dict[str, Any]  # JSON schema the Domain stage answer is constrained to

    def action_type(self, name: str) -> ActionType | None:
        return next((t for t in self.action_types if t.name == name), None)

    def __repr__(self) -> str:
        return f"Domain({self.name}, action_types={[t.name for t in self.action_types]})"


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
    prepared_payload: dict[str, Any] | None = None
    approved_by: str | None = None
    approved_at: datetime | None = None

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


def check_decisions_kept(stored: list[ProposedAction], actions: list[ProposedAction]) -> None:
    """A regeneration may replace Proposed actions only: every decided one stays in `actions`, unchanged."""
    new = {a.id: a for a in actions}
    for action in stored:
        if action.status is not ActionStatus.PROPOSED and new.get(action.id) != action:
            raise ValueError(f"regeneration would change or drop the decided Proposed Action {action.id}")


@dataclass(frozen=True, repr=False)
class ActionsSnapshot:
    """Detached copy of a Session's actions, paired with their store revision."""

    revision: int
    actions: tuple[ProposedAction, ...]

    def __repr__(self) -> str:
        return f"ActionsSnapshot(revision={self.revision}, actions={len(self.actions)})"


@dataclass(frozen=True)
class Suggestion:
    """Read-only advice derived from a Transcript; never approved or executed."""

    text: str
    transcript_excerpt: str = ""

    def __repr__(self) -> str:
        return f"Suggestion(text_len={len(self.text)})"


@dataclass(frozen=True)
class OthersPrompt:
    """The Others Domain prompt the User confirmed (#21): their context, the instructions that fill
    {{user_instructions}}, and `prompt`, the exact full prompt they saw. Kept with the Session as it
    was confirmed, never regenerated silently."""

    context: str
    instructions: str
    prompt: str

    def __repr__(self) -> str:
        return (f"OthersPrompt(context_len={len(self.context)}, instructions_len={len(self.instructions)}, "
                f"prompt_len={len(self.prompt)})")


@dataclass
class Session:
    """One recorded conversation. Audio and Transcript are referenced by opaque ids, never paths."""

    id: str
    started_at: datetime
    domain: str
    contact_id: str | None = None
    audio_ref: str | None = None
    # After transcription: the Transcript as spoken. After the first analysis (#19): the one
    # extraction runs on (English, unless not translated), and source_transcript_ref the spoken one.
    transcript_ref: str | None = None
    source_transcript_ref: str | None = None
    # Domain "others" only: the confirmed prompt; analysis is refused without it
    others_prompt: OthersPrompt | None = None


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


@dataclass(frozen=True)
class SendResult:
    """What happened to one attempt; written after the Connector answers or raises."""

    at: datetime
    action_id: str
    connector: str
    ok: bool
    error_type: str | None = None  # exception class name only, never its message


# Workflow execution (#23): the state of each step, persisted by the ExecutionStore port
class StepState(str, Enum):
    STARTED = "started"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True)
class StepRecord:
    """The state of one Workflow step of an action (#23)."""

    step: str
    state: StepState
    result: dict[str, Any] | None = None  # what the step answered, e.g. the path of the document
    error_type: str | None = None  # exception class name only, never its message

    def __repr__(self) -> str:
        return f"StepRecord({self.step}, {self.state.value}, result_keys={sorted(self.result or {})}, error={self.error_type})"
