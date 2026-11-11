"""Diarizer port: NVIDIA Streaming Sortformer v2 (NeMo), the option picked in the wave 1 spike (#5, #24).

Needs `requirements-diarization.txt`; the imports are inside the methods, so the package imports
without them. The model (CC-BY-4.0, not gated) is downloaded once from Hugging Face; after that,
HF_HUB_OFFLINE=1 runs it with no network (ADR 0001).
"""
import contextlib
import io
import os
import sys

from norev.core.models import SpeakerTurn
from norev.core.ports import SessionStore

MODEL = "nvidia/diar_streaming_sortformer_4spk-v2"  # at most 4 Speakers per Session
SAMPLE_RATE = 16000
# "High latency" streaming setup from the model card: the most accurate one, and the whole file
# is still processed in one call. (chunk_len, right_context, fifo_len, update_period, spkcache_len)
STREAMING = (340, 40, 40, 300, 188)


class SortformerDiarizer:
    def __init__(self, store: SessionStore, device: str | None = None) -> None:
        self._store = store
        self._device = device or os.getenv("DIARIZATION_DEVICE", "cuda")
        self._model = None

    def diarize(self, audio_ref: str) -> list[SpeakerTurn]:
        """Who spoke when, from the Session audio in the store (decoded in memory, never to a plain file)."""
        from faster_whisper import decode_audio
        import torch

        audio = decode_audio(io.BytesIO(self._store.load_audio(audio_ref)), sampling_rate=SAMPLE_RATE)
        # NeMo logs to stdout; callers such as `python -m norev.session transcribe` print their result there
        with contextlib.redirect_stdout(sys.stderr), torch.inference_mode():
            segments = self._load().diarize(audio=audio, sample_rate=SAMPLE_RATE, batch_size=1, verbose=False)[0]
        turns = []
        for segment in segments:
            start, end, speaker = segment.split() if isinstance(segment, str) else segment
            turns.append(SpeakerTurn(float(start), float(end), str(speaker)))
        return sorted(turns, key=lambda t: (t.start, t.end, t.speaker_id))

    def close(self) -> None:
        """Free the GPU memory, so Whisper or the LLM can load next (the Central Processor calls it)."""
        if self._model is None:
            return
        self._model = None
        import torch

        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    def _load(self):
        if self._model is None:
            from nemo.collections.asr.models import SortformerEncLabelModel

            model = SortformerEncLabelModel.from_pretrained(MODEL, map_location=self._device)
            model.eval()
            modules = model.sortformer_modules
            (modules.chunk_len, modules.chunk_right_context, modules.fifo_len,
             modules.spkcache_update_period, modules.spkcache_len) = STREAMING
            modules._check_streaming_parameters()
            self._model = model
        return self._model
