"""JSON over HTTP for the Connectors that leave the machine (Trello #72, Jira #73), with the stdlib only.

Errors keep only the HTTP status and a short error code, never the answer's text, a token or a
payload: an answer can quote Session content or credentials.
"""
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

MAX_ANSWER = 1_000_000  # bytes read from an answer


class HttpError(Exception):
    """The service answered with an HTTP error; only the status and its short error code are kept."""

    def __init__(self, status: int, error: str | None = None) -> None:
        super().__init__(f"the service answered HTTP {status}" + (f" ({error})" if error else ""))
        self.status = status
        self.error = error


class Unreachable(ConnectionError):
    """No answer (network, DNS, timeout)."""


def https_or_loopback(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname == "127.0.0.1"):
        return url
    raise ValueError("a service URL must be https (http only on 127.0.0.1, for the tests' fake server)")


def _short_code(raw: bytes) -> str | None:
    try:
        error = json.loads(raw).get("error")
    except Exception:
        return None
    if isinstance(error, dict):
        error = error.get("code") or error.get("status")
    return error if isinstance(error, str) and error.isascii() and len(error) <= 64 else None


def call_json(method: str, url: str, *, headers: dict[str, str] | None = None, query: dict[str, str] | None = None,
              body: Any = None, timeout: float = 15) -> Any:
    """One request; the decoded JSON answer (None for an empty one)."""
    if query:
        url += "?" + urllib.parse.urlencode(query)
    data = None if body is None else json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Accept": "application/json", **({"Content-Type": "application/json; charset=utf-8"} if data else {}),
        **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            raw = answer.read(MAX_ANSWER + 1)
            status = answer.status
    except urllib.error.HTTPError as exc:
        with exc:
            raise HttpError(exc.code, _short_code(exc.read(10_000))) from None
    except (urllib.error.URLError, OSError):
        raise Unreachable("the service could not be reached") from None
    if len(raw) > MAX_ANSWER:
        raise HttpError(status, "answer_too_large")
    if not raw.strip():
        return None
    try:
        return json.loads(raw)
    except ValueError:
        raise HttpError(status, "not_json") from None
