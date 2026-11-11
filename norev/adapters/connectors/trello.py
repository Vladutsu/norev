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
from dataclasses import dataclass
from typing import Any, Mapping

from norev.adapters.connectors.google_calendar import SecretStore
from norev.adapters.connectors.http import HttpError, call_json, https_or_loopback

NAME = "trello"
API = "https://api.trello.com/1"
AUTHORIZE = "https://trello.com/1/authorize"
TOKEN = "trello.token"  # SecretStore names
LIST = "trello.list"  # the list the User chose, as JSON {"id", "name"}: account data, kept encrypted too
MARKER = re.compile(r"norev:[0-9a-f]{32}")
CARD_FIELDS = {"name", "desc", "due"}
_ID = re.compile(r"[0-9a-fA-F]{24}")  # Trello object ids


class NotConfigured(ValueError):
    """No Trello API key: NOREV_TRELLO_API_KEY is not set."""


class NotConnected(Exception):
    """No Trello token: the User connects Trello first."""


class InvalidToken(ValueError):
    """Trello refused the token (wrong, expired or revoked); nothing was saved."""


@dataclass(frozen=True)
class TrelloSettings:
    api_key: str | None

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "TrelloSettings":
        env = os.environ if env is None else env
        return cls(env.get("NOREV_TRELLO_API_KEY") or None)


def marker_of(desc: Any) -> str | None:
    """The `norev:<id>` last line of a card description made by norev."""
    last = desc.rstrip().rsplit("\n", 1)[-1].strip() if isinstance(desc, str) else ""
    return last if MARKER.fullmatch(last) else None


class TrelloAccount:
    """The User's Trello token and chosen list."""

    def __init__(self, settings: TrelloSettings, store: SecretStore, *, api: str = API, timeout: float = 15) -> None:
        self._settings = settings
        self._store = store
        self._api = https_or_loopback(api)
        self._timeout = timeout

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

    def _key(self) -> str:
        if not self._settings.api_key:
            raise NotConfigured("set NOREV_TRELLO_API_KEY")
        return self._settings.api_key

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
