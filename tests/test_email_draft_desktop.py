"""The email draft in the desktop app (#61): preview with the decoded email above the exact file,
approve, Execute through the Central Processor, and "Deschide în mail" only when the User asks,
on the real encrypted store."""
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from norev.adapters.connectors.email_draft import EmailDraftConnector
from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.adapters.store.encrypted import KEY_BYTES, EncryptedStore
from norev.core.models import ProposedAction, Segment, Session, StepRecord, StepState, Transcript
from norev.processor.central import CentralProcessor
from norev.processor.compose import LOCAL_STEPS, desktop_payload_builder
from norev.processor.identity import LocalUser
from norev.testing.fakes import FakeLlmClient, FixedClock, InMemorySessionStore
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge
from norev.ui.session_backend import SessionBackend

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
USER = "local:ana"
LF = chr(10)
SUBJECT = "Cifrele pentru ședință"
BODY = "Bună ziua," + LF + "Cifrele pe Q4 sunt cele discutate azi." + LF


def follow_up(recipients: str | None = None) -> ProposedAction:
    return ProposedAction("s1-1", "Send the Q4 figures", "Email the figures.", "I'll email you the figures",
                          action_type="follow_up_email",
                          fields={"recipients": recipients, "subject": SUBJECT, "body": BODY})


class EmailDraftDesktopTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.export_dir = root / "export"
        self.store = EncryptedStore(root / "data" / "norev.db", os.urandom(KEY_BYTES))
        self.addCleanup(self.store.close)
        processor = CentralProcessor(FakeLlmClient([]), self.store, identity=LocalUser(USER), clock=FixedClock(NOW),
                                     connectors=[LocalDocumentConnector(self.export_dir), EmailDraftConnector(self.export_dir)])

        @contextmanager
        def no_llm():
            yield None

        self.opened: list[Path] = []
        backend = SessionBackend(self.store, approved_by=USER, processor=processor, export_dir=self.export_dir,
                                 llm_session=no_llm, record_source=lambda: iter(()), clock=FixedClock(NOW),
                                 builder=desktop_payload_builder(self.export_dir, calendar=False),
                                 open_file=self.opened.append)
        self.bridge = Bridge(backend)
        transcript = self.store.save_transcript("s1", Transcript((Segment(0, 1, "I'll email you the figures.", "S1"),), "en"))
        self.store.save_session(Session(id="s1", started_at=NOW, domain="corporate", transcript_ref=transcript))
        self.store.save_actions("s1", [follow_up()], expected_revision=0)

    def ok(self, result: dict):
        self.assertTrue(result["ok"], result)
        return result["data"]

    def code(self, result: dict) -> str:
        self.assertFalse(result["ok"], result)
        return result["error"]["code"]

    def preview(self, recipients: str = "ana@example.com") -> dict:
        review = self.ok(self.bridge.review("s1"))
        self.assertIn("Destinatari (emailuri)", review["actions"][0]["missing"])  # the model never fills them
        review = self.ok(self.bridge.edit("s1", "s1-1", {"fields": {"recipients": recipients}}, review["revision"]))
        return self.ok(self.bridge.preview("s1", "s1-1", review["revision"]))

    def test_preview_shows_the_decoded_email_above_the_exact_file_and_path(self) -> None:
        [step] = self.preview()["actions"][0]["plan"]
        self.assertEqual((step["connector"], step["connector_label"]), ("email_draft", "Draft de email"))
        self.assertEqual(step["readable"], {"to": "ana@example.com", "subject": SUBJECT, "body": BODY})
        self.assertIn("Subject: Cifrele pentru =?utf-8?", step["payload"]["content"])  # the exact file
        self.assertEqual(Path(step["payload"]["destination"]).parent, self.export_dir)

    def test_header_injection_through_the_ui_is_refused(self) -> None:
        review = self.ok(self.bridge.review("s1"))
        review = self.ok(self.bridge.edit("s1", "s1-1", {"fields": {
            "recipients": "ana@example.com" + LF + "Bcc: evil@example.com"}}, review["revision"]))
        self.assertEqual(self.code(self.bridge.preview("s1", "s1-1", review["revision"])), "invalid_header")
        review = self.ok(self.bridge.edit("s1", "s1-1", {"fields": {"recipients": "S2"}}, review["revision"]))
        self.assertEqual(self.code(self.bridge.preview("s1", "s1-1", review["revision"])), "invalid_email")

    def test_execute_saves_the_draft_and_opens_it_only_when_asked(self) -> None:
        review = self.preview()
        review = self.ok(self.bridge.approve("s1", "s1-1", review["revision"]))
        payload = review["actions"][0]["plan"][0]["payload"]
        self.assertEqual(self.code(self.bridge.open_draft("s1", "s1-1")), "not_executable")  # not saved yet

        review = self.ok(self.bridge.execute("s1", "s1-1"))
        action = review["actions"][0]
        self.assertEqual((action["status"], action["draft"]), ("executed", True))
        self.assertEqual(self.opened, [])  # Execute saves; it never opens the mail app by itself
        path = Path(payload["destination"])
        self.assertEqual(path.read_bytes(), payload["content"].encode("utf-8"))
        self.assertEqual([(r.connector, r.ok) for r in self.store.load_results()], [("email_draft", True)])

        self.assertEqual(self.ok(self.bridge.open_draft("s1", "s1-1")), {"opened": True})
        self.assertEqual(self.opened, [path])

    def test_a_draft_changed_or_gone_on_disk_is_not_opened(self) -> None:
        review = self.preview()
        review = self.ok(self.bridge.approve("s1", "s1-1", review["revision"]))
        self.ok(self.bridge.execute("s1", "s1-1"))
        path = Path(review["actions"][0]["plan"][0]["payload"]["destination"])
        path.write_bytes(path.read_bytes().replace(b"X-Unsent: 1", b"Bcc: evil@example.com"))
        self.assertEqual(self.code(self.bridge.open_draft("s1", "s1-1")), "not_executable")
        self.assertFalse(self.ok(self.bridge.review("s1"))["actions"][0]["draft"])
        path.unlink()
        self.assertEqual(self.code(self.bridge.open_draft("s1", "s1-1")), "not_executable")
        self.assertEqual(self.opened, [])

    def test_a_draft_is_not_opened_before_the_action_is_executed(self) -> None:
        # the app stopped after the draft was written but before the action was marked Executed
        review = self.preview()
        review = self.ok(self.bridge.approve("s1", "s1-1", review["revision"]))
        payload = review["actions"][0]["plan"][0]["payload"]
        EmailDraftConnector(self.export_dir).send(payload)
        self.store.save_step("s1-1", StepRecord("save_email", StepState.DONE, result={"path": payload["destination"]}))
        self.assertEqual(self.code(self.bridge.open_draft("s1", "s1-1")), "not_executable")
        self.assertEqual(self.opened, [])

    def test_retry_after_an_uncertain_save_writes_no_second_file(self) -> None:
        review = self.preview()
        self.ok(self.bridge.approve("s1", "s1-1", review["revision"]))
        self.ok(self.bridge.execute("s1", "s1-1"))
        # the same approved draft sent again (e.g. reconciled as not saved, then executed) finds the file
        action = self.store.load_actions("s1")[0]
        [step] = action.approved_payload["steps"]
        self.assertFalse(EmailDraftConnector(self.export_dir).send(step["payload"])["created"])
        self.assertEqual(len(list(self.export_dir.glob("*.eml"))), 1)

    def test_unknown_action_and_the_demo(self) -> None:
        self.assertEqual(self.code(self.bridge.open_draft("s1", "nope")), "not_found")
        demo = Bridge(DemoBackend(InMemorySessionStore(), approved_by=USER))
        self.assertEqual(self.code(demo.open_draft("s1", "s1-1")), "not_executable")

    def test_build_email_is_a_known_local_step(self) -> None:
        self.assertIn("build_email", LOCAL_STEPS)


if __name__ == "__main__":
    unittest.main()
