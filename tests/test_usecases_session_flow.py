"""Session flow end to end with fakes (#19): Corporate, Medical and Others, no model or GPU."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from norev.adapters.diarization.attributed import SpeakerTranscriber
from norev.adapters.domains.loader import load_domain
from norev.adapters.llm.translator import TurnTranslator
from norev.adapters.store.encrypted import KEY_BYTES, EncryptedStore
from norev.core.models import (
    ActionStatus, ActionType, Domain, DomainField, Role, Segment, Session, SpeakerTurn, Transcript, Workflow,
    WorkflowStep,
)
from norev.testing.fakes import FakeActionExtractor, FakeLlmClient, FakeTranscriber, InMemorySessionStore
from norev.usecases.session_flow import run_session, select_domain

DOMAINS = Path(__file__).resolve().parents[1] / "domains"
CORPORATE = load_domain(DOMAINS / "corporate")
MEDICAL = load_domain(DOMAINS / "medical")
# shortcut: stand-in until #21 adds domains/others with the User's confirmed prompt
OTHERS = Domain(
    name="others", label="Others", model="others.gguf", context=4096,
    roles=(Role("user", "The User."),),
    action_types=(ActionType("follow_up", (DomainField("note", "string", False),), Workflow((WorkflowStep("save_result"),))),),
    prompt_template="Transcript:\n{{transcript}}\nActions:\n{{proposed_actions}}",
    answer_schema={"type": "object"},
)

AUDIO = "audio-1"
SPOKEN = Transcript(
    segments=(
        Segment(0.0, 2.0, "Trimit raportul vineri."),
        Segment(2.0, 3.5, "Bine, mulțumesc."),
    ),
    language="ro",
)
TURNS = [SpeakerTurn(0.0, 2.1, "speaker_0"), SpeakerTurn(2.1, 3.5, "speaker_1")]
TRANSLATION = {"turns": [{"id": "1", "text": "I will send the report on Friday."}, {"id": "2", "text": "Fine, thanks."}]}
GENERIC = {"session_summary": "", "proposed_actions": [
    {"title": "Send the report", "description": "Send the report on Friday.", "owner": "speaker_0",
     "due_date": None, "transcript_excerpt": "I will send the report on Friday."},
]}
ANSWERS = {
    "corporate": {"proposed_actions": [{"id": "s1-1", "action_type": "work_item", "fields": {"deliverable": "report"}}]},
    "medical": {"proposed_actions": [{"id": "s1-1", "action_type": "schedule_visit", "fields": {"reason": "Report"}}]},
    "others": {"proposed_actions": [{"id": "s1-1", "action_type": "follow_up", "fields": {"note": "report"}}]},
}


class FakeDiarizer:
    def __init__(self, turns) -> None:
        self.turns = turns

    def diarize(self, audio_ref: str):
        return list(self.turns)


class Flow:
    """One Session wired to fakes; `llm` answers the translation first, then the Domain stage."""

    def __init__(self, domain: Domain, llm_answers: list, spoken: Transcript = SPOKEN) -> None:
        self.domain = domain
        self.store = InMemorySessionStore()
        self.store.save_session(Session("s1", datetime(2026, 11, 6, 10, tzinfo=timezone.utc), domain.name, audio_ref=AUDIO))
        self.transcriber = SpeakerTranscriber(FakeTranscriber({AUDIO: spoken}), FakeDiarizer(TURNS))
        self.llm = FakeLlmClient([a if isinstance(a, str) else json.dumps(a) for a in llm_answers])
        self.extractor = FakeActionExtractor(GENERIC)

    def run(self, domain: Domain | None = None):
        return run_session(
            "s1", store=self.store, transcriber=self.transcriber, translator=TurnTranslator(self.llm),
            extractor=self.extractor, domain=domain or self.domain, llm=self.llm,
        )


class SelectDomainTest(unittest.TestCase):
    def test_corporate_is_the_default_and_medical_is_explicit(self):
        self.assertEqual(select_domain(), "corporate")
        self.assertEqual(select_domain("medical"), "medical")

    def test_others_is_recognized_but_needs_21(self):
        with self.assertRaisesRegex(ValueError, "#21"):
            select_domain("others")

    def test_unknown_domain_is_an_error_not_a_fallback(self):
        for name in ("Corporate", "", "finance"):
            with self.assertRaises(ValueError):
                select_domain(name)


class RunSessionTest(unittest.TestCase):
    def test_each_domain_end_to_end(self):
        for domain in (CORPORATE, MEDICAL, OTHERS):
            with self.subTest(domain=domain.name):
                flow = Flow(domain, [TRANSLATION, ANSWERS[domain.name]])
                result = flow.run()

                self.assertEqual([a.action_type for a in result.actions], [domain.action_types[0].name])
                self.assertFalse(result.partial)
                self.assertEqual(flow.store.load_actions("s1")[0].status, ActionStatus.PROPOSED)
                # Speakers and times reach extraction and the Domain stage; extraction sees English only
                english, _ = flow.extractor.calls[0]
                self.assertEqual([(s.start, s.end, s.speaker_id) for s in english.segments],
                                 [(0.0, 2.0, "speaker_0"), (2.0, 3.5, "speaker_1")])
                self.assertEqual(english.language, "en")
                domain_prompt = flow.llm.calls[-1][0]
                self.assertIn("[speaker_0] I will send the report on Friday.", domain_prompt)
                self.assertIn("[speaker_1] Fine, thanks.", domain_prompt)

    def test_both_transcripts_are_saved_in_the_store(self):
        flow = Flow(CORPORATE, [TRANSLATION, ANSWERS["corporate"]])
        flow.run()
        session = flow.store.get_session("s1")
        self.assertEqual(flow.store.load_transcript(session.transcript_ref).language, "en")
        spoken = flow.store.load_transcript(session.source_transcript_ref)
        self.assertEqual((spoken.language, spoken.segments[0].text), ("ro", "Trimit raportul vineri."))
        self.assertEqual(spoken.segments[0].speaker_id, "speaker_0")

    def test_english_session_is_not_translated(self):
        english = Transcript((Segment(0.0, 2.0, "I will send the report."),), language="en")
        flow = Flow(CORPORATE, [ANSWERS["corporate"]], spoken=english)
        flow.run()
        self.assertEqual(len(flow.llm.calls), 1)  # only the Domain stage
        self.assertIsNone(flow.store.get_session("s1").source_transcript_ref)

    def test_untranslated_turn_makes_the_result_partial(self):
        only_first = {"turns": [TRANSLATION["turns"][0]]}
        flow = Flow(CORPORATE, [only_first, {"turns": []}, ANSWERS["corporate"]])  # the retry also misses it
        result = flow.run()
        self.assertTrue(result.partial)
        self.assertEqual([(s.speaker_id, s.text) for s in result.untranslated], [("speaker_1", "Bine, mulțumesc.")])

    def test_other_domain_than_the_sessions_is_refused(self):
        flow = Flow(CORPORATE, [])
        with self.assertRaises(ValueError):
            flow.run(MEDICAL)
        self.assertIsNone(flow.store.get_session("s1").transcript_ref)
        self.assertEqual(flow.llm.calls, [])

    def test_failed_domain_stage_saves_nothing_and_executes_nothing(self):
        flow = Flow(CORPORATE, [TRANSLATION, "not json"])
        with self.assertRaises(ValueError):
            flow.run()
        self.assertEqual(flow.store.load_actions("s1"), [])

    def test_failed_rerun_keeps_the_previous_typed_actions(self):
        flow = Flow(CORPORATE, [TRANSLATION, ANSWERS["corporate"], "not json"])
        flow.run()
        with self.assertRaises(ValueError):
            flow.run()
        actions = flow.store.load_actions("s1")
        self.assertEqual([(a.action_type, a.fields, a.status) for a in actions],
                         [("work_item", {"deliverable": "report"}, ActionStatus.PROPOSED)])

    def test_run_again_reuses_the_transcript_and_regenerates(self):
        flow = Flow(CORPORATE, [TRANSLATION, ANSWERS["corporate"], ANSWERS["corporate"]])
        flow.run()
        flow.transcriber = None  # a second transcription would fail
        result = flow.run()
        self.assertEqual([a.action_type for a in result.actions], ["work_item"])
        self.assertEqual(len(flow.extractor.calls), 2)

    def test_decided_actions_are_never_overwritten(self):
        flow = Flow(CORPORATE, [TRANSLATION, ANSWERS["corporate"], ANSWERS["corporate"]])
        flow.run()
        snapshot = flow.store.load_actions_snapshot("s1")
        action = snapshot.actions[0]
        action.reject()
        flow.store.save_actions("s1", [action], expected_revision=snapshot.revision)

        with self.assertRaises(ValueError):
            flow.run()
        self.assertEqual([a.status for a in flow.store.load_actions("s1")], [ActionStatus.REJECTED])


class EncryptedStoreFlowTest(unittest.TestCase):
    def test_flow_runs_on_the_encrypted_store_with_no_plaintext_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "norev.db"
            flow = Flow(CORPORATE, [TRANSLATION, ANSWERS["corporate"]])
            flow.store = EncryptedStore(path, os.urandom(KEY_BYTES))
            flow.store.save_session(Session("s1", datetime(2026, 11, 6, 10, tzinfo=timezone.utc), "corporate", audio_ref=AUDIO))
            try:
                result = flow.run()
                self.assertEqual([a.action_type for a in result.actions], ["work_item"])
                self.assertIsNotNone(flow.store.get_session("s1").source_transcript_ref)
            finally:
                flow.store.close()
            self.assertEqual(os.listdir(folder), ["norev.db"])  # no intermediate files
            raw = path.read_bytes()
            for text in ("raportul", "report", "speaker_0"):
                self.assertNotIn(text.encode(), raw)


class SpeakerTranscriberTest(unittest.TestCase):
    def test_gap_gets_no_speaker_and_tuples_are_accepted(self):
        spoken = Transcript((Segment(0.0, 1.0, "a"), Segment(5.0, 6.0, "b")), language="ro")
        transcriber = SpeakerTranscriber(FakeTranscriber({AUDIO: spoken}), FakeDiarizer([(0.0, 1.5, "speaker_0")]))
        result = transcriber.transcribe(AUDIO)
        self.assertEqual([s.speaker_id for s in result.segments], ["speaker_0", None])
        self.assertEqual(result.language, "ro")


if __name__ == "__main__":
    unittest.main()
