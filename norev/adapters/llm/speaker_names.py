"""Names of the Speakers, and owners for the Proposed Actions they took on ("I'll do it").

Diarization only says "S1", "S2". The LLM only lists the person names written in the Transcript;
which Speaker has which name is decided here, by rules the Transcript must show:
- self-introduction: a Speaker says "I'm Andrei" / "my name is Andrei" / "Andrei here";
- a question to someone by name ("Andrei, how are you doing?", "Alright, Maria, what's new?"),
  answered in the very next turn by another Speaker: that Speaker is the one asked.
A Speaker given two different names, or a name given to two Speakers, keeps no name.

A Proposed Action then gets a Speaker's name only when its Transcript excerpt is a first-person
commitment ("I'll send it", "I'm handling it") said in exactly one turn, by a Speaker who spoke
before that turn and whose name was already known from an earlier turn. The extractor cannot know
who "I" is, so that name replaces the owner it guessed. Otherwise the owner is left as it is.
"""
import json
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from norev.core.ports import LlmClient

PROMPT = """
List the names of the people that appear in this part of a session transcript.

Only names of people (first names, last names, nicknames), written exactly as in the transcript.
No companies, products, places, teams or job titles. If there are none, return an empty list.

Return ONLY valid JSON: {"names": ["..."]}

Transcript:
{{TEXT}}
""".strip()

SCHEMA = {
    "type": "object",
    "properties": {"names": {"type": "array", "items": {"type": "string", "minLength": 1}}},
    "required": ["names"],
    "additionalProperties": False,
}

# English, because the pipeline translates first; checked on the excerpt the extractor quoted
_COMMITMENT = re.compile(r"\bI(?:'ll|'m| will| am| can| shall)\b|\blet me\b", re.I)


@dataclass(frozen=True)
class Turn:
    speaker_id: str
    text: str


@dataclass(frozen=True)
class SpeakerName:
    name: str
    evidence_turn: int  # index of the turn that shows the name


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", text.lower()).split())


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def _introduces(text: str, name: str) -> bool:
    n = re.escape(name)
    return re.search(rf"\b(?:I'm|I am|my name is|this is)\s+{n}\b|\b{n}\s+(?:here|speaking)\b", text, re.I) is not None


def _asks(text: str, name: str) -> bool:
    """A question addressed to `name`: the name opens the sentence or sits between commas, and the sentence asks."""
    n = re.escape(name)
    return any(
        s.endswith("?") and re.search(rf"(?:^|,\s*){n}\s*,|,\s*{n}\s*\?$", s)
        for s in _sentences(text)
    )


def names_from_turns(turns: list[Turn], person_names: list[str]) -> dict[str, SpeakerName]:
    """Speaker id -> name, from self-introductions and answered questions; conflicts keep no name."""
    claims: list[tuple[str, str, int]] = []
    for name in {n.strip() for n in person_names if isinstance(n, str) and n.strip()}:
        for k, turn in enumerate(turns):
            if _introduces(turn.text, name):
                claims.append((turn.speaker_id, name, k))
            elif _asks(turn.text, name) and k + 1 < len(turns) and turns[k + 1].speaker_id != turn.speaker_id:
                claims.append((turns[k + 1].speaker_id, name, k))
    names_of: dict[str, set[str]] = {}
    speakers_of: dict[str, set[str]] = {}
    first: dict[str, int] = {}
    for speaker_id, name, k in claims:
        names_of.setdefault(speaker_id, set()).add(name)
        speakers_of.setdefault(name, set()).add(speaker_id)
        first[speaker_id] = min(first.get(speaker_id, k), k)
    return {
        speaker_id: SpeakerName(next(iter(names)), first[speaker_id])
        for speaker_id, names in sorted(names_of.items())
        if len(names) == 1 and len(speakers_of[next(iter(names))]) == 1
    }


def speaker_owners(turns: list[Turn], actions: list[dict], names: dict[str, SpeakerName]) -> dict[str, tuple[str, str]]:
    """action id -> (name, speaker_id) for the Proposed Actions a named Speaker took on in the first person."""
    result = {}
    for action in actions:
        excerpt = action.get("transcript_excerpt")
        # The extractor sees no Speakers, so an excerpt can span a question and its answer:
        # only its first-person sentences count, and they must all come from one turn.
        mine = [s for s in _sentences(excerpt) if _COMMITMENT.search(s)] if isinstance(excerpt, str) else []
        said_in = {k for s in mine for k, t in enumerate(turns) if _norm(s) and _norm(s) in _norm(t.text)}
        found_all = all(any(_norm(s) in _norm(t.text) for t in turns) for s in mine)
        if not mine or not found_all or len(said_in) != 1:
            continue
        k = said_in.pop()
        speaker_id = turns[k].speaker_id
        name = names.get(speaker_id)
        spoke_before = any(t.speaker_id == speaker_id for t in turns[:k])
        if name and spoke_before and name.evidence_turn < k:
            result[action["id"]] = (name.name, speaker_id)
    return result


class SpeakerNamer:
    def __init__(self, client: LlmClient, model: str | None = None, workers: int = 1, chunk_chars: int = 6000) -> None:
        self.client = client  # injected: only the Central Processor holds a real network client
        self.model = model
        self.workers = max(1, workers)
        self.chunk_chars = chunk_chars

    def name(self, turns: list[Turn]) -> dict[str, SpeakerName]:
        chunks: list[str] = []
        for turn in turns:
            text = " ".join(turn.text.split())
            if chunks and len(chunks[-1]) + 1 + len(text) <= self.chunk_chars:
                chunks[-1] += "\n" + text
            else:
                chunks.append(text)
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            listed = [name for names in pool.map(self._ask, chunks) for name in names]
        # a name the model made up, or changed, is not in the Transcript and is dropped
        everything = "\n".join(t.text for t in turns)
        present = [n for n in listed if re.search(rf"\b{re.escape(n.strip())}\b", everything)]
        return names_from_turns(turns, present)

    def _ask(self, text: str) -> list[str]:
        raw = self.client.generate_json(PROMPT.replace("{{TEXT}}", text), model=self.model, json_schema=SCHEMA)
        try:
            answer = json.loads(raw)
        except json.JSONDecodeError:  # not re-raised: the error would carry the answer (Session content)
            return []
        names = answer.get("names") if isinstance(answer, dict) else None
        return [n for n in names if isinstance(n, str) and n.strip()] if isinstance(names, list) else []
