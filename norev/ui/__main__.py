"""The norev desktop app (pywebview, #27, #43).

  python -m norev.ui           record or import a Session and analyse it locally (Whisper, llama-server)
  python -m norev.ui --demo    the synthetic examples, reviewed but never executed

With NOREV_STORE_KEY_FILE set, Sessions go to the encrypted store (#26); otherwise they stay in
memory and are gone when the window closes. Approved Proposed Actions are exported to
NOREV_EXPORT_DIR (default: Documents/norev). llama-server starts for the analysis only, with the
same variables as norev.sh (README.md). Needs `pip install -r requirements-ui.txt`.
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
AUDIO_TYPES = ("Audio și video (*.wav;*.mp3;*.m4a;*.flac;*.ogg;*.opus;*.aac;*.webm;*.mp4;*.mkv;*.mov;*.avi)",)


def session_backend(store, approved_by: str, pick_file):
    """The real backend, built here so the GPU libraries load only outside --demo."""
    from norev.adapters.recorder.recorder import capture, resolve_device
    from norev.adapters.whisper.transcriber import WhisperTranscriber
    from norev.processor.compose import build_desktop_processor
    from norev.processor.llama_server import LlamaServerSettings, llama_server
    from norev.session import WORKERS, _NoSpeakers
    from norev.ui.session_backend import SessionBackend

    llama = LlamaServerSettings.from_environment()
    os.environ.setdefault("LLM_MODEL", llama.model_name)  # the alias llama-server answers to
    try:
        from norev.adapters.diarization.sortformer import SortformerDiarizer

        diarizer = SortformerDiarizer(store)
    except ImportError:
        print("requirements-diarization.txt is not installed: Transcripts have no Speakers", file=sys.stderr)
        diarizer = _NoSpeakers()
    export_dir = Path(os.environ.get("NOREV_EXPORT_DIR") or Path.home() / "Documents" / "norev").resolve()
    processor = build_desktop_processor(store, export_dir, transcriber=WhisperTranscriber(store), diarizer=diarizer)
    return SessionBackend(
        store, approved_by=approved_by, processor=processor, export_dir=export_dir,
        llm_session=lambda: llama_server(llama),
        record_source=lambda: capture(resolve_device(os.environ.get("NOREV_INPUT_DEVICE"))),
        pick_file=pick_file, workers=WORKERS,
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m norev.ui", description=__doc__.splitlines()[0])
    parser.add_argument("--demo", action="store_true", help="the synthetic examples instead of real Sessions")
    args = parser.parse_args(argv)
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
