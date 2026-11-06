"""GoogleCalendarPayloadBuilder (#44): the exact events.insert payload the User approves. No network."""
import dataclasses
import os
import unittest
from datetime import datetime, timezone
from unittest import mock

from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.core.models import ActionStatus, InvalidTransition, ProposedAction, Session, UnsupportedWorkflow, ValidationError
from norev.processor.payloads import (
    ConnectorPayloadBuilder, GoogleCalendarPayloadBuilder, LocalDocumentPayloadBuilder,
)
from norev.testing.fakes import FixedClock, InMemorySessionStore
from norev.usecases.approve import approve_action, edit_action, preview_action

CORPORATE = load_domain(DOMAINS / "corporate")
MEDICAL = load_domain(DOMAINS / "medical")


def meeting(**fields) -> ProposedAction:
    values = {"event_date": "2026-11-10", "start_time": "10:00", "end_time": "11:00",
              "location": "room 2", "attendees": None} | fields
    return ProposedAction("c1", "Offer review", "Review the offer.", "Let's meet on Tuesday at 10",
                          action_type="calendar_event", fields=values)


def builder() -> GoogleCalendarPayloadBuilder:
    return GoogleCalendarPayloadBuilder("Europe/Bucharest", new_id=lambda: "0123456789abcdef")


def event_of(plan: dict) -> dict:
    [step] = plan["steps"]
    return step["payload"]["event"]


class CalendarPayloadTest(unittest.TestCase):
    def test_exact_payload(self):
        plan = builder().build(meeting(attendees="ana@example.com, Bob@Example.com ,ana@example.com"), CORPORATE, {})
        [step] = plan["steps"]
        self.assertEqual((step["step"], step["connector"]), ("send_event", "google_calendar"))
        self.assertEqual(step["payload"], {"calendar_id": "primary", "event": {
            "id": "0123456789abcdef",
            "summary": "Offer review",
            "description": "Review the offer.",
            "start": {"dateTime": "2026-11-10T10:00:00+02:00", "timeZone": "Europe/Bucharest"},
            "end": {"dateTime": "2026-11-10T11:00:00+02:00", "timeZone": "Europe/Bucharest"},
            "location": "room 2",
            "attendees": [{"email": "ana@example.com"}, {"email": "Bob@Example.com"}],
        }})

    def test_no_attendees_unless_the_user_typed_them(self):
        self.assertNotIn("attendees", event_of(builder().build(meeting(), CORPORATE, {})))

    def test_a_speaker_label_is_not_an_email(self):
        with self.assertRaises(ValidationError) as caught:
            builder().build(meeting(attendees="S1"), CORPORATE, {})
        self.assertEqual(caught.exception.code, "invalid_email")

    def test_summer_time_offset(self):
        event = event_of(builder().build(meeting(event_date="2026-07-01"), CORPORATE, {}))
        self.assertEqual(event["start"]["dateTime"], "2026-07-01T10:00:00+03:00")

    def test_day_of_the_spring_clock_change(self):
        # 29 March 2026: 03:00 local becomes 04:00, so 02:30 is still +02:00 and 04:30 is +03:00.
        event = event_of(builder().build(meeting(event_date="2026-03-29", start_time="02:30", end_time="04:30"),
                                         CORPORATE, {}))
        self.assertEqual((event["start"]["dateTime"], event["end"]["dateTime"]),
                         ("2026-03-29T02:30:00+02:00", "2026-03-29T04:30:00+03:00"))

    def test_a_time_skipped_by_the_spring_change_is_refused(self):
        with self.assertRaises(ValidationError) as caught:
            builder().build(meeting(event_date="2026-03-29", start_time="03:30", end_time="05:00"), CORPORATE, {})
        self.assertEqual((caught.exception.code, caught.exception.field), ("nonexistent_time", "start_time"))

    def test_day_of_the_autumn_clock_change(self):
        # 25 October 2026: 04:00 local goes back to 03:00; the repeated hour takes its first (summer) offset.
        event = event_of(builder().build(meeting(event_date="2026-10-25", start_time="03:30", end_time="05:00"),
                                         CORPORATE, {}))
        self.assertEqual((event["start"]["dateTime"], event["end"]["dateTime"]),
                         ("2026-10-25T03:30:00+03:00", "2026-10-25T05:00:00+02:00"))

    def test_end_must_be_after_start(self):
        with self.assertRaises(ValidationError) as caught:
            builder().build(meeting(end_time="09:00"), CORPORATE, {})
        self.assertEqual(caught.exception.code, "end_before_start")

    def test_options_are_refused(self):
        with self.assertRaises(ValidationError):
            builder().build(meeting(), CORPORATE, {"send_event": {"calendar_id": "someone-else"}})

    def test_medical_visit_puts_the_reason_in_the_description(self):
        visit = ProposedAction("v1", "Blood tests", "", "", action_type="schedule_visit", fields={
            "reason": "Cholesterol", "visit_date": "2026-11-13", "visit_time": "09:00",
            "visit_end_time": "09:30", "location": None})
        event = event_of(builder().build(visit, MEDICAL, {}))
        self.assertEqual((event["summary"], event["description"]), ("Blood tests", "Motiv: Cholesterol"))
        self.assertNotIn("location", event)

    def test_a_field_an_event_cannot_hold_is_refused_not_dropped(self):
        step = CORPORATE.action_type("calendar_event").workflow.steps[1]
        leaky = dataclasses.replace(step, sends=step.sends + ("transcript_excerpt",))
        workflow = dataclasses.replace(CORPORATE.action_type("calendar_event").workflow, steps=(leaky,))
        action_type = dataclasses.replace(CORPORATE.action_type("calendar_event"), workflow=workflow)
        domain = dataclasses.replace(CORPORATE, action_types=(action_type,))
        with self.assertRaises(UnsupportedWorkflow):
            builder().build(meeting(), domain, {})

    def test_recurring_reminders_are_out_of_scope(self):
        pill = ProposedAction("p1", "Amlodipine", "", "", action_type="medication_reminder", fields={})
        with self.assertRaises(UnsupportedWorkflow):
            builder().build(pill, MEDICAL, {})

    def test_time_zone_from_the_environment(self):
        with mock.patch.dict(os.environ, {"NOREV_TIMEZONE": "Europe/London"}):
            event = event_of(GoogleCalendarPayloadBuilder.from_environment().build(meeting(), CORPORATE, {}))
        self.assertEqual(event["start"], {"dateTime": "2026-11-10T10:00:00+00:00", "timeZone": "Europe/London"})

    def test_each_preview_gets_a_new_event_id(self):
        real = GoogleCalendarPayloadBuilder()
        first, second = (event_of(real.build(meeting(), CORPORATE, {}))["id"] for _ in range(2))
        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^[0-9a-v]{5,1024}$")  # Google's event id alphabet (base32hex)


class ConnectorPayloadBuilderTest(unittest.TestCase):
    def test_picks_the_builder_of_the_workflow_connector(self):
        router = ConnectorPayloadBuilder({"google_calendar": builder(),
                                          "local_document": LocalDocumentPayloadBuilder(os.path.abspath("unused"))})
        self.assertEqual(router.build(meeting(), CORPORATE, {})["steps"][0]["connector"], "google_calendar")
        work = ProposedAction("w1", "Send report", "", "", action_type="work_item", fields={"deliverable": None})
        self.assertEqual(router.build(work, CORPORATE, {})["steps"][0]["connector"], "local_document")

    def test_unknown_connector_is_refused(self):
        with self.assertRaises(UnsupportedWorkflow):
            ConnectorPayloadBuilder({}).build(meeting(), CORPORATE, {})


class CalendarApprovalTest(unittest.TestCase):
    """Through the approval use cases: a missing time blocks it, an edit needs a new preview and approval."""

    def setUp(self):
        self.store = InMemorySessionStore()
        self.store.save_session(Session(id="m1", started_at=datetime(2026, 11, 6, 10, 0), domain="corporate"))
        self.store.save_actions("m1", [meeting(start_time=None)], expected_revision=0)
        self.kw = {"store": self.store, "domain": CORPORATE}

    def revision(self) -> int:
        return self.store.load_actions_snapshot("m1").revision

    def test_missing_time_blocks_preview_until_the_user_fills_it(self):
        with self.assertRaises(ValidationError) as caught:
            preview_action("m1", "c1", {}, expected_revision=self.revision(), builder=builder(), **self.kw)
        self.assertEqual((caught.exception.code, caught.exception.field), ("required", "start_time"))
        edit_action("m1", "c1", {"fields": {"start_time": "10:00"}}, expected_revision=self.revision(), **self.kw)
        preview_action("m1", "c1", {}, expected_revision=self.revision(), builder=builder(), **self.kw)

    def test_approval_keeps_exactly_the_previewed_payload_and_an_edit_needs_a_new_preview(self):
        edit_action("m1", "c1", {"fields": {"start_time": "10:00"}}, expected_revision=self.revision(), **self.kw)
        preview_action("m1", "c1", {}, expected_revision=self.revision(), builder=builder(), **self.kw)
        edit_action("m1", "c1", {"fields": {"end_time": "12:00"}}, expected_revision=self.revision(), **self.kw)
        clock = FixedClock(datetime(2026, 11, 6, 11, 0, tzinfo=timezone.utc))
        with self.assertRaises(ValidationError) as caught:
            approve_action("m1", "c1", expected_revision=self.revision(), clock=clock, approved_by="u", **self.kw)
        self.assertEqual(caught.exception.code, "preview_required")
        preview_action("m1", "c1", {}, expected_revision=self.revision(), builder=builder(), **self.kw)
        shown = self.store.load_actions("m1")[0].prepared_payload
        approve_action("m1", "c1", expected_revision=self.revision(), clock=clock, approved_by="u", **self.kw)
        [approved] = self.store.load_actions("m1")
        self.assertEqual(approved.status, ActionStatus.APPROVED)
        self.assertEqual(approved.approved_payload, shown)
        self.assertEqual(event_of(shown)["end"]["dateTime"], "2026-11-10T12:00:00+02:00")
        with self.assertRaises(InvalidTransition):  # an approved payload is never edited in place
            edit_action("m1", "c1", {"fields": {"end_time": "13:00"}}, expected_revision=self.revision(), **self.kw)


if __name__ == "__main__":
    unittest.main()
