"""Generic extraction use case (ADR 0005): Session Transcript -> Proposed Actions waiting for the User.

The model's raw result is untrusted: it is validated here and never decides status (ADR 0002)
or invents a due date (OPEN-QUESTIONS #6).
"""
import re
from datetime import date
from typing import Any

from norev.core.models import ActionStatus, ProposedAction
from norev.core.ports import ActionExtractor, SessionStore

_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _due_date(value: Any) -> date | None:
    text = _text(value)
    if not _ISO_DATE.fullmatch(text):
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:  # e.g. 2026-02-30
        return None


def to_proposed_actions(raw: dict, id_prefix: str = "pa") -> list[ProposedAction]:
    """Map the ActionExtractor's raw result to Proposed Actions; invalid items are skipped.

    Ids are always `<id_prefix>-<n>`: the model's id is ignored (untrusted, and it repeats across Sessions).
    """
    items = raw.get("proposed_actions")
    if not isinstance(items, list):
        return []
    actions: list[ProposedAction] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title, description = _text(item.get("title")), _text(item.get("description"))
        if not title and not description:
            continue
        actions.append(
            ProposedAction(
                id=f"{id_prefix}-{len(actions) + 1}",
                title=title,
                description=description,
                transcript_excerpt=_text(item.get("transcript_excerpt")),
                owner=_text(item.get("owner")) or None,
                due_date=_due_date(item.get("due_date")),
            )
        )
    return actions


def extract_session_actions(session_id: str, store: SessionStore, extractor: ActionExtractor) -> list[ProposedAction]:
    """Extract the Session's Proposed Actions from its Transcript, save and return them.

    Refuses to run once the User has decided any action: replacing the list would drop approved
    payloads and could propose an executed action again.
    """
    session = store.get_session(session_id)
    if session is None:
        raise LookupError(f"no Session {session_id!r}")
    if not session.transcript_ref:
        raise LookupError(f"Session {session_id!r} has no Transcript")
    if any(a.status is not ActionStatus.PROPOSED for a in store.load_actions(session_id)):
        raise ValueError(f"Session {session_id!r} already has decided Proposed Actions")
    transcript = store.load_transcript(session.transcript_ref)
    started = session.started_at
    session_date = (started.astimezone() if started.tzinfo else started).date()  # local day of the Session
    actions = to_proposed_actions(extractor.extract(transcript, session_date), id_prefix=session_id)
    store.save_actions(session_id, actions)
    return actions
