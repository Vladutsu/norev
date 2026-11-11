"""The #25 evaluator: how each kind of mistake is counted, and the recorded evaluation replays (no model)."""
import contextlib
import importlib.util
import io
import unittest
from pathlib import Path

EVAL = Path(__file__).resolve().parent / "fixtures" / "eval"
spec = importlib.util.spec_from_file_location("evaluate", EVAL / "evaluate.py")
evaluate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluate)

EXPECTED = {
    "actions": [
        {"key": "invoice", "match": [["invoice"]], "owner": ["Mihai"], "due": ["2026-11-30"]},
        {"key": "training-date", "match": [["training"], ["date"]], "owner": [None], "due": [None], "optional": True},
        {"key": "training", "match": [["training"]], "owner": ["Mihai"], "due": [None]},
        {"key": "contract", "match": [["contract"]], "owner": [None, "organizer"], "due": ["2026-11-07"]},
        {"key": "list", "match": [["user"], ["list"]], "owner": ["Popescu"], "due": "any"},
    ],
    "not_actions": [{"key": "paypal", "match": [["paypal"]]}],
    "numbers": [{"key": "112", "english": ["112", "one hundred twelve"]}, {"key": "price", "english": ["12,500", "twelve thousand five hundred"]}],
    "known_cases": [{"id": "n", "what": "", "number": "112"}, {"id": "a", "what": "", "action": "invoice"}],
}


def action(title, owner=None, due=None, excerpt=""):
    return {"title": title, "description": "", "owner": owner, "due_date": due, "transcript_excerpt": excerpt}


class ScoreTest(unittest.TestCase):
    def run_score(self, actions, english="Call 112. The price is 12,500 lei."):
        return evaluate.score(EXPECTED, actions, english)

    def test_everything_right(self):
        r = self.run_score([
            action("Issue invoice", "Mihai", "2026-11-30"), action("Organize training", "Mihai"),
            action("Send signed contract", None, "2026-11-07"), action("Send user list", "Mrs. Popescu", "2026-11-10"),
        ])
        self.assertEqual((r["found"], r["omitted"], r["invented"]), (4, [], []))
        for key in ("owner_wrong", "owner_missing", "date_wrong", "date_missing", "numbers_changed", "cancelled_listed"):
            self.assertEqual(r[key], [], key)
        self.assertTrue(all(c["solved"] for c in r["known_cases"]))

    def test_omission_invention_and_cancelled_idea(self):
        r = self.run_score([action("Issue invoice", "Mihai", "2026-11-30"), action("Integrate PayPal"), action("Buy pizza")])
        self.assertEqual(r["omitted"], ["contract", "list", "training"])
        self.assertEqual(r["invented"], ["Buy pizza", "Integrate PayPal"])
        self.assertEqual(r["cancelled_listed"], ["paypal"])

    def test_wrong_and_missing_owner_and_date(self):
        r = self.run_score([
            action("Issue invoice", "Elena", None), action("Organize training", None, "2026-11-20"),
            action("Send signed contract", "Andrei", "2026-11-08"),
        ])
        self.assertEqual(r["owner_wrong"], ["contract", "invoice"])
        self.assertEqual(r["owner_missing"], ["training"])
        self.assertEqual(r["date_wrong"], ["contract", "training"])
        self.assertEqual(r["date_missing"], ["invoice"])
        self.assertEqual([c["solved"] for c in r["known_cases"]], [True, False])

    def test_the_most_specific_expectation_is_matched_first(self):
        r = self.run_score([action("Organize training", "Mihai"), action("Set training date")])
        self.assertEqual(r["details"]["training-date"]["got"]["title"], "Set training date")
        self.assertEqual(r["details"]["training"]["got"]["title"], "Organize training")

    def test_numbers_changed_by_the_translation(self):
        r = self.run_score([], english="Call 122 immediately. The price is twelve thousand five hundred lei.")
        self.assertEqual(r["numbers_changed"], ["112"])
        self.assertEqual([c["solved"] for c in r["known_cases"]], [False, False])

    def test_digits_inside_another_number_do_not_count(self):
        self.assertFalse(evaluate._number_kept("call 1122", ["112"]))
        self.assertTrue(evaluate._number_kept("on November 11th", ["11"]))

    def test_titles_and_diacritics_do_not_change_the_owner(self):
        self.assertTrue(evaluate._owner_ok("Doamna Dumitrescu", ["Dumitrescu"]))
        self.assertTrue(evaluate._owner_ok("Ionuț", ["Ionut"]))
        self.assertFalse(evaluate._owner_ok("Anastasia", ["Ana"]))


class DomainScoreTest(unittest.TestCase):
    EXPECTED = {
        "actions": [
            {"key": "demo", "match": [["demo"]], "owner": "any", "due": "any", "action_type": "calendar_event",
             "fields": {"event_date": ["2026-11-10"], "start_time": ["10:00"], "end_time": ["11:00"]}},
            {"key": "training", "match": [["training"]], "owner": "any", "due": "any", "action_type": "calendar_event",
             "fields": {"event_date": ["2026-11-12"], "start_time": [None]}},
            {"key": "slides", "match": [["slides"]], "owner": "any", "due": "any", "action_type": "work_item"},
        ],
        "not_actions": [{"key": "marketing", "match": [["marketing"]]}],
        "known_cases": [{"id": "missing-time", "what": "", "action": "training"}],
    }
    GENERIC = [action("Client demo"), action("Internal training"), action("Prepare slides"), action("Marketing meeting")]

    def typed(self, **changes):
        typed = [
            {"id": "pa-1", "title": "Client demo", "action_type": "calendar_event",
             "fields": {"event_date": "2026-11-10", "start_time": "10:00", "end_time": "11:00"}},
            {"id": "pa-2", "title": "Internal training", "action_type": "calendar_event",
             "fields": {"event_date": "2026-11-12", "start_time": None, "end_time": None}},
            {"id": "pa-3", "title": "Prepare slides", "action_type": "work_item", "fields": {}},
        ]
        for t in typed:
            t.update(changes.get(t["id"], {}))
        return [t for t in typed if t.get("action_type") != "drop"]

    def test_all_right(self):
        r = evaluate.score(self.EXPECTED, self.GENERIC, "", self.typed())
        self.assertEqual(r["domain"], {"checked": 3, "wrong": {}, "cancelled_as_event": []})
        self.assertTrue(r["known_cases"][0]["solved"])

    def test_wrong_type_wrong_field_and_dropped(self):
        typed = self.typed(**{
            "pa-1": {"fields": {"event_date": "2026-11-10", "start_time": "11:00", "end_time": "11:00"}},
            "pa-2": {"fields": {"event_date": "2026-11-12", "start_time": "09:00"}},  # a time nobody said
            "pa-3": {"action_type": "drop"},
        })
        r = evaluate.score(self.EXPECTED, self.GENERIC, "", typed)
        self.assertEqual(r["domain"]["wrong"], {
            "demo": ["start_time 11:00"], "training": ["start_time 09:00"], "slides": ["dropped by the Domain stage"]})
        self.assertFalse(r["known_cases"][0]["solved"])

    def test_cancelled_meeting_kept_as_event(self):
        typed = self.typed() + [{"id": "pa-4", "title": "Marketing meeting", "action_type": "calendar_event", "fields": {}}]
        self.assertEqual(evaluate.score(self.EXPECTED, self.GENERIC, "", typed)["domain"]["cancelled_as_event"], ["marketing"])

    def test_no_domain_stage_no_domain_score(self):
        self.assertNotIn("domain", evaluate.score(self.EXPECTED, self.GENERIC, ""))


class RecordedEvaluationTest(unittest.TestCase):
    def test_recordings_replay_and_rebuild_the_report(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = evaluate.check_all()
        self.assertEqual(code, 0, out.getvalue())

    def test_production_known_cases_are_solved_or_have_a_follow_up(self):
        """#25/#46: a defect may stay outstanding only while an issue says who fixes it."""
        report = evaluate.build_report()
        production = report["variants"]["neutral"]
        for session in production["sessions"].values():
            for case in session["known_cases"]:
                with self.subTest(case=case["id"]):
                    self.assertTrue(case["solved"] or case.get("follow_up"), "outstanding with no follow-up issue")

    def test_every_session_has_a_person_checked_expectation_file(self):
        for folder in evaluate.sessions():
            with self.subTest(folder.name):
                expected = evaluate._json(folder / "expected.json")
                self.assertTrue(expected.get("verified_by"), "expected.json must be checked by a person (#25)")
                self.assertTrue(all("match" in a and "owner" in a and "due" in a for a in expected["actions"]))


if __name__ == "__main__":
    unittest.main()
