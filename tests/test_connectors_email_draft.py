"""Connector email_draft and its PayloadBuilder (#61): an approved Proposed Action becomes an .eml
draft on this machine. Header injection, the exact file, atomic writes with no overwrite."""
import email
import email.policy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from norev.adapters.connectors.email_draft import (
    EmailDraftConnector,
    build_eml,
    destination,
    read_eml,
    recipients_of,
)
from norev.adapters.connectors.local_document import ExportConflict
from norev.adapters.domains.loader import DOMAINS, load_domain
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
from norev.processor.payloads import EmailDraftPayloadBuilder, email_preview
from norev.usecases.approve import domain_digest

CORPORATE = load_domain(DOMAINS / "corporate")
LF, CR = chr(10), chr(13)
CRLF = CR + LF
SUBJECT = "Cifrele pentru ședință"
BODY = "Bună ziua," + LF + LF + "Cifrele pe Q4 sunt cele discutate azi." + LF


def follow_up(**fields) -> ProposedAction:
    values = {"recipients": "ana@example.com", "subject": SUBJECT, "body": BODY} | fields
    return ProposedAction("s1-1", "Send the Q4 figures", "Email the figures.", "I'll email you the figures",
                          action_type="follow_up_email", fields=values)


class BuildEmlTest(unittest.TestCase):
    def test_exact_draft(self) -> None:
        content = build_eml("ana@example.com, Bob@Example.com; ana@example.com", SUBJECT, BODY)
        self.assertEqual(content, CRLF.join([
            "To: ana@example.com, Bob@Example.com",
            "Subject: Cifrele pentru =?utf-8?b?yJllZGluyJvEgw==?=",
            "X-Unsent: 1",
            'Content-Type: text/plain; charset="utf-8"',
            "Content-Transfer-Encoding: 8bit",
            "MIME-Version: 1.0",
            "",
            "Bună ziua,",
            "",
            "Cifrele pe Q4 sunt cele discutate azi.",
            "",
        ]))
        for header in ("From:", "Date:", "Message-ID:", "Bcc:", "Cc:"):
            self.assertNotIn(header, content)  # the mail app sets them when the User sends

    def test_a_mail_app_reads_it_back(self) -> None:
        message = email.message_from_bytes(build_eml("ana@example.com", SUBJECT, BODY).encode("utf-8"),
                                           policy=email.policy.default)
        self.assertEqual((message["To"], message["Subject"], message["X-Unsent"]), ("ana@example.com", SUBJECT, "1"))
        self.assertEqual(message.get_content().replace(CRLF, LF), BODY)

    def test_preview_decodes_to_subject_and_body(self) -> None:
        content = build_eml("ana@example.com", SUBJECT, BODY)
        self.assertEqual(read_eml(content), {"to": "ana@example.com", "subject": SUBJECT, "body": BODY})
        self.assertEqual(email_preview(content), read_eml(content))
        self.assertIsNone(email_preview("not a draft"))

    def test_a_very_long_line_is_quoted_printable_and_still_reads_back(self) -> None:
        body = "ș" * 700 + LF
        content = build_eml("ana@example.com", "x", body)
        self.assertIn("Content-Transfer-Encoding: quoted-printable", content)
        self.assertTrue(all(len(line.encode()) <= 998 for line in content.split(CRLF)))
        self.assertEqual(read_eml(content)["body"], body)

    def test_same_values_same_draft(self) -> None:
        self.assertEqual(build_eml("ana@example.com", SUBJECT, BODY), build_eml("ana@example.com", SUBJECT, BODY))


class HeaderInjectionTest(unittest.TestCase):
    """No value can add a header (Bcc to a third party) or break the draft's structure."""

    def assert_refused(self, code: str, field: str, **values) -> None:
        values = {"recipients": "ana@example.com", "subject": SUBJECT, "body": BODY} | values
        with self.assertRaises(ValidationError) as caught:
            build_eml(values["recipients"], values["subject"], values["body"])
        self.assertEqual((caught.exception.code, caught.exception.field), (code, field))

    def test_line_breaks_in_the_subject(self) -> None:
        for separator in (CRLF, LF, CR, chr(0x85), chr(0x2028), chr(0x2029), chr(0x0B), chr(0x0C)):
            with self.subTest(separator=hex(ord(separator[-1]))):
                self.assert_refused("invalid_header", "subject", subject="Hello" + separator + "Bcc: evil@example.com")

    def test_line_breaks_in_the_recipients(self) -> None:
        for separator in (CRLF, LF, CR, chr(0x2028)):
            with self.subTest(separator=hex(ord(separator[-1]))):
                self.assert_refused("invalid_header", "recipients",
                                    recipients="ana@example.com" + separator + "Bcc: evil@example.com")

    def test_other_control_characters(self) -> None:
        self.assert_refused("invalid_header", "subject", subject="a" + chr(0) + "b")
        self.assert_refused("invalid_header", "recipients", recipients="ana@example.com" + chr(9))
        self.assert_refused("invalid_text", "body", body="a" + chr(0) + "b")

    def test_recipients_must_be_email_addresses(self) -> None:
        for value in ("S1", "Ana", "ana@", "@example.com", "ana@example", "Ana <ana@example.com>",
                      "ana@example.com, S2", "ana@exa mple.com", "ană@example.com", "a" * 250 + "@example.com"):
            with self.subTest(value):
                self.assert_refused("invalid_email", "recipients", recipients=value)
        self.assertEqual(recipients_of(" ana@example.com ;bob@example.co.uk,"), ["ana@example.com", "bob@example.co.uk"])

    def test_required_and_limits(self) -> None:
        for name in ("recipients", "subject", "body"):
            for empty in (None, "", "   "):
                with self.subTest(name=name, empty=empty):
                    self.assert_refused("required", name, **{name: empty})
        self.assert_refused("too_long", "subject", subject="x" * 201)
        self.assert_refused("too_long", "body", body="x" * 50_001)
        self.assert_refused("too_many", "recipients", recipients=",".join(f"a{i}@example.com" for i in range(51)))

    def test_body_line_breaks_are_text_not_headers(self) -> None:
        content = build_eml("ana@example.com", "x", "Bcc: evil@example.com" + LF + "hello")
        message = email.message_from_bytes(content.encode(), policy=email.policy.default)
        self.assertIsNone(message["Bcc"])
        self.assertIn("Bcc: evil@example.com", message.get_content())

    def test_a_file_not_made_by_build_eml_is_refused(self) -> None:
        content = build_eml("ana@example.com", SUBJECT, BODY)
        for tampered in (
            content.replace("X-Unsent: 1" + CRLF, "X-Unsent: 1" + CRLF + "Bcc: evil@example.com" + CRLF),
            content.replace("To: ana@example.com", "To: ana@example.com" + CRLF + "To: evil@example.com"),
            content.replace("Content-Type: text/plain", "Content-Type: text/html"),
            content.replace(CRLF, LF),
            content + "more",
            "",
        ):
            with self.subTest(tampered=tampered[:60]), self.assertRaises(ValueError):
                read_eml(tampered)


class EmailDraftConnectorTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.export_dir = Path(tmp.name) / "export"
        self.connector = EmailDraftConnector(self.export_dir)
        self.content = build_eml("ana@example.com", SUBJECT, BODY)
        self.payload = {"destination": str(destination(self.export_dir, self.content)), "content": self.content}

    def test_writes_the_exact_draft(self) -> None:
        self.assertIsInstance(self.connector, Connector)
        result = self.connector.send(self.payload)
        path = Path(result["path"])
        self.assertEqual((result["created"], path.suffix, path.parent), (True, ".eml", self.export_dir))
        self.assertEqual(path.read_bytes(), self.content.encode("utf-8"))
        self.assertEqual([p.name for p in self.export_dir.iterdir()], [path.name])  # no temporary file left

    def test_retry_writes_no_second_file(self) -> None:
        self.connector.send(self.payload)
        self.assertEqual(self.connector.send(self.payload)["created"], False)
        self.assertEqual(len(list(self.export_dir.iterdir())), 1)

    def test_never_overwrites_a_different_file(self) -> None:
        target = Path(self.payload["destination"])
        self.export_dir.mkdir()
        target.write_bytes(b"the User's own file")
        with self.assertRaises(ExportConflict):
            self.connector.send(self.payload)
        self.assertEqual(target.read_bytes(), b"the User's own file")

    def test_payload_cannot_choose_the_path(self) -> None:
        for path in (str(self.export_dir / "x.eml"), str(self.export_dir.parent / Path(self.payload["destination"]).name),
                     str(self.export_dir / "norev-0000000000000000.eml")):
            with self.subTest(path), self.assertRaises(ValueError):
                self.connector.send({**self.payload, "destination": path})
        self.assertFalse(self.export_dir.exists())

    def test_only_a_draft_made_by_norev_is_written(self) -> None:
        injected = self.content.replace("X-Unsent: 1", "X-Unsent: 1" + CRLF + "Bcc: evil@example.com")
        for payload in (
            {"destination": str(destination(self.export_dir, injected)), "content": injected},
            {**self.payload, "extra": 1},
            {"content": self.content},
            {**self.payload, "content": None},
        ):
            with self.subTest(payload=str(payload)[:60]), self.assertRaises(ValueError):
                self.connector.send(payload)
        self.assertFalse(self.export_dir.exists())

    def test_the_export_folder_must_be_absolute(self) -> None:
        with self.assertRaises(ValueError):
            EmailDraftConnector(Path("relative"))

    def test_a_failed_write_leaves_no_file(self) -> None:
        with patch("norev.adapters.connectors.local_document.os.link", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                self.connector.send(self.payload)
        self.assertEqual(list(self.export_dir.iterdir()), [])


class EmailDraftPayloadBuilderTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.export_dir = Path(tmp.name)
        self.builder = EmailDraftPayloadBuilder(self.export_dir)

    def test_exact_payload(self) -> None:
        self.assertIsInstance(self.builder, PayloadBuilder)
        plan = self.builder.build(follow_up(), CORPORATE, {})
        content = build_eml("ana@example.com", SUBJECT, BODY)
        self.assertEqual(plan, {
            "version": 1, "domain": "corporate", "domain_digest": domain_digest(CORPORATE),
            "action_type": "follow_up_email",
            "steps": [{"step": "save_email", "connector": "email_draft",
                       "payload": {"destination": str(destination(self.export_dir, content)), "content": content}}],
        })

    def test_missing_or_invalid_fields_block_the_preview(self) -> None:
        for fields, code in (({"recipients": None}, "required"), ({"subject": None}, "required"),
                             ({"body": None}, "required"), ({"recipients": "S2"}, "invalid_email"),
                             ({"subject": "x" + LF + "Bcc: evil@example.com"}, "invalid_header")):
            with self.subTest(fields=fields), self.assertRaises(ValidationError) as caught:
                self.builder.build(follow_up(**fields), CORPORATE, {})
            self.assertEqual(caught.exception.code, code)

    def test_options_cannot_choose_anything(self) -> None:
        with self.assertRaises(ValidationError):
            self.builder.build(follow_up(), CORPORATE, {"save_email": {"destination": "/tmp/x.eml"}})

    def test_other_connectors_and_other_fields_are_refused(self) -> None:
        def domain(step: WorkflowStep) -> Domain:
            return Domain(name="t", label="T", model="m.gguf", context=4096, roles=(), prompt_template="", answer_schema={},
                          action_types=(ActionType("follow_up_email", (DomainField("recipients", "string", True),),
                                                   Workflow((step,))),))
        for step in (WorkflowStep("send", connector="local_document", sends=("recipients", "subject", "body")),
                     WorkflowStep("send", connector="email_draft", sends=("recipients", "subject", "body", "owner")),
                     WorkflowStep("send", connector="email_draft", sends=("recipients", "subject"))):
            with self.subTest(step=step), self.assertRaises(UnsupportedWorkflow):
                self.builder.build(follow_up(), domain(step), {})


if __name__ == "__main__":
    unittest.main()
