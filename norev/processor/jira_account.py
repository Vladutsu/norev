"""The User's Atlassian account and Jira destination as the desktop app sees it (#73): status,
connect, disconnect, the sites and projects to choose from, and the chosen one.

The UI may not import a Connector (tests/test_processor_composition.py), so it gets this object from
the composition root. No token leaves it, and every failure comes back as a fixed code.
"""
import threading
from typing import Any

from norev.adapters.connectors.jira import (
    AuthorizationFailed,
    JiraAuth,
    JiraClient,
    JiraError,
    JiraUnreachable,
    NotConfigured,
    NotConnected,
    choose_destination,
    clear_destination,
    load_destination,
)

CONNECT_WAIT = 300  # seconds the app waits for the User to finish signing in in the browser


def _failure(exc: Exception) -> str:
    if isinstance(exc, AuthorizationFailed):
        return str(exc)  # a fixed reason code, e.g. access_denied or timeout
    if isinstance(exc, NotConfigured):
        return "not_configured"
    if isinstance(exc, NotConnected):
        return "not_connected"
    if isinstance(exc, JiraUnreachable):
        return "unreachable"
    if isinstance(exc, JiraError):
        return "jira_error"
    raise exc


class JiraAccount:
    """`auth` is None when Jira is not set up on this machine (no OAuth app, or no encrypted store)."""

    def __init__(self, auth: JiraAuth | None) -> None:
        self._auth = auth
        self._connecting = threading.Lock()

    def __repr__(self) -> str:
        return f"JiraAccount(available={self._auth is not None})"

    def destination(self) -> dict[str, str] | None:
        """The site and project a work item goes to when the User picks Jira for it (#72), or None."""
        if self._auth is None or not self._auth.connected():
            return None
        return load_destination(self._auth.store)

    def ready(self) -> bool:
        """Jira can be picked for a work item: connected, with a project chosen."""
        return self.destination() is not None

    def status(self) -> dict[str, Any]:
        connected = self._auth is not None and self._auth.connected()
        return {"available": self._auth is not None, "connected": connected, "destination": self.destination()}

    def connect(self, wait: float = CONNECT_WAIT) -> dict[str, Any]:
        """Opens the sign-in in the User's browser and waits for it. `failed` is None or a fixed code."""
        if self._auth is None:
            return {**self.status(), "failed": "not_configured"}
        if not self._connecting.acquire(blocking=False):
            return {**self.status(), "failed": "already_connecting"}
        try:
            self._auth.connect(wait=wait)
            failed = None
        except Exception as exc:
            failed = _failure(exc)
        finally:
            self._connecting.release()
        return {**self.status(), "failed": failed}

    def disconnect(self) -> dict[str, Any]:
        """Deletes the local token and destination; the User revokes the app in their Atlassian account."""
        if self._auth is not None:
            self._auth.disconnect()
        return self.status()

    def projects(self) -> dict[str, Any]:
        """Every site the account can write to, with its projects, for the User to choose from."""
        if self._auth is None:
            return {**self.status(), "sites": [], "failed": "not_configured"}
        client = JiraClient(self._auth)
        try:
            sites = [{**site, "projects": client.projects(site["cloud_id"])} for site in client.sites()]
            failed = None
        except Exception as exc:
            sites, failed = [], _failure(exc)
        return {**self.status(), "sites": sites, "failed": failed}

    def choose(self, cloud_id: str | None, project_key: str | None) -> dict[str, Any]:
        """The project for the work items the User sends to Jira from now on (approved ones keep
        theirs); no project: Jira cannot be picked."""
        if self._auth is None:
            return {**self.status(), "failed": "not_configured"}
        if not cloud_id or not project_key:
            clear_destination(self._auth.store)
            return {**self.status(), "failed": None}
        try:
            choose_destination(self._auth, str(cloud_id), str(project_key))
            failed = None
        except ValueError:
            failed = "not_accessible"
        except Exception as exc:
            failed = _failure(exc)
        return {**self.status(), "failed": failed}
