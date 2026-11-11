import json
import os
import tempfile
import unittest
from datetime import date
from unittest.mock import patch

from LLM.AI_Model.prompt import PROPOSED_ACTION_SCHEMA, SUMMARY_SCHEMA
from norev.adapters.llm.legacy_extractor import LegacyActionExtractor
from norev.core.models import Segment, Transcript
from norev.core.ports import ActionExtractor
from norev.testing.fakes import FakeLlmClient

SESSION_DATE = date(2026, 11, 6)  # a Friday; "next Monday" is 2026-11-09

VISIT_TEXT = "Doctor: Come back next Monday so we can check your blood pressure again."
PILLS_TEXT = "Doctor: Take one tablet of amlodipine 5 mg every morning after breakfast."

# Recorded responses, valid against PROPOSED_ACTION_SCHEMA
VISIT_RESPONSE = json.dumps({
    "session_summary": "The doctor asked the patient to return next Monday for a blood pressure check.",
    "proposed_actions": [{
        "id": "A1",
        "title": "Schedule follow-up visit for blood pressure check",
        "description": "The patient returns on Monday so the doctor can re-check blood pressure.",
        "owner": None,
        "due_date": "2026-11-09",
        "priority": "high",
        "transcript_excerpt": "Come back next Monday so we can check your blood pressure",
    }],
    "decisions": [],
    "risks": [],
    "follow_up_questions": [],
})
PILLS_RESPONSE = json.dumps({
    "session_summary": "The doctor prescribed amlodipine 5 mg once a day in the morning.",
    "proposed_actions": [{
        "id": "A1",
        "title": "Remind patient to take amlodipine every morning",
        "description": "One 5 mg amlodipine tablet each morning after breakfast.",
        "owner": None,
        "due_date": None,
        "priority": "medium",
        "transcript_excerpt": "Take one tablet of amlodipine 5 mg every morning",
    }],
    "decisions": ["Start amlodipine 5 mg daily"],
    "risks": [],
    "follow_up_questions": [],
})
SUMMARY_RESPONSE = json.dumps({
    "session_summary": "Amlodipine 5 mg daily was prescribed and a follow-up visit was set for Monday.",
})


def _transcript(*texts: str) -> Transcript:
    return Transcript(segments=tuple(Segment(float(i), float(i + 1), t) for i, t in enumerate(texts)), language="en")


class LegacyActionExtractorTest(unittest.TestCase):
    def setUp(self):
        # Pin the env vars the legacy extractor reads, so the test does not depend on the shell
        env = patch.dict(os.environ, {
            "LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT": "0",  # as norev.sh sets it
            "LLM_SECOND_PASS": "0",
            "LLM_CHUNK_CHARS": "4000",
            "LLM_PROVIDER": "llamacpp",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        # Run in an empty directory: nothing may be written to disk (ADR 0004)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workdir = tmp.name
        cwd = os.getcwd()
        os.chdir(self.workdir)
        self.addCleanup(os.chdir, cwd)

    def tearDown(self):
        self.assertEqual(os.listdir(self.workdir), [], "the extractor wrote to disk")

    def test_is_an_action_extractor(self):
        self.assertIsInstance(LegacyActionExtractor(client=FakeLlmClient([])), ActionExtractor)

    def test_returns_recorded_actions_and_prompts_with_session_date(self):
        llm = FakeLlmClient([VISIT_RESPONSE])
        result = LegacyActionExtractor(client=llm).extract(_transcript(VISIT_TEXT), SESSION_DATE)

        [action] = result["proposed_actions"]
        self.assertEqual(action["title"], "Schedule follow-up visit for blood pressure check")
        self.assertEqual(action["due_date"], "2026-11-09")
        self.assertIn("blood pressure", result["session_summary"])

        [(prompt, _model, schema)] = llm.calls
        self.assertIn("The session took place today, 2026-11-06 (Friday).", prompt)
        self.assertIn(VISIT_TEXT, prompt)
        self.assertIs(schema, PROPOSED_ACTION_SCHEMA)

    def test_two_chunks_are_merged(self):
        with patch.dict(os.environ, {"LLM_CHUNK_CHARS": "40"}):  # each segment is longer: one chunk each
            # 2 chunks -> 2 extraction calls, then one call that rewrites the summary from both parts
            llm = FakeLlmClient([VISIT_RESPONSE, PILLS_RESPONSE, SUMMARY_RESPONSE])
            result = LegacyActionExtractor(client=llm).extract(_transcript(VISIT_TEXT, PILLS_TEXT), SESSION_DATE)

        titles = [a["title"] for a in result["proposed_actions"]]
        self.assertEqual(titles, [
            "Schedule follow-up visit for blood pressure check",
            "Remind patient to take amlodipine every morning",
        ])
        self.assertEqual([a["id"] for a in result["proposed_actions"]], ["A1", "A2"])
        self.assertEqual(result["decisions"], ["Start amlodipine 5 mg daily"])
        self.assertEqual(result["session_summary"], json.loads(SUMMARY_RESPONSE)["session_summary"])

        schemas = [schema for _prompt, _model, schema in llm.calls]
        self.assertEqual(schemas, [PROPOSED_ACTION_SCHEMA, PROPOSED_ACTION_SCHEMA, SUMMARY_SCHEMA])
        self.assertIn(VISIT_TEXT, llm.calls[0][0])
        self.assertIn(PILLS_TEXT, llm.calls[1][0])
        self.assertNotIn(VISIT_TEXT, llm.calls[1][0])

    def test_empty_transcript_returns_empty_dict(self):
        self.assertEqual(LegacyActionExtractor(client=FakeLlmClient([])).extract(_transcript(), SESSION_DATE), {})


if __name__ == "__main__":
    unittest.main()
