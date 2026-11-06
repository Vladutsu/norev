"""Transcriber + Diarizer -> one Transcript whose Segments carry the Speaker id (#19).

Pass the Central Processor as both, so Whisper and diarization take turns on the GPU. The
speaker_id is an opaque diarization label (e.g. "speaker_0"), not a person or a Role.
"""
import dataclasses

from norev.adapters.diarization.overlap import assign_speakers
from norev.core.models import SpeakerTurn, Transcript
from norev.core.ports import Diarizer, Transcriber


class SpeakerTranscriber:
    def __init__(self, transcriber: Transcriber, diarizer: Diarizer) -> None:
        self.transcriber = transcriber
        self.diarizer = diarizer

    def transcribe(self, audio_ref: str, language: str | None = None) -> Transcript:
        transcript = self.transcriber.transcribe(audio_ref, language)
        # shortcut: SortformerDiarizer still returns (start, end, id) tuples; #24 moves it to SpeakerTurn
        turns = [(t.start, t.end, t.speaker_id) if isinstance(t, SpeakerTurn) else tuple(t)
                 for t in self.diarizer.diarize(audio_ref)]
        ids = assign_speakers([(s.start, s.end) for s in transcript.segments], turns)
        segments = tuple(dataclasses.replace(s, speaker_id=i) for s, i in zip(transcript.segments, ids))
        return Transcript(segments=segments, language=transcript.language)
