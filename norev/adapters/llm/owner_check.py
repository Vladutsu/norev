"""Owner check for first-person commitments (#40): never give "I'll do it" to someone else.

The model reads the Transcript without Speakers and often gives "I'll call him tomorrow" to the
person being spoken to. With the Speakers of the Transcript this is checked here, without guessing
any name: when the excerpt is a first-person commitment said by one Speaker, and the Transcript ties
the owner's name to another Speaker, the owner is wrong and becomes None (unknown). A name is tied to
a Speaker only by the Transcript: a greeting ("Good day, Mrs. Dumitrescu.") or a question by name
("Maria, what's new?") answered by the next Speaker; "Thank you, Mihai" ties nothing. A name tied to two
Speakers is not used. No Speaker id ever becomes a name here (#24).

An explicit "I will take care of the <object>" reply can establish a different owner
when the object appears in both the action title and excerpt, and an addressed request
elsewhere in the conversation unambiguously links the accepting voice to a name.
"""
import re
from collections.abc import Sequence

from norev.core.models import Segment

_TITLES = r"(?:mr|mrs|ms|miss|dr|doctor|domnule|doamna)\.?\s+"
# "I" doing something, unless the sentence hands the action to "you": "I'll prescribe you...",
# "I want you to..." (but "I'll send you the list" / "send it to you" stay the speaker's)
_FIRST_PERSON = re.compile(r"\bI\b|\bI'(?:ll|m|ve|d)\b")
_GREETING = re.compile(r"\s*(?:good \w+|hello|hi|dear)\b", re.I)
_YOU = re.compile(r"\byou(?:r|rs)?\b", re.I)
_SPEAKER_STILL_ACTS = re.compile(r"\b(?:send|give|email|e-mail|call|show|tell|write)\s+(?:it\s+to\s+)?you\b|\bto you\b", re.I)


def _plain(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def _name_core(owner: str) -> str:
    return re.sub(rf"^{_TITLES}", "", owner.strip(), flags=re.I).strip()


def commitment_sentences(excerpt: str) -> list[str]:
    """The sentences of the excerpt in which the speaker commits to do something themselves."""
    return [
        s for s in _sentences(excerpt)
        if _FIRST_PERSON.search(s) and (not _YOU.search(s) or _SPEAKER_STILL_ACTS.search(s))
    ]


def speakers_of_name(turns: Sequence[Segment], name: str) -> set[str]:
    """Speakers the Transcript ties `name` to: greeted or asked by name, then answering."""
    n = re.escape(name)
    title = rf"(?:{_TITLES})?"
    thanked = re.compile(rf"\b(?:thank you|thanks)\s*,?\s*{title}{n}\b", re.I)
    vocative = re.compile(rf"(?:^|[.!?]\s+|,\s*){title}{n}\s*[,.!?]", re.I)
    found: set[str] = set()
    for k, turn in enumerate(turns):
        text = turn.text
        rest = thanked.sub("", text)  # "Thank you, X" is not addressing the next Speaker (nor surely the previous)
        first = _sentences(rest)[0] if _sentences(rest) else ""
        greets = re.search(rf"^(?:good \w+|hello|hi|dear)\s*,?\s*{title}{n}\b", first, re.I)  # "Good day, Mrs. Dumitrescu."
        if greets and k > 0 and _GREETING.match(turns[k - 1].text):
            # a greeting that answers a greeting ("Good day, Doctor.") is for the previous Speaker
            target = turns[k - 1].speaker_id
        elif (greets or (vocative.search(rest) and "?" in rest)) and k + 1 < len(turns):
            target = turns[k + 1].speaker_id
        else:
            continue
        if target not in (None, turn.speaker_id):
            found.add(target)
    return found


def checked_owner(owner: str | None, excerpt: str | None, turns: Sequence[Segment]) -> str | None:
    """The model's owner, or None when it is someone else than the Speaker who said "I'll do it"."""
    if not owner or not excerpt:
        return owner
    mine = commitment_sentences(excerpt)
    said_by = {t.speaker_id for s in mine for t in turns if _plain(s) and _plain(s) in _plain(t.text)}
    if len(said_by) != 1 or None in said_by:
        return owner
    speaker = said_by.pop()
    owners_speakers = speakers_of_name(turns, _name_core(owner))
    if len(owners_speakers) != 1:
        return owner  # the Transcript does not tie the name to one Speaker: nothing to check against
    return None if owners_speakers.pop() != speaker else owner


# A narrow, explicit handoff: the named volunteer takes care of the same object as
# the Proposed Action. Do not infer an owner from a mere adjacent reply or pronoun.
_TAKE_CARE = re.compile(r"^(?:Yes,?\s+|Sure,?\s+)?I(?: will|'ll) take care of (?:the |an? )?(\w{3,})\b", re.I)
_ADDRESSED_REQUEST = re.compile(r"(?:^|[.!?]\s+)([A-Z][\w'-]+),\s+please\s+", re.UNICODE)
_ACCEPTANCE = re.compile(r"^(?:Yes|Sure|Of course|Certainly)\b", re.I)
_UNCERTAIN = re.compile(r"\b(?:if|unless|maybe|perhaps|not|can't|cannot|won't)\b|\?", re.I)


def _turn_text(turn: Segment) -> str:
    text = turn.text.strip()
    prefix = f"[{turn.speaker_id}] "
    return text[len(prefix):] if turn.speaker_id and text.startswith(prefix) else text


def _checked_action_owner(action: dict, turns: Sequence[Segment]) -> str | None:
    """Resolve an explicit named volunteer; otherwise retain the existing owner check."""
    owner = action.get("owner")
    excerpt = action.get("transcript_excerpt")
    if not excerpt:
        return owner
    quoted = [i for i, t in enumerate(turns) if _plain(excerpt) in _plain(_turn_text(t))]
    if len(quoted) != 1:
        return checked_owner(owner, excerpt, turns)
    index = quoted[0]
    volunteers = []
    for i in (index, index + 1):
        if i >= len(turns) or not turns[i].speaker_id:
            continue
        text = _turn_text(turns[i])
        match = _TAKE_CARE.match(text)
        if not match or _UNCERTAIN.search(text):
            continue
        obj = match.group(1).lower()
        if obj in {"this", "that", "everything", "anything", "something"}:
            continue
        word = rf"\b{re.escape(obj)}\b"
        if not re.search(word, excerpt, re.I) or not re.search(word, action.get("title") or "", re.I):
            continue
        if i != index and turns[i].speaker_id == turns[index].speaker_id:
            continue
        volunteers.append(turns[i].speaker_id)
    if len(set(volunteers)) != 1:
        return checked_owner(owner, excerpt, turns)
    volunteer = volunteers[0]
    # Names must be explicitly addressed and the next, different voice must accept.
    # Require a one-to-one mapping: multiple names/voices make this inconclusive.
    identities: dict[str, set[str]] = {}
    speaker_ids = {turn.speaker_id for turn in turns}
    for request, reply in zip(turns, turns[1:]):
        names = _ADDRESSED_REQUEST.findall(_turn_text(request))
        response = _turn_text(reply)
        if (len(names) != 1 or names[0] in speaker_ids
                or not reply.speaker_id or reply.speaker_id == request.speaker_id
                or not _ACCEPTANCE.match(response) or _UNCERTAIN.search(response)):
            continue
        identities.setdefault(names[0], set()).add(reply.speaker_id)
    names = [name for name, speakers in identities.items() if volunteer in speakers]
    if len(names) == 1 and identities[names[0]] == {volunteer}:
        return names[0]
    return checked_owner(owner, excerpt, turns)
