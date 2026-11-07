"""Connector `jira` (#73): creates the approved work item as an issue in the Jira Cloud project the
User chose, with the stdlib only. The Central Processor calls it only with the exact approved payload
and audits each attempt (#22).

- send(): the payload is `{"site", "project", "issue"}`, built at preview by JiraPayloadBuilder
  (norev/processor/payloads.py) when the User picks Jira for a work item (#72); `issue` is the exact
  body of the create-issue request, for the project the User approved. Jira takes
  no id made here, so the approved issue carries the label `norev-<id>`: before creating, the
  Connector searches the project for that label, and a manual Retry after an uncertain attempt finds
  the issue Jira already has. Jira indexes a new issue for search within seconds; Retry is manual,
  so the User does not retry inside that window.

- connect(): Atlassian OAuth 2.0 (3LO) in the User's own browser. The redirect must match the one
  registered in the Atlassian app exactly, so the loopback port is fixed (NOREV_JIRA_REDIRECT_PORT);
  the callback listens on 127.0.0.1 only, for one answer with our `state`. Atlassian documents no PKCE
  for 3LO: the client secret and `state` protect the exchange.
- The refresh token is kept in the encrypted store (SecretStore) and replaced by the new one Atlassian
  sends at every refresh (rotation). The access token lives in memory only.
- disconnect(): Atlassian has no revocation API for 3LO; the local token and destination are deleted
  and the User revokes the app in their Atlassian account (Connected apps).
- Errors carry an HTTP status or a fixed reason code, never a token or an answer body.
"""
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from norev.adapters.connectors.google_calendar import SecretStore, _Callback

NAME = "jira"
SCOPES = ("read:jira-work", "write:jira-work", "offline_access")  # create and search issues, list projects
IDENTITY_SCOPES = ("read:jira-user",)  # ADR 0006: a Member's own user (rest/api/3/myself), nothing else
REFRESH_TOKEN = "jira.refresh_token"  # SecretStore names
DESTINATION = "jira.destination"  # the site and project the User chose, as JSON
DEFAULT_REDIRECT_PORT = 8765
MAX_ANSWER = 1_000_000  # bytes read from an Atlassian answer
EXPIRY_MARGIN = 60  # seconds: refresh an access token this long before it expires
MAX_PROJECTS = 100  # shortcut: the first page of project/search only; paginate when someone has more

CLOUD_ID = re.compile(r"[A-Za-z0-9-]{1,64}")  # goes into the API path
PROJECT_KEY = re.compile(r"[A-Z][A-Z0-9_]{1,9}")  # Jira's default key rule; goes into the JQL
LABEL = re.compile(r"norev-[0-9a-f]{32}")


class NotConfigured(ValueError):
    """No OAuth client: NOREV_JIRA_CLIENT_ID and NOREV_JIRA_CLIENT_SECRET are not set."""


class NotConnected(Exception):
    """No Atlassian account is connected: the User connects it first."""


class ReconnectNeeded(NotConnected):
    """Atlassian refused the refresh token (revoked, expired or rotated away). The local token was deleted."""


class AuthorizationFailed(Exception):
    """The sign-in did not complete; the message is a fixed reason code. Nothing was saved."""


class JiraError(Exception):
    """Atlassian answered with an HTTP error; only the status and a short code are kept."""

    def __init__(self, status: int, error: str | None = None) -> None:
        super().__init__(f"Jira answered HTTP {status}" + (f" ({error})" if error else ""))
        self.status = status
        self.error = error


class JiraUnreachable(ConnectionError):
    """No answer from Atlassian (network, DNS, timeout)."""


def _local_or_https(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" or (parts.scheme == "http" and parts.hostname == "127.0.0.1"):
        return url
    raise ValueError("an Atlassian endpoint must be https (http only on 127.0.0.1, for the tests' fake server)")


@dataclass(frozen=True)
class JiraEndpoints:
    auth: str = "https://auth.atlassian.com/authorize"
    token: str = "https://auth.atlassian.com/oauth/token"
    resources: str = "https://api.atlassian.com/oauth/token/accessible-resources"
    api: str = "https://api.atlassian.com/ex/jira"  # + /<cloud id>/rest/api/...

    def __post_init__(self) -> None:
        for url in (self.auth, self.token, self.resources, self.api):
            _local_or_https(url)


@dataclass(frozen=True)
class JiraSettings:
    """The OAuth 2.0 (3LO) app from the Atlassian developer console. Not committed: set in the environment."""

    client_id: str | None
    client_secret: str | None = field(default=None, repr=False)
    redirect_port: int = DEFAULT_REDIRECT_PORT

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "JiraSettings":
        env = os.environ if env is None else env
        port = int(env.get("NOREV_JIRA_REDIRECT_PORT") or DEFAULT_REDIRECT_PORT)
        if not 1024 <= port <= 65535:
            raise ValueError("NOREV_JIRA_REDIRECT_PORT must be between 1024 and 65535")
        return cls(env.get("NOREV_JIRA_CLIENT_ID") or None, env.get("NOREV_JIRA_CLIENT_SECRET") or None, port)

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}"


def _error_code(exc: urllib.error.HTTPError) -> str | None:
    """OAuth's short error code (invalid_grant), or the names of the fields Jira refused; never a message."""
    try:
        data = json.loads(exc.read(10_000))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    error = data.get("error")
    if not isinstance(error, str) and isinstance(data.get("errors"), dict) and data["errors"]:
        error = "fields:" + ",".join(sorted(str(k) for k in data["errors"]))  # e.g. a required custom field
    return error if isinstance(error, str) and error.isascii() and len(error) <= 64 else None


def _call(request: urllib.request.Request, timeout: float) -> Any:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            body = answer.read(MAX_ANSWER + 1)
            if len(body) > MAX_ANSWER:
                raise JiraError(answer.status, "answer_too_large")
            try:
                return json.loads(body or b"{}")
            except ValueError:
                raise JiraError(answer.status, "not_json") from None
    except urllib.error.HTTPError as exc:
        with exc:
            raise JiraError(exc.code, _error_code(exc)) from None
    except (urllib.error.URLError, OSError):
        raise JiraUnreachable("Atlassian could not be reached") from None


def _post_json(url: str, body: dict[str, Any], timeout: float, token: str | None = None) -> Any:
    headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
    return _call(urllib.request.Request(url, data=data, method="POST", headers=headers), timeout)


class JiraAuth:
    def __init__(
        self,
        settings: JiraSettings,
        store: SecretStore,
        *,
        endpoints: JiraEndpoints = JiraEndpoints(),
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
        return f"JiraAuth(connected={self.connected()})"

    @property
    def store(self) -> SecretStore:
        return self._store

    @property
    def endpoints(self) -> JiraEndpoints:
        return self._endpoints

    def connected(self) -> bool:
        return self._store.load_secret(REFRESH_TOKEN) is not None

    def connect(self, wait: float = 300) -> None:
        """Sign in in the User's browser and keep the refresh token. Blocks until the browser comes
        back to the app or `wait` seconds pass."""
        tokens = self._authorize(SCOPES, wait)
        refresh = tokens.get("refresh_token")
        if not isinstance(refresh, str) or not refresh:
            raise AuthorizationFailed("no_refresh_token")
        with self._lock:
            try:
                self._keep(tokens)  # checked before anything is saved
            except JiraError:
                raise AuthorizationFailed("no_access_token") from None
            self._store.save_secret(REFRESH_TOKEN, refresh)

    def identify(self, wait: float = 300) -> dict[str, str]:
        """A Member signs in once so the app learns their Jira user (ADR 0006): `{account_id, name}` on
        the site the Team Leader chose (or the Member's only site). Nothing of the sign-in is kept."""
        tokens = self._authorize(IDENTITY_SCOPES, wait)
        access = tokens.get("access_token")
        if not isinstance(access, str) or not access:
            raise AuthorizationFailed("no_access_token")
        get = lambda url: _call(urllib.request.Request(url, headers={  # noqa: E731
            "Authorization": f"Bearer {access}", "Accept": "application/json"}), self._timeout)
        sites = [s["id"] for s in get(self._endpoints.resources) or [] if isinstance(s, dict)
                 and isinstance(s.get("id"), str) and CLOUD_ID.fullmatch(s["id"])]
        chosen = load_destination(self._store)
        cloud = chosen["cloud_id"] if chosen and chosen["cloud_id"] in sites else sites[0] if len(sites) == 1 else None
        if cloud is None:
            raise AuthorizationFailed("no_site")
        me = get(f"{self._endpoints.api}/{cloud}/rest/api/3/myself")
        account = me.get("accountId") if isinstance(me, dict) else None
        if not isinstance(account, str) or not ACCOUNT_ID.fullmatch(account):
            raise JiraError(200, "unexpected_user")
        return {"account_id": account, "name": " ".join(str(me.get("displayName") or account).split())[:80]}

    def _authorize(self, scopes: tuple[str, ...], wait: float) -> dict[str, Any]:
        """The browser sign-in and the code exchange; the tokens, with every scope in `scopes` granted."""
        client = self._client()
        state = secrets.token_urlsafe(32)
        redirect = self._settings.redirect_uri
        try:
            server = _Callback(state, self._settings.redirect_port)
        except OSError:
            raise AuthorizationFailed("port_in_use") from None
        with server:
            url = self._endpoints.auth + "?" + urllib.parse.urlencode({
                "audience": "api.atlassian.com", "client_id": client["client_id"], "scope": " ".join(scopes),
                "redirect_uri": redirect, "state": state, "response_type": "code", "prompt": "consent",
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
            tokens = _post_json(self._endpoints.token, {
                **client, "grant_type": "authorization_code", "code": answer["code"], "redirect_uri": redirect,
            }, self._timeout)
        except JiraError:
            raise AuthorizationFailed("code_refused") from None
        if not isinstance(tokens, dict):
            raise AuthorizationFailed("no_access_token")
        granted = str(tokens.get("scope", "")).split()
        if any(scope not in granted for scope in scopes):
            raise AuthorizationFailed("scope_not_granted")
        return tokens

    def disconnect(self) -> None:
        """Delete the local token and the chosen destination. Atlassian has no revocation API for 3LO:
        the User removes the app from their Atlassian account."""
        with self._lock:
            self._access = None
            self._store.delete_secret(REFRESH_TOKEN)
            self._store.delete_secret(DESTINATION)

    def access_token(self) -> str:
        """A valid access token. A refresh rotates the refresh token: the new one replaces the old."""
        with self._lock:
            if self._access and self._clock() < self._access[1]:
                return self._access[0]
            refresh = self._store.load_secret(REFRESH_TOKEN)
            if refresh is None:
                raise NotConnected("no Atlassian account connected")
            try:
                tokens = _post_json(self._endpoints.token, {
                    **self._client(), "grant_type": "refresh_token", "refresh_token": refresh,
                }, self._timeout)
            except JiraError as exc:
                if exc.status in (400, 401, 403) and exc.error == "invalid_grant":
                    self._store.delete_secret(REFRESH_TOKEN)
                    raise ReconnectNeeded("Atlassian refused the saved authorization; connect the account again") from None
                raise
            if not isinstance(tokens, dict):
                raise JiraError(200, "no_access_token")
            access = self._keep(tokens)
            rotated = tokens.get("refresh_token")
            if isinstance(rotated, str) and rotated and rotated != refresh:
                self._store.save_secret(REFRESH_TOKEN, rotated)  # the old one is disabled now
            return access

    def forget_access_token(self) -> None:
        """After Jira answers 401 to a call: the next access_token() refreshes."""
        with self._lock:
            self._access = None

    def _client(self) -> dict[str, str]:
        if not self._settings.client_id or not self._settings.client_secret:
            raise NotConfigured("set NOREV_JIRA_CLIENT_ID and NOREV_JIRA_CLIENT_SECRET")
        return {"client_id": self._settings.client_id, "client_secret": self._settings.client_secret}

    def _keep(self, tokens: dict[str, Any]) -> str:
        access, expires_in = tokens.get("access_token"), tokens.get("expires_in")
        if not isinstance(access, str) or not access or not isinstance(expires_in, (int, float)):
            raise JiraError(200, "no_access_token")
        self._access = (access, self._clock() + max(0, expires_in - EXPIRY_MARGIN))
        return access


# ── the destination the User chose ────────────────────────────────────────────

DESTINATION_KEYS = {"cloud_id", "url", "name", "project_key", "project_name"}


def _check_destination(value: Any) -> dict[str, str]:
    if (not isinstance(value, dict) or set(value) != DESTINATION_KEYS
            or not all(isinstance(v, str) and v for v in value.values())):
        raise ValueError("a Jira destination is a site and a project")
    if not CLOUD_ID.fullmatch(value["cloud_id"]) or not PROJECT_KEY.fullmatch(value["project_key"]):
        raise ValueError("invalid Jira site id or project key")
    if not value["url"].startswith("https://") or len(value["url"]) > 200:
        raise ValueError("a Jira site URL must be https")
    return dict(value)


def load_destination(store: SecretStore) -> dict[str, str] | None:
    raw = store.load_secret(DESTINATION)
    if raw is None:
        return None
    try:
        return _check_destination(json.loads(raw))
    except ValueError:
        return None  # an unreadable setting is no destination: work items stay local


class JiraClient:
    """Calls to the Jira REST API with the User's access token; a 401 refreshes it once."""

    def __init__(self, auth: JiraAuth, *, timeout: float = 15) -> None:
        self._auth = auth
        self._timeout = timeout

    def sites(self) -> list[dict[str, str]]:
        """The Jira Cloud sites the User gave the app access to: `{cloud_id, url, name}`."""
        answer = self.request("GET", self._auth.endpoints.resources)
        sites = []
        for site in answer if isinstance(answer, list) else []:
            if (isinstance(site, dict) and isinstance(site.get("id"), str) and CLOUD_ID.fullmatch(site["id"])
                    and isinstance(site.get("url"), str) and site["url"].startswith("https://")
                    and "write:jira-work" in (site.get("scopes") or [])):
                sites.append({"cloud_id": site["id"], "url": site["url"], "name": str(site.get("name") or site["url"])})
        return sites

    def projects(self, cloud_id: str) -> list[dict[str, str]]:
        """The projects of a site the User can see: `{key, name}`."""
        if not CLOUD_ID.fullmatch(cloud_id):
            raise ValueError("invalid Jira site id")
        answer = self.request("GET", f"{self.api(cloud_id)}/rest/api/3/project/search?maxResults={MAX_PROJECTS}")
        values = answer.get("values") if isinstance(answer, dict) else None
        return [{"key": p["key"], "name": str(p.get("name") or p["key"])} for p in values or []
                if isinstance(p, dict) and isinstance(p.get("key"), str) and PROJECT_KEY.fullmatch(p["key"])]

    def api(self, cloud_id: str) -> str:
        return f"{self._auth.endpoints.api}/{cloud_id}"

    def request(self, method: str, url: str, body: dict[str, Any] | None = None) -> Any:
        for attempt in (1, 2):
            token = self._auth.access_token()
            try:
                if body is not None:
                    return _post_json(url, body, self._timeout, token)
                request = urllib.request.Request(url, method=method,
                                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
                return _call(request, self._timeout)
            except JiraError as exc:
                if exc.status != 401 or attempt == 2:
                    raise
                self._auth.forget_access_token()
        raise AssertionError("unreachable")


def choose_destination(auth: JiraAuth, cloud_id: str, project_key: str) -> dict[str, str]:
    """Keep the site and project the User picked, only if the account can see both now."""
    client = JiraClient(auth)
    site = next((s for s in client.sites() if s["cloud_id"] == cloud_id), None)
    if site is None:
        raise ValueError("this Jira site is not accessible to the connected account")
    project = next((p for p in client.projects(cloud_id) if p["key"] == project_key), None)
    if project is None:
        raise ValueError("this Jira project is not accessible to the connected account")
    destination = _check_destination({**site, "project_key": project["key"], "project_name": project["name"]})
    auth.store.save_secret(DESTINATION, json.dumps(destination, ensure_ascii=False, sort_keys=True))
    return destination


def clear_destination(store: SecretStore) -> None:
    store.delete_secret(DESTINATION)


# ── send ──────────────────────────────────────────────────────────────────────

ISSUE_FIELDS = {"project", "issuetype", "summary", "description", "duedate", "labels", "assignee"}
ACCOUNT_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}")  # a Jira Cloud accountId


def _check_payload(payload: Any) -> tuple[dict[str, str], dict[str, str], dict[str, Any], str]:
    """The shape JiraPayloadBuilder makes; the Central Processor already checked it is the approved one."""
    if not isinstance(payload, dict) or set(payload) != {"site", "project", "issue"}:
        raise ValueError("jira payload must be exactly site, project and issue")
    site, project, issue = payload["site"], payload["project"], payload["issue"]
    if not isinstance(site, dict) or set(site) != {"cloud_id", "url", "name"} or not isinstance(project, dict) \
            or set(project) != {"key", "name"} or not isinstance(issue, dict) or set(issue) != {"fields"}:
        raise ValueError("jira payload has an unexpected site, project or issue")
    fields = issue["fields"]
    if not isinstance(fields, dict) or set(fields) - ISSUE_FIELDS or not {"project", "issuetype", "summary", "labels"} <= set(fields):
        raise ValueError("jira issue has unexpected or missing fields")
    if fields["project"] != {"key": project.get("key")}:
        raise ValueError("jira issue is for another project than the payload's")
    if "assignee" in fields and (not isinstance(fields["assignee"], dict) or set(fields["assignee"]) != {"accountId"}
                                 or not isinstance(fields["assignee"]["accountId"], str)
                                 or not ACCOUNT_ID.fullmatch(fields["assignee"]["accountId"])):
        raise ValueError("jira issue has an invalid assignee")
    labels = fields["labels"]
    ours = [label for label in labels if isinstance(label, str) and LABEL.fullmatch(label)] if isinstance(labels, list) else []
    if len(ours) != 1:
        raise ValueError("jira issue needs exactly one norev-<id> label")
    _check_destination({**site, "project_key": project["key"], "project_name": project["name"]})
    return site, project, issue, ours[0]


class JiraConnector:
    name = NAME

    def __init__(self, auth: JiraAuth, *, timeout: float = 15) -> None:
        self._auth = auth
        self._client = JiraClient(auth, timeout=timeout)

    def __repr__(self) -> str:
        return "JiraConnector()"

    def send(self, payload: dict[str, Any]) -> dict[str, Any]:
        # the project is the one the User approved for this action (#72): the Central Processor checked
        # the payload is the approved one; choosing another project later changes only new previews
        site, project, issue, label = _check_payload(payload)
        api = self._client.api(site["cloud_id"])
        # project key and label are checked against fixed patterns above, so the JQL holds no free text
        jql = f'project = "{project["key"]}" AND labels = "{label}"'
        found = self._client.request("GET", f"{api}/rest/api/3/search/jql?" + urllib.parse.urlencode(
            {"jql": jql, "fields": "key", "maxResults": 1}))
        issues = found.get("issues") if isinstance(found, dict) else None
        if issues:  # created by an earlier attempt of the same approved payload
            return self._result(site, issues[0], created=False)
        return self._result(site, self._client.request("POST", f"{api}/rest/api/2/issue", issue), created=True)

    @staticmethod
    def _result(site: dict[str, str], answer: Any, created: bool) -> dict[str, Any]:
        key = answer.get("key") if isinstance(answer, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*-[0-9]+", key):
            raise JiraError(200, "unexpected_issue_key")
        return {"issue_key": key, "created": created, "link": f"{site['url'].rstrip('/')}/browse/{key}"}
