import json
import os
import socket
import time
from typing import Protocol
from urllib import error, request

from LLM.AI_Model.settings import DeepSeekSettings


class LLMClient(Protocol):
    def generate_json(
        self, prompt: str, *, model: str | None = None, json_schema: dict | None = None
    ) -> str: ...


class OllamaTimeoutError(RuntimeError):
    pass


class OllamaLLMClient:
    _provider_label = "Ollama"

    def __init__(
        self,
        ollama_url: str,
        model: str,
        temperature: float,
        keep_alive: str,
        request_timeout_seconds: int = 600,
        max_retries: int = 2,
        retry_backoff_seconds: float = 4.0,
        response_max_tokens: int | None = None,
    ) -> None:
        self._ollama_url = ollama_url
        self._default_model = model
        self._temperature = temperature
        self._keep_alive = keep_alive
        self._request_timeout_seconds = max(1, int(request_timeout_seconds))
        self._max_retries = max(0, int(max_retries))
        self._retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self._response_max_tokens = (
            max(1, int(response_max_tokens)) if response_max_tokens is not None else None
        )

    def generate_json(
        self, prompt: str, *, model: str | None = None, json_schema: dict | None = None
    ) -> str:
        """json_schema, when given, constrains decoding to that schema (always JSON mode)."""
        selected_model = (model or self._default_model).strip()
        if not selected_model:
            raise RuntimeError("LLM model is required.")

        json_mode = os.getenv("LLM_FORCE_JSON_FORMAT", "0").strip().lower()
        body = self._build_body(
            prompt,
            selected_model,
            force_json=json_mode not in {"0", "false", "no", "off"},
            json_schema=json_schema,
        )
        label = self._provider_label

        attempts = self._max_retries + 1
        for attempt in range(attempts):
            req = request.Request(
                self._request_url(),
                data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with request.urlopen(req, timeout=self._request_timeout_seconds) as response:
                    raw = response.read().decode("utf-8")

                result = self._extract_text(json.loads(raw))
                if not isinstance(result, str) or not result.strip():
                    raise RuntimeError(f"{label} response did not include any generated text.")
                return self._normalize_json_text(result)

            except (TimeoutError, socket.timeout) as exc:
                if attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise OllamaTimeoutError(
                    f"{label} request timed out after "
                    f"{self._request_timeout_seconds}s. Increase LLM_HTTP_TIMEOUT_SECONDS "
                    "or reduce LLM_CHUNK_CHARS."
                ) from exc
            except error.HTTPError as exc:
                details = exc.read().decode("utf-8", errors="replace")
                if exc.code in {408, 409, 425, 429, 500, 502, 503, 504} and attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise RuntimeError(f"{label} HTTP {exc.code}: {details}") from exc
            except error.URLError as exc:
                reason_text = str(getattr(exc, "reason", "")).lower()
                if "timed out" in reason_text:
                    if attempt < self._max_retries:
                        self._sleep_before_retry(attempt)
                        continue
                    raise OllamaTimeoutError(
                        f"{label} request timed out after "
                        f"{self._request_timeout_seconds}s. Increase LLM_HTTP_TIMEOUT_SECONDS "
                        "or reduce LLM_CHUNK_CHARS."
                    ) from exc
                raise RuntimeError(self._unreachable_message()) from exc
            except (json.JSONDecodeError, RuntimeError) as exc:
                if attempt < self._max_retries:
                    self._sleep_before_retry(attempt)
                    continue
                raise

        raise RuntimeError(f"{label} request failed after retry attempts.")

    def _request_url(self) -> str:
        return self._ollama_url

    def _build_body(
        self, prompt: str, model: str, *, force_json: bool, json_schema: dict | None = None
    ) -> dict:
        options: dict[str, float | int] = {"temperature": self._temperature}
        if self._response_max_tokens is not None:
            options["num_predict"] = self._response_max_tokens

        body: dict = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "keep_alive": self._keep_alive,
            "options": options,
        }
        if json_schema is not None:
            body["format"] = json_schema
        elif force_json:
            body["format"] = "json"
        return body

    def _extract_text(self, payload: dict) -> str:
        return payload.get("response", "")

    def _unreachable_message(self) -> str:
        return "Could not reach Ollama. Ensure 'ollama serve' is running and URL is correct."

    def _sleep_before_retry(self, attempt_index: int) -> None:
        if self._retry_backoff_seconds <= 0:
            return
        time.sleep(self._retry_backoff_seconds * (2**attempt_index))

    def _normalize_json_text(self, raw: str) -> str:
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()

        try:
            parsed = json.loads(text)
            return json.dumps(parsed, ensure_ascii=False, indent=2)
        except json.JSONDecodeError:
            pass

        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            candidate = text[start : end + 1].strip()
            parsed = json.loads(candidate)
            return json.dumps(parsed, ensure_ascii=False, indent=2)

        raise RuntimeError("Model response did not contain valid JSON.")


class LlamaCppLLMClient(OllamaLLMClient):
    """Talks to llama.cpp's `llama-server` through its OpenAI-compatible API."""

    _provider_label = "llama.cpp"

    def __init__(
        self,
        llamacpp_url: str,
        model: str,
        temperature: float,
        request_timeout_seconds: int = 600,
        max_retries: int = 2,
        retry_backoff_seconds: float = 4.0,
        response_max_tokens: int | None = None,
    ) -> None:
        super().__init__(
            ollama_url=llamacpp_url,
            model=model,
            temperature=temperature,
            keep_alive="",
            request_timeout_seconds=request_timeout_seconds,
            max_retries=max_retries,
            retry_backoff_seconds=retry_backoff_seconds,
            response_max_tokens=response_max_tokens,
        )

    def _request_url(self) -> str:
        return f"{llamacpp_base_url(self._ollama_url)}/v1/chat/completions"

    def _build_body(
        self, prompt: str, model: str, *, force_json: bool, json_schema: dict | None = None
    ) -> dict:
        body: dict = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self._temperature,
            "stream": False,
        }
        if self._response_max_tokens is not None:
            body["max_tokens"] = self._response_max_tokens
        if json_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "response", "schema": json_schema},
            }
        elif force_json:
            body["response_format"] = {"type": "json_object"}
        return body

    def _extract_text(self, payload: dict) -> str:
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        return content if isinstance(content, str) else ""

    def _unreachable_message(self) -> str:
        return "Could not reach llama.cpp. Ensure 'llama-server' is running and LLAMACPP_URL is correct."


def llamacpp_base_url(url: str) -> str:
    base = url.strip().rstrip("/")
    for suffix in ("/v1/chat/completions", "/v1"):
        if base.endswith(suffix):
            return base[: -len(suffix)]
    return base


def build_llm_client(settings: DeepSeekSettings) -> LLMClient:
    provider = (settings.provider or "").lower()
    if provider == "ollama":
        return OllamaLLMClient(
            ollama_url=settings.ollama_url,
            model=settings.model,
            temperature=settings.temperature,
            keep_alive=settings.keep_alive,
            request_timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            retry_backoff_seconds=settings.retry_backoff_seconds,
            response_max_tokens=settings.response_max_tokens,
        )
    if provider in {"llamacpp", "llama.cpp", "llama-cpp"}:
        return LlamaCppLLMClient(
            llamacpp_url=settings.llamacpp_url,
            model=settings.model,
            temperature=settings.temperature,
            request_timeout_seconds=settings.request_timeout_seconds,
            max_retries=settings.max_retries,
            retry_backoff_seconds=settings.retry_backoff_seconds,
            response_max_tokens=settings.response_max_tokens,
        )

    raise RuntimeError(
        f"Unsupported LLM provider '{settings.provider}'. "
        "Currently supported providers: ollama, llamacpp."
    )


# Backward compatibility for existing imports.
OllamaDeepSeekClient = OllamaLLMClient
