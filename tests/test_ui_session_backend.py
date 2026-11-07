"""#43: the desktop UI on the real Session flow, with fakes: no microphone, model, GPU or llama-server.

Recording or import -> encrypted store -> Transcript with Speakers -> analysis -> review -> Workflow
executor -> local document, through the Central Processor. Background work runs inline here.
"""
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.adapters.store.encrypted import KEY_BYTES, EncryptedStore
from norev.core.models import ActionStatus, StepRecord, StepState
from norev.processor.central import CentralProcessor
from norev.processor.identity import LocalUser
from norev.launcher.llama import LlamaError
from norev.testing.fakes import FakeActionExtractor, FakeLlmClient, FixedClock
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge
from norev.ui.session_backend import SessionBackend
from test_usecases_session_flow import ANSWERS, GENERIC, ROLES, SPOKEN, TRANSLATION, TURNS

NOW = datetime(2026, 11, 6, 10, 24, tzinfo=timezone.utc)
USER = "local:test"
EXTRACTED = {**GENERIC, "session_summary": "  Ședință despre raportul de vineri. "}
PCM = [b"\x01\x00" * 8000] * 3  # 1.5 s of 16 kHz mono s16


class AnyAudio:
    """Transcriber and Diarizer for whatever audio_ref the store gave; `fail` raises once."""

    def __init__(self) -> None:
        self.fail = None
        self.closed = 0

    def transcribe(self, audio_ref, language=None):
        if self.fail:
            error, self.fail = self.fail, None
            raise error
        return SPOKEN

    def diarize(self, audio_ref):
        return list(TURNS)

    def close(self):
        self.closed += 1


class SessionLlm(FakeLlmClient):
    """The recorded answers name the Proposed Action s1-1; here the Session id is generated."""

    def __init__(self, answers, store) -> None:
        super().__init__([json.dumps(a) for a in answers])
        self.store = store

    def generate_json(self, prompt, *, model=None, json_schema=None):
        raw = super().generate_json(prompt, model=model, json_schema=json_schema)
        sessions = self.store.list_sessions()
        return raw.replace('"s1-1"', f'"{sessions[0].id}-1"') if sessions else raw


class Case(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.export_dir = self.root / "export"
        self.store = self.open_store()
        self.audio = AnyAudio()
        self.llm = SessionLlm([TRANSLATION, ROLES, ANSWERS["corporate"]], self.store)
        self.llm_runs = []
        self.llm_error = None
        self.picked = None
        self.mic = lambda: iter(PCM)
        self.bridge = Bridge(self.backend())

    def open_store(self):
        key = self.root / "key"
        if not key.exists():
            key.write_bytes(os.urandom(KEY_BYTES))
        store = EncryptedStore(self.root / "data" / "norev.db", key.read_bytes())
        self.addCleanup(store.close)
        return store

    def backend(self, **kwargs):
        processor = CentralProcessor(self.llm, self.store, identity=LocalUser(USER), clock=FixedClock(NOW),
                                     connectors=[LocalDocumentConnector(self.export_dir)],
                                     transcriber=self.audio, diarizer=self.audio)

        @contextmanager
        def llm_session():
            if self.llm_error:
                raise self.llm_error
            self.llm_runs.append("start")
            yield "qwen3"
            self.llm_runs.append("stop")

        return SessionBackend(self.store, approved_by=USER, processor=processor, export_dir=self.export_dir,
                              llm_session=llm_session, record_source=lambda: self.mic(), pick_file=lambda: self.picked,
                              make_extractor=lambda llm, workers: FakeActionExtractor(EXTRACTED),
                              clock=FixedClock(NOW), run_in_background=lambda work: work(), **kwargs)

    def ok(self, result):
        self.assertTrue(result["ok"], result)
        return result["data"]

    def error(self, result):
        self.assertFalse(result["ok"], result)
        return result["error"]["code"]

    def recorded(self) -> str:
        session_id = self.ok(self.bridge.start("corporate", "ro", "Echipa"))["session_id"]
        self.ok(self.bridge.stop(session_id))
        return session_id

    def approved(self, session_id: str) -> dict:
        review = self.ok(self.bridge.review(session_id))
        action = review["actions"][0]
        review = self.ok(self.bridge.preview(session_id, action["id"], review["revision"]))
        return self.ok(self.bridge.approve(session_id, action["id"], review["revision"]))


class RecordAndAnalyseTest(Case):
    def test_recording_is_analysed_and_reviewed(self):
        session_id = self.recorded()

        self.assertEqual(self.ok(self.bridge.progress(session_id)), {"stage": "ready", "failed": None})
        review = self.ok(self.bridge.review(session_id))
        self.assertTrue(review["can_execute"])
        self.assertEqual([a["action_type"] for a in review["actions"]], ["work_item"])
        self.assertEqual([s["id"] for s in review["speakers"]], ["S1", "S2"])
        self.assertEqual(review["untranslated"], 0)
        # llama-server only for the LLM stages, after Whisper and diarization released the GPU
        self.assertEqual(self.llm_runs, ["start", "stop"])
        self.assertGreaterEqual(self.audio.closed, 1)

    def test_review_shows_the_final_transcript_spoken_and_translated(self):
        transcript = self.ok(self.bridge.review(self.recorded()))["transcript"]
        self.assertEqual(
            [(s["speaker_id"], s["start"], s["text"]) for s in transcript["spoken"]],
            [("S1", 0.0, "Trimit raportul vineri."), ("S2", 2.0, "Bine, mulțumesc.")])
        self.assertEqual(transcript["spoken_language"], "ro")
        self.assertEqual([(s["speaker_id"], s["text"], s["untranslated"]) for s in transcript["translated"]],
                         [("S1", "I will send the report on Friday.", False), ("S2", "Fine, thanks.", False)])
        self.assertEqual(transcript["translated_language"], "en")

    def test_summary_is_kept_and_shown(self):
        session_id = self.recorded()
        self.assertEqual(self.store.load_summary(session_id), "Ședință despre raportul de vineri.")
        self.assertEqual(self.ok(self.bridge.review(session_id))["summary"], "Ședință despre raportul de vineri.")

    def test_a_session_whose_analysis_failed_stays_in_the_list_after_a_restart(self):
        self.llm_error = LlamaError("llama-server nu există")
        session_id = self.recorded()
        self.bridge = Bridge(self.backend())  # a new app on the same store
        [listed] = self.ok(self.bridge.sessions())
        self.assertEqual((listed["session_id"], listed["ready"], listed["pending"]), (session_id, False, 0))
        self.assertEqual(self.ok(self.bridge.progress(session_id))["failed"], "interrupted")
        self.llm_error = None
        self.ok(self.bridge.stop(session_id))  # the User's "Try again"
        self.assertTrue(self.ok(self.bridge.sessions())[0]["ready"])

    def test_delete_after_confirmation_and_never_while_an_action_waits(self):
        session_id = self.recorded()
        action = self.approved(session_id)["actions"][0]
        self.assertEqual(self.error(self.bridge.delete(session_id)), "execution_pending")
        self.ok(self.bridge.execute(session_id, action["id"]))

        self.ok(self.bridge.delete(session_id))

        self.assertEqual(self.ok(self.bridge.sessions()), [])
        self.assertEqual(self.error(self.bridge.review(session_id)), "not_found")
        self.assertEqual(len(self.store.load_events()), 1)  # what was sent stays in the audit
        self.assertEqual(len(list(self.export_dir.iterdir())), 1)  # and the exported document is the User's
        self.assertEqual(self.error(self.bridge.delete(session_id)), "not_found")

    def test_regenerate_after_a_role_correction_keeps_the_decided_action(self):
        session_id = self.recorded()
        review = self.ok(self.bridge.review(session_id))
        self.assertEqual([s["role"] for s in review["speakers"]], [None, None])  # unknown: the UI says so
        first = review["actions"][0]
        review = self.ok(self.bridge.reject(session_id, first["id"], review["revision"]))
        self.ok(self.bridge.set_role(session_id, "S1", "organizer"))
        new_id = f"{session_id}-r{self.ok(self.bridge.review(session_id))['revision']}-1"
        self.llm._responses += [json.dumps({"speakers": [{"id": "S2", "role": "participant"}]}), json.dumps(
            {"proposed_actions": [{"id": new_id, "action_type": "work_item", "owner": "organizer", "fields": {}}]})]

        self.ok(self.bridge.regenerate(session_id))

        self.assertEqual(self.ok(self.bridge.progress(session_id)), {"stage": "ready", "failed": None})
        review = self.ok(self.bridge.review(session_id))
        self.assertEqual([(a["id"], a["status"], a["owner"]) for a in review["actions"]],
                         [(first["id"], "rejected", ""), (new_id, "proposed", "organizer")])
        self.assertEqual([s["role"] for s in review["speakers"]], ["organizer", "participant"])

    def test_a_corrected_segment_is_saved_and_survives_reopening(self):
        session_id = self.recorded()
        review = self.ok(self.bridge.edit_transcript(session_id, 0, "  Trimit raportul ANAF vineri. "))
        self.assertEqual([seg["text"] for seg in review["transcript"]["spoken"]], ["Trimit raportul ANAF vineri.", "Bine, mulțumesc."])
        self.assertEqual(review["transcript"]["spoken"][0]["speaker_id"], "S1")
        self.assertEqual(review["transcript"]["translated"], [])  # the translation is redone on "Regenerează"
        self.store.close()
        self.store = self.open_store()
        self.bridge = Bridge(self.backend())
        review = self.ok(self.bridge.review(session_id))
        self.assertEqual(review["transcript"]["spoken"][0]["text"], "Trimit raportul ANAF vineri.")
        self.assertTrue(review["retranslate"])  # the regenerate button comes back after a restart

    def test_an_invalid_correction_is_a_validation_error(self):
        session_id = self.recorded()
        self.assertEqual(self.error(self.bridge.edit_transcript(session_id, 2, "x")), "invalid_index")
        self.assertEqual(self.error(self.bridge.edit_transcript(session_id, "0", "x")), "invalid_index")
        self.assertEqual(self.error(self.bridge.edit_transcript(session_id, 0, "  ")), "required")
        self.assertEqual(self.error(self.bridge.edit_transcript(session_id, 0, None)), "invalid_string")
        self.assertEqual(self.error(self.bridge.edit_transcript("missing", 0, "x")), "not_found")

    def test_correction_is_refused_while_recording(self):
        session_id = self.ok(self.bridge.start("corporate", "ro", "Echipa"))["session_id"]
        self.assertEqual(self.error(self.bridge.edit_transcript(session_id, 0, "x")), "session_busy")
        self.ok(self.bridge.stop(session_id))

    def test_regenerate_after_a_correction_translates_the_corrected_text_and_keeps_the_decided_action(self):
        session_id = self.recorded()
        review = self.ok(self.bridge.review(session_id))
        first = review["actions"][0]
        self.ok(self.bridge.reject(session_id, first["id"], review["revision"]))
        review = self.ok(self.bridge.edit_transcript(session_id, 0, "Trimit raportul ANAF vineri."))
        new_id = f"{session_id}-r{review['revision'] + 1}-1"  # saving the new translation bumps the revision
        calls = len(self.llm.calls)
        self.llm._responses += [json.dumps({"turns": [{"id": "1", "text": "I will send the ANAF report on Friday."},
                                                      {"id": "2", "text": "Fine, thanks."}]}),
                                json.dumps(ROLES), json.dumps({"proposed_actions": [
                                    {"id": new_id, "action_type": "work_item", "fields": {}}]})]

        self.ok(self.bridge.regenerate(session_id))

        self.assertEqual(self.ok(self.bridge.progress(session_id)), {"stage": "ready", "failed": None})
        self.assertIn("ANAF", self.llm.calls[calls][0])  # the translation ran on the corrected text
        review = self.ok(self.bridge.review(session_id))
        self.assertEqual([(a["id"], a["status"]) for a in review["actions"]],
                         [(first["id"], "rejected"), (new_id, "proposed")])
        session = self.store.get_session(session_id)
        self.assertNotEqual(session.transcript_ref, session.source_transcript_ref)
        self.assertIn("ANAF", self.store.load_transcript(session.transcript_ref).text)
        self.assertFalse(review["retranslate"])

    def test_regenerate_is_refused_while_recording_and_in_the_demo(self):
        session_id = self.ok(self.bridge.start("corporate", "ro", "Echipa"))["session_id"]
        self.assertEqual(self.error(self.bridge.regenerate(session_id)), "session_busy")
        self.ok(self.bridge.stop(session_id))
        self.assertEqual(self.error(Bridge(DemoBackend(self.store, approved_by=USER, clock=FixedClock(NOW))).regenerate("any")), "mode_unavailable")

    def test_delete_is_refused_while_recording(self):
        session_id = self.ok(self.bridge.start("corporate", "ro", ""))["session_id"]
        self.assertEqual(self.error(self.bridge.delete(session_id)), "session_busy")
        self.ok(self.bridge.stop(session_id))

    def test_audio_is_sealed_in_the_store_never_written_to_a_file(self):
        session_id = self.recorded()
        session = self.store.get_session(session_id)
        audio = self.store.load_audio(session.audio_ref)
        self.assertTrue(audio.startswith(b"RIFF"))
        files = [p.name for p in self.root.rglob("*") if p.is_file()]
        self.assertEqual(sorted(f for f in files if not f.startswith("norev.db")), ["key"])

    def test_live_transcript_is_feedback_only(self):
        lives = []

        class FakeLive:
            def __init__(self, language):
                self.language, self.fed, self.closed = language, b"", False
                lives.append(self)

            def feed(self, block):
                self.fed += block

            def read(self):
                return [{"speaker_id": None, "text": "doar feedback live"}]

            def close(self):
                self.closed = True

        transcribe = self.audio.transcribe
        def final_transcript(audio_ref, language=None):
            self.assertTrue(lives[0].closed)  # the live model is gone before the final Transcript starts
            return transcribe(audio_ref, language)
        self.audio.transcribe = final_transcript
        self.bridge = Bridge(self.backend(live_feedback=FakeLive))
        session_id = self.ok(self.bridge.start("corporate", "ro", ""))["session_id"]
        self.assertEqual(self.ok(self.bridge.live(session_id))["segments"],
                         [{"speaker_id": None, "text": "doar feedback live"}])
        self.ok(self.bridge.stop(session_id))

        live = lives[0]
        self.assertEqual((live.language, live.fed, live.closed), ("ro", b"".join(PCM), True))
        transcript = self.store.load_transcript(self.store.get_session(session_id).transcript_ref)
        self.assertNotIn("doar feedback live", json.dumps([s.text for s in transcript.segments], ensure_ascii=False))
        self.assertNotIn("doar feedback live", json.dumps(self.ok(self.bridge.review(session_id)), ensure_ascii=False))

    def test_pause_drops_audio_and_stop_still_ends_the_recording(self):
        session_id = self.ok(self.bridge.start("corporate", "ro", ""))["session_id"]
        self.assertTrue(self.ok(self.bridge.pause(session_id))["paused"])
        self.assertEqual(self.ok(self.bridge.live(session_id))["segments"], [])
        self.ok(self.bridge.stop(session_id))
        self.assertIn(self.ok(self.bridge.progress(session_id))["stage"], ("ready", "recording"))

    def test_no_microphone_is_a_clear_error_and_saves_nothing(self):
        def broken():
            raise RuntimeError("Could not open input device 'Mic'")
        self.mic = broken
        self.assertEqual(self.error(self.bridge.start("corporate", "ro", "")), "no_microphone")
        self.assertEqual(self.store.list_sessions(), [])

    def test_an_empty_recording_is_reported_not_analysed(self):
        self.mic = lambda: iter([])
        session_id = self.ok(self.bridge.start("corporate", "ro", ""))["session_id"]
        self.ok(self.bridge.stop(session_id))
        self.assertEqual(self.ok(self.bridge.progress(session_id))["failed"], "no_audio")
        self.assertEqual(self.llm_runs, [])

    def test_import_picks_a_file_and_cancel_does_nothing(self):
        self.assertIsNone(self.ok(self.bridge.import_file("corporate", "auto", ""))["session_id"])
        audio = self.root / "meeting.wav"
        audio.write_bytes(b"RIFF fake audio")
        self.picked = str(audio)
        session_id = self.ok(self.bridge.import_file("corporate", "auto", ""))["session_id"]
        self.assertEqual(self.ok(self.bridge.progress(session_id))["stage"], "ready")
        self.assertEqual(len(self.ok(self.bridge.review(session_id))["actions"]), 1)

    def test_a_failed_stage_is_reported_and_stop_runs_it_again(self):
        self.audio.fail = OSError("CUDA out of memory while decoding Session audio")
        session_id = self.recorded()
        self.assertEqual(self.ok(self.bridge.progress(session_id)), {"stage": "transcribing", "failed": "OSError"})
        self.ok(self.bridge.stop(session_id))
        self.assertEqual(self.ok(self.bridge.progress(session_id))["stage"], "ready")

    def test_llama_server_not_starting_stops_at_its_stage(self):
        self.llm_error = LlamaError("llama-server nu există")
        session_id = self.recorded()
        self.assertEqual(self.ok(self.bridge.progress(session_id)), {"stage": "starting_llm", "failed": "LlamaError"})
        # never analysed: regenerating is refused, the first analysis runs again from stop
        self.assertEqual(self.error(self.bridge.regenerate(session_id)), "session_busy")
        # the Transcript is kept: a retry starts at the LLM stages
        self.llm_error = None
        self.ok(self.bridge.stop(session_id))
        self.assertEqual(self.ok(self.bridge.progress(session_id))["stage"], "ready")


class ExecuteTest(Case):
    def test_approved_action_is_exported_through_the_central_processor(self):
        session_id = self.recorded()
        review = self.approved(session_id)
        action = review["actions"][0]
        self.assertEqual(action["status"], "approved")

        review = self.ok(self.bridge.execute(session_id, action["id"]))

        self.assertEqual(review["actions"][0]["status"], "executed")
        [document] = list(self.export_dir.iterdir())
        self.assertEqual(str(document), action["plan"][0]["payload"]["destination"])
        [event] = self.store.load_events()
        self.assertEqual((event.action_id, event.connector, event.approved_by), (action["id"], "local_document", USER))
        self.assertEqual(self.error(self.bridge.execute(session_id, action["id"])), "not_executable")

    def test_review_shows_the_spoken_transcript_with_speakers(self):
        review = self.ok(self.bridge.review(self.recorded()))
        self.assertFalse(review["retranslate"])
        self.assertEqual([(s["index"], s["speaker_id"], s["text"]) for s in review["transcript"]["spoken"]], [
            (0, "S1", "Trimit raportul vineri."),
            (1, "S2", "Bine, mulțumesc."),
        ])

    def test_corrected_title_and_description_are_what_is_approved(self):
        session_id = self.recorded()
        review = self.ok(self.bridge.review(session_id))
        action = review["actions"][0]
        review = self.ok(self.bridge.preview(session_id, action["id"], review["revision"]))
        review = self.ok(self.bridge.edit(session_id, action["id"],
                                          {"title": "Trimite raportul ANAF", "description": ""}, review["revision"]))
        edited = review["actions"][0]
        self.assertEqual((edited["title"], edited["description"], edited["prepared"]), ("Trimite raportul ANAF", "", False))

        review = self.ok(self.bridge.preview(session_id, action["id"], review["revision"]))
        self.assertIn("Trimite raportul ANAF", json.dumps(review["actions"][0]["plan"], ensure_ascii=False))

    def test_an_empty_title_is_shown_missing_and_refused_at_preview(self):
        session_id = self.recorded()
        review = self.ok(self.bridge.review(session_id))
        action = review["actions"][0]
        review = self.ok(self.bridge.edit(session_id, action["id"], {"title": ""}, review["revision"]))
        self.assertIn("Titlu", review["actions"][0]["missing"])
        self.assertEqual(self.error(self.bridge.preview(session_id, action["id"], review["revision"])), "required")

    def test_a_proposed_action_is_never_executed(self):
        session_id = self.recorded()
        action = self.ok(self.bridge.review(session_id))["actions"][0]
        self.assertEqual(self.error(self.bridge.execute(session_id, action["id"])), "not_executable")
        self.assertFalse(self.export_dir.exists())

    def test_failed_export_shows_the_error_and_retry_sends_the_same_payload(self):
        session_id = self.recorded()
        action = self.approved(session_id)["actions"][0]
        self.export_dir.write_text("a file where the folder should be")

        review = self.ok(self.bridge.execute(session_id, action["id"]))
        failed = review["actions"][0]
        self.assertEqual(failed["status"], "failed")
        self.assertTrue(failed["error"])

        self.export_dir.unlink()
        review = self.ok(self.bridge.retry(session_id, action["id"]))
        self.assertEqual(review["actions"][0]["status"], "executed")
        self.assertEqual(review["actions"][0]["plan"], action["plan"])

    def test_after_a_restart_an_interrupted_step_is_reconciled_not_resent(self):
        session_id = self.recorded()
        action = self.approved(session_id)["actions"][0]
        # the app stopped while the document was being saved
        self.store.save_step(action["id"], StepRecord("build_document", StepState.DONE))
        self.store.save_step(action["id"], StepRecord("save_document", StepState.STARTED))

        self.bridge = Bridge(self.backend())  # a new app on the same store
        self.assertEqual([s["session_id"] for s in self.ok(self.bridge.sessions())], [session_id])
        review = self.ok(self.bridge.review(session_id))
        self.assertEqual(review["actions"][0]["uncertain_step"], "save_document")
        self.ok(self.bridge.execute(session_id, action["id"]))
        self.assertFalse(self.export_dir.exists())  # not sent again on its own

        review = self.ok(self.bridge.reconcile(session_id, action["id"], "save_document", True))
        review = self.ok(self.bridge.execute(session_id, action["id"]))
        self.assertEqual(review["actions"][0]["status"], "executed")
        self.assertFalse(self.export_dir.exists())  # the User said it was saved: not written twice

    def test_the_demo_never_executes(self):
        demo = Bridge(DemoBackend(self.store, approved_by=USER, clock=FixedClock(NOW)))
        session_id = self.ok(demo.import_example("corporate", "ro", ""))["session_id"]
        self.assertFalse(self.ok(demo.review(session_id))["can_execute"])
        for call in (demo.execute(session_id, "x"), demo.retry(session_id, "x"), demo.reconcile(session_id, "x", "s", True)):
            self.assertEqual(self.error(call), "not_executable")


class OthersTest(Case):
    def test_instructions_come_from_the_local_model_and_need_confirmation(self):
        self.llm = SessionLlm([{"instructions": "- Keep what was agreed, with dates."}, TRANSLATION, ROLES, ANSWERS["others"]], self.store)
        self.bridge = Bridge(self.backend())
        context = "Ședință cu constructorul despre bucătărie."
        instructions = self.ok(self.bridge.others_generate(context))["prompt"]
        self.assertEqual(self.llm_runs, ["start", "stop"])
        others = {"context": context, "prompt": instructions}
        self.assertEqual(self.error(self.bridge.start("others", "ro", "", others)), "prompt_not_confirmed")

        self.ok(self.bridge.others_confirm(context, instructions))
        session_id = self.ok(self.bridge.start("others", "ro", "", others))["session_id"]
        self.ok(self.bridge.stop(session_id))
        self.assertEqual(self.store.get_session(session_id).others_prompt.instructions, instructions)
        self.assertEqual(self.ok(self.bridge.review(session_id))["actions"][0]["action_type"], "action_item")


if __name__ == "__main__":
    unittest.main()
