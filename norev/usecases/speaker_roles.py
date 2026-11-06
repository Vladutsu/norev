"""Roles for the Speakers of a Session (#24): proposed by a model, validated, corrected by the User.

A Role is one the Session's Domain allows, or unknown (None). The model's answer is untrusted: an
unknown Speaker id, a Role the Domain does not have, or a Speaker answered twice gives no Role.
A Speaker is never given a person's name, Contact or owner here: S1 stays S1.
A Role the User set or confirmed (role_confirmed) is never replaced by a proposal.
"""
import dataclasses
import json
from typing import Any

from norev.core.models import Domain, Speaker, Transcript
from norev.core.ports import LlmClient, SessionStore

PROMPT = """
Give each speaker of this conversation a role from the list below, from what the transcript shows.

Roles:
{{ROLES}}

Rules:
- Use only a role name from the list, or null when the transcript does not make the speaker's role clear.
- Several speakers may have the same role.
- Never answer with a person's name.
- Answer every speaker id exactly once.

Return ONLY valid JSON: {"speakers": [{"id": "S1", "role": "<role name or null>"}]}

Transcript, one line per segment with its speaker id:
{{TRANSCRIPT}}
""".strip()

# shortcut: only the start of a long Transcript is sent; who is who is usually clear early on
MAX_TRANSCRIPT_CHARS = 6000


def build_prompt(domain: Domain, transcript: Transcript) -> str:
    roles = "\n".join(f"- {r.name}: {r.description}" for r in domain.roles)
    lines, size = [], 0
    for segment in transcript.segments:
        if segment.speaker_id is None:
            continue
        # one line per segment: newlines inside the text could forge speaker lines
        line = f"[{segment.speaker_id}] {' '.join(segment.text.split())}"
        if lines and size + len(line) > MAX_TRANSCRIPT_CHARS:
            break
        lines.append(line)
        size += len(line) + 1
    return PROMPT.replace("{{ROLES}}", roles).replace("{{TRANSCRIPT}}", "\n".join(lines))


def answer_schema(domain: Domain, speakers: list[Speaker]) -> dict:
    return {
        "type": "object",
        "properties": {
            "speakers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "enum": [s.id for s in speakers]},
                        "role": {"anyOf": [{"type": "string", "enum": [r.name for r in domain.roles]}, {"type": "null"}]},
                    },
                    "required": ["id", "role"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["speakers"],
        "additionalProperties": False,
    }


def roles_from_answer(raw: str, domain: Domain, speakers: list[Speaker]) -> dict[str, str | None]:
    """Speaker id -> Role name or None; anything the Domain or the Session does not have is dropped."""
    try:
        answer = json.loads(raw)
    except json.JSONDecodeError:  # not re-raised: the error would carry the answer (Session content)
        return {}
    items = answer.get("speakers") if isinstance(answer, dict) else None
    allowed = {r.name for r in domain.roles}
    ids = {s.id for s in speakers}
    answered: dict[str, list[Any]] = {}
    for item in items if isinstance(items, list) else []:
        if isinstance(item, dict) and item.get("id") in ids:
            answered.setdefault(item["id"], []).append(item.get("role"))
    return {
        speaker_id: roles[0] if len(roles) == 1 and roles[0] in allowed else None
        for speaker_id, roles in answered.items()
    }


def _load(session_id: str, store: SessionStore) -> tuple[Transcript, list[Speaker]]:
    session = store.get_session(session_id)
    if session is None:
        raise LookupError("Session does not exist")
    if not session.transcript_ref:
        raise ValueError("Session has no Transcript yet")
    return store.load_transcript(session.transcript_ref), store.load_speakers(session_id)


def propose_speaker_roles(
    session_id: str, store: SessionStore, domain: Domain, llm: LlmClient, model: str | None = None
) -> list[Speaker]:
    """Ask the model for the Roles of the Speakers the User has not confirmed; save and return all Speakers."""
    transcript, speakers = _load(session_id, store)
    open_speakers = [s for s in speakers if not s.role_confirmed]
    if not open_speakers:
        return speakers
    raw = llm.generate_json(build_prompt(domain, transcript), model=model, json_schema=answer_schema(domain, open_speakers))
    roles = roles_from_answer(raw, domain, open_speakers)
    proposed = [
        s if s.role_confirmed else dataclasses.replace(s, role=roles.get(s.id), role_confirmed=False) for s in speakers
    ]
    store.save_speakers(session_id, proposed)  # the store keeps a correction made in the meantime
    return store.load_speakers(session_id)


def set_speaker_role(session_id: str, store: SessionStore, domain: Domain, speaker_id: str, role: str | None) -> list[Speaker]:
    """The User's correction: a Role of the Domain, or None for unknown. It is kept from then on."""
    speakers = store.load_speakers(session_id)
    if speaker_id not in {s.id for s in speakers}:
        raise LookupError("Speaker does not exist")
    if role is not None and role not in {r.name for r in domain.roles}:
        raise ValueError("Role is not one of the Domain's Roles")
    corrected = [Speaker(s.id, role, role_confirmed=True) if s.id == speaker_id else s for s in speakers]
    store.save_speakers(session_id, corrected)
    return store.load_speakers(session_id)
