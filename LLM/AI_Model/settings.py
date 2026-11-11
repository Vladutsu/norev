import os
from dataclasses import dataclass, replace


def _first_env_value(*names: str) -> str:
    for name in names:
        value = os.getenv(name)
        if value is None:
            continue
        stripped = value.strip()
        if stripped:
            return stripped
    return ""


@dataclass(frozen=True)
class DeepSeekSettings:
    provider: str
    ollama_url: str
    model: str
    temperature: float
    chunk_char_threshold: int
    min_chunk_char_threshold: int
    request_timeout_seconds: int
    max_retries: int
    retry_backoff_seconds: float
    response_max_tokens: int | None
    keep_alive: str = "5m"
    llamacpp_url: str = "http://127.0.0.1:8080"

    @classmethod
    def from_environment(cls) -> "DeepSeekSettings":
        provider = _first_env_value("LLM_PROVIDER", "DEEPSEEK_PROVIDER").lower() or "ollama"
        chunk_threshold = int(
            _first_env_value("LLM_CHUNK_CHARS", "DEEPSEEK_CHUNK_CHARS") or "4000"
        )
        min_chunk_threshold = int(
            _first_env_value("LLM_MIN_CHUNK_CHARS", "DEEPSEEK_MIN_CHUNK_CHARS") or "700"
        )
        request_timeout = int(
            _first_env_value("LLM_HTTP_TIMEOUT_SECONDS", "DEEPSEEK_HTTP_TIMEOUT_SECONDS")
            or "2100"
        )
        max_retries = int(_first_env_value("LLM_MAX_RETRIES", "DEEPSEEK_MAX_RETRIES") or "2")
        retry_backoff_seconds = float(
            _first_env_value("LLM_RETRY_BACKOFF_SECONDS", "DEEPSEEK_RETRY_BACKOFF_SECONDS")
            or "4"
        )
        max_tokens_raw = _first_env_value("LLM_MAX_TOKENS", "DEEPSEEK_MAX_TOKENS")
        response_max_tokens = int(max_tokens_raw) if max_tokens_raw else 0
        return cls(
            provider=provider,
            ollama_url=_first_env_value("OLLAMA_URL", "LLM_OLLAMA_URL")
            or "http://127.0.0.1:11434/api/generate",
            model=_first_env_value("LLM_MODEL", "OLLAMA_MODEL") or "deepseek-r1:1.5b",
            temperature=float(_first_env_value("LLM_TEMPERATURE", "DEEPSEEK_TEMPERATURE") or "0.2"),
            chunk_char_threshold=max(1, chunk_threshold),
            min_chunk_char_threshold=max(120, min_chunk_threshold),
            request_timeout_seconds=max(30, request_timeout),
            max_retries=max(0, max_retries),
            retry_backoff_seconds=max(0.0, retry_backoff_seconds),
            response_max_tokens=response_max_tokens if response_max_tokens > 0 else None,
            keep_alive=_first_env_value("LLM_KEEP_ALIVE", "DEEPSEEK_KEEP_ALIVE") or "5m",
            llamacpp_url=_first_env_value("LLAMACPP_URL", "LLM_LLAMACPP_URL")
            or "http://127.0.0.1:8080",
        )

    def with_overrides(
        self,
        provider: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        request_timeout_seconds: int | None = None,
        max_retries: int | None = None,
        retry_backoff_seconds: float | None = None,
        response_max_tokens: int | None = None,
        keep_alive: str | None = None,
    ) -> "DeepSeekSettings":
        updates: dict[str, object] = {}
        if provider and provider.strip():
            updates["provider"] = provider.strip().lower()
        if model and model.strip():
            updates["model"] = model.strip()
        if temperature is not None:
            updates["temperature"] = float(temperature)
        if request_timeout_seconds is not None:
            updates["request_timeout_seconds"] = max(1, int(request_timeout_seconds))
        if max_retries is not None:
            updates["max_retries"] = max(0, int(max_retries))
        if retry_backoff_seconds is not None:
            updates["retry_backoff_seconds"] = max(0.0, float(retry_backoff_seconds))
        if response_max_tokens is not None:
            updates["response_max_tokens"] = max(1, int(response_max_tokens))
        if keep_alive and keep_alive.strip():
            updates["keep_alive"] = keep_alive.strip()

        if not updates:
            return self
        return replace(self, **updates)


LLMSettings = DeepSeekSettings
