import json
import re
from concurrent.futures import ThreadPoolExecutor

from LLM.AI_Model.client import LLMClient, build_llm_client
from LLM.AI_Model.settings import DeepSeekSettings

TRANSLATE_PROMPT = """
Translate this part of a session transcript from {{SOURCE}} into English.

The transcript comes from automatic speech recognition and has no speaker labels.
- Translate the meaning faithfully, sentence by sentence. Do not summarise, skip, merge or add anything.
- Keep questions as questions, and keep every name, number, date, time and amount exactly.
- Fix obvious speech-recognition misspellings only when the intended word is unambiguous.

Return ONLY valid JSON: {"translation": "..."}

Text:
{{TEXT}}
""".strip()

TRANSLATION_SCHEMA = {
    "type": "object",
    "properties": {"translation": {"type": "string", "minLength": 1}},
    "required": ["translation"],
    "additionalProperties": False,
}

# Whisper language codes -> names the model understands
LANGUAGE_NAMES = {
    "ro": "Romanian", "en": "English", "de": "German", "fr": "French", "it": "Italian",
    "es": "Spanish", "pt": "Portuguese", "nl": "Dutch", "pl": "Polish", "hu": "Hungarian",
    "cs": "Czech", "ru": "Russian", "uk": "Ukrainian", "bg": "Bulgarian", "tr": "Turkish",
}

# The answer is about as long as the input, so keep each request well inside a 4096-token context
CHUNK_CHARS = 2000


def split_sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


def chunk_text(text: str, max_chars: int = CHUNK_CHARS) -> list[str]:
    chunks: list[str] = []
    current = ""
    for sentence in split_sentences(text):
        if current and len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current)
            current = sentence
        else:
            current = f"{current} {sentence}".strip()
    if current:
        chunks.append(current)
    return chunks


class TranscriptTranslator:
    def __init__(self, settings: DeepSeekSettings | None = None, client: LLMClient | None = None) -> None:
        self._settings = settings or DeepSeekSettings.from_environment()
        self._client = client or build_llm_client(self._settings)

    @property
    def llm_model(self) -> str:
        return self._settings.model

    def translate(self, text: str, *, source: str, workers: int = 1) -> tuple[str, int]:
        """Returns (English text, number of chunks). Chunks are independent, so up to `workers`
        run concurrently (llama-server --parallel batches them on the GPU)."""
        source_name = LANGUAGE_NAMES.get(source, source or "the source language")
        chunks = chunk_text(text)
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            translated = list(pool.map(lambda c: self._translate_chunk(c, source_name), chunks))
        return " ".join(translated), len(chunks)

    def _translate_chunk(self, chunk: str, source_name: str) -> str:
        prompt = TRANSLATE_PROMPT.replace("{{SOURCE}}", source_name).replace("{{TEXT}}", chunk)
        raw = self._client.generate_json(prompt, model=self._settings.model, json_schema=TRANSLATION_SCHEMA)
        payload = json.loads(raw)
        translation = payload.get("translation") if isinstance(payload, dict) else None
        if not isinstance(translation, str) or not translation.strip():
            raise RuntimeError("Translation response did not contain a 'translation' text.")
        return translation.strip()
