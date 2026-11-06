"""Owner check for first-person commitments (#40): never give "I'll do it" to someone else.

The model reads the Transcript without Speakers and often gives "I'll call him tomorrow" to the
person being spoken to. With the Speakers of the Transcript this is checked here, without guessing
any name: when the excerpt is a first-person commitment said by one Speaker, and the Transcript ties
the owner's name to another Speaker, the owner is wrong and becomes None (unknown). A name is tied to
a Speaker only by the Transcript: a greeting ("Good day, Mrs. Dumitrescu.") or a question by name
("Maria, what's new?") answered by the next Speaker; "Thank you, Mihai" ties nothing. A name tied to two
Speakers is not used. No Speaker id ever becomes a name here (#24).
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
