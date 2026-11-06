import json
import os
import sys
from collections.abc import Sequence

from LLM.Whisper.media_files import MediaFileService
from LLM.Whisper.runtime_inputs import RuntimeInputResolver
from LLM.Whisper.settings import WhisperSettings
from LLM.Whisper.transcription_service import TranscriptionService


class WhisperCliApp:
    def __init__(
        self,
        settings: WhisperSettings,
        input_resolver: RuntimeInputResolver,
        media_service: MediaFileService,
        transcription_service: TranscriptionService,
    ) -> None:
        self._settings = settings
        self._input_resolver = input_resolver
        self._media_service = media_service
        self._transcription_service = transcription_service

    def run(self, argv: Sequence[str]) -> int:
        runtime_inputs = self._input_resolver.resolve(argv)

        if runtime_inputs.list_llm_models:
            try:
                models = self._transcription_service.list_llm_models(
                    provider=runtime_inputs.list_llm_models_provider or None,
                )
                print(
                    json.dumps(
                        {
                            "provider": runtime_inputs.list_llm_models_provider or None,
                            "models": models,
                        },
                        ensure_ascii=False,
                    )
                )
                return 0
            except Exception as exc:
                print(
                    json.dumps({"error": f"Could not list LLM models: {exc}"}, ensure_ascii=False),
                    file=sys.stderr,
                )
                return 1

        file_path = runtime_inputs.file_path
        language = runtime_inputs.language

        if not os.path.exists(file_path):
            print(
                json.dumps({"error": f"File not found: {file_path}"}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1

        if not self._media_service.is_supported_media_file(file_path):
            print(
                json.dumps(
                    {
                        "error": f"Unsupported file extension for: {file_path}",
                        "supported_extensions": sorted(
                            self._settings.SUPPORTED_MEDIA_EXTENSIONS
                        ),
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 1

        try:
            result = self._transcription_service.transcribe_media_json(
                file_path=file_path,
                language=language,
                llm_provider=runtime_inputs.llm_provider or None,
                llm_model=runtime_inputs.llm_model or None,
            )
            output_path = self._media_service.output_json_path_for_input(file_path)
            output_path.parent.mkdir(parents=True, exist_ok=True)
            with open(output_path, "w", encoding="utf-8") as handle:
                json.dump(result, handle, ensure_ascii=False, indent=2)
            print(str(output_path))
            return 0
        except Exception as exc:
            print(
                json.dumps({"error": f"Transcription failed: {exc}"}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 1
