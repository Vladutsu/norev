"""Which Member takes each Proposed Action, suggested from their Skills (ADR 0006).

One local model call after the Domain stage, with the Domain's assign prompt: the Members (an id, the
name and the Skills; no email) and the Proposed Actions. The answer names a Member per action, or null
when nobody's Skills fit; then the owner stays empty. Only ids from the prompt are taken, so the model
cannot name anyone else. It is a suggestion: the Team Leader picks another Member before approval.
"""
import dataclasses
import json

from norev.core.models import ActionStatus, Member, ProposedAction, Suggestion
from norev.core.ports import LlmClient
from norev.usecases.others_prompt import fill

FAILED = Suggestion("Responsabilii nu au putut fi propuși din skill-uri. Alege-i din echipă înainte de aprobare.")


def _line(*parts: object) -> str:
    """One prompt line: newlines in a value cannot forge another Member or action."""
    return " | ".join(" ".join(str(p or "").split()) for p in parts)


def assign_by_skills(llm: LlmClient, template: str, schema: dict, actions: list[ProposedAction],
                     members: list[Member], *, model: str | None = None) -> tuple[list[ProposedAction], bool]:
    """The actions with `owner` and `assignee` set from the answer, and whether the answer was valid.

    Only Proposed actions are asked about; a decided one is never changed. No Members or nothing to
    ask: no model call. An invalid answer leaves every action as it was."""
    open_actions = [a for a in actions if a.status is ActionStatus.PROPOSED]
    if not open_actions or not members:
        return actions, True
    member_ids = {f"m{i}": m for i, m in enumerate(members, 1)}
    action_ids = {f"a{i}": a.id for i, a in enumerate(open_actions, 1)}
    prompt = fill(template, {
        "members": "\n".join(_line(mid, m.name, ", ".join(m.skills) or "-") for mid, m in member_ids.items()),
        "actions": "\n".join(_line(aid, a.title, a.description, a.owner, a.transcript_excerpt)
                             for aid, a in zip(action_ids, open_actions)),
    })
    raw = llm.generate_json(prompt, model=model, json_schema=schema)
    try:
        answer = json.loads(raw)
    except (TypeError, json.JSONDecodeError):  # not re-raised: the error would carry the raw answer
        return actions, False
    pairs = answer.get("assignments") if isinstance(answer, dict) else None
    if not isinstance(pairs, list):
        return actions, False
    chosen: dict[str, Member | None] = {}
    for pair in pairs:
        if not isinstance(pair, dict) or pair.get("action") not in action_ids:
            continue
        member = pair.get("member")
        if member is None or member in member_ids:
            chosen.setdefault(action_ids[pair["action"]], member_ids.get(member) if member else None)
    # with a Team the owner is a Member or nobody: an action the answer skipped, or gave an unknown id, stays blank
    open_ids = set(action_ids.values())
    return [dataclasses.replace(a, assignee=chosen[a.id].email if chosen.get(a.id) else None,
                                owner=chosen[a.id].name if chosen.get(a.id) else None)
            if a.id in open_ids else a for a in actions], True
