import unittest
from datetime import date, datetime

from norev.core.models import ActionStatus, ProposedAction, Segment, Session, Transcript
from norev.testing.fakes import FakeActionExtractor, InMemorySessionStore
from norev.usecases.extract import extract_session_actions, to_proposed_actions

TRANSCRIPT = Transcript(segments=(Segment(0.0, 2.0, "Revenim luni la control.", "S1"),), language="ro")


def item(**overrides):
    raw = {
        "id": "a1",
        "title": "Control",
        "description": "Programare la control",
        "owner": "Doctor",
        "due_date": "2026-11-09",
        "transcript_excerpt": "Revenim luni la control.",
    }
    raw.update(overrides)
    return raw


class ToProposedActionsTest(unittest.TestCase):
    def test_maps_a_complete_item(self):
        actions = to_proposed_actions({"proposed_actions": [item()]})
        self.assertEqual(len(actions), 1)
        action = actions[0]
        self.assertIsInstance(action, ProposedAction)
        self.assertEqual(
            (action.id, action.title, action.description, action.owner, action.due_date, action.transcript_excerpt),
            ("pa-1", "Control", "Programare la control", "Doctor", date(2026, 11, 9), "Revenim luni la control."),
        )

    def test_missing_or_non_list_proposed_actions_gives_empty_list(self):
        for raw in ({}, {"proposed_actions": None}, {"proposed_actions": "x"}, {"proposed_actions": {"id": "a1"}}):
            with self.subTest(raw=raw):
                self.assertEqual(to_proposed_actions(raw), [])

    def test_skips_non_dict_items(self):
        actions = to_proposed_actions({"proposed_actions": ["text", 3, None, ["x"], item()]})
        self.assertEqual([a.title for a in actions], ["Control"])

    def test_skips_items_with_neither_title_nor_description(self):
        raw = {"proposed_actions": [item(title="", description="  "), item(title=None, description=None), item()]}
        self.assertEqual(len(to_proposed_actions(raw)), 1)

    def test_keeps_item_with_only_title_or_only_description(self):
        raw = {"proposed_actions": [item(id="a", description=None), item(id="b", title="")]}
        actions = to_proposed_actions(raw)
        self.assertEqual([(a.title, a.description) for a in actions], [("Control", ""), ("", "Programare la control")])

    def test_text_fields_are_stripped_and_non_strings_become_empty(self):
        raw = {"proposed_actions": [item(title="  Control \n", description=5, transcript_excerpt=["x"])]}
        action = to_proposed_actions(raw)[0]
        self.assertEqual((action.title, action.description, action.transcript_excerpt), ("Control", "", ""))

    def test_missing_text_fields_become_empty(self):
        action = to_proposed_actions({"proposed_actions": [{"title": "Control"}]})[0]
        self.assertEqual((action.description, action.transcript_excerpt, action.owner), ("", "", None))

    def test_owner_is_stripped_and_empty_or_non_string_becomes_none(self):
        for owner, expected in ((" Pacient ", "Pacient"), ("", None), ("   ", None), (None, None), (7, None)):
            with self.subTest(owner=owner):
                self.assertEqual(to_proposed_actions({"proposed_actions": [item(owner=owner)]})[0].owner, expected)

    def test_due_date_accepts_only_valid_iso_calendar_dates(self):
        cases = (
            ("2026-02-28", date(2026, 2, 28)),
            (" 2026-02-28 ", date(2026, 2, 28)),
            ("2026-02-30", None),
            ("next monday", None),
            ("20260228", None),  # basic format, accepted by date.fromisoformat but not YYYY-MM-DD
            ("2026-W09-6", None),  # ISO week date, same
            ("2026-02-28T10:00", None),
            ("2026-2-8", None),
            ("", None),
            (None, None),
            (5, None),
        )
        for value, expected in cases:
            with self.subTest(due_date=value):
                self.assertEqual(to_proposed_actions({"proposed_actions": [item(due_date=value)]})[0].due_date, expected)

    def test_missing_due_date_stays_none(self):
        self.assertIsNone(to_proposed_actions({"proposed_actions": [{"title": "Control"}]})[0].due_date)

    def test_status_is_always_proposed_and_extra_keys_are_ignored(self):
        raw = {
            "proposed_actions": [
                item(status="approved", approved_payload={"x": 1}, priority="high", confidence=0.9, needs_review=False)
            ]
        }
        action = to_proposed_actions(raw)[0]
        self.assertIs(action.status, ActionStatus.PROPOSED)
        self.assertIsNone(action.approved_payload)
        self.assertIsNone(action.action_type)
        self.assertEqual(action.fields, {})

    def test_ids_are_positional_and_ignore_the_model_ids(self):
        raw = {"proposed_actions": [item(id="x"), item(id=None), item(id="<script>" * 1000), item(id="pa-1")]}
        self.assertEqual([a.id for a in to_proposed_actions(raw)], ["pa-1", "pa-2", "pa-3", "pa-4"])

    def test_id_prefix(self):
        raw = {"proposed_actions": [item(), item()]}
        self.assertEqual([a.id for a in to_proposed_actions(raw, id_prefix="s9")], ["s9-1", "s9-2"])

    def test_positional_ids_count_kept_actions_only(self):
        raw = {"proposed_actions": ["junk", item(id=None, title="", description=""), item(id=None)]}
        self.assertEqual([a.id for a in to_proposed_actions(raw)], ["pa-1"])


class ExtractSessionActionsTest(unittest.TestCase):
    def setUp(self):
        self.store = InMemorySessionStore()
        ref = self.store.save_transcript("s1", TRANSCRIPT)
        self.store.save_session(Session(id="s1", started_at=datetime(2026, 11, 6, 9, 30), domain="medical", transcript_ref=ref))

    def test_extracts_maps_saves_and_returns_actions(self):
        extractor = FakeActionExtractor({"session_summary": "s", "proposed_actions": [item(), item(id="a2")]})
        actions = extract_session_actions("s1", self.store, extractor)
        self.assertEqual([a.id for a in actions], ["s1-1", "s1-2"])
        self.assertEqual([a.id for a in self.store.load_actions("s1")], ["s1-1", "s1-2"])

    def test_re_extraction_replaces_only_undecided_actions(self):
        extract_session_actions("s1", self.store, FakeActionExtractor({"proposed_actions": [item()]}))
        again = extract_session_actions("s1", self.store, FakeActionExtractor({"proposed_actions": [item(), item()]}))
        self.assertEqual(len(again), 2)

    def test_refuses_to_re_extract_once_an_action_is_decided(self):
        actions = extract_session_actions("s1", self.store, FakeActionExtractor({"proposed_actions": [item()]}))
        actions[0].approve({"title": "Control"})
        self.store.save_actions("s1", actions)
        extractor = FakeActionExtractor({"proposed_actions": [item()]})
        with self.assertRaises(ValueError):
            extract_session_actions("s1", self.store, extractor)
        self.assertEqual(extractor.calls, [])
        self.assertIs(self.store.load_actions("s1")[0].status, ActionStatus.APPROVED)

    def test_aware_start_time_uses_the_local_day(self):
        started = datetime(2026, 11, 6, 9, 30).astimezone()  # local time, made aware
        self.store.save_session(Session(id="s3", started_at=started, domain="medical", transcript_ref="transcript-1"))
        extractor = FakeActionExtractor({"proposed_actions": []})
        extract_session_actions("s3", self.store, extractor)
        self.assertEqual(extractor.calls[0][1], date(2026, 11, 6))

    def test_passes_the_transcript_and_the_session_date_to_the_extractor(self):
        extractor = FakeActionExtractor({"proposed_actions": []})
        extract_session_actions("s1", self.store, extractor)
        self.assertEqual(extractor.calls, [(TRANSCRIPT, date(2026, 11, 6))])

    def test_raises_lookup_error_for_unknown_session(self):
        extractor = FakeActionExtractor({"proposed_actions": [item()]})
        with self.assertRaises(LookupError):
            extract_session_actions("missing", self.store, extractor)
        self.assertEqual(extractor.calls, [])

    def test_raises_lookup_error_when_session_has_no_transcript(self):
        self.store.save_session(Session(id="s2", started_at=datetime(2026, 11, 6), domain="medical"))
        extractor = FakeActionExtractor({"proposed_actions": [item()]})
        with self.assertRaises(LookupError):
            extract_session_actions("s2", self.store, extractor)
        self.assertEqual(extractor.calls, [])
        self.assertEqual(self.store.load_actions("s2"), [])


if __name__ == "__main__":
    unittest.main()
