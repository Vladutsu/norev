import unittest
from datetime import datetime

from norev.core.models import (
    ActionStatus,
    AuditEvent,
    InvalidTransition,
    ProposedAction,
    Segment,
    Suggestion,
    Transcript,
)

SECRET = "PATIENT_SECRET"


def action(**overrides) -> ProposedAction:
    values = dict(id="a1", title="Schedule visit", description="d", transcript_excerpt="e")
    values.update(overrides)
    return ProposedAction(**values)


def in_status(status: ActionStatus) -> ProposedAction:
    a = action()
    if status is ActionStatus.REJECTED:
        a.reject()
    elif status is not ActionStatus.PROPOSED:
        a.approve({"title": "t"})
        if status is ActionStatus.EXECUTED:
            a.mark_executed()
        elif status is ActionStatus.FAILED:
            a.mark_failed("timeout")
    return a


class ValidTransitionsTest(unittest.TestCase):
    def test_new_action_is_proposed(self):
        self.assertIs(action().status, ActionStatus.PROPOSED)

    def test_proposed_to_approved(self):
        a = action()
        a.approve({"title": "t"})
        self.assertIs(a.status, ActionStatus.APPROVED)

    def test_proposed_to_rejected(self):
        a = action()
        a.reject()
        self.assertIs(a.status, ActionStatus.REJECTED)

    def test_approved_to_executed(self):
        a = in_status(ActionStatus.APPROVED)
        a.mark_executed()
        self.assertIs(a.status, ActionStatus.EXECUTED)

    def test_approved_to_failed_stores_error(self):
        a = in_status(ActionStatus.APPROVED)
        a.mark_failed("calendar unreachable")
        self.assertIs(a.status, ActionStatus.FAILED)
        self.assertEqual(a.error, "calendar unreachable")

    def test_retry_moves_failed_back_to_approved_with_same_payload_and_no_error(self):
        a = in_status(ActionStatus.FAILED)
        payload_before = a.approved_payload
        a.retry()
        self.assertIs(a.status, ActionStatus.APPROVED)
        self.assertEqual(a.approved_payload, {"title": "t"})
        self.assertIs(a.approved_payload, payload_before)
        self.assertIsNone(a.error)

    def test_retried_action_can_be_executed(self):
        a = in_status(ActionStatus.FAILED)
        a.retry()
        a.mark_executed()
        self.assertIs(a.status, ActionStatus.EXECUTED)


class InvalidTransitionsTest(unittest.TestCase):
    MOVES = {
        "approve": lambda a: a.approve({"title": "other"}),
        "reject": lambda a: a.reject(),
        "mark_executed": lambda a: a.mark_executed(),
        "mark_failed": lambda a: a.mark_failed("boom"),
        "retry": lambda a: a.retry(),
    }
    INVALID = {
        ActionStatus.PROPOSED: ["mark_executed", "mark_failed", "retry"],
        ActionStatus.APPROVED: ["approve", "reject", "retry"],
        ActionStatus.REJECTED: ["approve", "reject", "mark_executed", "mark_failed", "retry"],
        ActionStatus.EXECUTED: ["approve", "reject", "mark_executed", "mark_failed", "retry"],
        ActionStatus.FAILED: ["approve", "reject", "mark_executed", "mark_failed"],
    }

    def test_invalid_moves_raise_and_leave_action_unchanged(self):
        for status, moves in self.INVALID.items():
            for move in moves:
                with self.subTest(status=status.value, move=move):
                    a = in_status(status)
                    before = (a.status, a.approved_payload, a.error)
                    with self.assertRaises(InvalidTransition):
                        self.MOVES[move](a)
                    self.assertEqual((a.status, a.approved_payload, a.error), before)

    def test_invalid_transition_is_a_value_error(self):
        with self.assertRaises(ValueError):
            action().mark_executed()


class ApprovedPayloadTest(unittest.TestCase):
    def test_approve_keeps_a_deep_copy_of_the_payload(self):
        payload = {"title": "Visit", "attendees": ["a@x.ro"], "extra": {"room": "1"}}
        a = action()
        a.approve(payload)
        payload["title"] = "changed"
        payload["attendees"].append("b@x.ro")
        payload["extra"]["room"] = "2"
        self.assertEqual(a.approved_payload, {"title": "Visit", "attendees": ["a@x.ro"], "extra": {"room": "1"}})

    def test_proposed_action_has_no_approved_payload_or_error(self):
        a = action()
        self.assertIsNone(a.approved_payload)
        self.assertIsNone(a.error)


class ReprHidesContentTest(unittest.TestCase):
    def assert_hidden(self, obj, *expected):
        text = repr(obj)
        self.assertNotIn(SECRET, text)
        for part in expected:
            self.assertIn(part, text)
        self.assertNotIn(SECRET, str(obj))

    def test_segment(self):
        self.assert_hidden(Segment(0.0, 1.5, SECRET, speaker_id="S1"), "S1", f"text_len={len(SECRET)}")

    def test_transcript(self):
        t = Transcript((Segment(0, 1, SECRET), Segment(1, 2, SECRET)), language="ro")
        self.assert_hidden(t, "segments=2", f"text_len={len(t.text)}")

    def test_proposed_action(self):
        a = action(id="act-7", title=SECRET, description=SECRET, transcript_excerpt=SECRET,
                   owner=SECRET, fields={"dose": SECRET})
        a.approve({"title": SECRET})
        a.mark_failed(SECRET)
        self.assert_hidden(a, "act-7", f"title_len={len(SECRET)}", "dose")

    def test_suggestion(self):
        self.assert_hidden(Suggestion(SECRET, transcript_excerpt=SECRET), f"text_len={len(SECRET)}")

    def test_audit_event(self):
        e = AuditEvent(datetime(2026, 1, 2, 3, 4), "act-7", "google_calendar", {"title": SECRET}, "user")
        self.assert_hidden(e, "act-7", "google_calendar", "title")


class ValidationTest(unittest.TestCase):
    def test_segment_accepts_zero_length_at_zero(self):
        self.assertEqual(Segment(0, 0, "").end, 0)

    def test_segment_rejects_negative_start(self):
        with self.assertRaises(ValueError):
            Segment(-0.1, 1, "x")

    def test_segment_rejects_end_before_start(self):
        with self.assertRaises(ValueError):
            Segment(2, 1, "x")


if __name__ == "__main__":
    unittest.main()
