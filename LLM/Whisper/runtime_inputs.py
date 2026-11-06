import os
from collections.abc import Sequence
from dataclasses import dataclass

from LLM.Whisper.settings import WhisperSettings


@dataclass(frozen=True)
class RuntimeInputs:
    file_path: str
    language: str
    llm_provider: str
    llm_model: str
    list_llm_models: bool
    list_llm_models_provider: str


class RuntimeInputResolver:
    def __init__(self, settings: WhisperSettings) -> None:
        self._settings = settings

    def resolve(self, argv: Sequence[str]) -> RuntimeInputs:
        cli_file_path, cli_language, cli_llm_provider, cli_llm_model, list_llm_models = (
            self._parse_cli_args(argv)
        )

        env_file_path = os.getenv("WHISPER_INPUT_MEDIA_PATH", "").strip()
        env_language = os.getenv("WHISPER_INPUT_LANGUAGE", "").strip()
        env_llm_provider = (
            os.getenv("LLM_PROVIDER", "").strip()
            or os.getenv("DEEPSEEK_PROVIDER", "").strip()
        )
        env_llm_model = (
            os.getenv("LLM_MODEL", "").strip()
            or os.getenv("OLLAMA_MODEL", "").strip()
        )

        file_path = cli_file_path or env_file_path or self._settings.input_media_path
        language = cli_language or env_language or self._settings.input_language
        llm_provider = cli_llm_provider or env_llm_provider
        llm_model = cli_llm_model or env_llm_model
        list_provider = cli_llm_provider or llm_provider

        return RuntimeInputs(
            file_path=file_path,
            language=language.strip(),
            llm_provider=llm_provider.strip(),
            llm_model=llm_model.strip(),
            list_llm_models=list_llm_models,
            list_llm_models_provider=list_provider.strip(),
        )

    def _parse_cli_args(
        self,
        argv: Sequence[str],
    ) -> tuple[str, str, str, str, bool]:
        args = [arg.strip() for arg in argv[1:]]
        llm_provider = ""
        llm_model = ""
        list_llm_models = False
        positional: list[str] = []

        index = 0
        while index < len(args):
            arg = args[index]
            if arg == "--list-llm-models":
                list_llm_models = True
            elif arg.startswith("--llm-provider="):
                llm_provider = arg.split("=", 1)[1].strip()
            elif arg == "--llm-provider" and index + 1 < len(args):
                index += 1
                llm_provider = args[index].strip()
            elif arg.startswith("--llm-model="):
                llm_model = arg.split("=", 1)[1].strip()
            elif arg == "--llm-model" and index + 1 < len(args):
                index += 1
                llm_model = args[index].strip()
            else:
                positional.append(arg)
            index += 1

        cli_file_path = positional[0] if positional else ""
        cli_language = positional[1] if len(positional) > 1 else ""
        return (
            cli_file_path,
            cli_language,
            llm_provider,
            llm_model,
            list_llm_models,
        )
