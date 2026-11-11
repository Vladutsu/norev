"""norev web demo (#63): the desktop UI on synthetic data only, for showing it through ngrok.

  python -m norev.ui.web_demo [--port 8765]

Always DemoBackend on an in-memory store: there is no option, variable or file that reaches real
Sessions, the encrypted store, the key or the User's profile. It listens on 127.0.0.1 only; ngrok,
in the same container network, is the only way in (docker-compose.yml, profile demo).
Visitors share one demo; it starts over after MAX_SESSIONS so it cannot grow without bound.
"""
import argparse
import json
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from norev.testing.fakes import InMemorySessionStore
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge

HOST = "127.0.0.1"
WEB = Path(__file__).resolve().parent / "web"
API = frozenset(name for name in dir(Bridge) if not name.startswith("_"))
TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8", ".js": "text/javascript; charset=utf-8",
         ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8"}
MAX_BODY = 64 * 1024
MAX_SESSIONS = 200
SHIM = (b'<script>window.pywebview={api:new Proxy({},{get:(_,n)=>(...a)=>fetch("api/"+n,{method:"POST",'
        b'headers:{"Content-Type":"application/json"},body:JSON.stringify(a)}).then(r=>r.json())})};</script>')
BANNER = (b'<div role="note" style="position:sticky;top:0;z-index:9;padding:6px 12px;'
          b'background:#1E2633;color:#fff;font:600 13px Inter,sans-serif;text-align:center">'
          b'Demonstra\xc8\x9bie cu date sintetice. Nimic nu se salveaz\xc4\x83 \xc8\x99i nimic nu se trimite.</div>')


def new_bridge() -> Bridge:
    return Bridge(DemoBackend(InMemorySessionStore(), approved_by="demo:visitor"))


class DemoState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.bridge = new_bridge()
        self.started = 0

    def call(self, name: str, args: list) -> dict:
        with self.lock:
            if name in {"start", "import_example", "import_file"}:
                self.started += 1
                if self.started > MAX_SESSIONS:
                    self.bridge, self.started = new_bridge(), 1
            return getattr(self.bridge, name)(*args)


def static_file(url_path: str) -> Path | None:
    """A file directly under web/, or None. No dot-files, no '..', no other folder."""
    path = unquote(urlsplit(url_path).path)
    if path in ("", "/"):
        path = "/index.html"
    parts = path.lstrip("/").split("/")
    if any(p in ("", ".", "..") or p.startswith(".") or "\\" in p or ":" in p or not p.isprintable() for p in parts):
        return None
    try:
        candidate = (WEB / Path(*parts)).resolve()
    except (OSError, ValueError):
        return None
    if not candidate.is_relative_to(WEB) or not candidate.is_file() or candidate.suffix not in TYPES:
        return None
    return candidate


class Handler(BaseHTTPRequestHandler):
    server_version = "norev-demo"
    sys_version = ""
    state: DemoState

    def log_message(self, format, *args):  # no request paths or bodies in the logs
        pass

    def _send(self, status: int, body: bytes, content_type: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if urlsplit(self.path).path.startswith("/api/"):
            return self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"POST only")
        file = static_file(self.path)
        if file is None:
            return self._send(HTTPStatus.NOT_FOUND, b"not found")
        body = file.read_bytes()
        if file.name == "index.html":
            body = body.replace(b'<script src="app.js">', SHIM + b'<script src="app.js">', 1)
            body = body.replace(b'<div class="app">', BANNER + b'<div class="app">', 1)
        self._send(HTTPStatus.OK, body, TYPES[file.suffix])

    def do_POST(self):
        path = urlsplit(self.path).path
        name = path[len("/api/"):] if path.startswith("/api/") else ""
        if name not in API:
            return self._send(HTTPStatus.NOT_FOUND, b"not found")
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            return self._send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, b"too large")
        try:
            args = json.loads(self.rfile.read(length) or b"[]")
        except ValueError:
            args = None
        if not isinstance(args, list):
            return self._send(HTTPStatus.BAD_REQUEST, b"arguments must be a JSON list")
        try:
            result = self.state.call(name, args)
        except TypeError:  # wrong number of arguments
            return self._send(HTTPStatus.BAD_REQUEST, b"bad arguments")
        self._send(HTTPStatus.OK, json.dumps(result).encode(), "application/json")

    do_PUT = do_DELETE = do_PATCH = lambda self: self._send(HTTPStatus.METHOD_NOT_ALLOWED, b"not allowed")


def make_server(port: int) -> ThreadingHTTPServer:
    handler = type("DemoHandler", (Handler,), {"state": DemoState()})
    server = ThreadingHTTPServer((HOST, port), handler)
    server.daemon_threads = True
    return server


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m norev.ui.web_demo", description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    server = make_server(args.port)
    print(f"norev demo (synthetic data only) on http://{HOST}:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
