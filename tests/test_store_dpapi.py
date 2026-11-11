"""The store key sealed with Windows DPAPI (#63), tested on any OS with a fake DPAPI."""
import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

from norev.adapters.store import dpapi
from norev.adapters.store.encrypted import StoreKeyError, StoreSettings, create_key_file, open_store, read_key_file
from norev.core.models import Session
from norev.launcher import __main__ as launcher
from norev.launcher.keys import export_backup, protect_existing
from norev.launcher.paths import AppPaths
from norev.testing.fakes import fake_dpapi

SESSION = Session("s1", datetime(2026, 11, 7, 9, 0, tzinfo=timezone.utc), "corporate")


def answers(*replies):
    replies = iter(replies)
    return lambda prompt: next(replies)


class DpapiTest(unittest.TestCase):
    def test_without_windows_dpapi_refuses_and_a_sealed_key_is_a_clear_error(self):
        if dpapi.available():
            self.skipTest("Windows")
        with self.assertRaises(dpapi.DpapiError):
            dpapi.protect(b"x")
        with tempfile.TemporaryDirectory() as tmp:
            sealed = Path(tmp) / "norev.key.dpapi"
            sealed.write_bytes(b"blob")
            with self.assertRaisesRegex(StoreKeyError, "cannot be opened by this Windows account"):
                read_key_file(sealed)

    def test_a_plain_key_file_still_works_everywhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = Path(tmp) / "norev.key"
            create_key_file(key)
            self.assertEqual(len(read_key_file(key)), 32)


class KeyLifecycleTest(unittest.TestCase):
    """A Windows profile with a store written by the plain key, as before #63."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.paths = AppPaths(Path(tmp.name) / "norev", windows=True)
        self.outside = Path(tmp.name) / "usb"
        dpapi_patch = fake_dpapi()
        dpapi_patch.__enter__()
        self.addCleanup(dpapi_patch.__exit__, None, None, None)
        create_key_file(self.paths.plain_key)
        self.plain = self.paths.plain_key.read_bytes()
        store = open_store(StoreSettings(self.paths.store, self.paths.plain_key))
        store.save_session(SESSION)
        store.close()

    def opens_with(self, key_file: Path) -> bool:
        store = open_store(StoreSettings(self.paths.store, key_file))
        try:
            return store.get_session("s1") == SESSION
        finally:
            store.close()

    def run_quiet(self, fn, *args, **kwargs):
        with redirect_stdout(io.StringIO()) as out:
            code = fn(*args, **kwargs)
        return code, out.getvalue()

    def test_migration_asks_seals_keeps_the_data_and_keeps_the_plain_file_unless_told(self):
        code, out = self.run_quiet(launcher.protect_key, self.paths, ask=answers("DA", "nu"))
        self.assertEqual(code, 0)
        self.assertNotEqual(self.paths.key.read_bytes(), self.plain)
        self.assertEqual(read_key_file(self.paths.key), self.plain)
        self.assertTrue(self.opens_with(self.paths.key))
        self.assertTrue(self.paths.plain_key.is_file())
        self.assertIn("Am păstrat", out)

    def test_migration_happens_once(self):
        self.run_quiet(launcher.protect_key, self.paths, ask=answers("DA", "nu"))
        sealed = self.paths.key.read_bytes()
        code, out = self.run_quiet(launcher.protect_key, self.paths, ask=answers("DA", "DA"))
        self.assertEqual((code, self.paths.key.read_bytes()), (0, sealed))
        self.assertIn("deja protejată", out)
        self.assertTrue(self.paths.plain_key.is_file())

    def test_without_confirmation_nothing_changes(self):
        code, _ = self.run_quiet(launcher.protect_key, self.paths, ask=answers("da"))
        self.assertEqual(code, 1)
        self.assertFalse(self.paths.key.exists())

    def test_the_plain_file_is_deleted_only_on_a_second_explicit_yes(self):
        self.run_quiet(launcher.protect_key, self.paths, ask=answers("DA", "DA"))
        self.assertFalse(self.paths.plain_key.exists())
        self.assertTrue(self.opens_with(self.paths.key))

    def test_a_key_that_does_not_open_the_store_is_not_sealed(self):
        other = self.outside / "other.key"
        create_key_file(other)
        with self.assertRaises(StoreKeyError):
            protect_existing(other, self.paths.key, self.paths.store)
        self.assertFalse(self.paths.key.exists())

    def test_backup_is_deliberate_outside_norev_and_restores_on_a_new_account(self):
        self.run_quiet(launcher.protect_key, self.paths, ask=answers("DA", "DA"))
        backup = self.outside / "norev-key.bak"
        code, out = self.run_quiet(launcher.export_key, self.paths, str(backup), ask=answers("nu"))
        self.assertEqual(code, 1)
        self.assertFalse(backup.exists())
        self.assertIn("NEPROTEJATĂ", out)
        code, _ = self.run_quiet(launcher.export_key, self.paths, str(self.paths.root / "backup.key"), ask=answers("DA"))
        self.assertEqual(code, 1)
        code, _ = self.run_quiet(launcher.export_key, self.paths, str(backup), ask=answers("DA"))
        self.assertEqual((code, backup.read_bytes()), (0, self.plain))
        code, _ = self.run_quiet(launcher.export_key, self.paths, str(backup), ask=answers("DA"))
        self.assertEqual(code, 1)

        self.paths.key.unlink()  # a new Windows account: the old seal is gone
        code, _ = self.run_quiet(launcher.import_key, self.paths, str(backup))
        self.assertEqual(code, 0)
        self.assertTrue(self.opens_with(self.paths.key))
        code, out = self.run_quiet(launcher.import_key, self.paths, str(backup))
        self.assertEqual(code, 1)
        self.assertIn("Nu o suprascriu", out)

    def test_backup_refuses_a_folder_inside_norev(self):
        with self.assertRaisesRegex(ValueError, "outside"):
            export_backup(self.paths.plain_key, self.paths.root / "keys" / "copy.key", self.paths.root)

    def test_new_key_on_windows_is_sealed_and_never_written_in_clear(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp), windows=True)
            code, out = self.run_quiet(launcher.new_key, paths)
            self.assertEqual(code, 0)
            self.assertTrue(paths.key.is_file())
            self.assertFalse(paths.plain_key.exists())
            self.assertIn("export-key", out)

    def test_on_linux_the_key_stays_a_file_and_dpapi_commands_say_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = AppPaths(Path(tmp), windows=False)
            self.run_quiet(launcher.new_key, paths)
            self.assertEqual(paths.key, paths.plain_key)
            self.assertEqual(len(read_key_file(paths.key)), 32)
            code, out = self.run_quiet(launcher.protect_key, paths, ask=answers("DA"))
            self.assertEqual(code, 1)
            self.assertIn("doar pe Windows", out)


if __name__ == "__main__":
    unittest.main()
