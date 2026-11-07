import os
from pathlib import Path


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


class WhisperSettings:
    DEFAULT_INPUT_MEDIA_PATH = ""
    DEFAULT_INPUT_LANGUAGE = ""
    DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "Outputs" / "Audio"
    SUPPORTED_MEDIA_EXTENSIONS = frozenset(
        {
            ".wav",
            ".mp3",
            ".m4a",
            ".flac",
            ".ogg",
            ".opus",
            ".aac",
            ".webm",
            ".mp4",
            ".mkv",
            ".mov",
            ".avi",
        }
    )

    def __init__(
        self,
        input_media_path: str,
        input_language: str,
        output_dir: Path,
        model_name: str,
        model_device: str,
        model_compute_type: str,
        cpu_fallback_compute_type: str,
        beam_size: int,
        vad_filter: bool,
        batch_size: int,
        condition_on_previous_text: bool = False,
        vad_min_silence_ms: int = 500,
        cpu_threads: int = 0,
        num_workers: int = 1,
    ) -> None:
        self.input_media_path = input_media_path
        self.input_language = input_language
        self.output_dir = output_dir
        self.model_name = model_name
        self.model_device = model_device
        self.model_compute_type = model_compute_type
        self.cpu_fallback_compute_type = cpu_fallback_compute_type
        self.beam_size = max(1, int(beam_size))
        self.vad_filter = bool(vad_filter)
        self.batch_size = max(1, int(batch_size))
        # Disabling conditioning on previous text is the single biggest reliability
        # win across every model size: it stops the decoder from looping/repeating
        # and from propagating one bad segment into the rest of the transcript.
        self.condition_on_previous_text = bool(condition_on_previous_text)
        self.vad_min_silence_ms = max(0, int(vad_min_silence_ms))
        # 0 lets the backend pick (all logical cores); >0 pins the CPU thread count.
        self.cpu_threads = max(0, int(cpu_threads))
        self.num_workers = max(1, int(num_workers))

    @classmethod
    def from_environment(cls) -> "WhisperSettings":
        model_name = os.getenv("WHISPER_MODEL", "large-v3-turbo")
        model_device = os.getenv("WHISPER_DEVICE", "cuda").strip().lower()
        model_compute_type = os.getenv(
            "WHISPER_COMPUTE_TYPE",
            "int8" if model_device == "cpu" else "float16",
        )
        cpu_fallback_compute_type = os.getenv("WHISPER_CPU_COMPUTE_TYPE", "int8")
        beam_size = int(os.getenv("WHISPER_BEAM_SIZE", "5"))  # 1 lost words a Domain needs ("retrospectiva", #66)
        vad_filter = _env_bool("WHISPER_VAD_FILTER", True)
        batch_size_default = "1" if model_device == "cpu" else "8"
        batch_size = int(os.getenv("WHISPER_BATCH_SIZE", batch_size_default))
        condition_on_previous_text = _env_bool("WHISPER_CONDITION_ON_PREVIOUS_TEXT", False)
        vad_min_silence_ms = int(os.getenv("WHISPER_VAD_MIN_SILENCE_MS", "500"))
        cpu_threads = int(os.getenv("WHISPER_CPU_THREADS", "0"))
        num_workers = int(os.getenv("WHISPER_NUM_WORKERS", "1"))

        return cls(
            input_media_path=cls.DEFAULT_INPUT_MEDIA_PATH,
            input_language=cls.DEFAULT_INPUT_LANGUAGE,
            output_dir=cls.DEFAULT_OUTPUT_DIR,
            model_name=model_name,
            model_device=model_device,
            model_compute_type=model_compute_type,
            cpu_fallback_compute_type=cpu_fallback_compute_type,
            beam_size=max(1, beam_size),
            vad_filter=vad_filter,
            batch_size=max(1, batch_size),
            condition_on_previous_text=condition_on_previous_text,
            vad_min_silence_ms=vad_min_silence_ms,
            cpu_threads=cpu_threads,
            num_workers=num_workers,
        )
