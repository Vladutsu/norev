"""Connector `google_calendar` (#45): creates the approved event in the User's Google Calendar, with
the stdlib only. The first Connector that sends data off the machine; the Central Processor calls it
only with the exact approved payload and audits each attempt (#22).

- send(): the payload is #44's exact `events.insert` request, `{"calendar_id", "event"}`, built at
  preview by GoogleCalendarPayloadBuilder (norev/processor/payloads.py). The event id is in the
  approved payload, so sending it again (a manual Retry after an uncertain or failed attempt) finds
  the event Google already has instead of creating a second one. No invitation emails are sent.

- connect(): OAuth for installed apps in the User's own browser, with a loopback redirect
  (http://127.0.0.1:<free port>) and PKCE (S256). Scope: Calendar events only. Nothing goes through a
  public URL; the callback server listens on 127.0.0.1 only, for one answer with our `state`.
- The refresh token is kept in the app's encrypted store (SecretStore), never in the repository, a log
  or a plaintext file. The access token lives in memory only, until it expires.
- disconnect(): revokes the token at Google and always deletes the local one.
- Errors carry an HTTP status or a fixed reason code, never a token or a Google answer body.
"""
import base64
import hashlib
import json
import os
import secrets
import socketserver
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Protocol, runtime_checkable

NAME = "google_calendar"
SCOPE = "https://www.googleapis.com/auth/calendar.events"  # events only: no calendar list, sharing or settings
REFRESH_TOKEN = "google_calendar.refresh_token"  # the SecretStore name
MAX_ANSWER = 1_000_000  # bytes read from a Google answer
EXPIRY_MARGIN = 60  # seconds: refresh an access token this long before Google says it expires


@runtime_checkable
class SecretStore(Protocol):
    """Small secrets of the app, encrypted at rest (ADR 0004).

    # shortcut: defined here, not in core/ports, until laptop 1 confirms it; EncryptedStore implements it
    """

    def load_secret(self, name: str) -> str | None: ...

    def save_secret(self, name: str, value: str) -> None: ...

    def delete_secret(self, name: str) -> None: ...


class NotConfigured(ValueError):
    """No OAuth client: NOREV_GOOGLE_CLIENT_ID and NOREV_GOOGLE_CLIENT_SECRET are not set."""


class NotConnected(Exception):
    """No Google account is connected: the User connects it first."""


class ReconnectNeeded(NotConnected):
    """Google refused the refresh token (revoked or expired). The local token was deleted."""


class AuthorizationFailed(Exception):
    """The sign-in did not complete; the message is a fixed reason code. Nothing was saved."""


class GoogleError(Exception):
    """Google answered with an HTTP error; only the status and Google's error code are kept."""

    def __init__(self, status: int, error: str | None = None) -> None:
        super().__init__(f"Google answered HTTP {status}" + (f" ({error})" if error else ""))
        self.status = status
        self.error = error


class GoogleUnreachable(ConnectionError):
    """No answer from Google (network, DNS, timeout)."""


def _local_or_https(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname == "127.0.0.1"):
        return url
    raise ValueError("a Google endpoint must be https (http only on 127.0.0.1, for the tests' fake server)")


@dataclass(frozen=True)
class GoogleEndpoints:
    auth: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token: str = "https://oauth2.googleapis.com/token"
    revoke: str = "https://oauth2.googleapis.com/revoke"
    api: str = "https://www.googleapis.com/calendar/v3"

    def __post_init__(self) -> None:
        for url in (self.auth, self.token, self.revoke, self.api):
            _local_or_https(url)


@dataclass(frozen=True)
class GoogleCalendarSettings:
    """The OAuth client of a Google Cloud project ("Desktop app"). Not committed: set in the environment."""

    client_id: str | None
    client_secret: str | None = field(default=None, repr=False)

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "GoogleCalendarSettings":
        env = os.environ if env is None else env
        return cls(env.get("NOREV_GOOGLE_CLIENT_ID") or None, env.get("NOREV_GOOGLE_CLIENT_SECRET") or None)


def _read(answer) -> dict[str, Any]:
    body = answer.read(MAX_ANSWER + 1)
    if len(body) > MAX_ANSWER:
        raise GoogleError(answer.status, "answer_too_large")
    try:
        data = json.loads(body or b"{}")
    except ValueError:
        raise GoogleError(answer.status, "not_json") from None
    if not isinstance(data, dict):
        raise GoogleError(answer.status, "not_json")
    return data


def _error_code(exc: urllib.error.HTTPError) -> str | None:
    """Google's short error code (e.g. invalid_grant), never its description."""
    try:
        error = json.loads(exc.read(10_000)).get("error")
    except Exception:
        return None
    if isinstance(error, dict):  # the Calendar API nests it: {"error": {"errors": [{"reason": ...}]}}
        reasons = error.get("errors")
        first = reasons[0] if isinstance(reasons, list) and reasons and isinstance(reasons[0], dict) else {}
        error = first.get("reason") or error.get("status")
    return error if isinstance(error, str) and error.isascii() and len(error) <= 64 else None


def post_form(url: str, form: dict[str, str], timeout: float) -> dict[str, Any]:
    return _call(urllib.request.Request(url, data=urllib.parse.urlencode(form).encode("ascii"), method="POST",
                                        headers={"Content-Type": "application/x-www-form-urlencoded"}), timeout)


def _call(request: urllib.request.Request, timeout: float) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            return _read(answer)
    except urllib.error.HTTPError as exc:
        with exc:
            raise GoogleError(exc.code, _error_code(exc)) from None
    except (urllib.error.URLError, OSError):
        raise GoogleUnreachable("Google could not be reached") from None


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")


class _Callback(HTTPServer):
    """One loopback redirect: keeps the first answer that carries our state, ignores any other request."""

    def __init__(self, state: str, port: int = 0) -> None:  # port 0: any free port (Google); Jira needs a fixed one
        self.state = state
        self.answer: dict[str, str] | None = None
        super().__init__(("127.0.0.1", port), _CallbackHandler)

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # HTTPServer's would look up the host name: slow, not needed


class _CallbackHandler(BaseHTTPRequestHandler):
    server: _Callback

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        ours = parts.path == "/" and secrets.compare_digest(query.get("state", ""), self.server.state)
        if ours and self.server.answer is None:
            self.server.answer = query
        # fixed text: nothing from the request is echoed back
        text = "norev: you can close this tab and go back to the app." if ours else "Not found."
        body = text.encode("utf-8")
        self.send_response(200 if ours else 404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        pass  # the request line holds the authorization code: never logged


class GoogleAuth:
    def __init__(
        self,
        settings: GoogleCalendarSettings,
        store: SecretStore,
        *,
        endpoints: GoogleEndpoints = GoogleEndpoints(),
        open_browser: Callable[[str], bool] = webbrowser.open,
        clock: Callable[[], float] = time.monotonic,
        timeout: float = 15,
    ) -> None:
        self._settings = settings
        self._store = store
        self._endpoints = endpoints
        self._open_browser = open_browser
        self._clock = clock
        self._timeout = timeout
        self._lock = threading.Lock()
        self._access: tuple[str, float] | None = None  # (token, expires at), in memory only

    def __repr__(self) -> str:
        return f"GoogleAuth(connected={self.connected()})"

    def connected(self) -> bool:
        return self._store.load_secret(REFRESH_TOKEN) is not None

    def connect(self, wait: float = 300) -> None:
        """Sign in in the User's browser and keep the refresh token. Blocks until the browser comes
        back to the app or `wait` seconds pass."""
        client = self._client()
        verifier = secrets.token_urlsafe(64)  # 86 characters, within PKCE's 43..128
        state = secrets.token_urlsafe(32)
        with _Callback(state) as server:
            redirect = f"http://127.0.0.1:{server.server_address[1]}"
            url = self._endpoints.auth + "?" + urllib.parse.urlencode({
                "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
                "scope": SCOPE, "code_challenge": _challenge(verifier), "code_challenge_method": "S256",
                "state": state, "access_type": "offline", "prompt": "consent",  # consent: Google sends a refresh token
            })
            if not self._open_browser(url):
                raise AuthorizationFailed("no_browser")
            server.timeout = 0.2
            deadline = time.monotonic() + wait
            while server.answer is None and time.monotonic() < deadline:
                server.handle_request()
            answer = server.answer
        if answer is None:
            raise AuthorizationFailed("timeout")
        if "code" not in answer:
            raise AuthorizationFailed("access_denied" if answer.get("error") == "access_denied" else "no_code")
        try:
            tokens = post_form(self._endpoints.token, {
                **client, "code": answer["code"], "code_verifier": verifier,
                "grant_type": "authorization_code", "redirect_uri": redirect,
            }, self._timeout)
        except GoogleError:
            raise AuthorizationFailed("code_refused") from None
        if SCOPE not in str(tokens.get("scope", "")).split():
            raise AuthorizationFailed("scope_not_granted")
        refresh = tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AuthorizationFailed("no_refresh_token")
        with self._lock:
            try:
                self._keep(tokens)  # checked before anything is saved
            except GoogleError:
                raise AuthorizationFailed("no_access_token") from None
            self._store.save_secret(REFRESH_TOKEN, refresh)

    def disconnect(self) -> bool:
        """Revoke the token at Google and delete it here. The local token is deleted even if Google
        cannot be reached; returns whether Google confirmed the revocation."""
        with self._lock:
            refresh = self._store.load_secret(REFRESH_TOKEN)
            self._access = None
            if refresh is None:
                return False
            try:
                post_form(self._endpoints.revoke, {"token": refresh}, self._timeout)
                revoked = True
            except GoogleError as exc:
                revoked = exc.status == 400 and exc.error == "invalid_token"  # already revoked
            except GoogleUnreachable:
                revoked = False
            finally:
                self._store.delete_secret(REFRESH_TOKEN)
            return revoked

    def access_token(self) -> str:
        """A valid access token, refreshed when it is about to expire."""
        with self._lock:
            if self._access and self._clock() < self._access[1]:
                return self._access[0]
            refresh = self._store.load_secret(REFRESH_TOKEN)
            if refresh is None:
                raise NotConnected("no Google account connected")
            try:
                tokens = post_form(self._endpoints.token, {
                    **self._client(), "refresh_token": refresh, "grant_type": "refresh_token",
                }, self._timeout)
            except GoogleError as exc:
                if exc.status in (400, 401) and exc.error == "invalid_grant":
                    self._store.delete_secret(REFRESH_TOKEN)
                    raise ReconnectNeeded("Google refused the saved authorization; connect the account again") from None
                raise
            return self._keep(tokens)

    def forget_access_token(self) -> None:
        """After Google answers 401 to a call: the next access_token() refreshes."""
        with self._lock:
            self._access = None

    def _client(self) -> dict[str, str]:
        if not self._settings.client_id or not self._settings.client_secret:
            raise NotConfigured("set NOREV_GOOGLE_CLIENT_ID and NOREV_GOOGLE_CLIENT_SECRET")
        return {"client_id": self._settings.client_id, "client_secret": self._settings.client_secret}

    def _keep(self, tokens: dict[str, Any]) -> str:
        access, expires_in = tokens.get("access_token"), tokens.get("expires_in")
        if not isinstance(access, str) or not access or not isinstance(expires_in, (int, float)):
            raise GoogleError(200, "no_access_token")
        self._access = (access, self._clock() + max(0, expires_in - EXPIRY_MARGIN))
        return access


EVENT_FIELDS = {"id", "summary", "description", "start", "end", "location", "attendees"}  # #44's payload


def _check_payload(payload: Any) -> tuple[str, dict[str, Any]]:
    """The shape #44's builder makes; the Central Processor already checked it is the approved one."""
    if not isinstance(payload, dict) or set(payload) != {"calendar_id", "event"}:
        raise ValueError("google_calendar payload must be exactly calendar_id and event")
    calendar_id, event = payload["calendar_id"], payload["event"]
    if not isinstance(calendar_id, str) or not calendar_id or not isinstance(event, dict):
        raise ValueError("google_calendar payload has no calendar_id or event")
    if set(event) - EVENT_FIELDS or not {"id", "summary", "start", "end"} <= set(event):
        raise ValueError("google_calendar event has unexpected or missing fields")
    event_id = event["id"]
    # Google's rule for client ids: base32hex (a-v, 0-9), 5 to 1024 characters
    if not isinstance(event_id, str) or not 5 <= len(event_id) <= 1024 or set(event_id) - set("abcdefghijklmnopqrstuv0123456789"):
        raise ValueError("google_calendar event id is not a valid Google event id")
    return calendar_id, event


class EventConflict(Exception):
    """An event with the approved id exists at Google but was deleted (cancelled): nothing was created."""


class GoogleCalendarConnector:
    name = NAME

    def __init__(self, auth: GoogleAuth, *, endpoints: GoogleEndpoints = GoogleEndpoints(), timeout: float = 15) -> None:
        self._auth = auth
        self._endpoints = endpoints
        self._timeout = timeout

    def __repr__(self) -> str:
        return "GoogleCalendarConnector()"

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        calendar_id, event = _check_payload(payload)
        events = f"{self._endpoints.api}/calendars/{urllib.parse.quote(calendar_id, safe='')}/events"
        body = json.dumps(event, ensure_ascii=False, allow_nan=False).encode("utf-8")
        try:
            answer = self._api("POST", events + "?sendUpdates=none", body)  # no invitation emails (#44)
            return self._result(answer, event["id"], created=True)
        except GoogleError as exc:
            if exc.status != 409:
                raise
        # 409: Google already has this id, from an earlier attempt of the same approved payload
        answer = self._api("GET", f"{events}/{event['id']}")
        if answer.get("status") == "cancelled":
            raise EventConflict("the event was created earlier and then deleted in Google Calendar")
        return self._result(answer, event["id"], created=False)

    def _api(self, method: str, url: str, body: bytes | None = None) -> dict[str, Any]:
        """One Calendar API call; an access token Google refuses (401) is refreshed once."""
        for attempt in (1, 2):
            headers = {"Authorization": f"Bearer {self._auth.access_token()}"}
            if body is not None:
                headers["Content-Type"] = "application/json; charset=utf-8"
            try:
                return _call(urllib.request.Request(url, data=body, method=method, headers=headers), self._timeout)
            except GoogleError as exc:
                if exc.status != 401 or attempt == 2:
                    raise
                self._auth.forget_access_token()
        raise AssertionError("unreachable")

    @staticmethod
    def _result(answer: dict[str, Any], event_id: str, created: bool) -> dict[str, Any]:
        if answer.get("id") != event_id:
            raise GoogleError(200, "unexpected_event_id")
        result = {"event_id": event_id, "created": created}
        if isinstance(answer.get("htmlLink"), str) and answer["htmlLink"].startswith("https://"):
            result["link"] = answer["htmlLink"]  # opens the event in Google Calendar
        return result
