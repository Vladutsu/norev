"""The User corrects the spoken Transcript of a Session (#82): a misheard name or acronym.

Only a Segment's text changes, never its times or Speaker. The translation is dropped with the
correction, so the next analysis translates and extracts from the corrected text.
"""
import dataclasses
from typing import Any

from norev.core.models import ValidationError
from norev.core.ports import SessionStore


def correct_transcript(session_id: str, index: Any, text: Any, store: SessionStore) -> None:
    session = store.get_session(session_id)
    if session is None:
        raise LookupError("Session does not exist")
    spoken_ref = session.source_transcript_ref or session.transcript_ref
    if not spoken_ref:
        raise LookupError("Session is not transcribed yet")
    transcript = store.load_transcript(spoken_ref)
    if type(index) is not int or not 0 <= index < len(transcript.segments):
        raise ValidationError("invalid_index", "index")
    if not isinstance(text, str):
        raise ValidationError("invalid_string", "text")
    if not text.strip():
        raise ValidationError("required", "text")
    segments = list(transcript.segments)
    segments[index] = dataclasses.replace(segments[index], text=text.strip())
    store.correct_transcript(session_id, dataclasses.replace(transcript, segments=tuple(segments)))
