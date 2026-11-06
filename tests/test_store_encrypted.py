"""Encrypted SessionStore and AuditLog (issue #26, ADR 0004).

The contract tests run on the in-memory fake and on the real store, so both behave the same.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path

from norev.adapters.store.encrypted import (
    EncryptedStore,
    StoreCorrupted,
    StoreKeyError,
    StoreSettings,
    _action_dict,
    create_key_file,
    open_store,
    read_key_file,
)
from norev.core.models import (
    ActionStatus,
    AuditEvent,
    ProposedAction,
    Segment,
    Session,
    Speaker,
    StaleRevision,
    Suggestion,
    Transcript,
)
from norev.core.ports import AuditLog, SessionStore
from norev.testing.fakes import InMemoryAuditLog, InMemorySessionStore

SENTINEL = "SENTINEL-Ionescu-amlodipina-7f3a"
AT = datetime(2026, 11, 6, 10, 24)


def a_session(session_id: str = "s1") -> Session:
    return Session(id=session_id, started_at=AT, domain="medical", contact_id=f"contact {SENTINEL}")


def a_transcript() -> Transcript:
    return Transcript(
        segments=(
            Segment(0.0, 2.5, f"Bună ziua, {SENTINEL}", "S1"),
            Segment(2.5, 4.0, "Mă doare capul.", "S2", untranslated=True),
        ),
        language="ro",
    )


def approved_action() -> ProposedAction:
    action = ProposedAction(
        id="s1-1",
        title="Programare control",
        description=f"Control tensiune {SENTINEL}",
        transcript_excerpt="Ne revedem peste două săptămâni.",
        owner="doctor",
        due_date=date(2026, 11, 20),
        action_type="schedule_visit",
        fields={"reason": "control", "visit_date": "2026-11-20", "visit_time": None},
    )
    action.approve({"reason": f"control {SENTINEL}", "visit_date": "2026-11-20", "visit_time": "09:00"})
    return action


def an_event() -> AuditEvent:
    return AuditEvent(at=AT, action_id="s1-1", connector="local_document", payload={"text": SENTINEL}, approved_by="user")


class StoreContract:
    """Same behaviour for every SessionStore + AuditLog. Subclasses set up self.store, self.audit."""

    def events(self) -> list[AuditEvent]:
        raise NotImplementedError

    def test_implements_the_ports(self):
        self.assertIsInstance(self.store, SessionStore)
        self.assertIsInstance(self.audit, AuditLog)

    def test_session_round_trip_and_missing(self):
        self.store.save_session(a_session())
        self.assertEqual(self.store.get_session("s1"), a_session())
        self.assertIsNone(self.store.get_session("nope"))

    def test_saving_a_session_again_replaces_it(self):
        self.store.save_session(a_session())
        changed = a_session()
        changed.transcript_ref = "t-1"
        self.store.save_session(changed)
        self.assertEqual(self.store.get_session("s1").transcript_ref, "t-1")

    def test_transcript_round_trip_and_unknown_ref(self):
        ref = self.store.save_transcript("s1", a_transcript())
        self.assertEqual(self.store.load_transcript(ref), a_transcript())
        self.assertNotEqual(ref, self.store.save_transcript("s1", a_transcript()))
        with self.assertRaises(KeyError):
            self.store.load_transcript("unknown")

    def test_actions_keep_status_dates_fields_and_approved_payload(self):
        plain = ProposedAction(id="s1-2", title="Analize", description="", transcript_excerpt="")
        self.store.save_session(a_session())
        self.store.save_actions("s1", [approved_action(), plain], expected_revision=0)
        loaded = self.store.load_actions("s1")
        self.assertEqual([(a.id, a.status) for a in loaded], [("s1-1", ActionStatus.APPROVED), ("s1-2", ActionStatus.PROPOSED)])
        self.assertEqual(loaded[0].due_date, date(2026, 11, 20))
        self.assertEqual(loaded[0].fields, approved_action().fields)
        self.assertEqual(loaded[0].approved_payload, approved_action().approved_payload)
        self.assertEqual(self.store.load_actions("other"), [])

    def test_suggestions_round_trip(self):
        suggestions = [Suggestion("Ora lipsește", "peste două săptămâni"), Suggestion("Fără extras")]
        self.store.save_session(a_session())
        self.store.save_suggestions("s1", suggestions, expected_revision=0)
        self.assertEqual(self.store.load_suggestions("s1"), suggestions)
        self.assertEqual(self.store.load_suggestions("other"), [])

    def test_returned_objects_are_copies(self):
        self.store.save_session(a_session())
        self.store.save_actions("s1", [approved_action()], expected_revision=0)
        loaded = self.store.load_actions("s1")
        loaded[0].approved_payload["visit_time"] = "23:00"
        self.assertEqual(self.store.load_actions("s1")[0].approved_payload["visit_time"], "09:00")

    # Review revision (norev.core.ports.SessionStore, #18)

    def test_revision_starts_at_zero_and_each_save_returns_the_next(self):
        self.store.save_session(a_session())
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 0)
        self.assertEqual(self.store.save_actions("s1", [approved_action()], expected_revision=0), 1)
        self.assertEqual(self.store.save_suggestions("s1", [Suggestion("x")], expected_revision=1), 2)
        snapshot = self.store.load_actions_snapshot("s1")
        self.assertEqual((snapshot.revision, [a.id for a in snapshot.actions]), (2, ["s1-1"]))

    def test_stale_revision_saves_nothing(self):
        self.store.save_session(a_session())
        self.store.save_actions("s1", [approved_action()], expected_revision=0)
        other = ProposedAction(id="s1-9", title="Altceva", description="", transcript_excerpt="")
        with self.assertRaises(StaleRevision):
            self.store.save_actions("s1", [other], expected_revision=0, suggestions=[Suggestion("y")])
        with self.assertRaises(StaleRevision):
            self.store.save_suggestions("s1", [Suggestion("y")], expected_revision=0)
        self.assertEqual([a.id for a in self.store.load_actions("s1")], ["s1-1"])
        self.assertEqual(self.store.load_suggestions("s1"), [])
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 1)

    def test_unknown_session_and_invalid_revision_are_refused(self):
        with self.assertRaises(LookupError):
            self.store.load_actions_snapshot("nope")
        with self.assertRaises(LookupError):
            self.store.save_actions("nope", [], expected_revision=0)
        self.store.save_session(a_session())
        for bad in (-1, True, "0"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                self.store.save_actions("s1", [], expected_revision=bad)
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 0)

    def test_duplicate_action_ids_are_refused(self):
        self.store.save_session(a_session())
        with self.assertRaises(ValueError):
            self.store.save_actions("s1", [approved_action(), approved_action()], expected_revision=0)
        self.assertEqual(self.store.load_actions("s1"), [])
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 0)

    def test_suggestions_none_keeps_them_and_empty_list_clears_them(self):
        self.store.save_session(a_session())
        self.store.save_actions("s1", [], expected_revision=0, suggestions=[Suggestion("x")])
        self.store.save_actions("s1", [approved_action()], expected_revision=1)
        self.assertEqual(self.store.load_suggestions("s1"), [Suggestion("x")])
        self.store.save_actions("s1", [approved_action()], expected_revision=2, suggestions=[])
        self.assertEqual(self.store.load_suggestions("s1"), [])

    def test_session_inputs_are_frozen_once_review_started(self):
        self.store.save_session(a_session())
        changed = a_session()
        changed.transcript_ref = "t-1"
        self.store.save_session(changed)  # before review: a change is fine and bumps the revision
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 1)
        self.store.save_actions("s1", [approved_action()], expected_revision=1)
        self.store.save_session(changed)  # the same Session again changes nothing
        changed.transcript_ref = "t-2"
        with self.assertRaises(ValueError):
            self.store.save_session(changed)
        self.assertEqual(self.store.get_session("s1").transcript_ref, "t-1")

    # Explicit regeneration (#19)

    def test_regeneration_replaces_proposed_keeps_decided_and_sets_the_domain(self):
        self.store.save_session(a_session())
        proposed = ProposedAction(id="s1-2", title="Vechi", description="", transcript_excerpt="")
        self.store.save_actions("s1", [approved_action(), proposed], expected_revision=0, suggestions=[Suggestion("x")])
        new = ProposedAction(id="s1-r1-1", title="Nou", description="", transcript_excerpt="", action_type="work_item")
        kept = self.store.load_actions("s1")[0]

        revision = self.store.regenerate_actions("s1", [kept, new], domain="corporate", expected_revision=1,
                                                 suggestions=[Suggestion("y")])

        self.assertEqual(revision, 2)
        self.assertEqual(self.store.get_session("s1").domain, "corporate")
        self.assertEqual([(a.id, a.status) for a in self.store.load_actions("s1")],
                         [("s1-1", ActionStatus.APPROVED), ("s1-r1-1", ActionStatus.PROPOSED)])
        self.assertEqual(self.store.load_actions("s1")[0].approved_payload, approved_action().approved_payload)
        self.assertEqual(self.store.load_suggestions("s1"), [Suggestion("y")])

    def test_regeneration_that_drops_or_changes_a_decision_saves_nothing(self):
        self.store.save_session(a_session())
        self.store.save_actions("s1", [approved_action()], expected_revision=0)
        changed = self.store.load_actions("s1")[0]
        changed.title = "Altul"
        for actions in ([], [changed]):
            with self.subTest(actions=len(actions)), self.assertRaises(ValueError):
                self.store.regenerate_actions("s1", actions, domain="corporate", expected_revision=1, suggestions=[])
        with self.assertRaises(StaleRevision):
            self.store.regenerate_actions("s1", self.store.load_actions("s1"), domain="corporate",
                                          expected_revision=0, suggestions=[])
        self.assertEqual(self.store.get_session("s1").domain, "medical")
        self.assertEqual(self.store.load_actions("s1")[0].title, "Programare control")
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 1)

    # Audio and Speakers (#24)

    def test_audio_round_trip_and_unknown_ref(self):
        audio = b"RIFF" + SENTINEL.encode() + bytes(range(256))
        ref = self.store.save_audio("s1", audio)
        self.assertEqual(self.store.load_audio(ref), audio)
        self.assertNotEqual(ref, self.store.save_audio("s1", audio))
        with self.assertRaises(KeyError):
            self.store.load_audio("unknown")

    def test_speakers_round_trip_and_unknown_session(self):
        with self.assertRaises(LookupError):
            self.store.save_speakers("s1", [Speaker("S1")])
        self.store.save_session(a_session())
        self.assertEqual(self.store.load_speakers("s1"), [])
        speakers = [Speaker("S1", "doctor"), Speaker("S2", "patient", role_confirmed=True), Speaker("S3")]
        self.store.save_speakers("s1", speakers)
        self.assertEqual(self.store.load_speakers("s1"), speakers)

    def test_a_confirmed_role_is_not_overwritten_by_an_unconfirmed_one(self):
        self.store.save_session(a_session())
        self.store.save_speakers("s1", [Speaker("S1", "doctor", role_confirmed=True), Speaker("S2", None, role_confirmed=True)])
        self.store.save_speakers("s1", [Speaker("S1", "patient"), Speaker("S2", "patient"), Speaker("S3", "caregiver")])
        self.assertEqual(
            self.store.load_speakers("s1"),
            [Speaker("S1", "doctor", True), Speaker("S2", None, True), Speaker("S3", "caregiver")],
        )
        self.store.save_speakers("s1", [Speaker("S1", "caregiver", role_confirmed=True)])  # a new User correction wins
        self.assertEqual(self.store.load_speakers("s1"), [Speaker("S1", "caregiver", True)])

    def test_who_approved_and_when_round_trip(self):
        action = approved_action()
        action.approved_by, action.approved_at = "local:ana", datetime(2026, 11, 6, 9, 30, tzinfo=timezone.utc)
        self.store.save_session(a_session())
        self.store.save_actions("s1", [action], expected_revision=0)
        [loaded] = self.store.load_actions("s1")
        self.assertEqual((loaded.approved_by, loaded.approved_at), ("local:ana", action.approved_at))

    def test_audit_keeps_events_in_order(self):
        first, second = an_event(), AuditEvent(AT, "s1-2", "local_document", {"n": 2}, "user")
        self.audit.record(first)
        self.audit.record(second)
        self.assertEqual(self.events(), [first, second])


class InMemoryStoreContractTest(StoreContract, unittest.TestCase):
    def setUp(self):
        self.store = InMemorySessionStore()
        self.audit = InMemoryAuditLog()

    def events(self):
        return self.audit.events


class TempStoreCase(unittest.TestCase):
    """A real store in a temporary folder, with its key file in a separate folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.data_dir = self.root / "data"
        self.key_file = self.root / "keys" / "norev.key"
        create_key_file(self.key_file)
        self.db = self.data_dir / "norev.db"
        self.store = self.open()
        self.audit = self.store

    def open(self) -> EncryptedStore:
        store = open_store(StoreSettings(path=self.db, key_file=self.key_file))
        self.addCleanup(store.close)
        return store

    def data_bytes(self) -> bytes:
        return b"".join(p.read_bytes() for p in sorted(self.data_dir.iterdir()) if p.is_file())


class EncryptedStoreContractTest(StoreContract, TempStoreCase):
    def events(self):
        return self.store.load_events()


class EncryptedStoreTest(TempStoreCase):
    def fill(self) -> str:
        self.store.save_session(a_session())
        ref = self.store.save_transcript("s1", a_transcript())
        self.store.save_actions("s1", [approved_action()], expected_revision=0)
        self.store.save_suggestions("s1", [Suggestion(f"Verifică {SENTINEL}", SENTINEL)], expected_revision=1)
        self.store.record(an_event())
        self.audio_ref = self.store.save_audio("s1", f"audio {SENTINEL}".encode())
        self.store.save_speakers("s1", [Speaker("S1", "doctor", role_confirmed=True)])
        return ref

    def test_reopening_restores_everything(self):
        ref = self.fill()
        self.store.close()
        again = self.open()
        self.assertEqual(again.get_session("s1"), a_session())
        self.assertEqual(again.load_transcript(ref), a_transcript())
        self.assertEqual(again.load_actions("s1")[0].approved_payload, approved_action().approved_payload)
        self.assertEqual(again.load_events(), [an_event()])
        self.assertEqual(again.load_actions_snapshot("s1").revision, 2)
        self.assertEqual(again.load_audio(self.audio_ref), f"audio {SENTINEL}".encode())
        self.assertEqual(again.load_speakers("s1"), [Speaker("S1", "doctor", role_confirmed=True)])
        with self.assertRaises(StaleRevision):  # the revision survives reopening
            again.save_actions("s1", [], expected_revision=0)

    def test_no_sentinel_in_clear_in_any_file_of_the_data_folder(self):
        self.fill()
        for encoding in ("utf-8", "utf-16-le"):
            self.assertNotIn(SENTINEL.encode(encoding), self.data_bytes())
        self.store.close()
        for encoding in ("utf-8", "utf-16-le"):
            self.assertNotIn(SENTINEL.encode(encoding), self.data_bytes())

    def test_wrong_key_is_refused_and_changes_nothing(self):
        self.fill()
        self.store.close()
        before = self.data_bytes()
        other_key = self.root / "keys" / "other.key"
        create_key_file(other_key)
        with self.assertRaisesRegex(StoreKeyError, "does not open"):
            open_store(StoreSettings(path=self.db, key_file=other_key))
        self.assertEqual(self.data_bytes(), before)

    def test_missing_or_invalid_key_file(self):
        with self.assertRaisesRegex(StoreKeyError, "not set"):
            open_store(StoreSettings(path=self.db, key_file=None))
        with self.assertRaisesRegex(StoreKeyError, "not found"):
            read_key_file(self.root / "keys" / "missing.key")
        short = self.root / "keys" / "short.key"
        short.write_bytes(b"x" * 31)
        with self.assertRaisesRegex(StoreKeyError, "32 bytes"):
            read_key_file(short)

    def test_key_file_next_to_the_store_is_refused(self):
        beside = self.data_dir / "norev.key"
        create_key_file(beside)
        with self.assertRaisesRegex(StoreKeyError, "same folder"):
            open_store(StoreSettings(path=self.db, key_file=beside))

    def test_create_key_file_never_overwrites(self):
        before = self.key_file.read_bytes()
        self.assertEqual(len(before), 32)
        with self.assertRaises(FileExistsError):
            create_key_file(self.key_file)
        self.assertEqual(self.key_file.read_bytes(), before)

    def test_tampered_record_is_reported_without_content(self):
        self.fill()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE records SET blob = substr(blob, 1, 20) || x'00' || substr(blob, 22) WHERE kind = 'actions'")
        with self.assertRaises(StoreCorrupted) as caught:
            self.store.load_actions("s1")
        self.assertIn("actions", str(caught.exception))
        self.assertNotIn(SENTINEL, str(caught.exception))

    def test_an_action_saved_before_approved_at_existed_still_loads(self):
        self.store.save_session(a_session())
        record = _action_dict(approved_action())
        del record["approved_at"]
        self.store._put("actions", "s1", [record])  # as written before #18
        [loaded] = self.store.load_actions("s1")
        self.assertIsNone(loaded.approved_at)
        self.assertEqual(loaded.approved_payload, approved_action().approved_payload)

    def test_a_record_moved_to_another_id_is_refused(self):
        self.store.save_session(a_session("s1"))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE records SET id = 's2' WHERE kind = 'session' AND id = 's1'")
        with self.assertRaises(StoreCorrupted):
            self.store.get_session("s2")

    def test_payload_that_is_not_json_is_refused_and_nothing_is_saved(self):
        action = approved_action()
        action.approved_payload["when"] = datetime(2026, 11, 20, 9, 0)
        self.store.save_session(a_session())
        with self.assertRaises(TypeError):
            self.store.save_actions("s1", [action], expected_revision=0)
        self.assertEqual(self.store.load_actions("s1"), [])
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 0)

    def test_repr_shows_no_content(self):
        self.fill()
        self.assertNotIn(SENTINEL, repr(self.store))

    def test_settings_from_environment(self):
        env = {"NOREV_STORE_PATH": str(self.db), "NOREV_STORE_KEY_FILE": str(self.key_file)}
        self.assertEqual(StoreSettings.from_environment(env), StoreSettings(path=self.db, key_file=self.key_file))
        self.assertIsNone(StoreSettings.from_environment({}).key_file)


if __name__ == "__main__":
    unittest.main()
