"""GoogleCalendarConnector.send (#45) against a fake Calendar API on 127.0.0.1: no network."""
import copy
import json
import threading
import unittest
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from norev.adapters.connectors.google_calendar import (
    EventConflict,
    GoogleCalendarConnector,
    GoogleEndpoints,
    GoogleError,
    GoogleUnreachable,
    NotConnected,
)
from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.core.models import ActionStatus, ProposedAction, Session
from norev.core.ports import Connector
from norev.processor.central import CentralProcessor, SendRefused
from norev.processor.identity import LocalUser
from norev.processor.payloads import GoogleCalendarPayloadBuilder
from norev.processor.workflow import WorkflowExecutor
from norev.testing.fakes import FakeLlmClient, FixedClock, InMemorySessionStore
from norev.usecases.approve import approve_action, preview_action
from test_processor_send import RecordingAuditLog
from test_processor_workflow import InMemoryExecutionStore

AT = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
SECRET = "Ion Popescu, CNP 1800101123456"
CORPORATE = load_domain(DOMAINS / "corporate")
EVENT = {
    "id": "0123456789abcdef0123456789abcdef",
    "summary": "Offer review",
    "description": f"Review the offer with {SECRET}.",
    "start": {"dateTime": "2026-11-10T10:00:00+02:00", "timeZone": "Europe/Bucharest"},
    "end": {"dateTime": "2026-11-10T11:00:00+02:00", "timeZone": "Europe/Bucharest"},
    "location": "room 2",
}
PAYLOAD = {"calendar_id": "primary", "event": EVENT}


class FakeCalendar:
    """events.insert and events.get. `fail_next` gives the next calls an error status; with
    `lose_answer`, an insert creates the event and then answers 503, as if the answer was lost."""

    def __init__(self, timeline: list[str]) -> None:
        self.timeline = timeline
        self.events: dict[tuple[str, str], dict] = {}
        self.requests: list[tuple[str, str, dict | None, str]] = []  # method, path+query, body, token
        self.fail_next: list[tuple[int, dict]] = []
        self.lose_answer = False
        self.valid_tokens = {"access-1", "access-2"}
        calendar = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _handle(self, method: str) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                token = self.headers.get("Authorization", "").removeprefix("Bearer ")
                calendar.requests.append((method, self.path, body, token))
                calendar.timeline.append(method)
                self._answer(*calendar.answer(method, self.path, body, token))

            def do_POST(self) -> None:
                self._handle("POST")

            def do_GET(self) -> None:
                self._handle("GET")

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.endpoints = GoogleEndpoints(auth=base + "/auth", token=base + "/token", revoke=base + "/revoke",
                                         api=base + "/calendar/v3")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, method: str, path: str, body: dict | None, token: str) -> tuple[int, dict]:
        if token not in self.valid_tokens:
            return 401, {"error": {"code": 401, "errors": [{"reason": "authError"}]}}
        if self.fail_next:
            return self.fail_next.pop(0)
        parts = urllib.parse.urlsplit(path)
        segments = [urllib.parse.unquote(s) for s in parts.path.split("/")]
        # /calendar/v3/calendars/<calendar>/events[/<id>]
        calendar_id = segments[4]
        if method == "POST":
            key = (calendar_id, body["id"])
            if key in self.events:
                return 409, {"error": {"code": 409, "errors": [{"reason": "duplicate"}],
                                       "message": "The requested identifier already exists."}}
            self.events[key] = {**body, "status": "confirmed",
                                "htmlLink": f"https://www.google.com/calendar/event?eid={body['id']}"}
            if self.lose_answer:
                self.lose_answer = False
                return 503, {"error": {"code": 503, "errors": [{"reason": "backendError"}]}}
            return 200, self.events[key]
        event = self.events.get((calendar_id, segments[6]))
        return (200, event) if event else (404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}})


class StubAuth:
    """GoogleAuth as the Connector sees it: a token, and forgetting it after a 401."""

    def __init__(self, connected: bool = True) -> None:
        self.connected = connected
        self.issued = 1
        self.forgotten = 0

    def access_token(self) -> str:
        if not self.connected:
            raise NotConnected("no Google account connected")
        return f"access-{self.issued}"

    def forget_access_token(self) -> None:
        self.forgotten += 1
        self.issued += 1


class GoogleCalendarConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.timeline: list[str] = []
        self.google = FakeCalendar(self.timeline)
        self.addCleanup(self.google.close)
        self.auth = StubAuth()
        self.connector = GoogleCalendarConnector(self.auth, endpoints=self.google.endpoints, timeout=5)

    def test_sends_exactly_the_event_without_invitations(self) -> None:
        self.assertIsInstance(self.connector, Connector)
        result = self.connector.send(copy.deepcopy(PAYLOAD))
        self.assertEqual(result, {"event_id": EVENT["id"], "created": True,
                                  "link": f"https://www.google.com/calendar/event?eid={EVENT['id']}"})
        [(method, path, body, token)] = self.google.requests
        self.assertEqual((method, path, token), ("POST", "/calendar/v3/calendars/primary/events?sendUpdates=none", "access-1"))
        self.assertEqual(body, EVENT)

    def test_calendar_id_is_one_path_segment(self) -> None:
        self.connector.send({"calendar_id": "team/../x@group.calendar.google.com", "event": EVENT})
        self.assertEqual(self.google.requests[0][1],
                         "/calendar/v3/calendars/team%2F..%2Fx%40group.calendar.google.com/events?sendUpdates=none")

    def test_sending_the_same_payload_again_creates_no_second_event(self) -> None:
        self.connector.send(PAYLOAD)
        again = self.connector.send(PAYLOAD)
        self.assertEqual((again["event_id"], again["created"]), (EVENT["id"], False))
        self.assertEqual(len(self.google.events), 1)
        self.assertEqual([m for m, *_ in self.google.requests], ["POST", "POST", "GET"])

    def test_retry_after_a_lost_answer_finds_the_event(self) -> None:
        self.google.lose_answer = True
        with self.assertRaises(GoogleError) as caught:
            self.connector.send(PAYLOAD)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(self.google.events), 1)  # Google did create it
        self.assertFalse(self.connector.send(PAYLOAD)["created"])
        self.assertEqual(len(self.google.events), 1)

    def test_an_event_deleted_after_creation_is_a_conflict(self) -> None:
        self.connector.send(PAYLOAD)
        self.google.events[("primary", EVENT["id"])]["status"] = "cancelled"
        with self.assertRaises(EventConflict):
            self.connector.send(PAYLOAD)

    def test_expired_access_token_is_refreshed_once(self) -> None:
        self.google.valid_tokens = {"access-2"}
        self.assertTrue(self.connector.send(PAYLOAD)["created"])
        self.assertEqual([t for *_, t in self.google.requests], ["access-1", "access-2"])
        self.google.valid_tokens = set()
        with self.assertRaises(GoogleError) as caught:
            self.connector.send(PAYLOAD)
        self.assertEqual(caught.exception.status, 401)
        self.assertEqual(self.auth.forgotten, 2)  # once per send, never in a loop

    def test_google_errors_keep_only_the_status_and_reason(self) -> None:
        for status, reason in ((403, "forbidden"), (429, "rateLimitExceeded"), (500, "backendError")):
            with self.subTest(status):
                self.google.fail_next = [(status, {"error": {"code": status, "errors": [{"reason": reason}],
                                                             "message": f"bad event {SECRET}"}})]
                with self.assertRaises(GoogleError) as caught:
                    self.connector.send(PAYLOAD)
                self.assertEqual((caught.exception.status, caught.exception.error), (status, reason))
                self.assertNotIn(SECRET, str(caught.exception))

    def test_an_answer_for_another_event_is_an_error(self) -> None:
        self.google.fail_next = [(200, {"id": "someotherevent", "status": "confirmed"})]
        with self.assertRaises(GoogleError) as caught:
            self.connector.send(PAYLOAD)
        self.assertEqual(caught.exception.error, "unexpected_event_id")

    def test_not_connected_sends_nothing(self) -> None:
        self.auth.connected = False
        with self.assertRaises(NotConnected):
            self.connector.send(PAYLOAD)
        self.assertEqual(self.google.requests, [])

    def test_unreachable_google(self) -> None:
        self.google.close()
        with self.assertRaises(GoogleUnreachable):
            self.connector.send(PAYLOAD)

    def test_payloads_not_shaped_like_the_builders_are_refused_without_a_call(self) -> None:
        bad = [
            None,
            {"event": EVENT},
            {**PAYLOAD, "extra": 1},
            {"calendar_id": "", "event": EVENT},
            {"calendar_id": "primary", "event": {**EVENT, "recurrence": ["RRULE:FREQ=DAILY"]}},
            {"calendar_id": "primary", "event": {k: v for k, v in EVENT.items() if k != "id"}},
            {"calendar_id": "primary", "event": {**EVENT, "id": "UPPER0123"}},  # not base32hex
            {"calendar_id": "primary", "event": {**EVENT, "id": "a_b-c"}},
            {"calendar_id": "primary", "event": {**EVENT, "id": "abc"}},  # too short
        ]
        for payload in bad:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                self.connector.send(payload)
        self.assertEqual(self.google.requests, [])

    def test_repr_shows_nothing(self) -> None:
        self.assertEqual(repr(self.connector), "GoogleCalendarConnector()")


class CalendarWorkflowTest(unittest.TestCase):
    """A Corporate calendar_event approved through #18's use cases and #44's builder, executed by #23's
    executor through the Central Processor (#22) to the fake Google Calendar."""

    def setUp(self) -> None:
        self.timeline: list[str] = []
        self.google = FakeCalendar(self.timeline)
        self.addCleanup(self.google.close)
        self.audit = RecordingAuditLog(self.timeline)
        connector = GoogleCalendarConnector(StubAuth(), endpoints=self.google.endpoints, timeout=5)
        self.processor = CentralProcessor(FakeLlmClient([]), self.audit, identity=LocalUser("local:processor"),
                                          clock=FixedClock(AT), connectors=[connector])
        self.sessions = InMemorySessionStore()
        self.steps = InMemoryExecutionStore()
        self.sessions.save_session(Session(id="s1", started_at=AT, domain="corporate"))
        action = ProposedAction("s1-1", "Offer review", f"Review the offer with {SECRET}.", "Let's meet on Tuesday",
                                action_type="calendar_event",
                                fields={"event_date": "2026-11-10", "start_time": "10:00", "end_time": "11:00",
                                        "location": "room 2", "attendees": "ana@example.com"})
        revision = self.sessions.save_actions("s1", [action], expected_revision=0)
        snapshot = preview_action("s1", "s1-1", {}, expected_revision=revision, store=self.sessions, domain=CORPORATE,
                                  builder=GoogleCalendarPayloadBuilder("Europe/Bucharest"))
        approve_action("s1", "s1-1", expected_revision=snapshot.revision, store=self.sessions, domain=CORPORATE,
                       clock=FixedClock(AT), approved_by="local:ana")
        [step] = self.action().approved_payload["steps"]
        self.approved_event = step["payload"]["event"]
        # shortcut: build_event and save_result are not implemented by the app yet; stand-ins here
        local = {"build_event": lambda a: None, "save_result": lambda a: None}
        self.executor = WorkflowExecutor(self.processor, self.sessions, self.steps, CORPORATE, local)

    def action(self) -> ProposedAction:
        return self.sessions.load_actions("s1")[0]

    def test_the_approved_event_is_audited_then_sent_exactly(self) -> None:
        outcome = self.executor.execute("s1", "s1-1")
        self.assertEqual(outcome.status, "executed")
        self.assertEqual(self.action().status, ActionStatus.EXECUTED)
        self.assertEqual(self.timeline, ["audit", "POST", "ok"])
        [(_, _, body, _)] = self.google.requests
        self.assertEqual(body, self.approved_event)
        self.assertEqual(body["attendees"], [{"email": "ana@example.com"}])
        self.assertEqual(self.audit.events[0].payload["event"], self.approved_event)
        self.assertEqual(self.audit.events[0].approved_by, "local:ana")
        self.assertEqual(outcome.steps["send_event"].result["event_id"], self.approved_event["id"])

    def test_a_changed_event_is_refused_before_any_call(self) -> None:
        changed = {"calendar_id": "primary", "event": {**self.approved_event, "summary": "Something else"}}
        with self.assertRaises(SendRefused):
            self.processor.send(self.action(), "google_calendar", changed, step="send_event")
        self.assertEqual(self.timeline, [])

    def test_manual_retry_after_a_lost_answer_creates_one_event(self) -> None:
        self.google.lose_answer = True
        outcome = self.executor.execute("s1", "s1-1")
        self.assertEqual(outcome.status, "failed")
        action = self.action()
        self.assertEqual(action.status, ActionStatus.FAILED)
        self.assertEqual(action.error, "Step Trimite în Google Calendar failed (GoogleError).")
        self.assertNotIn(SECRET, action.error)

        self.executor.retry("s1", "s1-1")  # manual (OPEN-QUESTIONS #7)
        outcome = self.executor.execute("s1", "s1-1")
        self.assertEqual(outcome.status, "executed")
        self.assertFalse(outcome.steps["send_event"].result["created"])
        self.assertEqual(len(self.google.events), 1)
        self.assertEqual(self.timeline, ["audit", "POST", "failed", "audit", "POST", "GET", "ok"])


if __name__ == "__main__":
    unittest.main()
