"""Record Session audio from a microphone to a WAV file that the Whisper stage can transcribe.

  python -m LLM.Recorder.record                       -> Outputs/Recordings/session-<time>.wav (prints path)
  python -m LLM.Recorder.record out.wav --seconds 60  -> stops after 60 s
  python -m LLM.Recorder.record --device "Microphone (Realtek(R) Audio)"
  python -m LLM.Recorder.record --list-devices

Stop with Enter (or Ctrl+C). Messages go to stderr; stdout gets only the WAV path, like the
Whisper stage, so norev.sh can capture it.
"""
import argparse
import json
import sys
import threading
from datetime import datetime
from pathlib import Path

from LLM.Recorder.recorder import RecorderSettings, capture, default_device, list_input_devices, record


def _stop_on_enter(stop: threading.Event) -> None:
    # EOF ("") is not Enter: stdin may be closed or NUL, which even claims to be a TTY on Windows
    if sys.stdin.readline():
        stop.set()


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Record Session audio from a microphone")
    parser.add_argument("output", nargs="?", help="WAV path (default: Outputs/Recordings/session-<time>.wav)")
    parser.add_argument("--device", default="", help="input device name (default: NOREV_INPUT_DEVICE or the system default)")
    parser.add_argument("--seconds", type=float, default=None, help="stop after this many seconds")
    parser.add_argument("--list-devices", action="store_true", help="print the input devices and exit")
    args = parser.parse_args(argv)
    settings = RecorderSettings.from_environment()

    try:
        if args.list_devices:
            print(json.dumps({"input_devices": list_input_devices()}, ensure_ascii=False))
            return 0

        device = args.device or settings.device or default_device()
        output = Path(args.output) if args.output else (
            settings.output_dir / f"session-{datetime.now():%Y%m%d-%H%M%S}.wav"
        )
        stop = threading.Event()
        threading.Thread(target=_stop_on_enter, args=(stop,), daemon=True).start()
        limit = f", at most {args.seconds:g}s" if args.seconds else ""
        print(f"[INFO] Recording from {device!r}{limit}; press Enter (or Ctrl+C) to stop", file=sys.stderr)

        chunks = capture(device)
        try:
            seconds = record(chunks, output, stop, max_seconds=args.seconds)
        finally:
            chunks.close()
    except Exception as exc:
        print(json.dumps({"error": f"Recording failed: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1

    if seconds <= 0:
        output.unlink(missing_ok=True)
        print(json.dumps({"error": f"No audio was captured from {device!r}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(f"[INFO] Recorded {seconds:.1f}s to {output}", file=sys.stderr)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
