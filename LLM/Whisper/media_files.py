from pathlib import Path

from LLM.Whisper.settings import WhisperSettings


class MediaFileService:
    def __init__(self, settings: WhisperSettings) -> None:
        self._settings = settings

    def is_supported_media_file(self, file_path: str) -> bool:
        ext = Path(file_path).suffix.lower()
        return ext in self._settings.SUPPORTED_MEDIA_EXTENSIONS

    def output_json_path_for_input(self, file_path: str) -> Path:
        stem = Path(file_path).stem.strip() or "output"
        safe_stem = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in stem)
        return self._settings.output_dir / f"{safe_stem}.json"
