"""Transcriber port backed by faster-whisper (#24).

The audio comes from the SessionStore by its opaque audio_ref and is decoded in memory, so it is
never written to a plain file (ADR 0004). Settings are the ones LLM/Whisper already reads
(WHISPER_MODEL, WHISPER_DEVICE, WHISPER_LANGUAGES...). Whisper's segments can hold several Speakers,
so with per-word times they are cut at the end of each sentence: one sentence is usually one Speaker,
and the diarization then gives each Segment the Speaker it overlaps most.
faster_whisper is imported inside the methods, so the module imports without it.
"""
import gc
import io
import os
from collections.abc import Iterable

from LLM.Whisper.settings import WhisperSettings
from norev.core.models import Segment, Transcript
from norev.core.ports import SessionStore

SAMPLE_RATE = 16000
_SENTENCE_END = (".", "?", "!", "…")


def sentence_segments(words: Iterable[tuple[float, float, str]]) -> list[Segment]:
    """Whisper words (start, end, text with its leading space) -> one Segment per sentence."""
    segments: list[Segment] = []
    current: list[tuple[float, float, str]] = []
    for word in words:
        current.append(word)
        if word[2].strip().endswith(_SENTENCE_END):
            segments.append(_segment(current))
            current = []
    if current:
        segments.append(_segment(current))
    return [s for s in segments if s.text]


def _segment(words: list[tuple[float, float, str]]) -> Segment:
    text = " ".join("".join(w[2] for w in words).split())
    return Segment(round(words[0][0], 3), round(max(words[0][1], words[-1][1]), 3), text)


def _is_cuda_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(k in text for k in ("cuda", "cublas", "cudnn", "out of memory"))


class WhisperTranscriber:
    def __init__(self, store: SessionStore, settings: WhisperSettings | None = None) -> None:
        self._store = store
        self._settings = settings or WhisperSettings.from_environment()
        self._device = self._settings.model_device
        self._model = None

    def transcribe(self, audio_ref: str, language: str | None = None) -> Transcript:
        from faster_whisper import decode_audio

        audio = decode_audio(io.BytesIO(self._store.load_audio(audio_ref)), sampling_rate=SAMPLE_RATE)
        try:
            return self._transcribe(audio, language)
        except RuntimeError as exc:  # same fallback as LLM/Whisper: no usable GPU -> CPU
            if self._device == "cpu" or not _is_cuda_error(exc):
                raise
            self.close()
            self._device = "cpu"
            return self._transcribe(audio, language)

    def close(self) -> None:
        """Free the GPU memory, so diarization or the LLM can load next (the Central Processor calls it)."""
        self._model = None
        gc.collect()

    def _transcribe(self, audio, language: str | None) -> Transcript:
        model = self._load()
        language = language or self._allowed_language(model, audio)
        s = self._settings
        segments, info = model.transcribe(
            audio,
            language=language or None,
            beam_size=s.beam_size,
            vad_filter=s.vad_filter,
            vad_parameters={"min_silence_duration_ms": s.vad_min_silence_ms},
            condition_on_previous_text=s.condition_on_previous_text,
            word_timestamps=True,
        )
        words = []
        for segment in segments:
            if segment.words:
                words.extend((w.start, w.end, w.word) for w in segment.words)
            elif segment.text.strip():  # no word times for this segment: keep it whole
                words.append((segment.start, segment.end, " " + segment.text.strip()))
        return Transcript(segments=tuple(sentence_segments(words)), language=language or info.language)

    def _allowed_language(self, model, audio) -> str | None:
        """WHISPER_LANGUAGES=ro,en: the most likely of those, as LLM/Whisper does; empty: Whisper decides."""
        allowed = [c.strip().lower() for c in os.getenv("WHISPER_LANGUAGES", "").split(",") if c.strip()]
        if len(allowed) <= 1:
            return allowed[0] if allowed else None
        _, _, probabilities = model.detect_language(audio=audio, language_detection_segments=3)
        probs = dict(probabilities)
        return max(allowed, key=lambda code: probs.get(code, 0.0))

    def _load(self):
        if self._model is None:
            from faster_whisper import WhisperModel

            s = self._settings
            compute_type = s.model_compute_type if self._device != "cpu" else s.cpu_fallback_compute_type
            self._model = WhisperModel(
                s.model_name, device=self._device, compute_type=compute_type,
                cpu_threads=s.cpu_threads, num_workers=s.num_workers,
            )
        return self._model
