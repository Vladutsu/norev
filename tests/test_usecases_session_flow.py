"""Session flow end to end with fakes (#19): Corporate, Medical and Others, no model or GPU."""
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from norev.adapters.domains.loader import load_domain
from norev.adapters.llm.translator import TurnTranslator
from norev.adapters.store.encrypted import KEY_BYTES, EncryptedStore
from norev.core.models import (
    ActionStatus, Domain, OthersPrompt, Segment, Session, Speaker, SpeakerTurn, Transcript,
)
from norev.testing.fakes import FakeActionExtractor, FakeDiarizer, FakeLlmClient, FakeTranscriber, InMemorySessionStore
from norev.usecases.others_prompt import confirm
from norev.usecases.session_flow import run_session, select_domain
from norev.usecases.speaker_roles import set_speaker_role
from norev.usecases.transcribe import transcribe_session
from norev.usecases.transcript_correction import correct_transcript

DOMAINS = Path(__file__).resolve().parents[1] / "domains"
CORPORATE = load_domain(DOMAINS / "corporate")
MEDICAL = load_domain(DOMAINS / "medical")
OTHERS = load_domain(DOMAINS / "others")
OTHERS_PROMPT = confirm(OTHERS, "Weekly call with a contractor about a kitchen renovation.",
                        "- Keep what the contractor or the User agreed to do, with dates.")

AUDIO = "audio-1"
SPOKEN = Transcript(
    segments=(
        Segment(0.0, 2.0, "Trimit raportul vineri."),
        Segment(2.0, 3.5, "Bine, mulțumesc."),
    ),
    language="ro",
)
# Diarizer labels; attach_speakers (#24) renames them S1, S2 in order of first appearance
TURNS = [SpeakerTurn(0.0, 2.1, "spk_7"), SpeakerTurn(2.1, 3.5, "spk_3")]
TRANSLATION = {"turns": [{"id": "1", "text": "I will send the report on Friday."}, {"id": "2", "text": "Fine, thanks."}]}
# Roles call (#60) before the Domain stage: both unknown, so owners stay empty
ROLES = {"speakers": [{"id": "S1", "role": None}, {"id": "S2", "role": None}]}
GENERIC = {"session_summary": "", "proposed_actions": [
    {"title": "Send the report", "description": "Send the report on Friday.", "owner": None,
     "due_date": None, "transcript_excerpt": "I will send the report on Friday."},
]}
ANSWERS = {
    "corporate": {"proposed_actions": [{"id": "s1-1", "action_type": "work_item", "fields": {"deliverable": "report"}}]},
    "medical": {"proposed_actions": [{"id": "s1-1", "action_type": "schedule_visit", "fields": {"reason": "Report"}}]},
    "others": {"proposed_actions": [{"id": "s1-1", "action_type": "action_item", "fields": {"deliverable": "report"}}]},
}


class Flow:
    """One Session transcribed with Speakers by #24's use case; `llm` answers the translation
    first, then the Domain stage."""

    def __init__(self, domain: Domain, llm_answers: list, spoken: Transcript = SPOKEN, store=None,
                 others_prompt: OthersPrompt | None = OTHERS_PROMPT) -> None:
        self.domain = domain
        self.store = store or InMemorySessionStore()
        prompt = others_prompt if domain.name == "others" else None  # confirmed by the User before recording
        self.store.save_session(Session("s1", datetime(2026, 11, 6, 10, tzinfo=timezone.utc), domain.name,
                                        audio_ref=AUDIO, others_prompt=prompt))
        transcribe_session("s1", self.store, FakeTranscriber({AUDIO: spoken}), FakeDiarizer({AUDIO: TURNS}))
        self.llm = FakeLlmClient([a if isinstance(a, str) else json.dumps(a) for a in llm_answers])
        self.extractor = FakeActionExtractor(GENERIC)

    def run(self, domain: Domain | None = None):
        return run_session(
            "s1", store=self.store, translator=TurnTranslator(self.llm),
            extractor=self.extractor, domain=domain or self.domain, llm=self.llm,
        )


class SelectDomainTest(unittest.TestCase):
    def test_corporate_is_the_default_and_medical_is_explicit(self):
        self.assertEqual(select_domain(), "corporate")
        self.assertEqual(select_domain("medical"), "medical")

    def test_others_is_explicit(self):
        self.assertEqual(select_domain("others"), "others")

    def test_unknown_domain_is_an_error_not_a_fallback(self):
        for name in ("Corporate", "", "finance"):
            with self.assertRaises(ValueError):
                select_domain(name)


class RunSessionTest(unittest.TestCase):
    def test_each_domain_end_to_end(self):
        for domain in (CORPORATE, MEDICAL, OTHERS):
            with self.subTest(domain=domain.name):
                flow = Flow(domain, [TRANSLATION, ROLES, ANSWERS[domain.name]])
                result = flow.run()

                self.assertEqual([a.action_type for a in result.actions], [domain.action_types[0].name])
                self.assertFalse(result.partial)
                self.assertEqual(flow.store.load_actions("s1")[0].status, ActionStatus.PROPOSED)
                # Speakers and times reach extraction and the Domain stage; extraction sees English only
                english, _ = flow.extractor.calls[0]
                self.assertEqual([(s.start, s.end, s.speaker_id) for s in english.segments],
                                 [(0.0, 2.0, "S1"), (2.0, 3.5, "S2")])
                self.assertEqual(english.language, "en")
                domain_prompt = flow.llm.calls[-1][0]
                self.assertIn("[S1] I will send the report on Friday.", domain_prompt)
                self.assertIn("[S2] Fine, thanks.", domain_prompt)

    def test_both_transcripts_are_saved_in_the_store(self):
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"]])
        flow.run()
        session = flow.store.get_session("s1")
        self.assertEqual(flow.store.load_transcript(session.transcript_ref).language, "en")
        spoken = flow.store.load_transcript(session.source_transcript_ref)
        self.assertEqual((spoken.language, spoken.segments[0].text), ("ro", "Trimit raportul vineri."))
        self.assertEqual(spoken.segments[0].speaker_id, "S1")

    def test_english_session_is_not_translated(self):
        english = Transcript((Segment(0.0, 2.0, "I will send the report."),), language="en")
        flow = Flow(CORPORATE, [ROLES, ANSWERS["corporate"]], spoken=english)
        flow.run()
        self.assertEqual(len(flow.llm.calls), 2)  # only Roles and the Domain stage
        session = flow.store.get_session("s1")
        self.assertEqual(session.transcript_ref, session.source_transcript_ref)

    def test_translation_can_be_turned_off(self):
        flow = Flow(CORPORATE, [ROLES, ANSWERS["corporate"]])
        run_session("s1", store=flow.store, translator=TurnTranslator(flow.llm),
                    extractor=flow.extractor, domain=CORPORATE, llm=flow.llm, translate=False)
        self.assertEqual(flow.extractor.calls[0][0].language, "ro")
        self.assertEqual(len(flow.llm.calls), 2)

    def test_not_transcribed_session_is_refused(self):
        store = InMemorySessionStore()
        store.save_session(Session("s2", datetime(2026, 11, 6, tzinfo=timezone.utc), "corporate", audio_ref=AUDIO))
        llm = FakeLlmClient([])
        with self.assertRaisesRegex(LookupError, "not transcribed"):
            run_session("s2", store=store, translator=TurnTranslator(llm), extractor=FakeActionExtractor(GENERIC),
                        domain=CORPORATE, llm=llm)
        self.assertEqual(llm.calls, [])

    def test_untranslated_turn_makes_the_result_partial(self):
        only_first = {"turns": [TRANSLATION["turns"][0]]}
        flow = Flow(CORPORATE, [only_first, {"turns": []}, ROLES, ANSWERS["corporate"]])  # the retry also misses it
        result = flow.run()
        self.assertTrue(result.partial)
        self.assertEqual([(s.speaker_id, s.text) for s in result.untranslated], [("S2", "Bine, mulțumesc.")])

    def test_other_domain_than_the_sessions_is_refused(self):
        flow = Flow(CORPORATE, [])
        with self.assertRaises(ValueError):
            flow.run(MEDICAL)
        self.assertIsNone(flow.store.get_session("s1").source_transcript_ref)
        self.assertEqual(flow.llm.calls, [])

    def test_failed_domain_stage_saves_nothing_and_executes_nothing(self):
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, "not json"])
        with self.assertRaises(ValueError):
            flow.run()
        self.assertEqual(flow.store.load_actions("s1"), [])

    def test_failed_rerun_keeps_the_previous_typed_actions(self):
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"], ROLES, "not json"])
        flow.run()
        with self.assertRaises(ValueError):
            flow.run()
        actions = flow.store.load_actions("s1")
        self.assertEqual([(a.action_type, a.fields, a.status) for a in actions],
                         [("work_item", {"deliverable": "report"}, ActionStatus.PROPOSED)])

    def test_run_again_reuses_the_transcript_and_regenerates(self):
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"], ROLES, ANSWERS["corporate"]])
        flow.run()
        result = flow.run()  # one translation answer only: a second translation would fail
        self.assertEqual([a.action_type for a in result.actions], ["work_item"])
        self.assertEqual(len(flow.extractor.calls), 2)

    def test_run_after_a_correction_translates_the_corrected_text(self):
        corrected = {"turns": [{"id": "1", "text": "I will send the ANAF report on Friday."}, {"id": "2", "text": "Fine, thanks."}]}
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"], corrected, ROLES, ANSWERS["corporate"]])
        flow.run()
        old_translation = flow.store.get_session("s1").transcript_ref
        correct_transcript("s1", 0, "Trimit raportul ANAF vineri.", flow.store)

        flow.run()

        self.assertIn("Trimit raportul ANAF vineri.", flow.llm.calls[3][0])  # the translation prompt
        self.assertEqual(flow.extractor.calls[1][0].segments[0].text, "I will send the ANAF report on Friday.")
        session = flow.store.get_session("s1")
        self.assertNotIn(session.transcript_ref, (old_translation, session.source_transcript_ref))
        self.assertRaises(KeyError, flow.store.load_transcript, old_translation)

    def test_english_session_after_a_correction_is_still_not_translated(self):
        english = Transcript((Segment(0.0, 2.0, "I will send the report."),), language="en")
        flow = Flow(CORPORATE, [ROLES, ANSWERS["corporate"], ROLES, ANSWERS["corporate"]], spoken=english)
        flow.run()
        correct_transcript("s1", 0, "I will send the ANAF report.", flow.store)
        flow.run()
        self.assertEqual(len(flow.llm.calls), 4)  # Roles and Domain stage twice, no translation
        self.assertEqual(flow.extractor.calls[1][0].segments[0].text, "I will send the ANAF report.")

    def test_decided_actions_are_never_overwritten(self):
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"], ROLES, ANSWERS["corporate"]])
        flow.run()
        snapshot = flow.store.load_actions_snapshot("s1")
        action = snapshot.actions[0]
        action.reject()
        flow.store.save_actions("s1", [action], expected_revision=snapshot.revision)

        with self.assertRaises(ValueError):
            flow.run()
        self.assertEqual([a.status for a in flow.store.load_actions("s1")], [ActionStatus.REJECTED])


class RegenerationTest(unittest.TestCase):
    def _decide(self, flow: Flow, decide) -> None:
        snapshot = flow.store.load_actions_snapshot("s1")
        action = snapshot.actions[0]
        decide(action)
        flow.store.save_actions("s1", [action, *snapshot.actions[1:]], expected_revision=snapshot.revision)

    def _regenerate(self, flow: Flow, domain: Domain):
        return run_session("s1", store=flow.store, translator=TurnTranslator(flow.llm), extractor=flow.extractor,
                           domain=domain, llm=flow.llm, regenerate=True)

    def _answer_for_next_ids(self, flow: Flow, name: str) -> str:
        revision = flow.store.load_actions_snapshot("s1").revision
        return json.dumps(ANSWERS[name]).replace('"s1-1"', f'"s1-r{revision}-1"')

    def test_regeneration_keeps_the_decided_action_and_replaces_the_proposed_ones(self):
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"]])
        flow.run()
        self._decide(flow, lambda a: a.reject())
        flow.llm = FakeLlmClient([json.dumps(ROLES), self._answer_for_next_ids(flow, "corporate")])  # no translation: reused

        result = self._regenerate(flow, CORPORATE)

        new_id = f"s1-r{flow.store.load_actions_snapshot('s1').revision - 1}-1"
        self.assertEqual([a.id for a in result.actions], [new_id])
        self.assertEqual([(a.id, a.status) for a in flow.store.load_actions("s1")],
                         [("s1-1", ActionStatus.REJECTED), (new_id, ActionStatus.PROPOSED)])

    def test_regeneration_can_change_the_domain(self):
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"]])
        flow.run()
        self._decide(flow, lambda a: a.reject())
        flow.llm = FakeLlmClient([json.dumps(ROLES), self._answer_for_next_ids(flow, "medical")])

        result = self._regenerate(flow, MEDICAL)

        self.assertEqual(flow.store.get_session("s1").domain, "medical")
        self.assertEqual([a.action_type for a in result.actions], ["schedule_visit"])
        self.assertEqual(flow.store.load_actions("s1")[0].action_type, "work_item")  # the decision, unchanged

    def test_domain_change_waits_for_approved_actions(self):
        flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"]])
        flow.run()
        self._decide(flow, lambda a: a.approve({"steps": []}))
        with self.assertRaisesRegex(ValueError, "before changing the Domain"):
            self._regenerate(flow, MEDICAL)
        self.assertEqual(flow.store.get_session("s1").domain, "corporate")
        self.assertEqual(len(flow.extractor.calls), 1)  # refused before any model call


def roles(**by_speaker):
    return {"speakers": [{"id": sid, "role": role} for sid, role in by_speaker.items()]}


def medical_answer(owner):
    return {"proposed_actions": [{"id": "s1-1", "action_type": "schedule_visit", "owner": owner,
                                  "fields": {"reason": "Report"}}]}


class SpeakerRolesInFlowTest(unittest.TestCase):
    """#60: Roles are proposed after extraction, before the Domain stage, and decide the owner."""

    def test_roles_are_proposed_before_the_domain_stage_with_the_domain_model(self):
        flow = Flow(MEDICAL, [TRANSLATION, roles(S1="doctor", S2="patient"), medical_answer("patient")])
        result = flow.run()

        roles_call, domain_call = flow.llm.calls[-2:]
        self.assertEqual(roles_call[1], MEDICAL.model)
        self.assertIn("speakers", roles_call[2]["properties"])  # the Roles answer schema
        self.assertIn("S1: doctor\nS2: patient", domain_call[0])
        self.assertEqual(flow.store.load_speakers("s1"), [Speaker("S1", "doctor"), Speaker("S2", "patient")])
        self.assertEqual(result.actions[0].owner, "patient")

    def test_a_confirmed_role_is_never_overwritten(self):
        flow = Flow(MEDICAL, [TRANSLATION, roles(S2="doctor"), medical_answer(None)])
        set_speaker_role("s1", flow.store, MEDICAL, "S1", "caregiver")
        flow.run()

        roles_schema = flow.llm.calls[-2][2]
        self.assertEqual(roles_schema["properties"]["speakers"]["items"]["properties"]["id"]["enum"], ["S2"])
        self.assertEqual(flow.store.load_speakers("s1"),
                         [Speaker("S1", "caregiver", role_confirmed=True), Speaker("S2", "doctor")])
        self.assertIn("S1: caregiver\nS2: doctor", flow.llm.calls[-1][0])

    def test_an_invalid_roles_answer_leaves_them_unknown_and_the_analysis_goes_on(self):
        for bad in ("not json", roles(S1="Ion Popescu", S2="manager")):
            with self.subTest(bad=bad):
                flow = Flow(MEDICAL, [TRANSLATION, bad, medical_answer("doctor")])
                result = flow.run()
                self.assertEqual(flow.store.load_speakers("s1"), [Speaker("S1"), Speaker("S2")])
                self.assertIn("S1: unknown\nS2: unknown", flow.llm.calls[-1][0])
                self.assertEqual([a.action_type for a in result.actions], ["schedule_visit"])

    def test_owner_is_never_a_speaker_id(self):
        flow = Flow(MEDICAL, [TRANSLATION, ROLES, medical_answer("S1")])
        self.assertIsNone(flow.run().actions[0].owner)

    def test_regeneration_after_a_role_correction_keeps_the_decided_actions(self):
        flow = Flow(MEDICAL, [TRANSLATION, roles(S1="patient", S2="doctor"), medical_answer("patient")])
        flow.run()
        snapshot = flow.store.load_actions_snapshot("s1")
        snapshot.actions[0].reject()
        flow.store.save_actions("s1", list(snapshot.actions), expected_revision=snapshot.revision)
        set_speaker_role("s1", flow.store, MEDICAL, "S1", "doctor")
        revision = flow.store.load_actions_snapshot("s1").revision
        regenerated = json.dumps(medical_answer("doctor")).replace('"s1-1"', f'"s1-r{revision}-1"')
        flow.llm = FakeLlmClient([json.dumps(roles(S2="patient")), regenerated])

        run_session("s1", store=flow.store, translator=TurnTranslator(flow.llm), extractor=flow.extractor,
                    domain=MEDICAL, llm=flow.llm, regenerate=True)

        self.assertIn("S1: doctor\nS2: patient", flow.llm.calls[-1][0])
        self.assertEqual([(a.id, a.status, a.owner) for a in flow.store.load_actions("s1")],
                         [("s1-1", ActionStatus.REJECTED, "patient"),
                          (f"s1-r{revision}-1", ActionStatus.PROPOSED, "doctor")])


class EncryptedStoreFlowTest(unittest.TestCase):
    def test_flow_runs_on_the_encrypted_store_with_no_plaintext_on_disk(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "norev.db"
            store = EncryptedStore(path, os.urandom(KEY_BYTES))
            try:
                flow = Flow(CORPORATE, [TRANSLATION, ROLES, ANSWERS["corporate"]], store=store)
                result = flow.run()
                self.assertEqual([a.action_type for a in result.actions], ["work_item"])
                self.assertIsNotNone(flow.store.get_session("s1").source_transcript_ref)
            finally:
                store.close()
            self.assertEqual(os.listdir(folder), ["norev.db"])  # no intermediate files
            raw = path.read_bytes()
            for text in ("raportul", "report", "Bine", "spk_7"):
                self.assertNotIn(text.encode(), raw)


if __name__ == "__main__":
    unittest.main()
