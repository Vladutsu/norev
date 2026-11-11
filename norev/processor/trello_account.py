"""The User's Trello account as the desktop app sees it (#72): status, the token the User pastes, the
list work items go to, and disconnecting.

The UI may not import a Connector (tests/test_processor_composition.py), so it gets this object from
the composition root. No token leaves it, and every failure comes back as a fixed code.
"""
import webbrowser
from collections.abc import Callable
from typing import Any

from norev.adapters.connectors.http import HttpError, Unreachable
from norev.adapters.connectors.trello import InvalidToken, NotConfigured, NotConnected, TrelloAccount


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
        return {"available": account is not None, "connected": connected,
                "list": account.chosen_list() if connected else None}

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

    def disconnect(self) -> dict[str, Any]:
        """Revokes the token at Trello and deletes it here; `revoked` says whether Trello confirmed."""
        revoked = self._account.disconnect() if self._account is not None else False
        return {**self.status(), "revoked": revoked}

    def _attempt(self, work: Callable[[], Any]) -> dict[str, Any]:
        try:
            work()
            failed = None
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
