"""The User corrects the spoken Transcript of a Session (#82): a misheard name or acronym.

Only a Segment's text changes, never its times or Speaker. The translation is dropped with the
correction, so the next analysis translates and extracts from the corrected text.
"""
import dataclasses
from typing import Any

from norev.core.models import ValidationError
from norev.core.ports import SessionStore


MAX_NAME = 80


def _spoken(session_id: str, store: SessionStore):
    session = store.get_session(session_id)
    if session is None:
        raise LookupError("Session does not exist")
    spoken_ref = session.source_transcript_ref or session.transcript_ref
    if not spoken_ref:
        raise LookupError("Session is not transcribed yet")
    return store.load_transcript(spoken_ref)


def name_speaker(session_id: str, speaker_id: Any, name: Any, store: SessionStore) -> None:
    """The User names a Speaker (a citizen, or a voice the profiles left anonymous): every Segment of that
    Speaker gets the name; an empty name clears it. The minutes change only on "Regenerează"."""
    transcript = _spoken(session_id, store)
    if not isinstance(speaker_id, str) or all(s.speaker_id != speaker_id for s in transcript.segments):
        raise ValidationError("invalid_speaker", "speaker_id")
    if not isinstance(name, str):
        raise ValidationError("invalid_string", "name")
    name = " ".join(name.split())
    # Brackets would read as a label in the minutes prompts, like the profile names refuse them.
    if len(name) > MAX_NAME or "[" in name or "]" in name or not name.isprintable():
        raise ValidationError("invalid_name", "name")
    segments = tuple(dataclasses.replace(s, speaker_name=name or None) if s.speaker_id == speaker_id else s
                     for s in transcript.segments)
    store.correct_transcript(session_id, dataclasses.replace(transcript, segments=segments))


def correct_transcript(session_id: str, index: Any, text: Any, store: SessionStore) -> None:
    transcript = _spoken(session_id, store)
    if type(index) is not int or not 0 <= index < len(transcript.segments):
        raise ValidationError("invalid_index", "index")
    if not isinstance(text, str):
        raise ValidationError("invalid_string", "text")
    if not text.strip():
        raise ValidationError("required", "text")
    segments = list(transcript.segments)
    segments[index] = dataclasses.replace(segments[index], text=text.strip())
    store.correct_transcript(session_id, dataclasses.replace(transcript, segments=tuple(segments)))
