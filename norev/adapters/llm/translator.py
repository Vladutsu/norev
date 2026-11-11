"""Translator port (decision #13): translate a Transcript per Speaker turn and keep `speaker_id`.

Several turns go in one LLM call, each tagged with its id, in batches of about CHUNK_CHARS source
characters (the size LLM/AI_Model/translate.py uses, so a batch and its answer fit a 4096-token slot).
The answer is untrusted: an id must come back exactly once with a non-empty text; anything else is
ignored, the missing turns are retried once, and a turn still missing keeps its original text with
`untranslated=True`.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from LLM.AI_Model.translate import CHUNK_CHARS, LANGUAGE_NAMES, split_sentences
from norev.adapters.llm.numbers_ro import numbers_to_digits
from norev.core.models import Segment, Transcript
from norev.core.ports import LlmClient

# Same rules as LLM/AI_Model/translate.py TRANSLATE_PROMPT, applied per turn
PROMPT = """
Translate these turns of a session transcript from {{SOURCE}} into English.

The transcript comes from automatic speech recognition. Each turn is what one speaker said, in order.
- Translate each turn on its own, faithfully, sentence by sentence. Do not summarise, skip, merge or add anything, and do not move text from one turn to another.
- Keep questions as questions, and keep every name, number, date, time and amount exactly.
- Fix obvious speech-recognition misspellings only when the intended word is unambiguous.
- Answer with every turn id exactly once, in the same order.

Return ONLY valid JSON: {"turns": [{"id": "...", "text": "..."}]}

Turns, one JSON object per line:
{{TURNS}}
""".strip()


@dataclass(frozen=True)
class _Turn:
    start: float
    end: float
    speaker_id: str | None
    text: str


def speaker_turns(transcript: Transcript) -> list[_Turn]:
    """Consecutive Segments of the same Speaker form one turn (Segments without a Speaker too)."""
    turns: list[_Turn] = []
    for segment in transcript.segments:
        text = " ".join(segment.text.split())
        if turns and turns[-1].speaker_id == segment.speaker_id:
            last = turns[-1]
            turns[-1] = _Turn(last.start, segment.end, last.speaker_id, f"{last.text} {text}".strip())
        else:
            turns.append(_Turn(segment.start, segment.end, segment.speaker_id, text))
    return turns


def _pieces(text: str, max_chars: int) -> list[str]:
    """A turn longer than one batch (e.g. a Transcript without Speakers) is split between sentences."""
    pieces: list[str] = []
    for sentence in split_sentences(text):
        if pieces and len(pieces[-1]) + 1 + len(sentence) <= max_chars:
            pieces[-1] = f"{pieces[-1]} {sentence}"
        else:
            pieces.append(sentence)
    return pieces


def _schema(ids: list[str]) -> dict:
    """Constrains decoding to exactly the batch's ids; the answer is still checked, never trusted."""
    return {
        "type": "object",
        "properties": {
            "turns": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "enum": ids}, "text": {"type": "string", "minLength": 1}},
                    "required": ["id", "text"],
                    "additionalProperties": False,
                },
                "minItems": len(ids),
                "maxItems": len(ids),
            }
        },
        "required": ["turns"],
        "additionalProperties": False,
    }


class TurnTranslator:
    def __init__(
        self, client: LlmClient, model: str | None = None, workers: int = 1, chunk_chars: int = CHUNK_CHARS
    ) -> None:
        self.client = client  # injected: only the Central Processor holds a real network client
        self.model = model
        self.workers = max(1, workers)
        self.chunk_chars = chunk_chars

    def translate(self, transcript: Transcript, source_language: str) -> Transcript:
        turns = speaker_turns(transcript)
        # piece id -> source text; a turn is the list of its piece ids, in order
        source: dict[str, str] = {}
        turn_pieces: list[list[str]] = []
        for turn in turns:
            ids = []
            for piece in _pieces(turn.text, self.chunk_chars):
                ids.append(str(len(source) + 1))
                source[ids[-1]] = piece
            turn_pieces.append(ids)

        source_name = LANGUAGE_NAMES.get(source_language, source_language or "the source language")
        # Numbers written in words go as digits, so the model only has to copy them (#39); a turn left
        # untranslated still keeps its original text
        sent = {i: numbers_to_digits(t) for i, t in source.items()} if source_language == "ro" else source
        english = self._translate_pieces(list(source), sent, source_name)
        missing = [i for i in source if i not in english]
        if missing:  # one retry, in new batches of only the missing turns
            english.update(self._translate_pieces(missing, sent, source_name))

        segments = []
        for turn, ids in zip(turns, turn_pieces):
            untranslated = any(i not in english for i in ids)
            text = " ".join(english.get(i, source[i]) for i in ids)
            segments.append(Segment(turn.start, turn.end, text, turn.speaker_id, untranslated=untranslated))
        return Transcript(segments=tuple(segments), language="en")

    def _translate_pieces(self, ids: list[str], source: dict[str, str], source_name: str) -> dict[str, str]:
        batches: list[list[str]] = []
        size = 0
        for i in ids:
            if batches and size + len(source[i]) <= self.chunk_chars:
                batches[-1].append(i)
                size += len(source[i])
            else:
                batches.append([i])
                size = len(source[i])
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(lambda batch: self._translate_batch(batch, source, source_name), batches))
        return {i: text for result in results for i, text in result.items()}

    def _translate_batch(self, ids: list[str], source: dict[str, str], source_name: str) -> dict[str, str]:
        lines = "\n".join(json.dumps({"id": i, "text": source[i]}, ensure_ascii=False) for i in ids)
        prompt = PROMPT.replace("{{SOURCE}}", source_name).replace("{{TURNS}}", lines)
        raw = self.client.generate_json(prompt, model=self.model, json_schema=_schema(ids))
        try:
            answer = json.loads(raw)
        except json.JSONDecodeError:  # not re-raised: the error would carry the answer (Session content)
            return {}
        items = answer.get("turns") if isinstance(answer, dict) else None
        found: dict[str, list[str]] = {}
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            turn_id = str(item.get("id"))
            if turn_id in ids:  # unknown ids are ignored
                found.setdefault(turn_id, []).append(item["text"].strip())
        # an id answered twice is ambiguous, an empty text is not a translation: both count as missing
        return {i: texts[0] for i, texts in found.items() if len(texts) == 1 and texts[0]}
