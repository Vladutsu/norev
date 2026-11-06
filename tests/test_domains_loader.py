"""load_domain builds the core Domain from a folder in domains/ (domains/README.md)."""
import shutil
import tempfile
import unittest
from pathlib import Path

from norev.adapters.domains.loader import DOMAINS, load_domain
from datetime import date

from norev.core.models import ProposedAction, Role, Segment, Transcript
from norev.usecases.domain_stage import build_prompt


class LoadMedicalDomainTest(unittest.TestCase):
    def setUp(self):
        self.domain = load_domain(DOMAINS / "medical")

    def test_top_level_and_model(self):
        self.assertEqual(self.domain.name, "medical")
        self.assertEqual(self.domain.label, "Medicină de familie")
        self.assertEqual(self.domain.model, "medgemma-4b-it-Q4_K_M.gguf")
        self.assertEqual(self.domain.context, 8192)

    def test_roles_in_order(self):
        self.assertEqual(
            self.domain.roles,
            (
                Role("doctor", "The family doctor who records the Session (the User).", "Medic"),
                Role("patient", "The person being consulted (the Contact).", "Pacient"),
                Role("caregiver", "A family member or carer who speaks for or helps the patient.", "Aparținător"),
            ),
        )

    def test_action_types_and_fields_in_order(self):
        self.assertEqual([t.name for t in self.domain.action_types], ["schedule_visit", "medication_reminder"])
        visit = self.domain.action_type("schedule_visit")
        self.assertEqual(visit.label, "Programare vizită")
        self.assertTrue(visit.description)
        self.assertEqual(
            [(f.name, f.type, f.required) for f in visit.fields],
            [("reason", "string", True), ("visit_date", "date", True), ("visit_time", "time", True),
             ("visit_end_time", "time", True),
             ("location", "string", False)],
        )
        self.assertEqual(visit.fields[0].label, "Motiv")
        reminder = self.domain.action_type("medication_reminder")
        self.assertEqual(
            [(f.name, f.type, f.required) for f in reminder.fields],
            [("medication", "string", True), ("dose", "string", True), ("frequency", "string", True),
             ("times", "string", True), ("start_date", "date", True), ("end_date", "date", False)],
        )

    def test_workflow_steps(self):
        steps = self.domain.action_type("schedule_visit").workflow.steps
        self.assertEqual(
            [(s.name, s.connector, s.sends) for s in steps],
            [
                ("build_invite", None, ()),
                ("send_invite", "google_calendar",
                 ("title", "reason", "visit_date", "visit_time", "visit_end_time", "location")),
                ("save_result", None, ()),
            ],
        )
        self.assertEqual(steps[1].label, "Trimite în Google Calendar")
        steps = self.domain.action_type("medication_reminder").workflow.steps
        self.assertEqual(
            [(s.name, s.connector, s.sends) for s in steps],
            [
                ("build_reminders", None, ()),
                ("send_reminders", "google_calendar",
                 ("medication", "dose", "frequency", "times", "start_date", "end_date")),
                ("save_result", None, ()),
            ],
        )

    def test_prompt_and_schema(self):
        self.assertIn("{{transcript}}", self.domain.prompt_template)
        self.assertIsInstance(self.domain.answer_schema, dict)
        self.assertIn("proposed_actions", self.domain.answer_schema["properties"])


    def test_real_prompt_fills_with_no_placeholder_left(self):
        transcript = Transcript((Segment(0, 1, "Come back on Friday.", "S1"),), "en")
        action = ProposedAction("s1-1", "Follow-up", "Follow-up visit", "Come back on Friday.")
        prompt = build_prompt(self.domain, transcript, [action], date(2026, 11, 6))
        self.assertNotIn("{{", prompt)
        self.assertIn("[S1] Come back on Friday.", prompt)


class LoadBrokenDomainTest(unittest.TestCase):
    def broken_copy(self, old: str, new: str, folder_name: str = "medical") -> Path:
        """A copy of the medical Domain with `old` replaced by `new` in domain.toml."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        copy = Path(tmp.name) / folder_name
        shutil.copytree(DOMAINS / "medical", copy)
        path = copy / "domain.toml"
        text = path.read_text(encoding="utf-8")
        self.assertIn(old, text)
        path.write_text(text.replace(old, new, 1), encoding="utf-8")
        return copy

    def test_copy_unchanged_loads(self):
        self.assertEqual(load_domain(self.broken_copy("format = 1", "format = 1")).name, "medical")

    def test_rejects_another_format(self):
        with self.assertRaisesRegex(ValueError, "format must be 1"):
            load_domain(self.broken_copy("format = 1", "format = 2"))

    def test_rejects_name_other_than_folder(self):
        with self.assertRaisesRegex(ValueError, "must equal the folder name"):
            load_domain(self.broken_copy('name = "medical"', 'name = "dental"'))

    def test_rejects_prompt_path_outside_folder(self):
        copy = self.broken_copy('domain_stage = "prompts/domain_stage.md"', 'domain_stage = "../escape.md"')
        (copy.parent / "escape.md").write_text("{{transcript}}", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "outside the Domain folder"):
            load_domain(copy)


if __name__ == "__main__":
    unittest.main()
