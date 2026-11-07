"""The norev desktop app (pywebview, #27, #43).

  python -m norev.ui           record or import a Session and analyse it locally (Whisper, llama-server)
  python -m norev.ui --demo    the synthetic examples, reviewed but never executed

With NOREV_STORE_KEY_FILE set, Sessions go to the encrypted store (#26); otherwise they stay in
memory and are gone when the window closes. Approved Proposed Actions are exported to
NOREV_EXPORT_DIR (default: Documents/norev); with NOREV_GOOGLE_CLIENT_ID/SECRET and the encrypted
store, the User can connect Google Calendar and send approved events to it (#45); with
NOREV_TRELLO_API_KEY, Trello for work items (#72); with NOREV_JIRA_CLIENT_ID/SECRET, Jira Cloud (#73).
llama-server starts for the analysis only, as
`norev.cmd process` starts it (#48), with the same variables. Needs `pip install -r requirements-ui.txt`.
"""
import argparse
import logging
import os
import sys
from contextlib import contextmanager
from pathlib import Path

from norev.adapters.store.encrypted import StoreKeyError, StoreSettings, open_store
from norev.processor.identity import LocalUser
from norev.testing.fakes import InMemorySessionStore
from norev.ui.backend import DemoBackend
from norev.ui.bridge import Bridge

INDEX = Path(__file__).resolve().parent / "web" / "index.html"
AUDIO_TYPES = ("Audio și video (*.wav;*.mp3;*.m4a;*.flac;*.ogg;*.opus;*.aac;*.webm;*.mp4;*.mkv;*.mov;*.avi)",)


def session_backend(store, approved_by: str, pick_file):
    """The real backend, built here so the GPU libraries load only outside --demo. main() ran prepare()."""
    from norev.adapters import child_process
    from norev.adapters.recorder.recorder import capture, resolve_device
    from norev.adapters.child_process import ChildProcessAdapter
    from norev.launcher.__main__ import LLM_DEFAULTS
    from norev.launcher.llama import models_from_environment
    from norev.launcher.paths import AppPaths
    from norev.processor.compose import (
        build_desktop_processor, build_google_calendar, build_jira, build_trello, desktop_payload_builder,
    )
    from norev.session import WORKERS, _NoSpeakers
    from norev.ui.session_backend import SessionBackend

    paths = AppPaths.from_environment()
    for name, value in LLM_DEFAULTS.items():
        os.environ.setdefault(name, value)
    models = models_from_environment(paths.logs / "llama-server.log")
    os.environ.setdefault("LLAMACPP_URL", models.template.url)
    os.environ["LLM_MODEL"] = os.environ["NOREV_EXTRACT_MODEL"]  # the model extraction asks for (#62)
    # Whisper and diarization each in a child process that ends before llama-server starts, so their
    # CUDA contexts do not stay on the GPU during the LLM stage (#65)
    try:
        from norev.adapters.diarization.sortformer import SortformerDiarizer  # noqa: F401  (same check as before)

        diarizer = ChildProcessAdapter(store, child_process.sortformer)
    except ImportError:
        print("requirements-diarization.txt is not installed: Transcripts have no Speakers", file=sys.stderr)
        diarizer = _NoSpeakers()
    export_dir = Path(os.environ.get("NOREV_EXPORT_DIR") or Path.home() / "Documents" / "norev").resolve()
    google, calendar = build_google_calendar(store)  # no Connector without an OAuth client and the encrypted store
    trello, trello_connector = build_trello(store)  # none without NOREV_TRELLO_API_KEY and the encrypted store
    jira, jira_connector = build_jira(store)  # none without NOREV_JIRA_CLIENT_ID/SECRET and the encrypted store
    processor = build_desktop_processor(store, export_dir, transcriber=ChildProcessAdapter(store, child_process.whisper),
                                        diarizer=diarizer, calendar=calendar, trello=trello_connector,
                                        jira=jira_connector, models=models)
    return SessionBackend(
        store, approved_by=approved_by, processor=processor, export_dir=export_dir,
        builder=desktop_payload_builder(export_dir, calendar=calendar is not None, trello=trello, jira=jira),
        google=google, trello=trello, jira=jira,
        llm_session=lambda: _llm_session(models),
        record_source=lambda: capture(resolve_device(os.environ.get("NOREV_INPUT_DEVICE"))),
        pick_file=pick_file, workers=WORKERS,
    )


@contextmanager
def _llm_session(models):
    """Yields the translation model; each call starts its own stage's model, all stopped at the end (#62)."""
    try:
        yield os.environ["NOREV_TRANSLATE_MODEL"]
    finally:
        models.stop()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m norev.ui", description=__doc__.splitlines()[0])
    parser.add_argument("--demo", action="store_true", help="the synthetic examples instead of real Sessions")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO)
    if not args.demo:  # store, key, model, llama-server and CUDA DLL paths, as norev.cmd sets them (#48)
        from norev.launcher.__main__ import prepare
        from norev.launcher.paths import AppPaths

        prepare(AppPaths.from_environment())
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

    windows = []

    def pick_file() -> str | None:
        chosen = windows[0].create_file_dialog(webview.FileDialog.OPEN, file_types=AUDIO_TYPES)
        return chosen[0] if chosen else None

    approved_by = LocalUser.from_os().id
    backend = DemoBackend(store, approved_by=approved_by) if args.demo else session_backend(store, approved_by, pick_file)
    windows.append(webview.create_window("norev", url=str(INDEX), js_api=Bridge(backend), width=1360, height=900,
                                         min_size=(980, 680), background_color="#E7ECF3"))
    webview.start()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
