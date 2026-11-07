"""Encrypted SessionStore and AuditLog (issue #26, ADR 0004).

The contract tests run on the in-memory fake and on the real store, so both behave the same.
"""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime
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
    SendResult,
    Session,
    Speaker,
    StaleRevision,
    StepRecord,
    StepState,
    Suggestion,
    Transcript,
)
from norev.core.ports import AuditLog, ExecutionAuditLog, ExecutionStore, SessionStore
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


def a_done_step() -> StepRecord:
    return StepRecord("export", StepState.DONE, result={"path": f"/exports/{SENTINEL}.md"})


class StoreContract:
    """Same behaviour for every SessionStore + ExecutionStore + ExecutionAuditLog.
    Subclasses set up self.store and self.audit."""

    def events(self) -> list[AuditEvent]:
        raise NotImplementedError

    def test_implements_the_ports(self):
        self.assertIsInstance(self.store, SessionStore)
        self.assertIsInstance(self.audit, AuditLog)
        self.assertIsInstance(self.audit, ExecutionAuditLog)

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

    def test_approval_time_and_approver_round_trip(self):
        self.store.save_session(a_session())
        action = approved_action()
        action.approved_by, action.approved_at = "local:ana", datetime(2026, 11, 6, 10, 30)
        self.store.save_actions("s1", [action], expected_revision=0)
        self.assertEqual(self.store.load_actions("s1"), [action])

    def test_summary_round_trip_and_unknown_session(self):
        self.assertRaises(LookupError, self.store.save_summary, "s1", "x")
        self.store.save_session(a_session())
        self.assertEqual(self.store.load_summary("s1"), "")
        self.store.save_summary("s1", f"Ședință despre {SENTINEL}")
        self.store.save_summary("s1", "A doua analiză")
        self.assertEqual(self.store.load_summary("s1"), "A doua analiză")

    def _filled_session(self) -> tuple[Session, list[ProposedAction]]:
        session = a_session()
        session.audio_ref = self.store.save_audio("s1", b"audio")
        session.transcript_ref = self.store.save_transcript("s1", a_transcript())
        session.source_transcript_ref = self.store.save_transcript("s1", a_transcript())
        self.store.save_session(session)
        proposed = ProposedAction("s1-1", "Raport", "Trimite raportul", "Trimit raportul.")
        self.store.save_actions("s1", [proposed], expected_revision=0, suggestions=[Suggestion("Verifică", "x")])
        self.store.save_speakers("s1", [Speaker("S1")])
        self.store.save_summary("s1", "rezumat")
        self.store.save_step("s1-1", StepRecord("build_document", StepState.DONE))
        return session, [proposed]

    def test_delete_session_removes_everything_of_it_and_keeps_the_audit(self):
        session, _ = self._filled_session()
        self.store.save_session(a_session("s2"))
        self.audit.record(an_event())

        self.store.delete_session("s1")

        self.assertIsNone(self.store.get_session("s1"))
        self.assertEqual([s.id for s in self.store.list_sessions()], ["s2"])
        for ref in (session.transcript_ref, session.source_transcript_ref):
            self.assertRaises(KeyError, self.store.load_transcript, ref)
        self.assertRaises(KeyError, self.store.load_audio, session.audio_ref)
        self.assertEqual((self.store.load_actions("s1"), self.store.load_suggestions("s1"), self.store.load_speakers("s1"),
                          self.store.load_summary("s1"), self.store.load_steps("s1-1")), ([], [], [], "", {}))
        self.assertEqual(len(self.events()), 1)
        self.assertRaises(LookupError, self.store.delete_session, "s1")
        # a new Session with the same id starts clean
        self.store.save_session(a_session())
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 0)

    def test_correct_transcript_overwrites_the_spoken_one_and_drops_the_translation(self):
        session, _ = self._filled_session()
        revision = self.store.load_actions_snapshot("s1").revision
        corrected = Transcript((Segment(0.0, 2.5, "Bună ziua, ANAF", "S1"),), language="ro")

        self.store.correct_transcript("s1", corrected)

        after = self.store.get_session("s1")
        self.assertEqual((after.source_transcript_ref, after.transcript_ref),
                         (session.source_transcript_ref, session.source_transcript_ref))
        self.assertEqual(self.store.load_transcript(session.source_transcript_ref), corrected)
        self.assertRaises(KeyError, self.store.load_transcript, session.transcript_ref)
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, revision + 1)
        self.store.delete_session("s1")
        self.assertRaises(KeyError, self.store.load_transcript, session.source_transcript_ref)

    def test_save_translation_points_at_it_and_keeps_the_spoken_one(self):
        session = a_session()
        session.transcript_ref = self.store.save_transcript("s1", a_transcript())
        self.store.save_session(session)
        english = Transcript((Segment(0.0, 2.5, "Good morning", "S1"),), language="en")

        ref = self.store.save_translation("s1", english)

        after = self.store.get_session("s1")
        self.assertEqual((after.source_transcript_ref, after.transcript_ref), (session.transcript_ref, ref))
        self.assertEqual(self.store.load_transcript(ref), english)
        self.assertEqual(self.store.load_actions_snapshot("s1").revision, 1)
        # already translated: a second translation is refused, nothing changes
        self.assertRaises(ValueError, self.store.save_translation, "s1", english)
        self.assertEqual(self.store.get_session("s1"), after)
        self.assertRaises(LookupError, self.store.save_translation, "s2", english)

    def test_save_translation_after_a_correction_even_once_review_started(self):
        session, _ = self._filled_session()
        self.store.correct_transcript("s1", a_transcript())
        english = Transcript((Segment(0.0, 2.5, "Good morning", "S1"),), language="en")
        ref = self.store.save_translation("s1", english)
        after = self.store.get_session("s1")
        self.assertEqual((after.source_transcript_ref, after.transcript_ref), (session.source_transcript_ref, ref))
        self.store.delete_session("s1")
        for gone in (ref, session.source_transcript_ref):
            self.assertRaises(KeyError, self.store.load_transcript, gone)

    def test_correct_transcript_needs_a_transcribed_session(self):
        self.assertRaises(LookupError, self.store.correct_transcript, "s1", a_transcript())
        self.store.save_session(a_session())
        self.assertRaises(ValueError, self.store.correct_transcript, "s1", a_transcript())

    def test_delete_is_refused_while_an_approved_action_waits(self):
        self._filled_session()
        self.store.save_actions("s1", [approved_action()], expected_revision=1)
        with self.assertRaises(ValueError):
            self.store.delete_session("s1")
        self.assertIsNotNone(self.store.get_session("s1"))
        self.assertEqual(self.store.load_summary("s1"), "rezumat")

    def test_list_sessions_newest_first(self):
        self.assertEqual(self.store.list_sessions(), [])
        older, newer = a_session("s1"), a_session("s2")
        newer.started_at = datetime(2026, 11, 7, 9, 0)
        self.store.save_session(older)
        self.store.save_session(newer)
        self.assertEqual([s.id for s in self.store.list_sessions()], ["s2", "s1"])

    def test_workflow_steps_round_trip_per_step(self):
        self.assertIsInstance(self.store, ExecutionStore)
        self.assertEqual(self.store.load_steps("s1-1"), {})
        self.store.save_step("s1-1", StepRecord("save_document", StepState.STARTED))
        self.store.save_step("s1-1", StepRecord("build_document", StepState.DONE, result={"path": SENTINEL}))
        self.store.save_step("s1-1", StepRecord("save_document", StepState.FAILED, error_type="OSError"))
        self.assertEqual(self.store.load_steps("s1-1"), {
            "build_document": StepRecord("build_document", StepState.DONE, result={"path": SENTINEL}),
            "save_document": StepRecord("save_document", StepState.FAILED, error_type="OSError"),
        })
        self.assertEqual(self.store.load_steps("s1-2"), {})

    def test_audit_keeps_events_in_order(self):
        first, second = an_event(), AuditEvent(AT, "s1-2", "local_document", {"n": 2}, "user")
        self.audit.record(first)
        self.audit.record(second)
        self.assertEqual(self.events(), [first, second])

    def test_results_read_back_in_order(self):
        first = SendResult(AT, "s1-1", "local_document", ok=True)
        second = SendResult(AT, "s1-2", "google_calendar", ok=False, error_type="TimeoutError")
        self.assertEqual(self.audit.load_results(), [])
        self.audit.record_result(first)
        self.audit.record_result(second)
        self.assertEqual(self.audit.load_results(), [first, second])

    def test_loaded_steps_are_copies(self):
        self.store.save_step("s1-1", a_done_step())
        self.store.load_steps("s1-1")["export"].result["path"] = "changed"
        self.assertEqual(self.store.load_steps("s1-1"), {"export": a_done_step()})


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
    def test_used_from_several_threads(self):
        """pywebview calls the backend from its own threads; every write still lands, none is refused."""
        import threading

        self.store.save_session(a_session())
        errors = []

        def write(n):
            try:
                for i in range(20):
                    self.store.save_step(f"a{n}", StepRecord(f"step{i}", StepState.DONE))
                    self.store.get_session("s1")
            except Exception as e:  # pragma: no cover - reported below
                errors.append(e)

        threads = [threading.Thread(target=write, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual([len(self.store.load_steps(f"a{n}")) for n in range(4)], [20] * 4)

    def test_corrected_transcript_is_encrypted_survives_reopening_and_leaves_no_orphan(self):
        StoreContract._filled_session(self)
        corrected = Transcript((Segment(0.0, 2.5, f"corectat {SENTINEL}", "S1"),), language="ro")
        self.store.correct_transcript("s1", corrected)
        self.store.close()
        self.assertNotIn(SENTINEL.encode(), self.data_bytes())
        self.store = self.open()
        self.assertEqual(self.store.load_transcript(self.store.get_session("s1").transcript_ref), corrected)

        self.store.delete_session("s1")
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM records WHERE kind = 'transcript'").fetchone()[0], 0)

    def test_a_failed_correction_changes_nothing(self):
        session, _ = StoreContract._filled_session(self)
        self.assertRaises(TypeError, self.store.correct_transcript, "s1",
                          Transcript((Segment(0.0, 1.0, object()),)))  # not JSON: refused before any write
        self.assertEqual(self.store.get_session("s1"), session)
        self.assertEqual(self.store.load_transcript(session.transcript_ref), a_transcript())

    def test_deleted_session_leaves_no_sentinel_on_disk(self):
        self.store.save_session(a_session())
        self.store.save_summary("s1", f"rezumat {SENTINEL}")
        self.store.delete_session("s1")
        self.store.close()
        self.assertNotIn(SENTINEL.encode(), self.data_bytes())

    def test_steps_survive_a_restart_and_stay_encrypted(self):
        self.store.save_step("s1-1", StepRecord("save_document", StepState.STARTED, result={"note": SENTINEL}))
        self.store.close()
        self.assertNotIn(SENTINEL.encode(), self.data_bytes())
        self.assertEqual(self.open().load_steps("s1-1")["save_document"].state, StepState.STARTED)

    def fill(self) -> str:
        self.store.save_session(a_session())
        ref = self.store.save_transcript("s1", a_transcript())
        self.store.save_actions("s1", [approved_action()], expected_revision=0)
        self.store.save_suggestions("s1", [Suggestion(f"Verifică {SENTINEL}", SENTINEL)], expected_revision=1)
        self.store.record(an_event())
        self.store.record_result(SendResult(AT, "s1-1", "local_document", ok=True))
        self.store.save_step("s1-1", a_done_step())
        self.store.save_step("s1-2", StepRecord("export", StepState.STARTED))
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
        self.assertEqual(again.load_results(), [SendResult(AT, "s1-1", "local_document", ok=True)])
        self.assertEqual(again.load_steps("s1-1"), {"export": a_done_step()})
        self.assertEqual(again.load_steps("s1-2"), {"export": StepRecord("export", StepState.STARTED)})  # still uncertain
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

    def test_secrets_are_encrypted_survive_reopening_and_can_be_deleted(self):
        from norev.adapters.connectors.google_calendar import SecretStore

        self.assertIsInstance(self.store, SecretStore)
        self.assertIsNone(self.store.load_secret("google_calendar.refresh_token"))
        self.store.save_secret("google_calendar.refresh_token", f"refresh {SENTINEL}")
        for encoding in ("utf-8", "utf-16-le"):
            self.assertNotIn(SENTINEL.encode(encoding), self.data_bytes())
        self.store.close()
        self.store = self.open()
        self.assertEqual(self.store.load_secret("google_calendar.refresh_token"), f"refresh {SENTINEL}")
        self.store.delete_secret("google_calendar.refresh_token")
        self.store.delete_secret("google_calendar.refresh_token")  # deleting twice is fine
        self.assertIsNone(self.store.load_secret("google_calendar.refresh_token"))
        self.store.close()
        self.store = self.open()
        self.assertIsNone(self.store.load_secret("google_calendar.refresh_token"))
        with self.assertRaises(ValueError):
            self.store.save_secret("google_calendar.refresh_token", "")

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

    def test_tampered_steps_and_results_are_reported_without_content(self):
        self.fill()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE records SET blob = substr(blob, 1, 20) || x'00' || substr(blob, 22) WHERE kind = 'steps'")
            db.execute("UPDATE results SET blob = substr(blob, 1, 20) || x'00' || substr(blob, 22)")
        for load in (lambda: self.store.load_steps("s1-1"), self.store.load_results):
            with self.assertRaises(StoreCorrupted) as caught:
                load()
            self.assertNotIn(SENTINEL, str(caught.exception))

    def test_steps_moved_to_another_action_are_refused(self):
        self.store.save_step("s1-1", a_done_step())
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE records SET id = 's1-2' WHERE kind = 'steps' AND id = 's1-1'")
        with self.assertRaises(StoreCorrupted):
            self.store.load_steps("s1-2")

    def test_a_result_moved_to_another_seq_is_refused(self):
        self.store.record_result(SendResult(AT, "s1-1", "local_document", ok=True))
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("UPDATE results SET seq = 2")
        with self.assertRaises(StoreCorrupted):
            self.store.load_results()

    def test_step_result_that_is_not_json_is_refused_and_nothing_is_saved(self):
        self.store.save_step("s1-1", StepRecord("export", StepState.STARTED))
        with self.assertRaises(TypeError):
            self.store.save_step("s1-1", StepRecord("export", StepState.DONE, result={"at": datetime(2026, 11, 20)}))
        self.assertEqual(self.store.load_steps("s1-1"), {"export": StepRecord("export", StepState.STARTED)})

    def test_a_store_created_before_results_existed_gets_the_table(self):
        self.store.record(an_event())
        self.store.close()
        with closing(sqlite3.connect(self.db)) as db, db:
            db.execute("DROP TABLE results")
        again = self.open()
        self.assertEqual(again.load_results(), [])
        again.record_result(SendResult(AT, "s1-1", "local_document", ok=True))
        self.assertEqual(len(again.load_results()), 1)
        self.assertEqual(again.load_events(), [an_event()])

    def test_writes_are_fully_synchronous(self):
        self.assertEqual(self.store._db.execute("PRAGMA synchronous").fetchone()[0], 2)  # FULL

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
