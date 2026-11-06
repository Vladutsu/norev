"""The norev desktop app (pywebview, #27).

  python -m norev.ui          open the app on the demo consultation (the full flow comes with #19)

With NOREV_STORE_KEY_FILE set, Sessions go to the encrypted store (#26); otherwise they stay in
memory and are gone when the window closes. Needs `pip install -r requirements-ui.txt`.
"""
import argparse
import logging
import os
import sys
from pathlib import Path

from norev.adapters.store.encrypted import StoreKeyError, StoreSettings, open_store
from norev.processor.identity import LocalUser
from norev.testing.fakes import InMemorySessionStore
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge

INDEX = Path(__file__).resolve().parent / "web" / "index.html"


def main(argv: list[str]) -> int:
    argparse.ArgumentParser(prog="python -m norev.ui", description=__doc__.splitlines()[0]).parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    try:
        import webview
    except ImportError:
        print("pywebview is not installed: pip install -r requirements-ui.txt", file=sys.stderr)
        return 1

    if os.environ.get("NOREV_STORE_KEY_FILE"):
        try:
            store = open_store(StoreSettings.from_environment())
        except StoreKeyError as e:
            print(f"store: {e}", file=sys.stderr)
            return 1
    else:
        print("NOREV_STORE_KEY_FILE is not set: Sessions are kept in memory only", file=sys.stderr)
        store = InMemorySessionStore()

    backend = DemoBackend(store, approved_by=LocalUser.from_os().id)
    webview.create_window("norev", url=str(INDEX), js_api=Bridge(backend), width=1360, height=900,
                          min_size=(980, 680), background_color="#E7ECF3")
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
