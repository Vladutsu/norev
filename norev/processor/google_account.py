"""The User's Google account as the desktop app sees it (#45): status, connect and disconnect.

The UI may not import a Connector (tests/test_processor_composition.py), so it gets this object from
the composition root. No token leaves it, and every failure comes back as a fixed code.
"""
import threading
from typing import Any

from norev.adapters.connectors.google_calendar import (
    AuthorizationFailed,
    GoogleAuth,
    GoogleError,
    GoogleUnreachable,
    NotConfigured,
)

CONNECT_WAIT = 300  # seconds the app waits for the User to finish signing in in the browser


class GoogleAccount:
    """`auth` is None when Google Calendar is not set up on this machine (no OAuth client, or no
    encrypted store to keep the token in)."""

    def __init__(self, auth: GoogleAuth | None) -> None:
        self._auth = auth
        self._connecting = threading.Lock()

    def __repr__(self) -> str:
        return f"GoogleAccount({self.status()})"

    def status(self) -> dict[str, bool]:
        return {"available": self._auth is not None, "connected": self._auth is not None and self._auth.connected()}

    def connect(self, wait: float = CONNECT_WAIT) -> dict[str, Any]:
        """Opens the sign-in in the User's browser and waits for it. `failed` is None or a fixed code."""
        if self._auth is None:
            return {**self.status(), "failed": "not_configured"}
        if not self._connecting.acquire(blocking=False):
            return {**self.status(), "failed": "already_connecting"}
        try:
            self._auth.connect(wait=wait)
            failed = None
        except AuthorizationFailed as e:
            failed = str(e)  # a fixed reason code, e.g. access_denied or timeout
        except NotConfigured:
            failed = "not_configured"
        except GoogleUnreachable:
            failed = "unreachable"
        except GoogleError:
            failed = "google_error"
        finally:
            self._connecting.release()
        return {**self.status(), "failed": failed}

    def disconnect(self) -> dict[str, Any]:
        """Revokes the authorization at Google and deletes the local token; `revoked` says whether
        Google confirmed (the local token is deleted either way)."""
        revoked = self._auth.disconnect() if self._auth is not None else False
        return {**self.status(), "revoked": revoked}
