"""Web demo for ngrok (#63): synthetic data only, 127.0.0.1 only, nothing outside norev/ui/web."""
import http.client
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from norev.adapters.store.encrypted import create_key_file
from norev.testing.fakes import InMemorySessionStore
from norev.ui import web_demo
from norev.ui.backend import DemoBackend

ROOT = Path(__file__).resolve().parent.parent


class WebDemoCase(unittest.TestCase):
    def setUp(self):
        self.server = web_demo.make_server(0)
        self.port = self.server.server_address[1]
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def api(self, name, *args):
        status, body, _ = self.request("POST", f"/api/{name}", json.dumps(list(args)))
        self.assertEqual(status, 200)
        return json.loads(body)


class ServingTest(WebDemoCase):
    def test_listens_on_localhost_only_and_has_no_host_option(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")
        self.assertNotIn("--host", (ROOT / "norev" / "ui" / "web_demo.py").read_text())

    def test_index_runs_the_ui_against_the_http_bridge_with_a_synthetic_data_banner(self):
        status, body, headers = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn(b'fetch("api/"', body)
        self.assertIn("date sintetice".encode(), body)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        status, _, headers = self.request("GET", "/fonts/inter-latin-400-normal.woff2")
        self.assertEqual((status, headers["Content-Type"]), (200, "font/woff2"))

    def test_nothing_outside_the_web_folder_is_reachable(self):
        for path in ("/../backend.py", "/%2e%2e/backend.py", "/fonts/../../backend.py", "/..%2f..%2fdemo_session.json",
                     "/web_demo.py", "/demo_session.json", "/.git/config", "//etc/passwd", "/C:/Windows/win.ini",
                     "/fonts/", "/fonts", "/%5c..%5cbackend.py", "/app.js%00.py", "/../../../../README.md"):
            with self.subTest(path=path):
                status, body, _ = self.request("GET", path)
                self.assertEqual(status, 404, body[:80])

    def test_only_public_bridge_methods_by_post_with_a_json_list(self):
        self.assertTrue(self.api("modes")["ok"])
        for name in ("_call", "__init__", "_backend", "__class__", "nope"):
            with self.subTest(name=name):
                self.assertEqual(self.request("POST", f"/api/{name}", "[]")[0], 404)
        self.assertEqual(self.request("GET", "/api/modes")[0], 405)
        self.assertEqual(self.request("POST", "/api/modes", '{"a": 1}')[0], 400)
        self.assertEqual(self.request("POST", "/api/modes", "not json")[0], 400)
        self.assertEqual(self.request("POST", "/api/review", "[1, 2, 3, 4]")[0], 400)
        self.assertEqual(self.request("POST", "/api/modes", "[" + " " * web_demo.MAX_BODY + "]")[0], 413)
        self.assertEqual(self.request("DELETE", "/")[0], 405)

    def test_a_demo_session_is_reviewed_but_never_executed(self):
        session_id = self.api("import_example", "corporate", "ro", "Echipa")["data"]["session_id"]
        review = self.api("review", session_id)["data"]
        action = review["actions"][0]
        review = self.api("preview", session_id, action["id"], review["revision"])["data"]
        review = self.api("approve", session_id, action["id"], review["revision"])["data"]
        self.assertEqual(review["actions"][0]["status"], "approved")
        executed = self.api("execute", session_id, action["id"])
        self.assertFalse(executed["ok"])


class IsolationTest(WebDemoCase):
    def test_the_backend_is_always_the_demo_on_memory(self):
        bridge = self.server.RequestHandlerClass.state.bridge
        self.assertIsInstance(bridge._backend, DemoBackend)
        self.assertIsInstance(bridge._backend.store, InMemorySessionStore)
        source = (ROOT / "norev" / "ui" / "web_demo.py").read_text()
        for forbidden in ("environ", "getenv", "open_store", "session_backend", "launcher", "AppPaths", "--demo"):
            self.assertNotIn(forbidden, source)

    def test_a_real_store_key_and_profile_in_the_environment_are_never_touched(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            create_key_file(home / "keys" / "norev.key")
            secret = home / "norev.db"
            env = {"NOREV_HOME": str(home), "NOREV_STORE_PATH": str(secret), "NOREV_STORE_KEY_FILE": str(home / "keys" / "norev.key")}
            with mock.patch.dict(os.environ, env):
                server = web_demo.make_server(0)
                server.server_close()
                session_id = self.api("import_example", "medical", "ro", "Maria")["data"]["session_id"]
                self.assertTrue(self.api("review", session_id)["ok"])
            self.assertFalse(secret.exists())
            self.assertEqual(sorted(p.name for p in home.rglob("*")), ["keys", "norev.key"])

    def test_the_demo_starts_over_instead_of_growing_without_bound(self):
        state = self.server.RequestHandlerClass.state
        with mock.patch.object(web_demo, "MAX_SESSIONS", 2):
            first = state.bridge
            for _ in range(3):
                self.api("import_example", "corporate", "ro", "")
            self.assertIsNot(state.bridge, first)
            self.assertEqual(len(self.api("sessions")["data"]), 1)


class ComposeTest(unittest.TestCase):
    def test_ngrok_runs_in_docker_with_the_url_and_token_from_the_local_environment(self):
        compose = (ROOT / "docker-compose.yml").read_text()
        self.assertIn("norev.ui.web_demo", compose)
        self.assertIn("network_mode: service:demo", compose)
        self.assertIn("${NOREV_DEMO_URL", compose)
        self.assertIn("NGROK_AUTHTOKEN", compose)
        self.assertNotRegex(compose, r"ngrok-free\.app|ngrok\.io|ngrok\.app")
        self.assertIn(".env", (ROOT / ".gitignore").read_text().splitlines())


if __name__ == "__main__":
    unittest.main()
