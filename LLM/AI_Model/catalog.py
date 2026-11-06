import json
from urllib import request

from LLM.AI_Model.client import llamacpp_base_url
from LLM.AI_Model.settings import DeepSeekSettings


class ModelCatalogService:
    def __init__(self, settings: DeepSeekSettings | None = None) -> None:
        self._settings = settings or DeepSeekSettings.from_environment()

    def list_models(self, provider: str | None = None) -> list[str]:
        selected_provider = (provider or self._settings.provider).strip().lower()
        if selected_provider == "ollama":
            return self._list_ollama_models()
        if selected_provider in {"llamacpp", "llama.cpp", "llama-cpp"}:
            return self._list_llamacpp_models()

        raise RuntimeError(
            f"Unsupported LLM provider '{selected_provider}'. "
            "Currently supported providers: ollama, llamacpp."
        )

    def _list_llamacpp_models(self) -> list[str]:
        models_url = f"{llamacpp_base_url(self._settings.llamacpp_url)}/v1/models"
        req = request.Request(models_url, method="GET")
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")

        payload = json.loads(raw)
        models = payload.get("data")
        if not isinstance(models, list):
            return []

        names = [
            item["id"].strip()
            for item in models
            if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip()
        ]
        return sorted(set(names))

    def _list_ollama_models(self) -> list[str]:
        tags_url = self._ollama_tags_url(self._settings.ollama_url)
        req = request.Request(tags_url, method="GET")
        with request.urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8")

        payload = json.loads(raw)
        models = payload.get("models")
        if not isinstance(models, list):
            return []

        names: list[str] = []
        for item in models:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name.strip():
                names.append(name.strip())

        return sorted(set(names))

    @staticmethod
    def _ollama_tags_url(ollama_generate_url: str) -> str:
        url = ollama_generate_url.strip().rstrip("/")
        if "/api/" in url:
            base, _ = url.split("/api/", 1)
            return f"{base}/api/tags"
        return f"{url}/api/tags"
