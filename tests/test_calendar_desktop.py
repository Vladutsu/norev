"""Google Calendar in the desktop app (#45): composition, the User's account, and a calendar_event
approved and executed from the UI bridge on the real encrypted store, to a fake Google on 127.0.0.1."""
import os
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from norev.adapters.connectors.google_calendar import (
    AuthorizationFailed,
    GoogleCalendarConnector,
    GoogleCalendarSettings,
    GoogleError,
    GoogleUnreachable,
    NotConfigured,
)
from norev.adapters.connectors.local_document import LocalDocumentConnector
from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.adapters.store.encrypted import KEY_BYTES, EncryptedStore
from norev.core.models import ActionStatus, ProposedAction, Segment, Session, StepState, Transcript, UnsupportedWorkflow
from norev.processor.central import CentralProcessor
from norev.processor.compose import LOCAL_STEPS, build_desktop_processor, build_google_calendar, desktop_payload_builder
from norev.processor.google_account import GoogleAccount
from norev.processor.identity import LocalUser
from norev.testing.fakes import FakeLlmClient, FixedClock, InMemorySessionStore
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge
from norev.ui.session_backend import SessionBackend
from test_connectors_google_auth import MemorySecrets
from test_connectors_google_calendar import SECRET, FakeCalendar, StubAuth

NOW = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
USER = "local:ana"
CONFIGURED = GoogleCalendarSettings("id.apps.googleusercontent.com", "secret")
CORPORATE = load_domain(DOMAINS / "corporate")


def calendar_event(action_id: str = "s1-1") -> ProposedAction:
    return ProposedAction(action_id, "Offer review", f"Review the offer with {SECRET}.", "Let's meet on Tuesday",
                          action_type="calendar_event",
                          fields={"event_date": "2026-11-10", "start_time": "10:00", "end_time": "11:00",
                                  "location": "room 2", "attendees": None})


class TempStore(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / "key").write_bytes(os.urandom(KEY_BYTES))
        self.store = self.open_store()

    def open_store(self) -> EncryptedStore:
        store = EncryptedStore(self.root / "data" / "norev.db", (self.root / "key").read_bytes())
        self.addCleanup(store.close)
        return store


class DesktopCompositionTest(TempStore):
    def test_no_calendar_without_an_oauth_client(self) -> None:
        account, connector = build_google_calendar(self.store, GoogleCalendarSettings(None, None))
        self.assertIsNone(connector)
        self.assertEqual(account.status(), {"available": False, "connected": False})

    def test_no_calendar_without_an_encrypted_store_for_the_token(self) -> None:
        account, connector = build_google_calendar(InMemorySessionStore(), CONFIGURED)
        self.assertIsNone(connector)
        self.assertFalse(account.status()["available"])

    def test_calendar_with_an_oauth_client_and_the_encrypted_store(self) -> None:
        account, connector = build_google_calendar(self.store, CONFIGURED)
        self.assertIsInstance(connector, GoogleCalendarConnector)
        self.assertEqual(account.status(), {"available": True, "connected": False})  # nothing contacted
        with patch.dict(os.environ, {"LLM_PROVIDER": "llamacpp", "LLAMACPP_URL": "http://127.0.0.1:9"}):
            processor = build_desktop_processor(self.store, self.root / "export", calendar=connector)
            without = build_desktop_processor(self.store, self.root / "export")
        self.assertTrue(processor.has_connector("google_calendar") and processor.has_connector("local_document"))
        self.assertFalse(without.has_connector("google_calendar"))
        self.assertIn("build_event", LOCAL_STEPS)  # the Corporate calendar_event Workflow's first step

    def test_previews_pick_the_builder_of_the_workflows_connector(self) -> None:
        export = self.root / "export"
        plan = desktop_payload_builder(export, calendar=True).build(calendar_event(), CORPORATE, {})
        self.assertEqual(plan["steps"][0]["connector"], "google_calendar")
        self.assertEqual(plan["steps"][0]["payload"]["event"]["summary"], "Offer review")
        work_item = ProposedAction("s1-2", "Send the budget", "d", "e", action_type="work_item",
                                   fields={"deliverable": "Q4 budget"})
        plan = desktop_payload_builder(export, calendar=False).build(work_item, CORPORATE, {})
        self.assertEqual(plan["steps"][0]["connector"], "local_document")
        with self.assertRaises(UnsupportedWorkflow):  # not set up: nothing approved that could not be sent
            desktop_payload_builder(export, calendar=False).build(calendar_event(), CORPORATE, {})


class FailingAuth:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.is_connected = False

    def connected(self) -> bool:
        return self.is_connected

    def connect(self, wait: float) -> None:
        if self.error is not None:
            raise self.error
        self.is_connected = True

    def disconnect(self) -> bool:
        self.is_connected = False
        return True


class GoogleAccountTest(unittest.TestCase):
    def test_connect_and_disconnect(self) -> None:
        account = GoogleAccount(FailingAuth())
        self.assertEqual(account.connect(), {"available": True, "connected": True, "failed": None})
        self.assertEqual(account.disconnect(), {"available": True, "connected": False, "revoked": True})

    def test_failures_come_back_as_fixed_codes(self) -> None:
        for error, code in ((AuthorizationFailed("access_denied"), "access_denied"),
                            (AuthorizationFailed("timeout"), "timeout"),
                            (NotConfigured("x"), "not_configured"),
                            (GoogleUnreachable("x"), "unreachable"),
                            (GoogleError(500, "backendError"), "google_error")):
            with self.subTest(code):
                self.assertEqual(GoogleAccount(FailingAuth(error)).connect(),
                                 {"available": True, "connected": False, "failed": code})

    def test_one_sign_in_at_a_time(self) -> None:
        account = GoogleAccount(FailingAuth())
        account._connecting.acquire()  # a sign-in is waiting in the browser
        self.assertEqual(account.connect()["failed"], "already_connecting")

    def test_unavailable(self) -> None:
        account = GoogleAccount(None)
        self.assertEqual(account.connect()["failed"], "not_configured")
        self.assertEqual(account.disconnect(), {"available": False, "connected": False, "revoked": False})


class DemoTest(unittest.TestCase):
    def test_the_demo_has_no_google_account(self) -> None:
        bridge = Bridge(DemoBackend(InMemorySessionStore(), approved_by=USER))
        self.assertEqual(bridge.google_status(), {"ok": True, "data": {"available": False, "connected": False}})
        self.assertEqual(bridge.google_connect()["error"]["code"], "mode_unavailable")
        self.assertEqual(bridge.google_disconnect()["error"]["code"], "mode_unavailable")


class DesktopCalendarFlowTest(TempStore):
    """Preview, approve and Execute from the bridge, on the encrypted store (results and steps, #51)."""

    def setUp(self) -> None:
        super().setUp()
        self.timeline: list[str] = []
        self.google = FakeCalendar(self.timeline)
        self.addCleanup(self.google.close)
        self.auth = StubAuth()
        connector = GoogleCalendarConnector(self.auth, endpoints=self.google.endpoints, timeout=5)
        export = self.root / "export"
        processor = CentralProcessor(FakeLlmClient([]), self.store, identity=LocalUser(USER), clock=FixedClock(NOW),
                                     connectors=[LocalDocumentConnector(export), connector])

        @contextmanager
        def no_llm():
            yield None

        account = GoogleAccount(FailingAuth())
        backend = SessionBackend(self.store, approved_by=USER, processor=processor, export_dir=export,
                                 llm_session=no_llm, record_source=lambda: iter(()), clock=FixedClock(NOW),
                                 builder=desktop_payload_builder(export, calendar=True), google=account)
        self.bridge = Bridge(backend)
        transcript = self.store.save_transcript("s1", Transcript((Segment(0, 1, "Ne vedem marți la 10.", "S1"),), "ro"))
        self.store.save_session(Session(id="s1", started_at=NOW, domain="corporate", transcript_ref=transcript))
        self.store.save_actions("s1", [calendar_event()], expected_revision=0)

    def ok(self, result: dict) -> dict:
        self.assertTrue(result["ok"], result)
        return result["data"]

    def approve(self) -> dict:
        review = self.ok(self.bridge.review("s1"))
        review = self.ok(self.bridge.preview("s1", "s1-1", review["revision"]))
        [step] = review["actions"][0]["plan"]
        self.assertEqual((step["connector"], step["connector_label"]), ("google_calendar", "Google Calendar"))
        review = self.ok(self.bridge.approve("s1", "s1-1", review["revision"]))
        return review["actions"][0]["plan"][0]["payload"]

    def test_account_status_comes_through_the_bridge(self) -> None:
        self.assertEqual(self.ok(self.bridge.google_status()), {"available": True, "connected": False})
        self.assertEqual(self.ok(self.bridge.google_connect())["connected"], True)

    def test_execute_sends_the_approved_event_and_the_store_keeps_the_result(self) -> None:
        payload = self.approve()
        review = self.ok(self.bridge.execute("s1", "s1-1"))
        self.assertEqual(review["actions"][0]["status"], "executed")
        [(method, _, body, _)] = self.google.requests
        self.assertEqual((method, body), ("POST", payload["event"]))
        self.assertEqual([(r.connector, r.ok) for r in self.store.load_results()], [("google_calendar", True)])
        self.assertEqual(self.store.load_events()[0].payload, payload)
        self.store.close()
        again = self.open_store()  # the step states survive a restart
        self.assertEqual(again.load_steps("s1-1")["send_event"].state, StepState.DONE)

    def test_not_connected_fails_and_a_manual_retry_after_connecting_sends_once(self) -> None:
        self.approve()
        self.auth.connected = False
        review = self.ok(self.bridge.execute("s1", "s1-1"))
        action = review["actions"][0]
        self.assertEqual(action["status"], "failed")
        self.assertIn("NotConnected", action["error"])
        self.assertNotIn(SECRET, action["error"])
        self.assertEqual(self.google.requests, [])

        self.auth.connected = True
        self.google.lose_answer = True  # Google creates the event, the answer is lost
        review = self.ok(self.bridge.retry("s1", "s1-1"))
        self.assertEqual(review["actions"][0]["status"], "failed")
        review = self.ok(self.bridge.retry("s1", "s1-1"))
        self.assertEqual(review["actions"][0]["status"], "executed")
        self.assertEqual(len(self.google.events), 1)
        self.assertEqual([m for m, *_ in self.google.requests], ["POST", "POST", "GET"])
        self.assertEqual(ActionStatus.EXECUTED, self.store.load_actions("s1")[0].status)


if __name__ == "__main__":
    unittest.main()
