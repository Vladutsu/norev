"""Connector `local_document` (domains/README.md, #23); its PayloadBuilder is norev/processor/payloads.py.

A deliberate export of an approved Proposed Action as a plain-text document on this machine,
separate from the encrypted SessionStore (ADR 0004).

The step's payload is the final document: `{"destination": ..., "content": ...}`. The builder makes
it at preview time, so the User sees and approves the exact path and text (ADR 0002). The app picks
the folder when it builds the builder and the Connector, and the file name comes from the content:
nothing in the options, a Domain or a model answer chooses the path. send() checks that the
destination is the one the app would choose for that content, then writes it.

Writes are atomic (temporary file, then a hard link that fails if the name exists), and an existing
file is never overwritten. The same content always gives the same file, so sending it again after
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


def render(values: dict[str, Any]) -> str:
    """The document: one `field: value` line per field, in the step's `sends` order."""
    lines = ["# norev: Proposed Action", ""]
    for name, value in values.items():
        if value is None:
            text = ""
        elif isinstance(value, str):
            text = value.replace("\r\n", "\n").replace("\n", "\n  ")  # continuation lines stay under their field
        else:
            text = json.dumps(value, ensure_ascii=False)
        lines.append(f"{name}: {text}")
    return "\n".join(lines) + "\n"


def export_dir_of(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute():
        raise ValueError("export_dir must be an absolute path chosen by the app")
    return path


def destination(export_dir: Path, content: str) -> Path:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return export_dir / f"norev-{digest}.md"


class LocalDocumentConnector:
    name = NAME

    def __init__(self, export_dir: Path) -> None:
        self.export_dir = export_dir_of(export_dir)

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"destination", "content"} \
                or not isinstance(payload["content"], str):
            raise ValueError("local_document payload must be exactly destination and content")
        content = payload["content"]
        target = destination(self.export_dir, content)
        if payload["destination"] != str(target):
            raise ValueError("destination is not the one the app chose for this content")
        data = content.encode("utf-8")
        self.export_dir.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.export_dir, prefix=".norev-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            try:
                os.link(tmp, target)  # atomic, and never replaces an existing file
            except FileExistsError:
                if target.read_bytes() != data:
                    raise ExportConflict(f"a different file already exists: {target.name}") from None
                return {"path": str(target), "created": False}
        finally:
            os.unlink(tmp)
        return {"path": str(target), "created": True}
