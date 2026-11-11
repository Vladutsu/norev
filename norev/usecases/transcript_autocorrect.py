"""Speech-recognition errors fixed by the large model once the meeting is over, before any analysis.

Whisper writes the live Transcript; this pass reads it again with the Domain's names and terms and
returns only the lines it changes. The code then keeps, word by word, only what is a correction:
punctuation and capitals; a word that is not Romanian (word frequency 0, e.g. "dureria") replaced by a
real word spelled almost the same ("durerea"); or a misheard name replaced by one from the Domain's
list ("Zenopint" -> "Zeno Pinter"). A real word swapped for another, a paraphrase, an added or dropped
word, a number or a negation keeps exactly what Whisper wrote. Times and Speakers never change.
(Measured on a 51-min council meeting: the model alone also "fixed" împotrivă -> împotriva and
se destramă -> se despart; those are the changes this rule throws away.)
"""
import dataclasses
import difflib
import json
import re
import unicodedata
from functools import lru_cache

from norev.core.models import Transcript
from norev.usecases.others_prompt import fill

LIMIT = 2500  # characters of transcript per request: the answer repeats the lines it changes
NUMBER_WORDS = {"zero", "unu", "una", "un", "o", "doi", "doua", "trei", "patru", "cinci", "sase", "sapte", "opt",
                "noua", "zece", "unsprezece", "doisprezece", "douasprezece", "douazeci", "suta", "sute", "mie", "mii",
                "milion", "milioane", "jumatate", "jumate", "procent", "procente", "unanimitate", "unanim"}
NEGATIONS = {"nu", "n", "nici", "niciodata", "nimic", "nimeni", "niciun", "nicio", "fara"}


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", text.lower()) if unicodedata.category(c) != "Mn")


def _words(text: str) -> list[str]:
    return re.findall(r"\w+", _fold(text))


def _counted(words: list[str], kept: set[str]) -> list[str]:
    return sorted(w for w in words if w in kept)


def vocabulary_words(vocabulary: list[str]) -> set[str]:
    return {w for entry in vocabulary for w in _words(entry)}


UNKNOWN = 1.0  # Zipf frequency under which a word is not Romanian (a misrecognition)
KNOWN = 2.0  # Zipf frequency from which a replacement is an ordinary Romanian word
SPELLING = 0.75  # how close a replacement must be spelled to the word Whisper wrote
NAME = 0.6  # the same, for a name from the Domain's list (names are misheard more freely)
_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")


@lru_cache(maxsize=20000)
def frequency(word: str) -> float:
    try:
        from wordfreq import zipf_frequency
    except ImportError:  # without the dictionary only names from the list are corrected
        return 0.0
    return zipf_frequency(word.lower(), "ro")


def _similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _fold(a), _fold(b)).ratio()


def _accepted(old: list[str], new: list[str], allowed: set[str]) -> bool:
    old_words = [t for t in old if re.fullmatch(r"\w+", t)]
    new_words = [t for t in new if re.fullmatch(r"\w+", t)]
    if not old_words and not new_words:  # punctuation and spacing only
        return True
    if [w.lower() for w in old_words] == [w.lower() for w in new_words]:  # capitals only
        return True
    if not old_words or not new_words or any(re.search(r"\d", w) for w in old_words + new_words):
        return False  # a word added or dropped, or a number touched
    if all(_fold(w) in allowed for w in new_words):
        before, after = _fold("".join(old_words)), _fold("".join(new_words))
        # "Sibiul" is a real word, the name with its article: not a misspelling of "Sibiu"
        inflected = (before.startswith(after) or after.startswith(before)) and all(frequency(w) >= UNKNOWN for w in old_words)
        return not inflected and _similar(before, after) >= NAME
    if len(old_words) != len(new_words):
        return False
    return all(frequency(o) < UNKNOWN and _fold(o) not in allowed
               and (frequency(n) >= KNOWN or _fold(n) in allowed) and _similar(o, n) >= SPELLING
               for o, n in zip(old_words, new_words))


def merge(old: str, new: str, allowed: set[str]) -> str | None:
    """`old` with only the parts of `new` that are corrections; None if nothing is left to change."""
    a, b = _TOKEN.findall(" ".join(old.split())), _TOKEN.findall(" ".join(new.split()))
    out = []
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        out.extend(b[j1:j2] if tag != "equal" and _accepted(a[i1:i2], b[j1:j2], allowed) else a[i1:i2])
    merged = " ".join("".join(out).split())
    if merged == " ".join(old.split()):
        return None
    old_words, new_words = _words(old), _words(merged)
    if (re.findall(r"\d+", old) != re.findall(r"\d+", merged)
            or _counted(old_words, NUMBER_WORDS) != _counted(new_words, NUMBER_WORDS)
            or _counted(old_words, NEGATIONS) != _counted(new_words, NEGATIONS)):
        return None
    return merged


def safe(old: str, new: str, allowed: set[str]) -> bool:
    """True if all of `new` is a correction of `old`."""
    merged = merge(old, new, allowed)
    return merged is not None and merged == " ".join(new.split())


def _batches(transcript: Transcript):
    batch, size = [], 0
    for i, segment in enumerate(transcript.segments):
        length = len(segment.text) + 8
        if batch and size + length > LIMIT:
            yield batch
            batch, size = [], 0
        batch.append(i)
        size += length
    if batch:
        yield batch


def autocorrect(llm, transcript: Transcript, correction: tuple[str, dict, list[str]], model: str | None = None
                ) -> tuple[Transcript, int]:
    """The Transcript with the safe corrections applied, and how many lines changed."""
    template, schema, vocabulary = correction
    allowed = vocabulary_words(vocabulary)
    listed = "\n".join(vocabulary) or "(nicio listă)"
    segments = list(transcript.segments)
    changed = 0
    for batch in _batches(transcript):
        source = "\n".join(f"[{i}] {' '.join(segments[i].text.split())}" for i in batch)
        try:
            answer = json.loads(llm.generate_json(fill(template, {"vocabulary": listed, "transcript": source}),
                                                  model=model, json_schema=schema))
        except (json.JSONDecodeError, ValueError):  # one fragment stays as Whisper wrote it
            continue
        fixes = answer.get("fixes") if isinstance(answer, dict) else None
        for fix in fixes if isinstance(fixes, list) else []:
            if not isinstance(fix, dict) or fix.get("i") not in batch or not isinstance(fix.get("text"), str):
                continue
            i = fix["i"]
            merged = merge(transcript.segments[i].text, fix["text"], allowed)
            if merged is not None:
                segments[i] = dataclasses.replace(segments[i], text=merged)
                changed += 1
    return dataclasses.replace(transcript, segments=tuple(segments)), changed
