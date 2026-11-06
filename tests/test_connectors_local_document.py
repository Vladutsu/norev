import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from norev.adapters.connectors.local_document import ExportConflict, LocalDocumentConnector, render
from norev.core.ports import Connector

PAYLOAD = {"title": "Send the Q4 budget", "owner": None, "topic": "Budget\nline two", "attendees": ["Ana", "Ion"]}


class LocalDocumentTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "exports"  # created on the first send
        self.connector = LocalDocumentConnector(self.dir)

    def _files(self) -> list[str]:
        return sorted(os.listdir(self.dir)) if self.dir.exists() else []

    def test_is_a_connector(self) -> None:
        self.assertIsInstance(self.connector, Connector)
        self.assertEqual(self.connector.name, "local_document")

    def test_writes_exactly_what_the_preview_shows(self) -> None:
        preview = self.connector.preview(PAYLOAD)
        self.assertEqual(self._files(), [], "preview wrote something")
        result = self.connector.send(PAYLOAD)
        self.assertEqual(result, {"path": preview["destination"], "created": True})
        self.assertEqual(Path(result["path"]).read_text(encoding="utf-8"), preview["content"])
        self.assertEqual(Path(result["path"]).parent, self.dir)
        self.assertEqual(self._files(), [Path(result["path"]).name], "a temporary file was left")

    def test_document_has_one_line_per_field(self) -> None:
        self.assertEqual(
            render(PAYLOAD),
            '# norev: Proposed Action\n\ntitle: Send the Q4 budget\nowner: \ntopic: Budget\n  line two\nattendees: ["Ana", "Ion"]\n',
        )

    def test_payload_cannot_choose_the_path(self) -> None:
        result = self.connector.send({"title": "x", "path": "../../evil.bat", "destination": "C:/Windows/x"})
        self.assertEqual(Path(result["path"]).parent, self.dir)
        self.assertRegex(Path(result["path"]).name, r"^norev-[0-9a-f]{16}\.md$")

    def test_sending_again_finds_the_same_file(self) -> None:
        first = self.connector.send(PAYLOAD)
        again = self.connector.send(dict(PAYLOAD))
        self.assertEqual(again, {"path": first["path"], "created": False})
        self.assertEqual(len(self._files()), 1)

    def test_never_overwrites_a_different_file(self) -> None:
        destination = Path(self.connector.preview(PAYLOAD)["destination"])
        self.dir.mkdir()
        destination.write_text("the User's own file", encoding="utf-8")
        with self.assertRaises(ExportConflict):
            self.connector.send(PAYLOAD)
        self.assertEqual(destination.read_text(encoding="utf-8"), "the User's own file")
        self.assertEqual(self._files(), [destination.name])

    def test_failed_write_leaves_nothing(self) -> None:
        with patch("norev.adapters.connectors.local_document.os.link", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.connector.send(PAYLOAD)
        self.assertEqual(self._files(), [])

    def test_export_dir_must_be_absolute(self) -> None:
        with self.assertRaises(ValueError):
            LocalDocumentConnector(Path("exports"))


if __name__ == "__main__":
    unittest.main()
