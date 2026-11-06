"""Regression tests for the legacy LLM package (used by ./norev.sh and LLM/pipeline.py).

No network, no model: the extractor gets a FakeLlmClient, and every env var that changes
behaviour is pinned with patch.dict.
"""
import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from LLM.AI_Model.extractor import RealtimeActionExtractor
from LLM.AI_Model.prompt import DeepSeekPromptBuilder
from norev.testing.fakes import FakeLlmClient

RESPONSE = json.dumps(
    {
        "session_summary": "The team agreed on the release plan.",
        "proposed_actions": [
            {
                "id": "A1",
                "title": "Send the release notes draft",
                "description": "Elena sends the draft to the team.",
                "owner": "Elena",
                "due_date": "2026-11-09",
                "priority": "high",
                "transcript_excerpt": "Elena, please send the draft by Monday.",
            }
        ],
        "decisions": ["Release on Friday"],
        "risks": [],
        "follow_up_questions": [],
    }
)
SEGMENTS = ["Elena, please send the draft by Monday.", "Sure, I will."]


def _date_line(day: date) -> str:
    return f"The session took place today, {day.isoformat()} ({day.strftime('%A')})."


def _tomorrow_entry(day: date) -> str:
    tomorrow = day + timedelta(days=1)
    return f"{tomorrow.strftime('%a')} {tomorrow.isoformat()} (tomorrow)"


class PromptSessionDateTest(unittest.TestCase):
    def _prompt(self, **kwargs) -> str:
        return DeepSeekPromptBuilder(**kwargs).build("", "Elena sends the draft.")

    def test_constructor_session_date_wins_over_env(self):
        with patch.dict(os.environ, {"LLM_SESSION_DATE": "2025-01-01"}):
            prompt = self._prompt(session_date=date(2026, 11, 13))

        self.assertIn(_date_line(date(2026, 11, 13)), prompt)
        self.assertIn(_tomorrow_entry(date(2026, 11, 13)), prompt)
        self.assertNotIn("2025-01-01", prompt)

    def test_env_session_date_is_used_without_constructor_argument(self):
        with patch.dict(os.environ, {"LLM_SESSION_DATE": "2025-12-31"}):
            prompt = self._prompt()

        self.assertIn(_date_line(date(2025, 12, 31)), prompt)
        self.assertIn("Thu 2026-01-01 (tomorrow)", prompt)

    def test_today_is_used_without_constructor_argument_or_env(self):
        with patch.dict(os.environ, {"LLM_SESSION_DATE": ""}):
            before = date.today()
            prompt = self._prompt()
            after = date.today()

        # before/after: the test stays correct if it runs across midnight
        today = before if _date_line(before) in prompt else after
        self.assertIn(_date_line(today), prompt)
        self.assertIn(_tomorrow_entry(today), prompt)


@patch.dict(
    os.environ,
    {
        "LLM_PROVIDER": "ollama",  # never reached: the fake client is injected
        "LLM_CHUNK_CHARS": "4000",  # SEGMENTS fit in one chunk -> one model call, no summary pass
        "LLM_SECOND_PASS": "0",
        "LLM_SESSION_DATE": "2026-11-06",
    },
)
class ExtractBatchPersistenceTest(unittest.TestCase):
    # "1" is the code default (sequential, persists after every chunk);
    # "0" is what ./norev.sh sets (parallel, persists once at the end).
    INCLUDE_PREVIOUS = ("1", "0")

    def _extract(self, output_path: Path | None) -> str:
        llm = FakeLlmClient([RESPONSE])
        extractor = RealtimeActionExtractor(output_path=output_path, client=llm)
        result = extractor.extract_batch(SEGMENTS)
        self.assertEqual(len(llm.calls), 1)
        return result

    def test_output_path_receives_the_merged_json(self):
        for include_previous in self.INCLUDE_PREVIOUS:
            with self.subTest(LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT=include_previous), \
                    patch.dict(os.environ, {"LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT": include_previous}), \
                    tempfile.TemporaryDirectory() as tmp:
                output_path = Path(tmp) / "nested" / "session.actions.json"

                result = self._extract(output_path)

                written = output_path.read_text(encoding="utf-8")
                self.assertEqual(written, result.strip() + "\n")
                actions = json.loads(written)["proposed_actions"]
                self.assertEqual(len(actions), 1)
                self.assertEqual(actions[0]["title"], "Send the release notes draft")
                self.assertEqual(actions[0]["owner"], "Elena")
                self.assertEqual(actions[0]["due_date"], "2026-11-09")

    def test_no_output_path_writes_nothing_and_returns_the_json(self):
        for include_previous in self.INCLUDE_PREVIOUS:
            with self.subTest(LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT=include_previous), \
                    patch.dict(os.environ, {"LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT": include_previous}), \
                    tempfile.TemporaryDirectory() as tmp:
                cwd = os.getcwd()
                os.chdir(tmp)  # a relative write would land here
                try:
                    result = self._extract(None)
                finally:
                    os.chdir(cwd)

                self.assertEqual(os.listdir(tmp), [])
                actions = json.loads(result)["proposed_actions"]
                self.assertEqual([a["title"] for a in actions], ["Send the release notes draft"])


if __name__ == "__main__":
    unittest.main()
