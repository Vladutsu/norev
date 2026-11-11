"""ActionExtractor port (ADR 0005) backed by the tuned legacy extractor in LLM/AI_Model."""
import json
from datetime import date
from typing import Any

from LLM.AI_Model.extractor import RealtimeActionExtractor
from LLM.AI_Model.prompt import DeepSeekPromptBuilder
from LLM.AI_Model.settings import DeepSeekSettings
from norev.adapters.llm.excerpt_dates import corrected_due_date
from norev.adapters.llm.owner_check import _checked_action_owner
from norev.core.models import Transcript
from norev.core.ports import LlmClient


class LegacyActionExtractor:
    """Wraps RealtimeActionExtractor.extract_batch; writes nothing to disk (ADR 0004).

    The pipeline translates the Transcript to English first and extracts from the translation
    (tests/fixtures/llm: translate, then extract), so pass the English translation; the Transcript
    excerpts then come out in English.
    Two env vars are read at call time, not from settings: LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT and
    LLM_SECOND_PASS; the tuned values are 0 and 0, as ./norev.sh sets them (the code default "1"
    runs chunks one at a time and ignores `workers`).
    """

    def __init__(
        self,
        client: LlmClient,
        settings: DeepSeekSettings | None = None,
        workers: int = 1,
    ) -> None:
        self.client = client  # injected: only the Central Processor holds a real network client
        self.settings = settings or DeepSeekSettings.from_environment()
        self.workers = workers

    def extract(self, transcript: Transcript, session_date: date) -> dict[str, Any]:
        extractor = RealtimeActionExtractor(
            output_path=None,
            settings=self.settings,
            client=self.client,
            prompt_builder=DeepSeekPromptBuilder(session_date=session_date),
        )
        raw = extractor.extract_batch([s.text for s in transcript.segments], workers=self.workers)
        result = json.loads(raw) if raw.strip() else {}
        # The model's answer is checked against its own excerpt and the Transcript's Speakers:
        # the day the excerpt names wins over a wrong calendar row (#41), and "I'll do it" is never
        # given to another Speaker (#40)
        for action in result.get("proposed_actions") or []:
            if isinstance(action, dict):
                excerpt = action.get("transcript_excerpt")
                action["due_date"] = corrected_due_date(action.get("due_date"), excerpt, session_date)
                action["owner"] = _checked_action_owner(action, transcript.segments)
        return result
