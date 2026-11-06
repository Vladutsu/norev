"""Record Session audio from a microphone to a WAV file the existing pipeline can transcribe.

  python -m norev.adapters.recorder <out.wav> [--device N]   record until Ctrl+C
  python -m norev.adapters.recorder --list-devices           N and name of each input device

Messages go to stderr; stdout gets only the WAV path.
"""
import argparse
import json
import sys
from pathlib import Path

from norev.adapters.recorder.recorder import Recorder, list_input_devices, resolve_device


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m norev.adapters.recorder", description=__doc__.splitlines()[0])
    parser.add_argument("out_wav", nargs="?", help="WAV file to write")
    parser.add_argument("--device", help="N from --list-devices, or a device name (default: the first device)")
    parser.add_argument("--list-devices", action="store_true", help="print the input devices and exit")
    args = parser.parse_args(argv)
    if not args.list_devices and not args.out_wav:
        parser.error("out_wav is required")

    try:
        if args.list_devices:
            for index, name in enumerate(list_input_devices()):
                print(f"{index}: {name}")
            return 0

        device = resolve_device(args.device)
        recorder = Recorder(Path(args.out_wav), device=device)
        print(f"[INFO] Recording from {device!r}; Ctrl+C to stop", file=sys.stderr)
        seconds = recorder.record()
    except Exception as exc:
        print(json.dumps({"error": f"Recording failed: {exc}"}, ensure_ascii=False), file=sys.stderr)
        return 1

    if seconds <= 0:
        recorder.wav_path.unlink(missing_ok=True)
        print(json.dumps({"error": f"No audio was captured from {device!r}"}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(f"[INFO] Recorded {seconds:.1f}s to {recorder.wav_path}", file=sys.stderr)
    print(recorder.wav_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
