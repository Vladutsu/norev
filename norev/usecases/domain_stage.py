"""Domain stage use case (ADR 0005): generic Proposed Actions -> Action Types, Domain fields and Suggestions.

The prompt is the Domain's template with the placeholders of domains/README.md filled in. The
model's answer is untrusted: it is validated here against the Domain, never decides status
(ADR 0002) and never fills a value that is not valid (OPEN-QUESTIONS #6).
"""
import dataclasses
import json
import re
from datetime import date
from typing import Any

from norev.core.models import ActionStatus, Domain, ProposedAction, Suggestion, Transcript
from norev.core.ports import LlmClient, SessionStore

_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}")
_ISO_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_TIME = re.compile(r"([01][0-9]|2[0-3]):[0-5][0-9]")


def _action_types_text(domain: Domain) -> str:
    lines = []
    for action_type in domain.action_types:
        lines.append(f"- {action_type.name}: {action_type.description}")
        for f in action_type.fields:
            required = "required" if f.required else "optional"
            lines.append(f"  - {f.name} ({f.type}, {required})" + (f": {f.description}" if f.description else ""))
    return "\n".join(lines)


def build_prompt(domain: Domain, transcript: Transcript, actions: list[ProposedAction], session_date: date) -> str:
    """Fill the Domain stage prompt; any `{{...}}` other than the five known placeholders is a ValueError."""
    values = {
        # One line per segment: newlines inside the text could forge Speaker lines or sections
        "transcript": "\n".join(f"[{s.speaker_id or '?'}] {' '.join(s.text.split())}" for s in transcript.segments),
        "proposed_actions": json.dumps(
            [
                {
                    "id": a.id,
                    "title": a.title,
                    "description": a.description,
                    "owner": a.owner,
                    "due_date": a.due_date.isoformat() if a.due_date else None,
                    "transcript_excerpt": a.transcript_excerpt,
                }
                for a in actions
            ],
            ensure_ascii=False,
        ),
        "session_date": session_date.isoformat(),
        "roles": "\n".join(f"{r.name}: {r.description}" for r in domain.roles),
        "action_types": _action_types_text(domain),
    }
    # Checked on the template and replaced in one pass, so placeholder text inside the Transcript
    # is neither replaced nor rejected.
    unknown = sorted({name for name in _PLACEHOLDER.findall(domain.prompt_template) if name not in values})
    if unknown:
        raise ValueError(f"unknown placeholders in the {domain.name} prompt: {unknown}")
    return _PLACEHOLDER.sub(lambda m: values[m.group(1)], domain.prompt_template)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _field_value(field_type: str, value: Any) -> str | None:
    text = _text(value)
    if field_type == "date":
        if not _ISO_DATE.fullmatch(text):
            return None
        try:
            date.fromisoformat(text)
        except ValueError:  # e.g. 2026-02-30
            return None
    elif field_type == "time" and not _TIME.fullmatch(text):
        return None
    return text or None


def apply_answer(
    domain: Domain, actions: list[ProposedAction], answer: dict
) -> tuple[list[ProposedAction], list[Suggestion]]:
    """Validate the model's answer against the Domain; return the typed Proposed Actions and the Suggestions.

    Generic actions the answer leaves out are dropped. Items with an unknown id or Action Type are
    skipped; for a repeated id the first valid item wins.
    """
    by_id = {a.id: a for a in actions}
    typed: dict[str, ProposedAction] = {}
    items = answer.get("proposed_actions")
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        action_id, type_name = item.get("id"), item.get("action_type")
        if not isinstance(action_id, str) or action_id not in by_id or action_id in typed:
            continue
        action_type = domain.action_type(type_name) if isinstance(type_name, str) else None
        if action_type is None:
            continue
        raw_fields = item.get("fields")
        raw_fields = raw_fields if isinstance(raw_fields, dict) else {}
        typed[action_id] = dataclasses.replace(
            by_id[action_id],
            action_type=action_type.name,
            prepared_payload=None,
            fields={f.name: _field_value(f.type, raw_fields.get(f.name)) for f in action_type.fields},
        )

    suggestions = []
    raw_suggestions = answer.get("suggestions")
    for item in raw_suggestions if isinstance(raw_suggestions, list) else []:
        if isinstance(item, dict) and _text(item.get("text")):
            suggestions.append(Suggestion(_text(item.get("text")), _text(item.get("transcript_excerpt"))))
    return list(typed.values()), suggestions


def run_domain_stage(
    session_id: str, store: SessionStore, domain: Domain, llm: LlmClient, transcript: Transcript
) -> tuple[list[ProposedAction], list[Suggestion]]:
    """Run the Domain stage on the Session's generic Proposed Actions; save and return the result.

    `transcript` is the one the generic stage used (the English translation). Refuses to run once
    the User has decided any action, or a second time: actions it dropped are gone, so a re-run needs
    the generic extraction first. A non-JSON or non-object answer raises ValueError and saves nothing.
    """
    snapshot = store.load_actions_snapshot(session_id)
    session = store.get_session(session_id)
    if session is None:
        raise LookupError(f"no Session {session_id!r}")
    actions = list(snapshot.actions)
    if any(a.status is not ActionStatus.PROPOSED for a in actions):
        raise ValueError(f"Session {session_id!r} already has decided Proposed Actions")
    if any(a.action_type is not None for a in actions):
        raise ValueError(f"Session {session_id!r} already went through the Domain stage; run the generic extraction again first")
    started = session.started_at
    session_date = (started.astimezone() if started.tzinfo else started).date()  # local day of the Session
    prompt = build_prompt(domain, transcript, actions, session_date)
    raw = llm.generate_json(prompt, model=domain.model, json_schema=domain.answer_schema)
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:  # not re-raised: the error object would carry the raw answer (patient data)
        answer = None
    if not isinstance(answer, dict):
        raise ValueError("Domain stage answer is not a JSON object")
    typed, suggestions = apply_answer(domain, actions, answer)
    # Actions and Suggestions commit together, or neither does if a User decision won.
    store.save_actions(session_id, typed, expected_revision=snapshot.revision, suggestions=suggestions)
    return typed, suggestions
