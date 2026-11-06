import traceback
import unittest
from datetime import datetime, timezone

from norev.core.models import AuditEvent, ProposedAction
from norev.processor.central import (
    AuditFailed,
    CentralProcessor,
    ConnectorFailed,
    ExecutionAuditLog,
    SendRefused,
    SendResult,
)
from norev.processor.identity import LocalUser
from norev.testing.fakes import FakeLlmClient, FixedClock

AT = datetime(2026, 11, 6, 10, 0, tzinfo=timezone.utc)
SECRET = "Ion Popescu, CNP 1800101123456"  # sentinel: must never reach logs, errors or tracebacks
PAYLOAD = {"title": "Follow-up", "notes": SECRET, "items": [1, {"due": "2026-11-09"}], "urgent": True}


class RecordingAuditLog:
    """ExecutionAuditLog fake that writes into a shared timeline, to check the order of events."""

    def __init__(self, timeline: list, fail: bool = False) -> None:
        self.timeline = timeline
        self.fail = fail
        self.events: list[AuditEvent] = []
        self.results: list[SendResult] = []

    def record(self, event: AuditEvent) -> None:
        if self.fail:
            raise OSError(f"disk full while writing {event.payload}")  # an error that quotes the payload
        self.events.append(event)
        self.timeline.append("audit")

    def record_result(self, result: SendResult) -> None:
        self.results.append(result)
        self.timeline.append("ok" if result.ok else "failed")


class FakeConnector:
    def __init__(self, timeline: list, name: str = "local_document", error: Exception | None = None) -> None:
        self.name = name
        self.timeline = timeline
        self.error = error
        self.sent: list[dict] = []

    def send(self, payload: dict) -> dict:
        self.timeline.append("send")
        self.sent.append(payload)
        payload["title"] = "changed by the Connector"  # must not change the approved snapshot
        if self.error is not None:
            raise self.error
        return {"ref": "doc-1"}


def _approved(payload=None) -> ProposedAction:
    action = ProposedAction(id="s1-1", title="t", description="d", transcript_excerpt="e")
    action.approve(PAYLOAD if payload is None else payload)
    return action


class SendTest(unittest.TestCase):
    def setUp(self) -> None:
        self.timeline: list[str] = []
        self.audit = RecordingAuditLog(self.timeline)
        self.connector = FakeConnector(self.timeline)

    def _processor(self, audit=None, connector=None) -> CentralProcessor:
        return CentralProcessor(
            FakeLlmClient([]),
            audit or self.audit,
            identity=LocalUser("local:ana"),
            clock=FixedClock(AT),
            connectors=[connector or self.connector],
        )

    def test_fake_is_an_execution_audit_log(self) -> None:
        self.assertIsInstance(self.audit, ExecutionAuditLog)

    def test_identical_payload_is_sent_in_order_audit_send_result(self) -> None:
        action = _approved()
        answer = self._processor().send(action, "local_document", dict(reversed(PAYLOAD.items())))  # key order is irrelevant

        self.assertEqual(answer, {"ref": "doc-1"})
        self.assertEqual(self.timeline, ["audit", "send", "ok"])
        [event] = self.audit.events
        self.assertEqual(
            (event.at, event.action_id, event.connector, event.approved_by), (AT, "s1-1", "local_document", "local:ana")
        )
        self.assertEqual(event.payload, PAYLOAD)
        self.assertEqual(self.audit.results, [SendResult(AT, "s1-1", "local_document", ok=True)])
        self.assertEqual(action.approved_payload, PAYLOAD, "the Connector changed the approved snapshot")
        self.assertEqual(event.payload, PAYLOAD, "the Connector changed the audited payload")

    def test_does_not_change_the_action_status(self) -> None:
        action = _approved()
        self._processor().send(action, "local_document", PAYLOAD)
        self.assertEqual(action.status.value, "approved")  # the Workflow executor (#23) marks Executed

    def test_payload_different_from_the_snapshot_is_refused(self) -> None:
        changed = [
            {**PAYLOAD, "title": "Other"},
            {**PAYLOAD, "urgent": 1},  # equal in Python, not the same JSON
            {**PAYLOAD, "items": [1.0, {"due": "2026-11-09"}]},
            {**PAYLOAD, "items": [{"due": "2026-11-09"}, 1]},
            {**PAYLOAD, "extra": None},
            {k: v for k, v in PAYLOAD.items() if k != "notes"},
            {**PAYLOAD, "when": datetime(2026, 11, 9)},  # not JSON
        ]
        for payload in changed:
            with self.subTest(payload=sorted(payload)):
                with self.assertRaises(SendRefused) as raised:
                    self._processor().send(_approved(), "local_document", payload)
                self.assertNotIn(SECRET, str(raised.exception))
        self.assertEqual(self.timeline, [], "a refused send reached the audit log or the Connector")

    def test_action_that_is_not_approved_is_refused(self) -> None:
        proposed = ProposedAction(id="s1-1", title="t", description="d", transcript_excerpt="e")
        rejected = ProposedAction(id="s1-2", title="t", description="d", transcript_excerpt="e")
        rejected.reject()
        executed, failed = _approved(), _approved()
        executed.mark_executed()
        failed.mark_failed("timeout")  # a Retry must move it back to Approved first
        for action in (proposed, rejected, executed, failed):
            with self.subTest(status=action.status.value):
                with self.assertRaises(SendRefused):
                    self._processor().send(action, "local_document", PAYLOAD)
        self.assertEqual(self.timeline, [])

    def test_a_step_sends_only_its_fields_of_the_snapshot(self) -> None:
        processor = self._processor()
        processor.send(_approved(), "local_document", {"urgent": True, "title": "Follow-up"}, fields=("title", "urgent"))
        self.assertEqual(self.connector.sent[0], {"title": "changed by the Connector", "urgent": True})  # (changed after the send)
        self.assertEqual(self.audit.events[0].payload, {"title": "Follow-up", "urgent": True})
        for payload, fields in (
            ({"title": "Other"}, ("title",)),
            ({"title": "Follow-up"}, ("title", "urgent")),  # field left out
            ({"title": "Follow-up", "missing": None}, ("title", "missing")),  # not in the snapshot
            ({}, ()),
        ):
            with self.subTest(fields=fields), self.assertRaises(SendRefused):
                processor.send(_approved(), "local_document", payload, fields=fields)
        self.assertEqual(len(self.connector.sent), 1)

    def test_unknown_connector_is_refused(self) -> None:
        with self.assertRaises(SendRefused):
            self._processor().send(_approved(), "google_calendar", PAYLOAD)
        self.assertEqual(self.timeline, [])

    def test_two_connectors_with_one_name_are_refused(self) -> None:
        with self.assertRaises(ValueError):
            CentralProcessor(
                FakeLlmClient([]), self.audit, identity=LocalUser("u"), clock=FixedClock(AT),
                connectors=[FakeConnector([]), FakeConnector([])],
            )

    def test_audit_failure_means_no_send(self) -> None:
        processor = self._processor(audit=RecordingAuditLog(self.timeline, fail=True))
        with self.assertLogs("norev.processor", "ERROR") as logs:
            with self.assertRaises(AuditFailed) as raised:
                processor.send(_approved(), "local_document", PAYLOAD)
        self.assertEqual(self.timeline, [])
        self.assertEqual(self.connector.sent, [])
        self._assert_no_secret(raised.exception, logs.output)

    def test_connector_failure_is_recorded_without_its_text(self) -> None:
        connector = FakeConnector(self.timeline, error=RuntimeError(f"rejected {SECRET}"))
        with self.assertLogs("norev.processor", "WARNING") as logs:
            with self.assertRaises(ConnectorFailed) as raised:
                self._processor(connector=connector).send(_approved(), "local_document", PAYLOAD)
        self.assertEqual(self.timeline, ["audit", "send", "failed"])
        self.assertEqual(self.audit.results, [SendResult(AT, "s1-1", "local_document", ok=False, error_type="RuntimeError")])
        self.assertIs(raised.exception.cause, connector.error)  # still there for the executor
        self._assert_no_secret(raised.exception, logs.output)

    def test_success_log_has_no_payload_text(self) -> None:
        with self.assertLogs("norev.processor", "INFO") as logs:
            self._processor().send(_approved(), "local_document", PAYLOAD)
        self.assertNotIn(SECRET, "\n".join(logs.output))

    def test_result_not_recorded_leaves_an_uncertain_attempt(self) -> None:
        class NoResults(RecordingAuditLog):
            def record_result(self, result: SendResult) -> None:
                raise OSError("disk full")

        audit = NoResults(self.timeline)
        with self.assertRaises(OSError) as raised:
            self._processor(audit=audit).send(_approved(), "local_document", PAYLOAD)
        self.assertEqual(self.timeline, ["audit", "send"])  # attempt with no result: shown for reconciliation (#23)
        self.assertIsNone(raised.exception.__context__)

    def _assert_no_secret(self, exc: BaseException, log_lines: list[str]) -> None:
        text = "".join(traceback.format_exception(exc)) + "\n".join(log_lines)
        self.assertNotIn(SECRET, text)


class LocalUserTest(unittest.TestCase):
    def test_valid_ids(self) -> None:
        for value in ("local:ana", "Ana Pop", "x" * 128):
            self.assertEqual(LocalUser(value).id, value)

    def test_invalid_ids(self) -> None:
        for value in ("", " ana", "ana\n", "ana\x00", "x" * 129, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                LocalUser(value)

    def test_from_os_is_a_valid_local_id(self) -> None:
        self.assertTrue(LocalUser.from_os().id.startswith("local:"))


if __name__ == "__main__":
    unittest.main()
