"""The corporate Domain (issue #20): loads like any Domain and its Domain stage keeps only work items.

Synthetic meeting, handwritten model answer; no model is run.
"""
import json
import unittest
from datetime import date, datetime

from norev.adapters.domains.loader import DOMAINS, load_domain
from norev.core.models import ActionStatus, ProposedAction, Segment, Session, Transcript
from norev.testing.fakes import FakeLlmClient, InMemorySessionStore
from norev.usecases.domain_stage import run_domain_stage

TRANSCRIPT = Transcript(
    segments=(
        Segment(0.0, 4.0, "Elena, can you send the Q3 report draft to the client by Friday?", "S1"),
        Segment(4.0, 5.0, "Yes, I'll send it.", "S2"),
        Segment(5.0, 9.0, "Ionuț was supposed to fix the login bug, but Elena takes it over.", "S1"),
        Segment(9.0, 11.0, "Mihai will check the server costs.", "S1"),
        Segment(11.0, 14.0, "We planned a newsletter, but we won't do the newsletter anymore.", "S1"),
        Segment(14.0, 16.0, "Maybe we could try a new CRM someday.", "S2"),
    ),
    language="en",
)

# What the generic stage could give, with its usual mistake: the reassigned bug keeps the first owner.
GENERIC = [
    ProposedAction("a1", "Send the Q3 report draft", "Send the draft to the client.",
                   "can you send the Q3 report draft to the client by Friday?", owner="Elena", due_date=date(2026, 11, 13)),
    ProposedAction("a2", "Fix the login bug", "Fix the login bug.", "fix the login bug", owner="Ionuț"),
    ProposedAction("a3", "Check the server costs", "Check the server costs.", "Mihai will check the server costs.",
                   owner="Mihai"),
    ProposedAction("a4", "Write the newsletter", "Write the newsletter.", "we won't do the newsletter anymore"),
    ProposedAction("a5", "Try a new CRM", "Evaluate a new CRM.", "Maybe we could try a new CRM someday."),
]

# As a model following domains/corporate/prompts/domain_stage.md would answer, plus untrusted extras.
ANSWER = {
    "proposed_actions": [
        {"id": "a1", "action_type": "work_item", "fields": {"deliverable": "the Q3 report draft"}},
        {"id": "a2", "action_type": "work_item", "fields": {"deliverable": "the login fix"}, "owner": "Elena"},
        {"id": "a3", "action_type": "work_item", "fields": {"deliverable": None, "due_date": "2026-11-20"}},
        {"id": "a9", "action_type": "work_item", "fields": {"deliverable": "made up"}},
        {"id": "a5", "action_type": "idea", "fields": {}},
    ],
    "suggestions": [
        {"text": "The login bug was taken over by Elena, not Ionuț.",
         "transcript_excerpt": "but Elena takes it over"},
        {"text": "No deadline was said for checking the server costs.",
         "transcript_excerpt": "Mihai will check the server costs."},
    ],
}


class LoadCorporateDomainTest(unittest.TestCase):
    def test_loads_work_items_and_calendar_events(self):
        domain = load_domain(DOMAINS / "corporate")
        self.assertEqual(domain.name, "corporate")
        self.assertEqual([r.name for r in domain.roles], ["organizer", "participant"])
        work_item, event = domain.action_types
        self.assertEqual((work_item.name, event.name), ("work_item", "calendar_event"))
        self.assertEqual([(f.name, f.type, f.required) for f in event.fields], [
            ("event_date", "date", True), ("start_time", "time", True), ("end_time", "time", True),
            ("location", "string", False), ("attendees", "string", False)])
        self.assertEqual([(s.name, s.connector) for s in event.workflow.steps],
                         [("build_event", None), ("send_event", "google_calendar"), ("save_result", None)])
        self.assertEqual([(f.name, f.type, f.required) for f in work_item.fields], [("deliverable", "string", False)])
        self.assertEqual(
            [(s.name, s.connector) for s in work_item.workflow.steps],
            [("build_document", None), ("save_document", "local_document"), ("save_result", None)],
        )
        self.assertIn("deliverable", work_item.workflow.steps[1].sends)


class CorporateDomainStageTest(unittest.TestCase):
    def setUp(self):
        self.domain = load_domain(DOMAINS / "corporate")
        self.store = InMemorySessionStore()
        self.store.save_session(Session(id="m1", started_at=datetime(2026, 11, 6, 10, 0), domain="corporate"))
        self.store.save_actions("m1", GENERIC, expected_revision=0)
        self.llm = FakeLlmClient([json.dumps(ANSWER)])
        self.actions, self.suggestions = run_domain_stage("m1", self.store, self.domain, self.llm, TRANSCRIPT)
        self.by_id = {a.id: a for a in self.actions}

    def test_keeps_work_items_and_drops_cancelled_idea_and_unknown(self):
        self.assertEqual([a.id for a in self.actions], ["a1", "a2", "a3"])
        self.assertEqual([a.id for a in self.store.load_actions("m1")], ["a1", "a2", "a3"])

    def test_explicit_commitment_gets_its_deliverable(self):
        a1 = self.by_id["a1"]
        self.assertEqual((a1.action_type, a1.fields), ("work_item", {"deliverable": "the Q3 report draft"}))
        self.assertEqual((a1.owner, a1.due_date), ("Elena", date(2026, 11, 13)))

    def test_reassignment_is_a_suggestion_not_a_silent_owner_change(self):
        self.assertEqual(self.by_id["a2"].owner, "Ionuț")
        self.assertIn("Elena", self.suggestions[0].text)

    def test_missing_deadline_stays_empty(self):
        a3 = self.by_id["a3"]
        self.assertIsNone(a3.due_date)
        self.assertEqual(a3.fields, {"deliverable": None})
        self.assertEqual(self.suggestions[1].transcript_excerpt, "Mihai will check the server costs.")

    def test_statuses_stay_proposed(self):
        self.assertTrue(all(a.status is ActionStatus.PROPOSED for a in self.actions))

    def test_prompt_and_model_come_from_the_domain(self):
        [(prompt, model, schema)] = self.llm.calls
        self.assertNotIn("{{", prompt)
        self.assertIn("organizer: The person who records the Session (the User).", prompt)
        self.assertIn("[S2] Maybe we could try a new CRM someday.", prompt)
        self.assertEqual((model, schema), (self.domain.model, self.domain.answer_schema))


# Calendar examples (#44): explicit meeting, "next week" with no time, a move and a cancellation.
CALENDAR_TRANSCRIPT = Transcript(
    segments=(
        Segment(0.0, 4.0, "Let's meet on Tuesday at 10 for an hour in room 2 to review the offer.", "S1"),
        Segment(4.0, 6.0, "Fine, Tuesday at 10.", "S2"),
        Segment(6.0, 9.0, "We'll see each other next week about the budget.", "S2"),
        Segment(9.0, 13.0, "Let's move Thursday's call with the client to Friday at 9 until half past nine.", "S1"),
        Segment(13.0, 16.0, "And the Monday training is cancelled.", "S1"),
    ),
    language="en",
)
CALENDAR_GENERIC = [
    ProposedAction("c1", "Offer review meeting", "Review the offer.", "Let's meet on Tuesday at 10 for an hour"),
    ProposedAction("c2", "Budget meeting", "Talk about the budget.", "We'll see each other next week about the budget."),
    ProposedAction("c3", "Client call", "Call with the client.", "move Thursday's call with the client to Friday at 9"),
    ProposedAction("c4", "Training", "The Monday training.", "the Monday training is cancelled."),
]
# Session on Friday 2026-11-06: "Tuesday" is 2026-11-10, "Friday" is 2026-11-13. The model also tries to
# fill attendees from the Speakers; the core drops that because the schema allows only null.
CALENDAR_ANSWER = {
    "proposed_actions": [
        {"id": "c1", "action_type": "calendar_event", "fields": {
            "event_date": "2026-11-10", "start_time": "10:00", "end_time": "11:00", "location": "room 2",
            "attendees": "S2"}},
        {"id": "c2", "action_type": "calendar_event", "fields": {
            "event_date": None, "start_time": None, "end_time": None, "location": None, "attendees": None}},
        {"id": "c3", "action_type": "calendar_event", "fields": {
            "event_date": "2026-11-13", "start_time": "09:00", "end_time": "09:30", "location": None,
            "attendees": "client@example.com"}},
    ],
    "suggestions": [
        {"text": "The budget meeting has no day or time.",
         "transcript_excerpt": "We'll see each other next week about the budget."},
        {"text": "The client call moved from Thursday; remove the old event.",
         "transcript_excerpt": "move Thursday's call with the client to Friday at 9"},
        {"text": "The Monday training was cancelled; you may want to remove it from the calendar.",
         "transcript_excerpt": "the Monday training is cancelled."},
    ],
}


class CorporateCalendarEventTest(unittest.TestCase):
    def setUp(self):
        self.domain = load_domain(DOMAINS / "corporate")
        self.store = InMemorySessionStore()
        self.store.save_session(Session(id="m2", started_at=datetime(2026, 11, 6, 10, 0), domain="corporate"))
        self.store.save_actions("m2", CALENDAR_GENERIC, expected_revision=0)
        self.llm = FakeLlmClient([json.dumps(CALENDAR_ANSWER)])
        actions, self.suggestions = run_domain_stage("m2", self.store, self.domain, self.llm, CALENDAR_TRANSCRIPT)
        self.by_id = {a.id: a for a in actions}

    def test_explicit_meeting_is_resolved_against_the_session_date(self):
        self.assertIn("Session date: 2026-11-06", self.llm.calls[0][0])
        self.assertEqual(self.by_id["c1"].fields, {"event_date": "2026-11-10", "start_time": "10:00",
                                                   "end_time": "11:00", "location": "room 2", "attendees": None})

    def test_next_week_without_a_time_stays_empty(self):
        self.assertEqual(set(self.by_id["c2"].fields.values()), {None})
        self.assertIn("no day or time", self.suggestions[0].text)

    def test_a_move_is_a_new_event_and_a_suggestion(self):
        self.assertEqual(self.by_id["c3"].fields["event_date"], "2026-11-13")
        self.assertIn("remove the old event", self.suggestions[1].text)

    def test_a_cancellation_is_only_a_suggestion(self):
        self.assertNotIn("c4", self.by_id)
        self.assertIn("cancelled", self.suggestions[2].text)

    def test_attendees_are_never_filled_by_the_model(self):
        self.assertIsNone(self.by_id["c1"].fields["attendees"])
        self.assertIsNone(self.by_id["c3"].fields["attendees"])


if __name__ == "__main__":
    unittest.main()
