import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from norev.adapters.connectors.local_document import ExportConflict, LocalDocumentConnector, destination, render
from norev.core.models import (
    ActionType,
    Domain,
    DomainField,
    ProposedAction,
    UnsupportedWorkflow,
    ValidationError,
    Workflow,
    WorkflowStep,
)
from norev.core.ports import Connector, PayloadBuilder
from norev.processor.payloads import LocalDocumentPayloadBuilder
from norev.usecases.approve import domain_digest

VALUES = {"title": "Send the Q4 budget", "owner": None, "topic": "Budget\nline two", "attendees": ["Ana", "Ion"]}


def _domain(*steps: WorkflowStep) -> Domain:
    return Domain(
        name="test", label="Test", model="m.gguf", context=4096, roles=(), prompt_template="", answer_schema={},
        action_types=(ActionType("minutes", (DomainField("topic", "string", True),), Workflow(steps)),),
    )


EXPORT = WorkflowStep("export", connector="local_document", sends=("title", "owner", "due_date", "topic"))
ACTION = ProposedAction(id="s1-1", title="Send the Q4 budget", description="d", transcript_excerpt="e",
                        due_date=date(2026, 11, 9), action_type="minutes", fields={"topic": "Budget"})


class LocalDocumentConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "exports"  # created on the first send
        self.connector = LocalDocumentConnector(self.dir)
        self.content = render(VALUES)
        self.payload = {"destination": str(destination(self.dir, self.content)), "content": self.content}

    def _files(self) -> list[str]:
        return sorted(os.listdir(self.dir)) if self.dir.exists() else []

    def test_is_a_connector(self) -> None:
        self.assertIsInstance(self.connector, Connector)
        self.assertEqual(self.connector.name, "local_document")

    def test_writes_the_content_at_the_destination(self) -> None:
        result = self.connector.send(self.payload)
        self.assertEqual(result, {"path": self.payload["destination"], "created": True})
        self.assertEqual(Path(result["path"]).read_text(encoding="utf-8"), self.content)
        self.assertEqual(self._files(), [Path(result["path"]).name], "a temporary file was left")

    def test_document_has_one_line_per_field(self) -> None:
        self.assertEqual(
            self.content,
            '# norev: Proposed Action\n\ntitle: Send the Q4 budget\nowner: \ntopic: Budget\n  line two\nattendees: ["Ana", "Ion"]\n',
        )

    def test_payload_cannot_choose_the_path(self) -> None:
        for bad in ("../../evil.bat", "C:/Windows/x.md", str(self.dir / "chosen.md"), str(self.dir.parent / Path(self.payload["destination"]).name)):
            with self.subTest(destination=bad), self.assertRaises(ValueError):
                self.connector.send({"destination": bad, "content": self.content})
        self.assertEqual(self._files(), [])

    def test_payload_must_be_exactly_destination_and_content(self) -> None:
        for bad in ({"content": self.content}, {**self.payload, "path": "x"}, {**self.payload, "content": 1}, ["x"]):
            with self.subTest(payload=bad), self.assertRaises(ValueError):
                self.connector.send(bad)

    def test_sending_again_finds_the_same_file(self) -> None:
        first = self.connector.send(self.payload)
        again = self.connector.send(dict(self.payload))
        self.assertEqual(again, {"path": first["path"], "created": False})
        self.assertEqual(len(self._files()), 1)

    def test_never_overwrites_a_different_file(self) -> None:
        target = Path(self.payload["destination"])
        self.dir.mkdir()
        target.write_text("the User's own file", encoding="utf-8")
        with self.assertRaises(ExportConflict):
            self.connector.send(self.payload)
        self.assertEqual(target.read_text(encoding="utf-8"), "the User's own file")
        self.assertEqual(self._files(), [target.name])

    def test_failed_write_leaves_nothing(self) -> None:
        with patch("norev.adapters.connectors.local_document.os.link", side_effect=OSError("no space")):
            with self.assertRaises(OSError):
                self.connector.send(self.payload)
        self.assertEqual(self._files(), [])

    def test_export_dir_must_be_absolute(self) -> None:
        with self.assertRaises(ValueError):
            LocalDocumentConnector(Path("exports"))


class LocalDocumentPayloadBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.builder = LocalDocumentPayloadBuilder(self.dir)

    def test_is_a_payload_builder(self) -> None:
        self.assertIsInstance(self.builder, PayloadBuilder)

    def test_plan_holds_the_final_document_and_its_path(self) -> None:
        domain = _domain(WorkflowStep("prepare"), EXPORT)
        plan = self.builder.build(ACTION, domain, {})
        content = "# norev: Proposed Action\n\ntitle: Send the Q4 budget\nowner: \ndue_date: 2026-11-09\ntopic: Budget\n"
        self.assertEqual(plan, {
            "version": 1, "domain": "test", "domain_digest": domain_digest(domain), "action_type": "minutes",
            "steps": [{"step": "export", "connector": "local_document",
                       "payload": {"destination": str(destination(self.dir, content)), "content": content}}],
        })
        self.assertEqual(os.listdir(self.dir), [], "building a plan wrote something")

    def test_edited_content_gets_its_own_path(self) -> None:
        plan = self.builder.build(ACTION, _domain(EXPORT), {"export": {"content": "My text.\n"}})
        self.assertEqual(plan["steps"][0]["payload"],
                         {"destination": str(destination(self.dir, "My text.\n")), "content": "My text.\n"})

    def test_options_cannot_choose_the_path_or_anything_else(self) -> None:
        for options in ({"export": {"destination": "C:/x.md"}}, {"export": {"content": ""}}, {"export": {"content": 3}},
                        {"export": "text"}, {"other_step": {}}):
            with self.subTest(options=options), self.assertRaises(ValidationError):
                self.builder.build(ACTION, _domain(EXPORT), options)

    def test_other_connectors_are_unsupported(self) -> None:
        calendar = WorkflowStep("send", connector="google_calendar", sends=("topic",))
        with self.assertRaises(UnsupportedWorkflow):
            self.builder.build(ACTION, _domain(EXPORT, calendar), {})

    def test_export_dir_must_be_absolute(self) -> None:
        with self.assertRaises(ValueError):
            LocalDocumentPayloadBuilder(Path("exports"))


if __name__ == "__main__":
    unittest.main()
