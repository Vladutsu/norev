"""Key slots (ADR 0006): the data key sealed per User password and per Invitation code."""
import json
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from norev.adapters.store.slots import InvitationExpired, KeySlots, SignInFailed, slot_id

KEY = bytes(range(32))
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


class KeySlotsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "keys" / "norev.slots"
        self.slots = KeySlots(self.path, n=2 ** 10)  # fast in tests; the app uses 2**15

    def tearDown(self):
        self.dir.cleanup()

    def test_a_password_opens_its_slot(self):
        self.assertTrue(self.slots.empty())
        self.slots.add("user", "Ana@Firma.ro", "parola lunga", KEY)
        self.assertFalse(self.slots.empty())
        self.assertEqual(self.slots.unlock("user", " ana@firma.ro ", "parola lunga"), KEY)

    def test_wrong_password_and_unknown_email_fail_the_same_way(self):
        self.slots.add("user", "ana@firma.ro", "parola lunga", KEY)
        for email, secret in (("ana@firma.ro", "parola gresita"), ("ion@firma.ro", "parola lunga"), ("ana@firma.ro", None)):
            with self.assertRaises(SignInFailed):
                self.slots.unlock("user", email, secret)

    def test_kinds_are_separate(self):
        self.slots.add("invitation", "ion@firma.ro", "COD-123", KEY, expires=NOW + timedelta(days=7))
        self.assertTrue(self.slots.empty())  # an Invitation is not a User
        with self.assertRaises(SignInFailed):
            self.slots.unlock("user", "ion@firma.ro", "COD-123")
        self.assertEqual(self.slots.unlock("invitation", "ion@firma.ro", "COD-123", now=NOW), KEY)

    def test_an_expired_invitation_does_not_open(self):
        self.slots.add("invitation", "ion@firma.ro", "COD-123", KEY, expires=NOW + timedelta(days=7))
        with self.assertRaises(InvitationExpired):
            self.slots.unlock("invitation", "ion@firma.ro", "COD-123", now=NOW + timedelta(days=7))
        with self.assertRaises(SignInFailed):  # the wrong code still says nothing about expiry
            self.slots.unlock("invitation", "ion@firma.ro", "ALT", now=NOW + timedelta(days=8))

    def test_no_email_password_or_key_in_clear_and_owner_only(self):
        self.slots.add("user", "ana@firma.ro", "parola lunga", KEY)
        raw = self.path.read_bytes()
        for secret in (b"ana@firma.ro", b"parola lunga", KEY, KEY.hex().encode()):
            self.assertNotIn(secret, raw)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_a_slot_copied_under_another_email_does_not_open(self):
        self.slots.add("user", "ana@firma.ro", "parola", KEY)
        data = json.loads(self.path.read_text())
        data["slots"][slot_id("user", "mallory@x.ro")] = data["slots"][slot_id("user", "ana@firma.ro")]
        self.path.write_text(json.dumps(data))
        with self.assertRaises(SignInFailed):
            self.slots.unlock("user", "mallory@x.ro", "parola")

    def test_remove_and_replace(self):
        self.slots.add("user", "ana@firma.ro", "veche", KEY)
        self.slots.add("user", "ana@firma.ro", "noua", KEY)
        with self.assertRaises(SignInFailed):
            self.slots.unlock("user", "ana@firma.ro", "veche")
        self.slots.remove("user", "ana@firma.ro")
        self.assertFalse(self.slots.has("user", "ana@firma.ro"))
        self.slots.remove("user", "ana@firma.ro")  # removing twice is fine

    def test_a_foreign_file_is_refused(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text('{"not": "slots"}')
        with self.assertRaises(ValueError):
            self.slots.empty()


if __name__ == "__main__":
    unittest.main()
