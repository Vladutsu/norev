"""Prototype Diarizer: NVIDIA Streaming Sortformer v2 (NeMo), the best option in the wave 1 spike (#5).

Needs `requirements-diarization.txt`; the imports are inside the methods, so the package imports
without them. The model (CC-BY-4.0, not gated) is downloaded once from Hugging Face; after that,
HF_HUB_OFFLINE=1 runs it with no network (ADR 0001).

    python -m norev.adapters.diarization.sortformer <audio_or_video>   # prints the turns
"""
import os
import sys

from norev.adapters.diarization.overlap import Turn

MODEL = "nvidia/diar_streaming_sortformer_4spk-v2"  # at most 4 Speakers per Session
SAMPLE_RATE = 16000
# "High latency" streaming setup from the model card: the most accurate one, and the whole file
# is still processed in one call. (chunk_len, right_context, fifo_len, update_period, spkcache_len)
STREAMING = (340, 40, 40, 300, 188)


class SortformerDiarizer:
    def __init__(self, device: str | None = None) -> None:
        self._device = device or os.getenv("DIARIZATION_DEVICE", "cuda")
        self._model = None

    def diarize(self, audio_ref: str) -> list[Turn]:
        """audio_ref: path to any audio or video file ffmpeg can read (decoded to 16 kHz mono)."""
        # shortcut: audio_ref is a file path; the SessionStore will resolve opaque refs later
        from faster_whisper import decode_audio
        import torch

        audio = decode_audio(audio_ref, sampling_rate=SAMPLE_RATE)
        with torch.inference_mode():
            segments = self._load().diarize(audio=audio, sample_rate=SAMPLE_RATE, batch_size=1, verbose=False)[0]
        turns = []
        for segment in segments:
            start, end, speaker = segment.split() if isinstance(segment, str) else segment
            turns.append((float(start), float(end), str(speaker)))
        return sorted(turns)

    def close(self) -> None:
        """Free the GPU memory, so Whisper or the LLM can load next (they never run at the same time)."""
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


if __name__ == "__main__":
    for start, end, speaker in SortformerDiarizer().diarize(sys.argv[1]):
        print(f"{start:8.2f} {end:8.2f}  {speaker}")
