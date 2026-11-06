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


class RecordedEvaluationTest(unittest.TestCase):
    def test_recordings_replay_and_rebuild_the_report(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            code = evaluate.check_all()
        self.assertEqual(code, 0, out.getvalue())

    def test_every_session_has_a_person_checked_expectation_file(self):
        for folder in evaluate.sessions():
            with self.subTest(folder.name):
                expected = evaluate._json(folder / "expected.json")
                self.assertTrue(expected.get("verified_by"), "expected.json must be checked by a person (#25)")
                self.assertTrue(all("match" in a and "owner" in a and "due" in a for a in expected["actions"]))


if __name__ == "__main__":
    unittest.main()
