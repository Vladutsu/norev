"""The User's Trello account as the desktop app sees it (#72): status, the token the User pastes, the
list work items go to, and disconnecting.

The UI may not import a Connector (tests/test_processor_composition.py), so it gets this object from
the composition root. No token leaves it, and every failure comes back as a fixed code.
"""
import webbrowser
from collections.abc import Callable
from typing import Any

from norev.adapters.connectors.http import HttpError, Unreachable
from norev.adapters.connectors.trello import AuthorizationFailed, InvalidKey, InvalidToken, NotConfigured, NotConnected, TrelloAccount


class TrelloLink:
    """`account` is None when Trello is not set up on this machine (no API key, or no encrypted store
    to keep the token in)."""

    def __init__(self, account: TrelloAccount | None, *, open_browser: Callable[[str], bool] = webbrowser.open) -> None:
        self._account = account
        self._open_browser = open_browser

    def __repr__(self) -> str:
        return f"TrelloLink({self.status()})"

    def status(self) -> dict[str, Any]:
        account = self._account
        connected = account is not None and account.connected()
        # available: Trello can be used (the app has its key); key_settable: the key can be entered in
        # the app (there is an encrypted store), until it is
        return {"available": account is not None and account.configured(), "connected": connected,
                "list": account.chosen_list() if connected else None, "key_settable": account is not None,
                "key_in_app": account is not None and account.key_in_app()}

    def set_key(self, key: Any) -> dict[str, Any]:
        """The Team Leader enters the app's Trello API key (ADR 0006); `failed` is None or invalid_key."""
        if self._account is None:
            return {**self.status(), "failed": "not_configured"}
        if self._account.connected():  # the token belongs to the key it was given for
            return {**self.status(), "failed": "disconnect_first"}
        try:
            self._account.set_api_key(key)
            failed = None
        except InvalidKey:
            failed = "invalid_key"
        return {**self.status(), "failed": failed}

    def ready(self) -> bool:
        """Work items can be sent to Trello: connected, with a list chosen."""
        status = self.status()
        return status["connected"] and status["list"] is not None

    def chosen_list(self) -> dict[str, str] | None:
        return self.status()["list"]

    def authorize(self) -> dict[str, Any]:
        """Opens Trello's page where the User allows norev and gets the token to paste."""
        if self._account is None:
            return {**self.status(), "failed": "not_configured"}
        try:
            opened = bool(self._open_browser(self._account.authorize_url()))
        except NotConfigured:
            return {**self.status(), "failed": "not_configured"}
        return {**self.status(), "failed": None if opened else "no_browser"}

    def connect(self, token: Any) -> dict[str, Any]:
        """Checks the pasted token with Trello and keeps it in the encrypted store."""
        if self._account is None:
            return {**self.status(), "failed": "not_configured"}
        return self._attempt(lambda: self._account.connect(token))

    def board_members(self) -> dict[str, Any]:
        """The people on the chosen list's board, for the Team Leader to link Members (ADR 0006)."""
        if self._account is None:
            return {"members": [], "failed": "not_configured"}
        found: list[dict[str, str]] = []
        result = self._attempt(lambda: found.extend(self._account.board_members()))
        return {"members": found, "failed": result["failed"]}

    def boards(self) -> dict[str, Any]:
        """The User's open boards and lists, to choose where work items go."""
        if self._account is None:
            return {**self.status(), "boards": [], "failed": "not_configured"}
        boards: list[dict[str, Any]] = []

        def load() -> None:
            boards.extend(self._account.boards())
        return {**self._attempt(load), "boards": boards}

    def choose_list(self, list_id: Any) -> dict[str, Any]:
        if self._account is None:
            return {**self.status(), "failed": "not_configured"}
        return self._attempt(lambda: self._account.choose_list(list_id))

    def connect_in_browser(self) -> dict[str, Any]:
        """The Team Leader signs in to Trello in the browser, which brings the token back (no paste)."""
        if self._account is None:
            return {**self.status(), "failed": "not_configured"}
        return self._attempt(self._account.connect_in_browser)

    def identify_in_browser(self) -> dict[str, Any]:
        """A Member's own Trello user (ADR 0006), from a sign-in that keeps nothing:
        `{member_id, username, failed}`."""
        if self._account is None:
            return {"failed": "not_configured"}
        found: dict[str, Any] = {}
        result = self._attempt(lambda: found.update(self._account.identify_in_browser()))
        return {**found, "failed": result["failed"]}

    def disconnect(self) -> dict[str, Any]:
        """Revokes the token at Trello and deletes it here; `revoked` says whether Trello confirmed."""
        revoked = self._account.disconnect() if self._account is not None else False
        return {**self.status(), "revoked": revoked}

    def _attempt(self, work: Callable[[], Any]) -> dict[str, Any]:
        try:
            work()
            failed = None
        except AuthorizationFailed as e:
            failed = str(e)  # a fixed code: access_denied, timeout, port_in_use, no_browser
        except InvalidToken:
            failed = "invalid_token"
        except NotConnected:
            failed = "not_connected"
        except NotConfigured:
            failed = "not_configured"
        except LookupError:
            failed = "no_such_list"
        except Unreachable:
            failed = "unreachable"
        except HttpError as e:
            failed = "invalid_token" if e.status == 401 else "trello_error"
        return {**self.status(), "failed": failed}
