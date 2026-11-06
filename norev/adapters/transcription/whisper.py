"""Transcriber port on the tuned faster-whisper manager in LLM/Whisper; writes nothing to disk.

faster_whisper is imported on first use, so the package imports without it (CPU tests, CI).
# shortcut: minimal adapter so the CLI runs end to end; #24 owns the real Whisper adapter
# (opaque audio refs, measurements) and replaces this one.
"""
import gc

from LLM.Whisper.settings import WhisperSettings
from norev.core.models import Segment, Transcript


class WhisperTranscriber:
    def __init__(self, settings: WhisperSettings | None = None) -> None:
        self._settings = settings or WhisperSettings.from_environment()
        self._manager = None

    def transcribe(self, audio_ref: str, language: str | None = None) -> Transcript:
        """audio_ref: path to the audio or video file. Empty language: WHISPER_LANGUAGES or auto-detect."""
        if self._manager is None:
            from LLM.Whisper.cuda_runtime import CudaRuntimeLoader
            from LLM.Whisper.model_manager import WhisperModelManager

            self._manager = WhisperModelManager(self._settings, CudaRuntimeLoader())
        segments, info = self._manager.transcribe(audio_ref, language or "")
        return Transcript(
            segments=tuple(
                Segment(round(float(s.start), 3), round(float(s.end), 3), s.text.strip())
                for s in segments if s.text.strip()
            ),
            language=getattr(info, "language", None),
        )

    def close(self) -> None:
        """Drop the model so the diarizer or the LLM can use the GPU next."""
        self._manager = None
        gc.collect()  # CTranslate2 frees its GPU memory only when the model object is collected
