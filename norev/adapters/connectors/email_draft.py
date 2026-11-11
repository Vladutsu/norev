"""Connector `email_draft` (#61): an approved Proposed Action becomes an email draft (.eml) on this
machine. The User opens it in their own mail app and sends it themselves; norev sends no email and
has no access to a mail account.

The step's payload is the final draft, `{"destination": ..., "content": ...}`, like `local_document`:
the builder (norev/processor/payloads.py) makes it at preview time, so the User approves the exact
file text and path (ADR 0002). The folder is the app's and the file name comes from the content.

The draft is plain text, made with the stdlib `email` package:
- headers To, Subject and `X-Unsent: 1` (Outlook opens such a file as a draft ready to send); no
  From, Date or Message-ID, which the mail app sets when the User sends;
- a subject with non-ASCII letters (ș, ț, ă) is written as an RFC 2047 encoded word, which every mail
  app decodes; raw UTF-8 headers are often shown garbled in a loose .eml file;
- the body is UTF-8 text, readable as is (quoted-printable only for lines over 998 characters).

Header injection: recipients must be email addresses, subject and recipients may hold no line
break or other control character, and send() accepts only a file that is exactly what build_eml()
makes from its own To, Subject and body.
"""
import email.parser
import email.policy
import hashlib
import re
import unicodedata
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from norev.adapters.connectors.local_document import export_dir_of, write_new
from norev.core.models import ValidationError

NAME = "email_draft"
MAX_RECIPIENTS = 50
MAX_SUBJECT = 200  # characters
MAX_BODY = 50_000  # characters
MAX_LINE = 998  # RFC 5322 line limit, without CRLF

_POLICY = email.policy.SMTP.clone(max_line_length=MAX_LINE)  # CRLF line ends, as mail files have
_EMAIL = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
                    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+")


def recipients_of(value: Any) -> list[str]:
    """The emails the User typed, separated by commas or semicolons; duplicates dropped."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("required", "recipients")
    _no_control(value, "recipients")
    found: list[str] = []
    for part in re.split(r"[,;]", value):
        address = part.strip()
        if not address:
            continue
        if len(address) > 254 or not _EMAIL.fullmatch(address):
            raise ValidationError("invalid_email", "recipients")
        if address.lower() not in {a.lower() for a in found}:
            found.append(address)
    if not found:
        raise ValidationError("required", "recipients")
    if len(found) > MAX_RECIPIENTS:
        raise ValidationError("too_many", "recipients")
    return found


def _no_control(value: str, field: str) -> None:
    # CR and LF would start a new header, and so can the Unicode line and paragraph separators;
    # no other control character has a place in a header either
    if any(unicodedata.category(c) in ("Cc", "Zl", "Zp") for c in value):
        raise ValidationError("invalid_header", field)


def _subject(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("required", "subject")
    _no_control(value, "subject")
    if len(value.strip()) > MAX_SUBJECT:
        raise ValidationError("too_long", "subject")
    return value.strip()


def _body(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("required", "body")
    body = value.replace("\r\n", "\n").replace("\r", "\n").strip() + "\n"
    if len(body) > MAX_BODY:
        raise ValidationError("too_long", "body")
    if any(unicodedata.category(c) == "Cc" and c not in "\n\t" for c in body):
        raise ValidationError("invalid_text", "body")
    return body


def build_eml(recipients: Any, subject: Any, body: Any) -> str:
    """The exact draft file for these values; ValidationError (code, field) if one is not valid."""
    to, subject, body = recipients_of(recipients), _subject(subject), _body(body)
    message = EmailMessage(policy=_POLICY)
    message["To"] = ", ".join(to)
    message["Subject"] = subject
    message["X-Unsent"] = "1"
    long_lines = any(len(line.encode("utf-8")) > MAX_LINE for line in body.split("\n"))
    message.set_content(body, charset="utf-8", cte="quoted-printable" if long_lines else "8bit")
    return message.as_bytes().decode("utf-8")  # as_string() would re-encode an 8bit body as base64


def read_eml(content: Any) -> dict[str, str]:
    """To, Subject and body of a draft made by build_eml(), decoded for the preview. Anything else
    (extra or repeated headers, another structure, a different encoding) raises ValueError."""
    if not isinstance(content, str):
        raise ValueError("an email draft is text")
    try:
        message = email.parser.BytesParser(policy=_POLICY).parsebytes(content.encode("utf-8"))
        to, subject = str(message["To"] or ""), str(message["Subject"] or "")
        body = message.get_content() if message.get_content_type() == "text/plain" else None
    except Exception:  # a malformed file: its parts may quote Session text, so not in the message
        raise ValueError("not an email draft made by norev") from None
    if body is None or build_eml(to, subject, body) != content:
        raise ValueError("not an email draft made by norev")
    return {"to": to, "subject": subject, "body": body.replace("\r\n", "\n")}


def destination(export_dir: Path, content: str) -> Path:
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
    return export_dir / f"norev-{digest}.eml"


class EmailDraftConnector:
    name = NAME

    def __init__(self, export_dir: Path) -> None:
        self.export_dir = export_dir_of(export_dir)

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or set(payload) != {"destination", "content"} \
                or not isinstance(payload["content"], str):
            raise ValueError("email_draft payload must be exactly destination and content")
        content = payload["content"]
        try:
            read_eml(content)
        except ValidationError:
            raise ValueError("not an email draft made by norev") from None
        target = destination(self.export_dir, content)
        if payload["destination"] != str(target):
            raise ValueError("destination is not the one the app chose for this draft")
        return {"path": str(target), "created": write_new(target, content.encode("utf-8"))}
