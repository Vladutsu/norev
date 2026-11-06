import sys

from LLM.Whisper.cli_app import WhisperCliApp
from LLM.Whisper.cuda_runtime import CudaRuntimeLoader
from LLM.Whisper.media_files import MediaFileService
from LLM.Whisper.model_manager import WhisperModelManager
from LLM.Whisper.runtime_inputs import RuntimeInputResolver
from LLM.Whisper.settings import WhisperSettings
from LLM.Whisper.transcription_service import TranscriptionService


def main() -> int:
    settings = WhisperSettings.from_environment()
    input_resolver = RuntimeInputResolver(settings)
    media_service = MediaFileService(settings)
    cuda_loader = CudaRuntimeLoader()
    model_manager = WhisperModelManager(settings, cuda_loader)
    transcription_service = TranscriptionService(model_manager, settings)
    app = WhisperCliApp(
        settings=settings,
        input_resolver=input_resolver,
        media_service=media_service,
        transcription_service=transcription_service,
    )
    return app.run(sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
