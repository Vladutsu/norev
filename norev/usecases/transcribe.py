"""Final Transcript with Speakers (#24): Whisper, then diarization, then a Speaker on every Segment.

Pass the Central Processor as both `transcriber` and `diarizer`: its GPU gate runs Whisper and the
diarization model one after the other, never together (6-8 GB of VRAM).
"""
from norev.core.models import Transcript
from norev.core.ports import Diarizer, SessionStore, Transcriber
from norev.core.speakers import attach_speakers, speakers_in


def transcribe_session(
    session_id: str, store: SessionStore, transcriber: Transcriber, diarizer: Diarizer, language: str | None = None
) -> Transcript:
    """Transcribe the Session's audio, attach Speakers, save the Transcript and the Speakers (no Roles yet).

    A Segment that no diarization turn overlaps keeps no Speaker. Raises LookupError for an unknown
    Session and ValueError when it has no audio or its review already started (the store refuses).
    """
    session = store.get_session(session_id)
    if session is None:
        raise LookupError("Session does not exist")
    if not session.audio_ref:
        raise ValueError("Session has no audio")
    transcript = transcriber.transcribe(session.audio_ref, language)
    turns = diarizer.diarize(session.audio_ref)
    with_speakers = attach_speakers(transcript, turns)
    session.transcript_ref = store.save_transcript(session_id, with_speakers)
    store.save_session(session)
    store.save_speakers(session_id, speakers_in(with_speakers))
    return with_speakers
