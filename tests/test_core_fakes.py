import unittest
from datetime import datetime

from norev.core.models import AuditEvent, ProposedAction, Segment, Session, Transcript
from norev.core.ports import AuditLog, Clock, LlmClient, SessionStore, Transcriber
from norev.testing.fakes import (
    FakeLlmClient,
    FakeTranscriber,
    FixedClock,
    InMemoryAuditLog,
    InMemorySessionStore,
)

AT = datetime(2026, 11, 6, 10, 0)
TRANSCRIPT = Transcript(segments=(Segment(0.0, 1.5, "Bună ziua", "S1"),), language="ro")


class FakesMatchPortsTest(unittest.TestCase):
    def test_each_fake_is_instance_of_its_port(self):
        self.assertIsInstance(FakeLlmClient([]), LlmClient)
        self.assertIsInstance(FakeTranscriber({}), Transcriber)
        self.assertIsInstance(FixedClock(AT), Clock)
        self.assertIsInstance(InMemorySessionStore(), SessionStore)
        self.assertIsInstance(InMemoryAuditLog(), AuditLog)


class FakeLlmClientTest(unittest.TestCase):
    def test_returns_responses_in_order_and_records_calls(self):
        llm = FakeLlmClient(['{"a": 1}', '{"b": 2}'])
        schema = {"type": "object"}
        self.assertEqual(llm.generate_json("p1"), '{"a": 1}')
        self.assertEqual(llm.generate_json("p2", model="m", json_schema=schema), '{"b": 2}')
        self.assertEqual(llm.calls, [("p1", None, None), ("p2", "m", schema)])

    def test_raises_when_out_of_responses(self):
        llm = FakeLlmClient(["{}"])
        llm.generate_json("p1")
        with self.assertRaisesRegex(AssertionError, "ran out"):
            llm.generate_json("p2")


class FakeTranscriberAndClockTest(unittest.TestCase):
    def test_transcribe_known_and_unknown_ref(self):
        transcriber = FakeTranscriber({"audio-1": TRANSCRIPT})
        self.assertEqual(transcriber.transcribe("audio-1", language="ro"), TRANSCRIPT)
        with self.assertRaisesRegex(KeyError, "audio-2"):
            transcriber.transcribe("audio-2")

    def test_fixed_clock(self):
        self.assertEqual(FixedClock(AT).now(), AT)


class InMemorySessionStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemorySessionStore()

    def test_session_round_trip_returns_copies(self):
        session = Session(id="s1", started_at=AT, domain="medical")
        self.store.save_session(session)
        session.domain = "changed after save"
        loaded = self.store.get_session("s1")
        self.assertEqual(loaded, Session(id="s1", started_at=AT, domain="medical"))
        loaded.domain = "changed after load"
        self.assertEqual(self.store.get_session("s1").domain, "medical")
        self.assertIsNone(self.store.get_session("missing"))

    def test_transcript_round_trip_with_opaque_ref(self):
        ref = self.store.save_transcript("s1", TRANSCRIPT)
        self.assertEqual(ref, "transcript-1")
        self.assertEqual(self.store.load_transcript(ref), TRANSCRIPT)
        self.assertNotEqual(self.store.save_transcript("s1", TRANSCRIPT), ref)
        with self.assertRaises(KeyError):
            self.store.load_transcript("transcript-99")

    def test_actions_round_trip_returns_copies(self):
        action = ProposedAction(id="a1", title="Trimitere", description="d", transcript_excerpt="e")
        self.store.save_actions("s1", [action])
        action.approve({"to": "after save"})
        loaded = self.store.load_actions("s1")
        self.assertEqual(loaded[0].status.value, "proposed")
        loaded[0].fields["x"] = 1
        loaded.append(action)
        self.assertEqual(self.store.load_actions("s1")[0].fields, {})
        self.assertEqual(len(self.store.load_actions("s1")), 1)
        self.assertEqual(self.store.load_actions("other"), [])


class InMemoryAuditLogTest(unittest.TestCase):
    def test_record_appends_events(self):
        log = InMemoryAuditLog()
        event = AuditEvent(at=AT, action_id="a1", connector="email", payload={"to": "x"}, approved_by="u1")
        log.record(event)
        self.assertEqual(log.events, [event])

    def test_recorded_event_does_not_follow_later_payload_changes(self):
        log = InMemoryAuditLog()
        payload = {"to": "x"}
        log.record(AuditEvent(at=AT, action_id="a1", connector="email", payload=payload, approved_by="u1"))
        payload["to"] = "changed"
        self.assertEqual(log.events[0].payload, {"to": "x"})


if __name__ == "__main__":
    unittest.main()
