"""Connector `local_document` (domains/README.md): saves an approved Proposed Action as a document on
this machine (#23).

This is a deliberate export, in plain text, separate from the encrypted SessionStore (ADR 0004).
The app picks the folder when it builds the Connector, and the file name comes from the content:
nothing in the payload or in a model answer chooses the path. preview() returns the exact
destination and content that send() writes.

Writes are atomic (temporary file, then a hard link that fails if the name exists), and an existing
file is never overwritten. The same payload always gives the same file, so sending it again after
an uncertain attempt finds the file and does not write a second one.
"""
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

NAME = "local_document"


class ExportConflict(FileExistsError):
    """A different file already has the export's name. Nothing was written."""


def render(payload: dict[str, Any]) -> str:
    """The document: one `field: value` line per payload field, in payload order."""
    lines = ["# norev: Proposed Action", ""]
    for name, value in payload.items():
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value.replace("\r\n", "\n").replace("\n", "\n  ")  # continuation lines stay under their field
        else:
            text = json.dumps(value, ensure_ascii=False)
        lines.append(f"{name}: {text}")
    return "\n".join(lines) + "\n"


class LocalDocumentConnector:
    name = NAME

    def __init__(self, export_dir: Path) -> None:
        export_dir = Path(export_dir)
        if not export_dir.is_absolute():
            raise ValueError("export_dir must be an absolute path chosen by the app")
        self.export_dir = export_dir

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = render(payload)
        return {"destination": str(self._destination(content)), "content": content}

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = render(payload)
        data = content.encode("utf-8")
        destination = self._destination(content)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.export_dir, prefix=".norev-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, destination)  # atomic, and never replaces an existing file
            except FileExistsError:
                if destination.read_bytes() != data:
                    raise ExportConflict(f"a different file already exists: {destination.name}") from None
                return {"path": str(destination), "created": False}
        finally:
            os.unlink(tmp)
        return {"path": str(destination), "created": True}

    def _destination(self, content: str) -> Path:
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        return self.export_dir / f"norev-{digest}.md"
