import json
import os
import unittest
from datetime import date
from unittest.mock import patch

from norev.adapters.llm.excerpt_dates import corrected_due_date, day_said
from norev.adapters.llm.legacy_extractor import LegacyActionExtractor
from norev.core.models import Segment, Transcript
from norev.testing.fakes import FakeLlmClient

FRIDAY = date(2026, 11, 6)


class DaySaidTest(unittest.TestCase):
    def check(self, cases):
        for excerpt, expected in cases:
            with self.subTest(excerpt):
                self.assertEqual(day_said(excerpt, FRIDAY), date.fromisoformat(expected) if expected else None)

    def test_weekdays_are_the_next_one_after_the_session(self):
        self.check([
            ("I configure the automatic renewal by Thursday.", "2026-11-12"),
            ("I will write the incident report until Monday.", "2026-11-09"),
            ("Yes, I'll send it by Tuesday", "2026-11-10"),
            ("I'll send it for review by Friday", "2026-11-13"),  # said on a Friday: the next one
            ("a viewing on Saturday at eleven", "2026-11-07"),
        ])

    def test_relative_and_explicit_days(self):
        self.check([
            ("I'll call him tomorrow.", "2026-11-07"),
            ("I'll send the list tonight.", "2026-11-06"),
            ("Come back on Friday, the 13th of November, at ten.", "2026-11-13"),
            ("by Wednesday, November 11th", "2026-11-11"),
            ("on twenty November at eleven", None),  # spelled day: not parsed, left to the model
            ("until January 5th", "2027-01-05"),
        ])

    def test_no_single_day(self):
        self.check([
            ("I'll talk to the bank next week.", None),
            ("by the end of the month", None),
            ("recovery classes every Tuesday", None),
            ("on Monday or Tuesday", None),
            ("Friday, November 20th", None),  # two different days: ambiguous
            ("Please do it soon.", None),
        ])


class CorrectedDueDateTest(unittest.TestCase):
    def test_the_day_of_the_excerpt_wins_over_a_wrong_row(self):
        self.assertEqual(corrected_due_date("2026-11-10", "I'll do it by Thursday.", FRIDAY), "2026-11-12")

    def test_a_right_date_or_no_named_day_is_kept(self):
        self.assertEqual(corrected_due_date("2026-11-12", "by Thursday", FRIDAY), "2026-11-12")
        self.assertEqual(corrected_due_date("2026-11-20", "as soon as possible", FRIDAY), "2026-11-20")

    def test_no_date_is_never_invented(self):
        self.assertIsNone(corrected_due_date(None, "we meet on Thursday at the retrospective", FRIDAY))


class ExtractorAppliesTheCorrectionTest(unittest.TestCase):
    def test_extract_corrects_the_due_date(self):
        answer = json.dumps({
            "session_summary": "Ana renews the certificates.",
            "proposed_actions": [{
                "id": "A1", "title": "Configure renewal", "description": "", "owner": "Ana",
                "due_date": "2026-11-10", "priority": "high", "transcript_excerpt": "I configure the renewal by Thursday.",
            }],
            "decisions": [], "risks": [], "follow_up_questions": [],
        })
        env = {"LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT": "0", "LLM_SECOND_PASS": "0", "LLM_PROVIDER": "llamacpp"}
        with patch.dict(os.environ, env):
            result = LegacyActionExtractor(client=FakeLlmClient([answer])).extract(
                Transcript((Segment(0, 1, "I configure the renewal by Thursday.", "S2"),), language="en"), FRIDAY
            )
        self.assertEqual(result["proposed_actions"][0]["due_date"], "2026-11-12")


if __name__ == "__main__":
    unittest.main()
