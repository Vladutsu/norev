"""The desktop UI's bridge (#27), with fakes: no pywebview, model, network or Connector."""
import ast
import re
import unittest
from datetime import datetime, timezone
from pathlib import Path

from norev.testing.fakes import FixedClock, InMemorySessionStore
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "norev" / "ui" / "web"
NOW = datetime(2026, 11, 6, 10, 24, tzinfo=timezone.utc)


class FakeMonotonic:
    def __init__(self) -> None:
        self.t = 100.0

    def __call__(self) -> float:
        return self.t


class BridgeCase(unittest.TestCase):
    def setUp(self):
        self.store = InMemorySessionStore()
        self.time = FakeMonotonic()
        self.bridge = Bridge(DemoBackend(self.store, approved_by="local:test", clock=FixedClock(NOW), monotonic=self.time))

    def ok(self, result):
        self.assertTrue(result["ok"], result)
        return result["data"]

    def error(self, result):
        self.assertFalse(result["ok"], result)
        return result["error"]["code"]

    def reviewed(self):
        session_id = self.ok(self.bridge.import_example("medical", "ro", "Maria Ionescu"))["session_id"]
        return session_id, self.ok(self.bridge.review(session_id))

    def action(self, review, title):
        return next(a for a in review["actions"] if a["title"] == title)


class ModesTest(BridgeCase):
    def test_lists_the_three_modes_in_order_and_corporate_waits_for_its_domain(self):
        modes = self.ok(self.bridge.modes())
        self.assertEqual([m["id"] for m in modes], ["corporate", "medical", "others"])
        available = {m["id"]: m["available"] for m in modes}
        self.assertTrue(available["medical"])
        self.assertTrue(available["others"])
        self.assertEqual(available["corporate"], (ROOT / "domains" / "corporate" / "domain.toml").is_file())

    def test_a_mode_without_its_domain_cannot_start(self):
        self.assertEqual(self.error(self.bridge.start("nope", "ro", "")), "mode_unavailable")


class OthersTest(BridgeCase):
    def others(self, context="Ședință de echipă", prompt="Listează acțiunile."):
        return {"context": context, "prompt": prompt}

    def test_does_not_start_with_an_unconfirmed_prompt(self):
        self.assertEqual(self.error(self.bridge.start("others", "ro", "", self.others())), "prompt_not_confirmed")
        self.assertEqual(self.error(self.bridge.import_example("others", "ro", "", None)), "prompt_not_confirmed")

    def test_starts_with_the_exact_confirmed_prompt(self):
        self.ok(self.bridge.others_confirm("Ședință de echipă", "Listează acțiunile."))
        self.ok(self.bridge.start("others", "ro", "", self.others()))

    def test_changing_the_context_or_the_prompt_needs_a_new_confirmation(self):
        self.ok(self.bridge.others_confirm("Ședință de echipă", "Listează acțiunile."))
        for changed in (self.others(context="Alt context"), self.others(prompt="Alt prompt")):
            with self.subTest(changed=changed):
                self.assertEqual(self.error(self.bridge.start("others", "ro", "", changed)), "prompt_not_confirmed")

    def test_generating_needs_a_context_and_never_runs_on_its_own(self):
        self.assertEqual(self.error(self.bridge.others_generate("  ")), "invalid")
        prompt = self.ok(self.bridge.others_generate("Interviu"))["prompt"]
        self.assertIn("Interviu", prompt)
        self.assertIn("never invent", prompt)


class RecordingTest(BridgeCase):
    def test_live_transcript_grows_pauses_and_stop_runs_once(self):
        session_id = self.ok(self.bridge.start("medical", "ro", "Maria Ionescu"))["session_id"]
        first = self.ok(self.bridge.live(session_id))
        self.time.t += 5
        later = self.ok(self.bridge.live(session_id))
        self.assertGreater(len(later["segments"]), len(first["segments"]))
        self.assertTrue(self.ok(self.bridge.pause(session_id))["paused"])
        self.time.t += 60
        self.assertEqual(self.ok(self.bridge.live(session_id))["elapsed"], 5)
        self.ok(self.bridge.stop(session_id))
        revision = self.ok(self.bridge.review(session_id))["revision"]
        self.ok(self.bridge.stop(session_id))
        self.assertEqual(self.ok(self.bridge.review(session_id))["revision"], revision)

    def test_unknown_session(self):
        self.assertEqual(self.error(self.bridge.live("missing")), "not_found")


class ReviewTest(BridgeCase):
    def test_review_shows_fields_excerpt_speakers_and_read_only_suggestions(self):
        _, review = self.reviewed()
        self.assertEqual(review["domain"]["name"], "medical")
        visit = self.action(review, "Control pentru tensiune")
        self.assertEqual(visit["type_label"], "Programare vizită")
        self.assertTrue(visit["transcript_excerpt"])
        self.assertEqual([s["role"] for s in review["speakers"]], ["doctor", "patient"])
        self.assertTrue(review["suggestions"])
        self.assertFalse(any(name for name in dir(self.bridge) if "suggestion" in name and not name.startswith("_")))

    def test_payload_is_shown_before_approval_and_approval_keeps_exactly_it(self):
        session_id, review = self.reviewed()
        visit = self.action(review, "Control pentru tensiune")
        self.assertEqual(visit["plan"], [])
        review = self.ok(self.bridge.preview(session_id, visit["id"], review["revision"]))
        shown = self.action(review, "Control pentru tensiune")["plan"]
        self.assertEqual(shown[0]["connector"], "google_calendar")
        self.assertEqual(shown[0]["payload"]["visit_date"], "2026-11-13")
        review = self.ok(self.bridge.approve(session_id, visit["id"], review["revision"]))
        approved = self.action(review, "Control pentru tensiune")
        self.assertEqual((approved["status"], approved["plan"]), ("approved", shown))

    def test_missing_required_field_blocks_approval_in_the_backend(self):
        session_id, review = self.reviewed()
        pill = self.action(review, "Amlodipină 5 mg")
        self.assertEqual(pill["missing"], ["Ore"])
        self.assertEqual(self.error(self.bridge.preview(session_id, pill["id"], review["revision"])), "required")
        self.assertEqual(self.error(self.bridge.approve(session_id, pill["id"], review["revision"])), "required")
        review = self.ok(self.bridge.edit(session_id, pill["id"], {"fields": {"times": "08:00"}}, review["revision"]))
        review = self.ok(self.bridge.preview(session_id, pill["id"], review["revision"]))
        review = self.ok(self.bridge.approve(session_id, pill["id"], review["revision"]))
        self.assertEqual(self.action(review, "Amlodipină 5 mg")["status"], "approved")

    def test_a_double_press_decides_once(self):
        session_id, review = self.reviewed()
        visit = self.action(review, "Control pentru tensiune")
        review = self.ok(self.bridge.preview(session_id, visit["id"], review["revision"]))
        self.ok(self.bridge.approve(session_id, visit["id"], review["revision"]))
        self.assertEqual(self.error(self.bridge.approve(session_id, visit["id"], review["revision"])), "stale")
        current = self.ok(self.bridge.review(session_id))
        self.assertEqual(self.error(self.bridge.approve(session_id, visit["id"], current["revision"])), "already_decided")
        self.assertEqual(self.error(self.bridge.reject(session_id, visit["id"], current["revision"])), "already_decided")

    def test_editing_after_preview_needs_a_new_preview(self):
        session_id, review = self.reviewed()
        visit = self.action(review, "Control pentru tensiune")
        review = self.ok(self.bridge.preview(session_id, visit["id"], review["revision"]))
        review = self.ok(self.bridge.edit(session_id, visit["id"], {"fields": {"visit_time": "11:00"}}, review["revision"]))
        self.assertFalse(self.action(review, "Control pentru tensiune")["prepared"])
        self.assertEqual(self.error(self.bridge.approve(session_id, visit["id"], review["revision"])), "preview_required")

    def test_invalid_values_are_refused_with_a_code_only(self):
        session_id, review = self.reviewed()
        visit = self.action(review, "Control pentru tensiune")
        result = self.bridge.edit(session_id, visit["id"], {"fields": {"visit_time": "SECRET 25:99"}}, review["revision"])
        self.assertEqual(self.error(result), "invalid_time")
        self.assertNotIn("SECRET", repr(result))

    def test_roles_are_correctable_but_only_to_domain_roles(self):
        session_id, _ = self.reviewed()
        review = self.ok(self.bridge.set_role(session_id, "S2", "caregiver"))
        self.assertEqual(review["speakers"][1]["role"], "caregiver")
        self.assertEqual(self.error(self.bridge.set_role(session_id, "S2", "admin")), "invalid")

    def test_sessions_list_counts_pending_actions(self):
        session_id, review = self.reviewed()
        listed = self.ok(self.bridge.sessions())
        self.assertEqual((listed[0]["session_id"], listed[0]["pending"]), (session_id, 3))
        self.ok(self.bridge.reject(session_id, review["actions"][0]["id"], review["revision"]))
        self.assertEqual(self.ok(self.bridge.sessions())[0]["pending"], 2)


class CorporateTest(BridgeCase):
    def test_meeting_review_owner_and_due_date_are_editable_and_shown_in_the_payload(self):
        session_id = self.ok(self.bridge.import_example("corporate", "ro", "Echipa de produs"))["session_id"]
        review = self.ok(self.bridge.review(session_id))
        self.assertEqual(review["domain"]["name"], "corporate")
        budget = self.action(review, "Confirmă bugetul pilotului cu financiarul")
        self.assertEqual((budget["action_type"], budget["owner"], budget["due_date"]), ("work_item", "Ioana", ""))
        review = self.ok(self.bridge.edit(session_id, budget["id"], {"due_date": "2026-11-13", "owner": "Mihai"}, review["revision"]))
        review = self.ok(self.bridge.preview(session_id, budget["id"], review["revision"]))
        step = self.action(review, "Confirmă bugetul pilotului cu financiarul")["plan"][0]
        self.assertEqual(step["connector"], "local_document")
        self.assertEqual((step["payload"]["owner"], step["payload"]["due_date"]), ("Mihai", "2026-11-13"))
        self.assertEqual(self.error(self.bridge.edit(session_id, budget["id"], {"due_date": "mâine"}, review["revision"])), "invalid_date")

    def test_others_records_but_has_no_analysis_until_its_domain_exists(self):
        self.ok(self.bridge.others_confirm("Interviu", "Listează acțiunile."))
        session_id = self.ok(self.bridge.start("others", "ro", "", {"context": "Interviu", "prompt": "Listează acțiunile."}))["session_id"]
        self.assertTrue(self.ok(self.bridge.live(session_id))["segments"])
        self.assertEqual(self.error(self.bridge.stop(session_id)), "mode_unavailable")


class BoundaryTest(unittest.TestCase):
    def test_the_bridge_exposes_no_send_or_connector_call(self):
        # #43: execute/retry/reconcile reach the Workflow executor (tests/test_ui_session_backend.py), never a Connector
        public = {n for n in dir(Bridge) if not n.startswith("_")}
        self.assertFalse({n for n in public if re.search(r"send|connector", n)})
        self.assertEqual({n for n in public if re.search(r"execute|retry|reconcile", n)}, {"execute", "retry", "reconcile"})
        for path in (ROOT / "norev" / "ui").glob("*.py"):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse((node.module or "").startswith(("norev.adapters.connectors", "norev.processor.central")),
                                     f"{path.name} imports {node.module}")

    def test_web_assets_are_local(self):
        for path in WEB.rglob("*"):
            if path.suffix in {".html", ".css", ".js"}:
                text = path.read_text(encoding="utf-8")
                urls = re.findall(r"""(?:src|href|url)\s*[=(]\s*["']?(https?:|//)""", text)
                self.assertEqual(urls, [], f"{path.name} loads something from the network")
        fonts = re.findall(r"url\(([^)]+)\)", (WEB / "app.css").read_text(encoding="utf-8"))
        self.assertTrue(fonts)
        for font in fonts:
            self.assertTrue((WEB / font.strip("\"'")).is_file(), font)

    def test_no_undo_after_a_decision_and_session_text_never_goes_through_innerhtml(self):
        js = (WEB / "app.js").read_text(encoding="utf-8")
        self.assertNotRegex(js, r"(?i)undo|anulează decizia")
        self.assertEqual(re.findall(r"\.innerHTML\s*=[^;]*", js), [".innerHTML = ICON[name]"])

    def test_reduced_motion_is_respected(self):
        self.assertIn("prefers-reduced-motion", (WEB / "app.css").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
