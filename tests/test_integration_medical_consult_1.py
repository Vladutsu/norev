"""consult_1 end to end through the new core, with recorded and handwritten LLM answers (no model).

English Transcript per Speaker turn (transcript.en.json, from the recorded "translate" answer) ->
generic stage (LegacyActionExtractor replaying the recorded "extract" answer) -> Domain stage
(medical Domain, one handwritten answer).
"""
import json
import os
import re
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest.mock import patch

from norev.adapters.domains.loader import load_domain
from norev.adapters.llm.legacy_extractor import LegacyActionExtractor
from norev.core.models import ActionStatus, Segment, Session, Suggestion, Transcript
from norev.testing.fakes import FakeLlmClient, InMemorySessionStore
from norev.usecases.domain_stage import run_domain_stage
from norev.usecases.extract import extract_session_actions

REPO = Path(__file__).resolve().parents[1]
FIXTURE = REPO / "tests" / "fixtures" / "llm" / "consult_1"
MEDICAL = REPO / "domains" / "medical"
SESSION_ID = "consult_1"
SESSION_DATE = date(2026, 11, 6)


def _read_json(name: str):
    return json.loads((FIXTURE / name).read_text(encoding="utf-8"))


RECORDED_CALLS = _read_json("responses.json")["calls"]
EXTRACT_CALLS = [c for c in RECORDED_CALLS if c["stage"] == "extract"]
EXPECTED_GENERIC = _read_json("proposed_actions.json")["proposed_actions"]
ENGLISH = _read_json("transcript.en.json")

# Generic ids after re-numbering by extract_session_actions (order of proposed_actions.json):
MEASURE_BP, BLOOD_TESTS, ECG, MEDICATION, FOLLOW_UP = (f"{SESSION_ID}-{n}" for n in range(1, 6))

SUGGESTION = {
    "text": "The patient reports evening palpitations after coffee; check whether they need follow-up.",
    "transcript_excerpt": "I also have palpitations in the evening, especially after I drink coffee.",
}
# Handwritten Domain answer, as a model following domains/medical/prompts/domain_stage.md would give
# it: "by Wednesday" is a deadline, not a visit date, so what was not said stays null.
DOMAIN_ANSWER = {
    "proposed_actions": [
        {
            "id": BLOOD_TESTS,
            "action_type": "schedule_visit",
            "fields": {
                "reason": "Blood tests: cholesterol, blood sugar, creatinine and ionogram",
                "visit_date": None,
                "visit_time": None,
                "location": "the lab next to the polyclinic",
            },
        },
        {
            "id": MEDICATION,
            "action_type": "medication_reminder",
            "fields": {
                "medication": "amlodipine",
                "dose": "five milligrams, one tablet",
                "frequency": "once a day, in the morning",
                "times": None,
                "start_date": None,
                "end_date": None,
            },
        },
    ],
    "suggestions": [SUGGESTION],
}


class MedicalConsult1IntegrationTest(unittest.TestCase):
    def setUp(self):
        # Pin the env vars the legacy extractor reads (as tests/test_adapters_legacy_extractor.py),
        # with the chunk size of the recording: the translation (~2.1k chars) is one chunk.
        env = patch.dict(os.environ, {
            "LLM_INCLUDE_PREVIOUS_JSON_IN_PROMPT": "0",
            "LLM_SECOND_PASS": "0",
            "LLM_CHUNK_CHARS": "4000",
            "LLM_PROVIDER": "llamacpp",
        }, clear=False)
        env.start()
        self.addCleanup(env.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workdir = tmp.name
        cwd = os.getcwd()
        os.chdir(self.workdir)
        self.addCleanup(os.chdir, cwd)

        self.transcript = Transcript(
            segments=tuple(Segment(s["start"], s["end"], s["text"], s["speaker_id"]) for s in ENGLISH["segments"]),
            language=ENGLISH["language"],
        )
        self.store = InMemorySessionStore()
        session = Session(id=SESSION_ID, started_at=datetime(2026, 11, 6, 9, 0), domain="medical")
        session.transcript_ref = self.store.save_transcript(SESSION_ID, self.transcript)
        self.store.save_session(session)

        self.extract_llm = FakeLlmClient([c["response"] for c in EXTRACT_CALLS])
        self.generic = extract_session_actions(
            SESSION_ID, self.store, LegacyActionExtractor(client=self.extract_llm)
        )

        self.domain = load_domain(MEDICAL)
        self.domain_llm = FakeLlmClient([json.dumps(DOMAIN_ANSWER)])
        self.typed, self.suggestions = run_domain_stage(
            SESSION_ID, self.store, self.domain, self.domain_llm, self.transcript
        )

    def tearDown(self):
        self.assertEqual(os.listdir(self.workdir), [], "the pipeline wrote to disk")

    # Generic stage

    def test_generic_stage_replays_the_recorded_extraction(self):
        self.assertEqual(len(self.extract_llm.calls), len(EXTRACT_CALLS), "chunking differs from the recording")
        self.assertEqual([c[0] for c in self.extract_llm.calls], [c["prompt"] for c in EXTRACT_CALLS])

    def test_generic_actions_match_the_fixture(self):
        self.assertEqual(
            [(a.title, a.due_date) for a in self.generic],
            [(e["title"], date.fromisoformat(e["due_date"]) if e["due_date"] else None) for e in EXPECTED_GENERIC],
        )
        self.assertEqual([a.id for a in self.generic], [f"{SESSION_ID}-{n}" for n in range(1, 6)])

    # Domain stage: what the model is asked

    def test_domain_prompt_is_fully_filled(self):
        [(prompt, _model, _schema)] = self.domain_llm.calls
        self.assertNotIn("{{", prompt)
        self.assertIn("2026-11-06", prompt)
        for role in self.domain.roles:
            self.assertIn(role.name, prompt)
        for action_type in self.domain.action_types:
            self.assertIn(action_type.name, prompt)
        for action in self.generic:
            self.assertIn(action.id, prompt)
        self.assertIn("[S2] Can I do them at the lab next to the polyclinic?", prompt)

    def test_domain_prompt_shows_who_said_each_turn(self):
        [(prompt, _model, _schema)] = self.domain_llm.calls
        self.assertNotIn("[?]", prompt)
        lines = [line for line in prompt.splitlines() if re.match(r"\[S\d\] ", line)]
        self.assertEqual(lines, [f"[{s.speaker_id}] {s.text}" for s in self.transcript.segments])
        self.assertIn("[S1] Yes. I'll prescribe you amlodipine five milligrams", prompt)

    def test_domain_stage_uses_the_medical_model_and_schema(self):
        [(_prompt, model, schema)] = self.domain_llm.calls
        self.assertEqual(model, "medgemma-4b-it-Q4_K_M.gguf")
        self.assertEqual(model, self.domain.model)
        self.assertEqual(schema, json.loads((MEDICAL / "prompts" / "domain_stage.schema.json").read_text("utf-8")))

    # Domain stage: what is saved

    def test_saved_actions_are_exactly_the_typed_ones(self):
        saved = {a.id: a for a in self.store.load_actions(SESSION_ID)}
        self.assertEqual(list(saved), [BLOOD_TESTS, MEDICATION])
        self.assertEqual(self.store.load_actions(SESSION_ID), self.typed)

        visit, pill = saved[BLOOD_TESTS], saved[MEDICATION]
        self.assertEqual(visit.action_type, "schedule_visit")
        self.assertEqual(visit.fields, {
            "reason": "Blood tests: cholesterol, blood sugar, creatinine and ionogram",
            "visit_date": None,
            "visit_time": None,
            "location": "the lab next to the polyclinic",
        })
        self.assertEqual(pill.action_type, "medication_reminder")
        self.assertEqual(pill.fields, {
            "medication": "amlodipine",
            "dose": "five milligrams, one tablet",
            "frequency": "once a day, in the morning",
            "times": None,
            "start_date": None,
            "end_date": None,
        })

    def test_typed_actions_have_every_field_of_their_type_and_stay_proposed(self):
        generic = {a.id: a for a in self.generic}
        for action in self.store.load_actions(SESSION_ID):
            with self.subTest(action.id):
                action_type = self.domain.action_type(action.action_type)
                self.assertEqual(list(action.fields), [f.name for f in action_type.fields])
                self.assertIs(action.status, ActionStatus.PROPOSED)
                self.assertIsNone(action.approved_payload)
                # generic fields come through unchanged
                self.assertEqual(action.title, generic[action.id].title)
                self.assertEqual(action.due_date, generic[action.id].due_date)
                self.assertEqual(action.transcript_excerpt, generic[action.id].transcript_excerpt)

    def test_actions_left_out_of_the_domain_answer_are_dropped(self):
        saved_ids = [a.id for a in self.store.load_actions(SESSION_ID)]
        for dropped in (MEASURE_BP, ECG, FOLLOW_UP):
            self.assertNotIn(dropped, saved_ids)

    def test_suggestion_is_saved(self):
        expected = [Suggestion(SUGGESTION["text"], SUGGESTION["transcript_excerpt"])]
        self.assertEqual(self.suggestions, expected)
        self.assertEqual(self.store.load_suggestions(SESSION_ID), expected)

    # The handwritten answer itself

    def test_handwritten_answer_fits_the_schema_structure(self):
        """Subset check without jsonschema: per-type branch, exact field set, date/time patterns."""
        schema = self.domain.answer_schema
        branches = {
            b["properties"]["action_type"]["const"]: b["properties"]["fields"]
            for b in schema["properties"]["proposed_actions"]["items"]["anyOf"]
        }
        self.assertEqual(set(DOMAIN_ANSWER), set(schema["required"]))
        for item in DOMAIN_ANSWER["proposed_actions"]:
            with self.subTest(item["id"]):
                fields_schema = branches[item["action_type"]]
                self.assertEqual(set(item), {"id", "action_type", "fields"})
                self.assertEqual(set(item["fields"]), set(fields_schema["required"]))
                for name, value in item["fields"].items():
                    if value is None:
                        continue
                    self.assertIsInstance(value, str)
                    spec = fields_schema["properties"][name]
                    pattern = next((s.get("pattern") for s in spec.get("anyOf", []) if "pattern" in s), None)
                    if pattern:
                        self.assertRegex(value, re.compile(pattern))
        for suggestion in DOMAIN_ANSWER["suggestions"]:
            self.assertEqual(set(suggestion), {"text", "transcript_excerpt"})

    def test_handwritten_answer_validates_with_jsonschema(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema is not installed")
        jsonschema.validate(DOMAIN_ANSWER, self.domain.answer_schema)


if __name__ == "__main__":
    unittest.main()
