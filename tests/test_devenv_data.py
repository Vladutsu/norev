import hashlib
import importlib.util
import tempfile
import unittest
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "data.py"
_spec = importlib.util.spec_from_file_location("devenv_data", _SCRIPT)
data = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(data)


def manifest_line(relative_path: str, content: bytes) -> str:
    return f"{hashlib.sha256(content).hexdigest()}  {len(content)}  {relative_path}\n"


class DataManifestTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.data_dir = self.root / "data"
        (self.data_dir / "clips").mkdir(parents=True)
        self.manifest = self.root / "data.manifest"

    def tearDown(self):
        self._tmp.cleanup()

    def write_manifest(self, *lines: str):
        self.manifest.write_text("# dev data\n\n" + "".join(lines), encoding="utf-8")

    def test_verify_passes_when_every_file_matches(self):
        (self.data_dir / "clips" / "a.wav").write_bytes(b"audio")
        self.write_manifest(manifest_line("clips/a.wav", b"audio"))

        self.assertEqual(data.verify(self.manifest, self.data_dir), [])
        self.assertEqual(data.main(["verify", "--manifest", str(self.manifest), "--data", str(self.data_dir)]), 0)

    def test_verify_fails_on_a_missing_file(self):
        self.write_manifest(manifest_line("clips/missing.wav", b"audio"))

        problems = data.verify(self.manifest, self.data_dir)

        self.assertEqual(len(problems), 1)
        self.assertIn("missing", problems[0])
        self.assertEqual(data.main(["verify", "--manifest", str(self.manifest), "--data", str(self.data_dir)]), 1)

    def test_verify_fails_on_a_wrong_checksum(self):
        (self.data_dir / "clips" / "a.wav").write_bytes(b"AUDIO")  # same size, other content
        self.write_manifest(manifest_line("clips/a.wav", b"audio"))

        problems = data.verify(self.manifest, self.data_dir)

        self.assertEqual(len(problems), 1)
        self.assertIn("checksum", problems[0])

    def test_verify_fails_on_a_wrong_size(self):
        (self.data_dir / "clips" / "a.wav").write_bytes(b"longer audio")
        self.write_manifest(manifest_line("clips/a.wav", b"audio"))

        self.assertIn("size", data.verify(self.manifest, self.data_dir)[0])

    def test_manifest_rejects_paths_outside_the_data_dir(self):
        for bad in ("../escape.wav", "/etc/passwd"):
            self.write_manifest(manifest_line(bad, b"x"))
            with self.assertRaises(ValueError):
                data.read_manifest(self.manifest)

    def test_fetch_copies_missing_files_from_a_local_source(self):
        source = self.root / "source"
        (source / "clips").mkdir(parents=True)
        (source / "clips" / "a.wav").write_bytes(b"audio")
        self.write_manifest(manifest_line("clips/a.wav", b"audio"))

        self.assertEqual(data.fetch(self.manifest, self.data_dir, str(source)), [])
        self.assertEqual((self.data_dir / "clips" / "a.wav").read_bytes(), b"audio")

    def test_fetch_skips_files_that_already_match(self):
        (self.data_dir / "clips" / "a.wav").write_bytes(b"audio")
        self.write_manifest(manifest_line("clips/a.wav", b"audio"))

        # the source does not exist, so any download attempt would be reported
        self.assertEqual(data.fetch(self.manifest, self.data_dir, str(self.root / "no-source")), [])

    def test_fetch_does_not_keep_a_file_with_a_wrong_checksum(self):
        source = self.root / "source"
        (source / "clips").mkdir(parents=True)
        (source / "clips" / "a.wav").write_bytes(b"AUDIO")
        self.write_manifest(manifest_line("clips/a.wav", b"audio"))

        problems = data.fetch(self.manifest, self.data_dir, str(source))

        self.assertEqual(len(problems), 1)
        self.assertFalse((self.data_dir / "clips" / "a.wav").exists())


if __name__ == "__main__":
    unittest.main()
