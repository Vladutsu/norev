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
    def test_loads_with_one_work_item_type(self):
        domain = load_domain(DOMAINS / "corporate")
        self.assertEqual(domain.name, "corporate")
        self.assertEqual([r.name for r in domain.roles], ["organizer", "participant"])
        [work_item] = domain.action_types
        self.assertEqual(work_item.name, "work_item")
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
        self.store.save_actions("m1", GENERIC)
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


if __name__ == "__main__":
    unittest.main()
