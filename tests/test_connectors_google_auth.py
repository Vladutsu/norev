"""OAuth of the google_calendar Connector (#45) against a fake Google on 127.0.0.1: no network."""
import base64
import contextlib
import hashlib
import io
import json
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from norev.adapters.connectors.google_calendar import (
    REFRESH_TOKEN,
    SCOPE,
    AuthorizationFailed,
    GoogleAuth,
    GoogleCalendarSettings,
    GoogleEndpoints,
    GoogleError,
    NotConfigured,
    NotConnected,
    ReconnectNeeded,
    SecretStore,
)

SETTINGS = GoogleCalendarSettings("client-1.apps.googleusercontent.com", "client-secret-1")


class MemorySecrets:
    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    def load_secret(self, name: str) -> str | None:
        return self.values.get(name)

    def save_secret(self, name: str, value: str) -> None:
        self.values[name] = value

    def delete_secret(self, name: str) -> None:
        self.values.pop(name, None)


class FakeGoogle:
    """Token and revoke endpoints. `answers` maps a path to (status, JSON) for the next calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.challenges: dict[str, str] = {}  # code -> PKCE challenge sent with it
        self.answers: dict[str, list[tuple[int, dict]]] = {}
        self.access_count = 0
        google = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                form = {k: v[0] for k, v in urllib.parse.parse_qs(
                    self.rfile.read(int(self.headers["Content-Length"])).decode()).items()}
                google.calls.append((self.path, form))
                status, body = google.answer(self.path, form)
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, args=(0.05,), daemon=True).start()
        base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.endpoints = GoogleEndpoints(auth=base + "/auth", token=base + "/token", revoke=base + "/revoke",
                                         api=base + "/calendar/v3")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def answer(self, path: str, form: dict[str, str]) -> tuple[int, dict]:
        if self.answers.get(path):
            return self.answers[path].pop(0)
        if path == "/revoke":
            return 200, {}
        if form.get("client_id") != SETTINGS.client_id or form.get("client_secret") != SETTINGS.client_secret:
            return 401, {"error": "invalid_client"}
        self.access_count += 1
        access = {"access_token": f"access-{self.access_count}", "expires_in": 3599, "scope": SCOPE,
                  "token_type": "Bearer"}
        if form.get("grant_type") == "refresh_token":
            return (200, access) if form.get("refresh_token") == "refresh-1" else (400, {"error": "invalid_grant"})
        challenge = self.challenges.get(form.get("code", ""))
        verifier = form.get("code_verifier", "")
        digest = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        if challenge is None or digest != challenge:
            return 400, {"error": "invalid_grant", "error_description": "bad verifier"}
        return 200, {**access, "refresh_token": "refresh-1"}


class FakeBrowser:
    """The User's browser: reads the authorization URL, then calls the redirect like Google would."""

    def __init__(self, google: FakeGoogle, redirects: list[dict[str, str]] | None = None) -> None:
        self.google = google
        self.redirects = redirects  # the queries sent to the redirect; None: approve with our state
        self.url: dict[str, str] | None = None
        self.statuses: list[int] = []

    def __call__(self, url: str) -> bool:
        self.url = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).items()}
        self.google.challenges["code-1"] = self.url["code_challenge"]
        redirects = self.redirects if self.redirects is not None else [{"code": "code-1"}]
        threading.Thread(target=self._redirect, args=(redirects,), daemon=True).start()
        return True

    def _redirect(self, redirects: list[dict[str, str]]) -> None:
        for query in redirects:
            query = {"state": self.url["state"], **query}
            try:
                with urllib.request.urlopen(self.url["redirect_uri"] + "/?" + urllib.parse.urlencode(query), timeout=5) as r:
                    self.statuses.append(r.status)
            except urllib.error.HTTPError as exc:
                self.statuses.append(exc.code)
                exc.close()


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class GoogleAuthTest(unittest.TestCase):
    def setUp(self) -> None:
        self.google = FakeGoogle()
        self.addCleanup(self.google.close)
        self.secrets = MemorySecrets()
        self.clock = Clock()

    def auth(self, browser=None, settings=SETTINGS) -> GoogleAuth:
        return GoogleAuth(settings, self.secrets, endpoints=self.google.endpoints,
                          open_browser=browser or FakeBrowser(self.google), clock=self.clock, timeout=5)

    def connect(self, browser: FakeBrowser, wait: float = 5) -> None:
        self.auth(browser).connect(wait=wait)

    # ── connect ─────────────────────────────────────────────────────────────

    def test_connect_keeps_the_refresh_token_in_the_secret_store(self) -> None:
        self.assertIsInstance(self.secrets, SecretStore)
        browser = FakeBrowser(self.google)
        auth = self.auth(browser)
        auth.connect(wait=5)
        self.assertEqual(self.secrets.values, {REFRESH_TOKEN: "refresh-1"})
        self.assertTrue(auth.connected())
        self.assertEqual(auth.access_token(), "access-1")  # from the exchange, no second call
        self.assertEqual([p for p, _ in self.google.calls], ["/token"])

    def test_authorization_request_is_loopback_pkce_and_events_only(self) -> None:
        browser = FakeBrowser(self.google)
        self.connect(browser)
        url = browser.url
        self.assertEqual(url["scope"], "https://www.googleapis.com/auth/calendar.events")
        self.assertEqual(url["code_challenge_method"], "S256")
        self.assertEqual(url["response_type"], "code")
        self.assertEqual(url["access_type"], "offline")
        self.assertRegex(url["redirect_uri"], r"^http://127\.0\.0\.1:\d+$")
        self.assertGreaterEqual(len(url["state"]), 32)
        exchange = self.google.calls[0][1]
        self.assertEqual(exchange["redirect_uri"], url["redirect_uri"])
        self.assertNotIn("code_verifier", url)  # only its challenge leaves before the exchange

    def test_a_request_without_our_state_is_ignored(self) -> None:
        browser = FakeBrowser(self.google, [{"state": "forged", "code": "code-evil"}, {"code": "code-1"}])
        self.connect(browser)
        self.assertEqual(browser.statuses, [404, 200])
        self.assertEqual([f["code"] for _, f in self.google.calls], ["code-1"])

    def test_denied_sign_in_saves_nothing(self) -> None:
        browser = FakeBrowser(self.google, [{"error": "access_denied"}])
        with self.assertRaisesRegex(AuthorizationFailed, "^access_denied$"):
            self.connect(browser)
        self.assertEqual(self.secrets.values, {})
        self.assertEqual(self.google.calls, [])

    def test_no_answer_times_out(self) -> None:
        with self.assertRaisesRegex(AuthorizationFailed, "^timeout$"):
            self.connect(FakeBrowser(self.google, []), wait=0.5)
        self.assertEqual(self.secrets.values, {})

    def test_no_browser(self) -> None:
        with self.assertRaisesRegex(AuthorizationFailed, "^no_browser$"):
            self.auth(lambda url: False).connect(wait=1)

    def test_refused_code_saves_nothing(self) -> None:
        self.google.answers["/token"] = [(400, {"error": "invalid_grant", "error_description": "code-1 expired"})]
        with self.assertRaisesRegex(AuthorizationFailed, "^code_refused$"):
            self.connect(FakeBrowser(self.google))
        self.assertEqual(self.secrets.values, {})

    def test_incomplete_token_answer_saves_nothing(self) -> None:
        for answer, reason in (
            ({"access_token": "a", "expires_in": 3599, "refresh_token": "r", "scope": "openid"}, "scope_not_granted"),
            ({"access_token": "a", "expires_in": 3599, "scope": SCOPE}, "no_refresh_token"),
            ({"refresh_token": "r", "scope": SCOPE}, "no_access_token"),
        ):
            with self.subTest(reason):
                self.google.answers["/token"] = [(200, answer)]
                with self.assertRaisesRegex(AuthorizationFailed, f"^{reason}$"):
                    self.connect(FakeBrowser(self.google))
                self.assertEqual(self.secrets.values, {})

    def test_not_configured(self) -> None:
        for settings in (GoogleCalendarSettings(None, None), GoogleCalendarSettings("id", None)):
            with self.assertRaises(NotConfigured):
                self.auth(settings=settings).connect(wait=1)

    # ── access token ────────────────────────────────────────────────────────

    def test_access_token_is_refreshed_only_when_it_expires(self) -> None:
        self.secrets.values[REFRESH_TOKEN] = "refresh-1"
        auth = self.auth()
        self.assertEqual(auth.access_token(), "access-1")
        self.clock.now += 3000
        self.assertEqual(auth.access_token(), "access-1")
        self.clock.now += 600  # within a minute of Google's expiry
        self.assertEqual(auth.access_token(), "access-2")
        self.assertEqual([f["grant_type"] for _, f in self.google.calls], ["refresh_token", "refresh_token"])
        auth.forget_access_token()  # e.g. after a 401
        self.assertEqual(auth.access_token(), "access-3")

    def test_not_connected_calls_nothing(self) -> None:
        with self.assertRaises(NotConnected):
            self.auth().access_token()
        self.assertEqual(self.google.calls, [])

    def test_revoked_refresh_token_is_deleted_and_asks_to_reconnect(self) -> None:
        self.secrets.values[REFRESH_TOKEN] = "refresh-revoked"
        with self.assertRaises(ReconnectNeeded):
            self.auth().access_token()
        self.assertEqual(self.secrets.values, {})

    def test_google_error_keeps_the_token_and_says_nothing_sensitive(self) -> None:
        self.secrets.values[REFRESH_TOKEN] = "refresh-1"
        self.google.answers["/token"] = [(503, {"error": "backendError", "error_description": "refresh-1 leaked"})]
        with self.assertRaises(GoogleError) as caught:
            self.auth().access_token()
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn("refresh-1", str(caught.exception))
        self.assertTrue(caught.exception.__suppress_context__)  # urllib's error, with the request, is not chained
        self.assertEqual(self.secrets.values, {REFRESH_TOKEN: "refresh-1"})

    def test_unreachable_google(self) -> None:
        from norev.adapters.connectors.google_calendar import GoogleUnreachable

        self.secrets.values[REFRESH_TOKEN] = "refresh-1"
        self.google.close()
        with self.assertRaises(GoogleUnreachable):
            self.auth().access_token()
        self.assertEqual(self.secrets.values, {REFRESH_TOKEN: "refresh-1"})

    # ── disconnect ──────────────────────────────────────────────────────────

    def test_disconnect_revokes_and_deletes_the_local_token(self) -> None:
        self.secrets.values[REFRESH_TOKEN] = "refresh-1"
        auth = self.auth()
        auth.access_token()
        self.assertTrue(auth.disconnect())
        self.assertEqual(self.google.calls[-1], ("/revoke", {"token": "refresh-1"}))
        self.assertEqual(self.secrets.values, {})
        self.assertFalse(auth.connected())
        with self.assertRaises(NotConnected):
            auth.access_token()  # the access token in memory is gone too

    def test_disconnect_deletes_the_local_token_even_when_google_fails(self) -> None:
        for answer, revoked in (((400, {"error": "invalid_token"}), True), ((500, {}), False)):
            with self.subTest(answer):
                self.secrets.values[REFRESH_TOKEN] = "refresh-1"
                self.google.answers["/revoke"] = [answer]
                self.assertIs(self.auth().disconnect(), revoked)
                self.assertEqual(self.secrets.values, {})
        self.secrets.values[REFRESH_TOKEN] = "refresh-1"
        self.google.close()
        self.assertFalse(self.auth().disconnect())
        self.assertEqual(self.secrets.values, {})

    def test_disconnect_without_account(self) -> None:
        self.assertFalse(self.auth().disconnect())
        self.assertEqual(self.google.calls, [])

    # ── nothing leaks ───────────────────────────────────────────────────────

    def test_no_token_in_output_or_repr(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stderr(out), contextlib.redirect_stdout(out):
            auth = self.auth()
            auth.connect(wait=5)
            text = repr(auth) + repr(SETTINGS)
        self.assertEqual(out.getvalue(), "")
        for secret in ("refresh-1", "access-1", "code-1", SETTINGS.client_secret):
            self.assertNotIn(secret, text)

    def test_endpoints_must_be_https_or_loopback(self) -> None:
        for url in ("http://oauth2.googleapis.com/token", "http://localhost:8080/token", "file:///etc/passwd"):
            with self.subTest(url), self.assertRaises(ValueError):
                GoogleEndpoints(token=url)

    def test_settings_from_environment(self) -> None:
        settings = GoogleCalendarSettings.from_environment(
            {"NOREV_GOOGLE_CLIENT_ID": "id", "NOREV_GOOGLE_CLIENT_SECRET": "s"})
        self.assertEqual((settings.client_id, settings.client_secret), ("id", "s"))
        self.assertEqual(GoogleCalendarSettings.from_environment({}), GoogleCalendarSettings(None, None))


if __name__ == "__main__":
    unittest.main()
