"""Connector `trello` (#72): an approved work item becomes a card in the Trello list the User chose.
It sends data off the machine; the Central Processor calls it only with the exact approved payload
and audits each attempt (#22).

Access (decided in #72): the app's API key comes from NOREV_TRELLO_API_KEY. The User opens Trello's
authorization page for that key (read and write, expires in 30 days), copies the token and pastes it
in the app. The token is checked with Trello, kept in the encrypted store (SecretStore) and sent in
the Authorization header, never in a query string (only the revoke call names it in its path, as
Trello requires). Disconnecting revokes it at Trello and always deletes it here.

The payload is built at preview (norev/processor/payloads.py), `{"list": {"id", "name"}, "card":
{"name", "desc", "due"?}}`. Trello takes no id from the client, so the card's description ends with a
visible `norev:<id>` line: before creating, send() looks for it in the list (archived cards
included) and returns the card it finds, so a Retry after an uncertain attempt makes no second card.
"""
import json
import os
import re
import secrets
import socketserver
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable, Mapping

from norev.adapters.connectors.google_calendar import SecretStore
from norev.adapters.connectors.http import HttpError, call_json, https_or_loopback

NAME = "trello"
API = "https://api.trello.com/1"
AUTHORIZE = "https://trello.com/1/authorize"
DEFAULT_REDIRECT_PORT = 8766  # Jira's is 8765
TOKEN = "trello.token"  # SecretStore names
API_KEY = "trello.api_key"  # the app's key, when the Team Leader entered it in the app instead of NOREV_TRELLO_API_KEY
LIST = "trello.list"  # the list the User chose, as JSON {"id", "name"}: account data, kept encrypted too
MARKER = re.compile(r"norev:[0-9a-f]{32}")
CARD_FIELDS = {"name", "desc", "due", "idMembers"}  # idMembers: one Member's Trello id (ADR 0006)
_ID = re.compile(r"[0-9a-fA-F]{24}")  # Trello object ids


class NotConfigured(ValueError):
    """No Trello API key: NOREV_TRELLO_API_KEY is not set."""


class NotConnected(Exception):
    """No Trello token: the User connects Trello first."""


class InvalidKey(ValueError):
    """Not a Trello API key (32 to 64 hexadecimal characters)."""


class InvalidToken(ValueError):
    """Trello refused the token (wrong, expired or revoked); nothing was saved."""


@dataclass(frozen=True)
class TrelloSettings:
    api_key: str | None
    redirect_port: int = DEFAULT_REDIRECT_PORT  # where Trello sends the browser back with the token

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "TrelloSettings":
        env = os.environ if env is None else env
        port = int(env.get("NOREV_TRELLO_REDIRECT_PORT") or DEFAULT_REDIRECT_PORT)
        if not 1024 <= port <= 65535:
            raise ValueError("NOREV_TRELLO_REDIRECT_PORT must be between 1024 and 65535")
        return cls(env.get("NOREV_TRELLO_API_KEY") or None, port)


class AuthorizationFailed(Exception):
    """Signing in to Trello in the browser did not end with a token; the message is a fixed code."""


# Trello puts the token after "#" in the address it sends the browser back to, which never reaches a
# server: this page, served on the loopback, reads it and posts it to the app. Fixed text; only our
# state goes into it, never anything from the request.
_RELAY = """<!doctype html><meta charset="utf-8"><title>norev</title><p id="m">norev…</p><script>
const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
history.replaceState(null, "", "/");
fetch("/token?state=STATE", {method: "POST", body: token}).then(() => {
  document.getElementById("m").textContent = token ? "norev: poți închide fila și te întorci în aplicație."
    : "norev: Trello nu a dat acces.";
});
</script>"""


class _TokenCallback(HTTPServer):
    """One loopback return from Trello: keeps the first token posted with our state."""

    def __init__(self, state: str, port: int) -> None:
        self.state = state
        self.token: str | None = None
        self.cancelled = False  # a newer sign-in took over
        self.closed = threading.Event()
        super().__init__(("127.0.0.1", port), _TokenHandler)

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)  # no host name lookup


class _TokenHandler(BaseHTTPRequestHandler):
    server: _TokenCallback

    def _ours(self) -> bool:
        parts = urllib.parse.urlsplit(self.path)
        state = urllib.parse.parse_qs(parts.query).get("state", [""])[0]
        return secrets.compare_digest(state, self.server.state)

    def _send(self, status: int, body: str, kind: str) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        ours = urllib.parse.urlsplit(self.path).path == "/" and self._ours()
        self._send(200, _RELAY.replace("STATE", self.server.state), "text/html") if ours else self._send(404, "Not found.", "text/plain")

    def do_POST(self) -> None:
        if urllib.parse.urlsplit(self.path).path != "/token" or not self._ours():
            return self._send(404, "Not found.", "text/plain")
        length = min(int(self.headers.get("Content-Length") or 0), 1024)
        token = self.rfile.read(length).decode("ascii", "replace").strip()
        if self.server.token is None:
            self.server.token = token
        self._send(200, "ok", "text/plain")

    def log_message(self, format: str, *args: Any) -> None:
        pass


def marker_of(desc: Any) -> str | None:
    """The `norev:<id>` last line of a card description made by norev."""
    last = desc.rstrip().rsplit("\n", 1)[-1].strip() if isinstance(desc, str) else ""
    return last if MARKER.fullmatch(last) else None


class TrelloAccount:
    """The User's Trello token and chosen list."""

    def __init__(self, settings: TrelloSettings, store: SecretStore, *, api: str = API, timeout: float = 15,
                 authorize: str = AUTHORIZE, open_browser: Callable[[str], bool] = webbrowser.open) -> None:
        self._settings = settings
        self._store = store
        self._api = https_or_loopback(api)
        self._timeout = timeout
        self._authorize = authorize
        self._open_browser = open_browser
        self._waiting: _TokenCallback | None = None  # the sign-in waiting for the browser, if any
        self._waiting_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"TrelloAccount(connected={self.connected()})"

    def authorize_url(self) -> str:
        """Trello's page where the User allows norev and gets a token to paste in the app."""
        return f"{AUTHORIZE}?expiration=30days&name=norev&scope=read,write&response_type=token&key={self._key()}"

    def connected(self) -> bool:
        return self._store.load_secret(TOKEN) is not None

    def connect(self, token: Any) -> str:
        """Checks the pasted token with Trello and keeps it; returns the Trello username."""
        token = token.strip() if isinstance(token, str) else ""
        if not 32 <= len(token) <= 512 or not token.isascii() or not token.isalnum():
            raise InvalidToken("not a Trello token")
        try:
            me = self.request("GET", "/members/me", token=token, query={"fields": "username"})
        except HttpError as exc:
            if exc.status in (400, 401):
                raise InvalidToken("Trello refused the token") from None
            raise
        username = me.get("username") if isinstance(me, dict) else None
        if not isinstance(username, str):
            raise HttpError(200, "unexpected_answer")
        self._store.save_secret(TOKEN, token)
        return username

    def disconnect(self) -> bool:
        """Revokes the token at Trello and deletes it here, with the chosen list; returns whether
        Trello confirmed. The local token is deleted even if Trello cannot be reached."""
        token = self._store.load_secret(TOKEN)
        if token is None:
            return False
        try:
            self.request("DELETE", f"/tokens/{token}", token=token)
            revoked = True
        except (HttpError, ConnectionError):
            revoked = False
        finally:
            self._store.delete_secret(TOKEN)
            self._store.delete_secret(LIST)
        return revoked

    def connect_in_browser(self, wait: float = 300) -> str:
        """The Team Leader allows norev in Trello in their browser, which comes back to the app with the
        token (nothing to paste); the token is kept as with connect(). Returns the Trello username."""
        return self.connect(self._token_in_browser("read,write", "30days", wait))

    def identify_in_browser(self, wait: float = 300) -> dict[str, str]:
        """A Member signs in to Trello once so the app learns who they are (ADR 0006): a read-only token
        for one hour, used for /members/me and revoked at once; `{member_id, username}`."""
        return self.identify(self._token_in_browser("read", "1hour", wait))

    def _token_in_browser(self, scope: str, expiration: str, wait: float) -> str:
        # a new sign-in replaces one still waiting (e.g. Trello showed an error and never came back)
        with self._waiting_lock:
            previous = self._waiting
        if previous is not None:
            previous.cancelled = True
            previous.closed.wait(2)
        state = secrets.token_urlsafe(24)
        try:
            server = _TokenCallback(state, self._settings.redirect_port)
        except OSError:
            raise AuthorizationFailed("port_in_use") from None
        with self._waiting_lock:
            self._waiting = server
        try:
            return self._wait_for_token(server, state, scope, expiration, wait)
        finally:
            server.closed.set()
            with self._waiting_lock:
                if self._waiting is server:
                    self._waiting = None

    def _wait_for_token(self, server: _TokenCallback, state: str, scope: str, expiration: str, wait: float) -> str:
        with server:
            # Trello's Allowed origins refuses an IP address; localhost is this machine all the same
            back = f"http://localhost:{self._settings.redirect_port}/?state={state}"
            url = self._authorize + "?" + urllib.parse.urlencode({
                "expiration": expiration, "name": "norev", "scope": scope, "response_type": "token",
                "key": self._key(), "callback_method": "fragment", "return_url": back})
            if not self._open_browser(url):
                raise AuthorizationFailed("no_browser")
            server.timeout = 0.2
            deadline = time.monotonic() + wait
            while server.token is None and not server.cancelled and time.monotonic() < deadline:
                server.handle_request()
            token = server.token
        if token is None:
            raise AuthorizationFailed("cancelled" if server.cancelled else "timeout")
        if not token:
            raise AuthorizationFailed("access_denied")
        return token

    def identify(self, token: Any) -> dict[str, str]:
        """The Member behind a pasted token, `{member_id, username}`; the token is then revoked, never kept."""
        token = token.strip() if isinstance(token, str) else ""
        if not 32 <= len(token) <= 512 or not token.isascii() or not token.isalnum():
            raise InvalidToken("not a Trello token")
        try:
            me = self.request("GET", "/members/me", token=token, query={"fields": "id,username"})
        except HttpError as exc:
            if exc.status in (400, 401):
                raise InvalidToken("Trello refused the token") from None
            raise
        finally:
            try:
                self.request("DELETE", f"/tokens/{token}", token=token)
            except (HttpError, ConnectionError):
                pass  # it expires within the hour anyway
        member, username = (me.get("id"), me.get("username")) if isinstance(me, dict) else (None, None)
        if not isinstance(member, str) or not _ID.fullmatch(member) or not isinstance(username, str):
            raise HttpError(200, "unexpected_answer")
        return {"member_id": member, "username": username[:80]}

    def boards(self) -> list[dict[str, Any]]:
        """The User's open boards with their open lists: [{"id", "name", "lists": [{"id", "name"}]}]."""
        answer = self.request("GET", "/members/me/boards",
                            query={"filter": "open", "fields": "name", "lists": "open", "list_fields": "name"})
        boards = []
        for board in answer if isinstance(answer, list) else []:
            if not isinstance(board, dict) or not isinstance(board.get("name"), str):
                continue
            lists = [{"id": li["id"], "name": li["name"]} for li in board.get("lists") or []
                     if isinstance(li, dict) and isinstance(li.get("id"), str) and isinstance(li.get("name"), str)]
            boards.append({"id": board.get("id"), "name": board["name"], "lists": lists})
        return boards

    def choose_list(self, list_id: Any) -> dict[str, str]:
        """Keeps one of the User's lists as the destination of work items; returns {"id", "name"}."""
        for board in self.boards():
            for li in board["lists"]:
                if li["id"] == list_id:
                    chosen = {"id": li["id"], "name": f"{board['name']} / {li['name']}"}
                    self._store.save_secret(LIST, json.dumps(chosen, ensure_ascii=False))
                    return chosen
        raise LookupError("no such Trello list in the User's open boards")

    def chosen_list(self) -> dict[str, str] | None:
        raw = self._store.load_secret(LIST)
        return json.loads(raw) if raw else None

    def board_members(self) -> list[dict[str, str]]:
        """The people on the board of the chosen list, `{id, username, name}` (ADR 0006: the Team Leader
        says which of them each Member is). Trello gives no emails here, so nothing is matched by email."""
        if not self.connected():
            raise NotConnected("no Trello account connected")
        chosen = self.chosen_list()
        if chosen is None:
            raise LookupError("no list chosen")
        found = self.request("GET", f"/lists/{chosen['id']}", query={"fields": "idBoard"})
        board = found.get("idBoard") if isinstance(found, dict) else None
        if not isinstance(board, str) or not _ID.fullmatch(board):
            raise HttpError(200, "unexpected_answer")
        answer = self.request("GET", f"/boards/{board}/members", query={"fields": "id,username,fullName"})
        return [{"id": m["id"], "username": str(m.get("username") or "")[:80], "name": str(m.get("fullName") or "")[:80]}
                for m in answer if isinstance(m, dict) and isinstance(m.get("id"), str) and _ID.fullmatch(m["id"])] \
            if isinstance(answer, list) else []

    def _key(self) -> str:
        key = self._settings.api_key or self._store.load_secret(API_KEY)
        if not key:
            raise NotConfigured("set NOREV_TRELLO_API_KEY, or enter the key in the app")
        return key

    def configured(self) -> bool:
        """The app has a Trello API key: from NOREV_TRELLO_API_KEY, or entered in the app."""
        return bool(self._settings.api_key or self._store.load_secret(API_KEY))

    def key_in_app(self) -> bool:
        """The key was entered in the app (not NOREV_TRELLO_API_KEY), so it can be changed there."""
        return not self._settings.api_key and self._store.load_secret(API_KEY) is not None

    def set_api_key(self, key: Any) -> None:
        """The app's Trello API key (from trello.com/power-ups/admin), kept in the encrypted store.
        It is not a credential on its own: access starts only when someone allows norev in Trello."""
        key = key.strip() if isinstance(key, str) else ""
        # usually 32 hexadecimal characters; a wrong but well-formed key is refused by Trello's own page
        if not re.fullmatch(r"[0-9a-fA-F]{32,64}", key):
            raise InvalidKey("not a Trello API key")
        self._store.save_secret(API_KEY, key)

    def request(self, method: str, path: str, *, token: str | None = None, query: dict[str, str] | None = None,
              body: Any = None) -> Any:
        token = token or self._store.load_secret(TOKEN)
        if token is None:
            raise NotConnected("no Trello account connected")
        # in the header, never in the URL
        auth = f'OAuth oauth_consumer_key="{self._key()}", oauth_token="{token}"'
        return call_json(method, self._api + path, headers={"Authorization": auth}, query=query, body=body,
                         timeout=self._timeout)


def _check_payload(payload: Any) -> tuple[dict[str, str], dict[str, Any], str]:
    """The shape the builder makes; the Central Processor already checked it is the approved one."""
    if not isinstance(payload, dict) or set(payload) != {"list", "card"}:
        raise ValueError("trello payload must be exactly list and card")
    target, card = payload["list"], payload["card"]
    if not isinstance(target, dict) or set(target) != {"id", "name"} or not isinstance(target["id"], str) \
            or not _ID.fullmatch(target["id"]):
        raise ValueError("trello payload has no valid list")
    if not isinstance(card, dict) or set(card) - CARD_FIELDS or not {"name", "desc"} <= set(card) \
            or not all(isinstance(v, str) for v in card.values()) or not card["name"].strip():
        raise ValueError("trello card has unexpected or missing fields")
    if "idMembers" in card and not _ID.fullmatch(card["idMembers"]):
        raise ValueError("trello card has an invalid member")
    marker = marker_of(card["desc"])
    if marker is None:
        raise ValueError("trello card has no norev marker")
    return target, card, marker


class TrelloConnector:
    name = NAME

    def __init__(self, account: TrelloAccount) -> None:
        self._account = account

    def __repr__(self) -> str:
        return "TrelloConnector()"

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        target, card, marker = _check_payload(payload)
        # a card from an earlier attempt of the same approved payload: found by its marker, not made again
        existing = self._account.request("GET", f"/lists/{target['id']}/cards/all", query={"fields": "desc,shortUrl"})
        for found in existing if isinstance(existing, list) else []:
            if isinstance(found, dict) and marker_of(found.get("desc")) == marker:
                return self._result(found, created=False)
        created = self._account.request("POST", "/cards", body={"idList": target["id"], "pos": "bottom", **card})
        if not isinstance(created, dict) or created.get("idList") != target["id"] or marker_of(created.get("desc")) != marker:
            raise HttpError(200, "unexpected_card")
        return self._result(created, created=True)

    @staticmethod
    def _result(card: dict[str, Any], created: bool) -> dict[str, Any]:
        result = {"card_id": card.get("id"), "created": created}
        if isinstance(card.get("shortUrl"), str) and card["shortUrl"].startswith("https://"):
            result["link"] = card["shortUrl"]
        return result
