import inspect
import os
import sys
import threading
from typing import Optional

from faster_whisper import BatchedInferencePipeline, WhisperModel, decode_audio

from LLM.Whisper.cuda_runtime import CudaRuntimeLoader
from LLM.Whisper.settings import WhisperSettings


class WhisperModelManager:
    def __init__(self, settings: WhisperSettings, cuda_loader: CudaRuntimeLoader) -> None:
        self._settings = settings
        self._cuda_loader = cuda_loader
        self._model: Optional[WhisperModel] = None
        self._batched_pipeline: Optional[BatchedInferencePipeline] = None
        self._model_init_lock = threading.RLock()
        self._transcribe_lock = threading.Lock()
        self._runtime_device = settings.model_device
        self._runtime_compute_type = settings.model_compute_type
        self._active_batch_size = settings.batch_size
        self._active_compute_type = settings.model_compute_type

    @property
    def runtime_device(self) -> str:
        return self._runtime_device

    @property
    def runtime_compute_type(self) -> str:
        return self._runtime_compute_type

    def get_model(self) -> WhisperModel:
        if self._model is None:
            with self._model_init_lock:
                if self._model is None:
                    try:
                        self._model = self._new_model(
                            self._settings.model_device,
                            self._active_compute_type,
                        )
                        self._runtime_device = self._settings.model_device
                        self._runtime_compute_type = self._active_compute_type
                    except Exception as exc:
                        requested_device = (self._settings.model_device or "").lower()
                        if requested_device == "cpu":
                            raise
                        self._switch_to_cpu_model(str(exc))
        return self._model

    def transcribe(
        self,
        file_path: str,
        language: str,
        on_segment=None,
    ) -> tuple[object, object]:
        self.get_model()
        with self._transcribe_lock:
            language = language or self._detect_allowed_language(file_path)
            while True:
                try:
                    segments, info = self._transcribe_with_runtime(file_path, language)
                    return self._materialize_segments(segments, on_segment), info
                except Exception as exc:
                    if self._runtime_device != "cpu" and self._looks_like_cuda_runtime_error(exc):
                        if self._looks_like_cuda_oom(exc) and self._relax_cuda_runtime(str(exc)):
                            continue
                        self._switch_to_cpu_model(str(exc))
                        self.get_model()
                        continue
                    raise

    def _detect_allowed_language(self, file_path: str) -> str:
        """With WHISPER_LANGUAGES=ro,en,de pick the most likely of those languages, so detection can
        never land on a language the user does not speak. Empty: Whisper's own auto-detection."""
        allowed = [c.strip().lower() for c in os.getenv("WHISPER_LANGUAGES", "").split(",") if c.strip()]
        if not allowed:
            return ""
        if len(allowed) == 1:
            return allowed[0]
        audio = decode_audio(file_path, sampling_rate=16000)
        _, _, all_probs = self.get_model().detect_language(audio=audio, language_detection_segments=3)
        probs = dict(all_probs)
        best = max(allowed, key=lambda code: probs.get(code, 0.0))
        print(f"[INFO] Language detected among {','.join(allowed)}: {best} "
              f"(p={probs.get(best, 0.0):.2f})", file=sys.stderr)
        return best

    def _transcribe_with_runtime(self, file_path: str, language: str) -> tuple[object, object]:
        kwargs = {
            "language": language or None,
            "beam_size": self._settings.beam_size,
            "vad_filter": self._settings.vad_filter,
            "vad_parameters": {"min_silence_duration_ms": self._settings.vad_min_silence_ms},
            "condition_on_previous_text": self._settings.condition_on_previous_text,
            "word_timestamps": self._settings.word_timestamps,
        }

        if self._runtime_device != "cpu" and self._active_batch_size > 1:
            pipeline = self._get_batched_pipeline()
            return pipeline.transcribe(
                file_path,
                batch_size=self._active_batch_size,
                **self._supported_kwargs(pipeline.transcribe, kwargs),
            )

        model = self.get_model()
        return model.transcribe(file_path, **self._supported_kwargs(model.transcribe, kwargs))

    @staticmethod
    def _supported_kwargs(func, candidate: dict) -> dict:
        """Keep only kwargs the installed faster-whisper build accepts.

        The sequential model and the batched pipeline expose different transcribe
        signatures across versions; filtering here lets us pass the richer, more
        reliable param set without ever raising TypeError on an older build.
        """
        try:
            params = inspect.signature(func).parameters
        except (TypeError, ValueError):
            return dict(candidate)
        if any(p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()):
            return dict(candidate)
        return {key: value for key, value in candidate.items() if key in params}

    def _get_batched_pipeline(self) -> BatchedInferencePipeline:
        if self._batched_pipeline is None:
            model = self.get_model()
            self._batched_pipeline = BatchedInferencePipeline(model=model)
        return self._batched_pipeline

    def _new_model(self, device: str, compute_type: str) -> WhisperModel:
        normalized = (device or "").lower()
        if normalized in ("cuda", "auto"):
            self._cuda_loader.preload()

        kwargs = {"device": device}
        if compute_type and compute_type != "default":
            kwargs["compute_type"] = compute_type
        if self._settings.cpu_threads > 0:
            kwargs["cpu_threads"] = self._settings.cpu_threads
        if self._settings.num_workers > 1:
            kwargs["num_workers"] = self._settings.num_workers
        return WhisperModel(self._settings.model_name, **kwargs)

    @staticmethod
    def _materialize_segments(segments: object, on_segment=None) -> list[object]:
        if isinstance(segments, list):
            if on_segment is not None:
                for seg in segments:
                    txt = getattr(seg, "text", "").strip()
                    if txt:
                        on_segment({"start": round(float(getattr(seg, "start", 0)), 3), "end": round(float(getattr(seg, "end", 0)), 3), "text": txt})
            return segments
        result = []
        for segment in segments:
            result.append(segment)
            if on_segment is not None:
                txt = getattr(segment, "text", "").strip()
                if txt:
                    on_segment({"start": round(float(getattr(segment, "start", 0)), 3), "end": round(float(getattr(segment, "end", 0)), 3), "text": txt})
        return result

    @staticmethod
    def _looks_like_cuda_runtime_error(exc: Exception) -> bool:
        message = str(exc).lower()
        markers = ("libcublas", "libcudnn", "cuda", "cublas", "cudnn", "out of memory")
        return any(marker in message for marker in markers)

    @staticmethod
    def _looks_like_cuda_oom(exc: Exception) -> bool:
        message = str(exc).lower()
        markers = (
            "out of memory",
            "failed to allocate",
            "memory allocation",
            "cudnn_status_alloc_failed",
            "cuda_error_out_of_memory",
        )
        return any(marker in message for marker in markers)

    def _relax_cuda_runtime(self, reason: str) -> bool:
        with self._model_init_lock:
            if self._runtime_device == "cpu":
                return False

            if self._active_batch_size > 1:
                previous_batch_size = self._active_batch_size
                self._active_batch_size = max(1, self._active_batch_size // 2)
                self._batched_pipeline = None
                print(
                    "[WARN] Whisper reduced CUDA batch size "
                    f"from {previous_batch_size} to {self._active_batch_size} because: {reason}",
                    file=sys.stderr,
                )
                return True

            next_compute_type = "int8_float16"
            if self._active_compute_type != next_compute_type:
                try:
                    self._model = self._new_model(self._settings.model_device, next_compute_type)
                except Exception:
                    return False
                self._batched_pipeline = None
                self._active_compute_type = next_compute_type
                self._runtime_compute_type = next_compute_type
                print(
                    "[WARN] Whisper switched CUDA compute_type "
                    f"to {next_compute_type} because: {reason}",
                    file=sys.stderr,
                )
                return True

            return False

    def _switch_to_cpu_model(self, reason: str) -> None:
        with self._model_init_lock:
            if self._runtime_device == "cpu" and self._model is not None:
                return
            self._model = self._new_model("cpu", self._settings.cpu_fallback_compute_type)
            self._batched_pipeline = None
            self._runtime_device = "cpu"
            self._runtime_compute_type = self._settings.cpu_fallback_compute_type
            self._active_batch_size = 1
            self._active_compute_type = self._settings.cpu_fallback_compute_type
            print(
                "[WARN] Whisper switched to CPU fallback "
                f"(compute_type={self._settings.cpu_fallback_compute_type}) because: {reason}",
                file=sys.stderr,
            )

    def force_cpu_fallback(self, reason: str) -> None:
        self._switch_to_cpu_model(reason)
