"""Resume the local Sibiu 2026 batch from small CPU diarization blocks."""

import argparse
import csv
from io import BytesIO
import json
import math
from pathlib import Path
import sys
from time import monotonic
from zipfile import ZIP_DEFLATED, ZipFile

import av
import numpy as np

from batch_colab import RATE, analyze, digest, make_models, speaker_profiles, suggestions, write_json
from embeddings import MODEL_HASH


INVENTORY = Path(__file__).with_name("meetings-colab.csv")
BLOCK_SECONDS = 600


def audio_blocks(recording):
    resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)
    buffer = BytesIO()
    block_bytes = BLOCK_SECONDS * RATE * 2
    with av.open(str(recording), metadata_errors="ignore") as container:
        def frames():
            source = iter(container.decode(audio=0))
            while True:
                try:
                    frame = next(source)
                except av.error.InvalidDataError:
                    continue
                except StopIteration:
                    break
                frame.pts = None
                yield from resampler.resample(frame)
            yield from resampler.resample(None)

        for frame in frames():
            buffer.write(frame.to_ndarray())
            if buffer.tell() >= block_bytes:
                buffer.seek(0)
                audio = np.frombuffer(buffer.read(block_bytes), dtype=np.int16).astype(np.float32) / 32768
                remainder = buffer.read()
                buffer = BytesIO()
                buffer.write(remainder)
                yield audio
        if buffer.tell():
            yield np.frombuffer(buffer.getvalue(), dtype=np.int16).astype(np.float32) / 32768


def progress_callback():
    last = monotonic()

    def report(done, total):
        nonlocal last
        now = monotonic()
        if done == total or now - last >= 20:
            print(f"  diarizare {done}/{total} ({done / max(1, total):.0%})", flush=True)
            last = now
        return 0
    return report


def process(meeting, recording, audio_hash, models, profiles, output):
    meeting_id = meeting["meeting_id"]
    checkpoints = output / "blocks" / meeting_id / audio_hash
    checkpoints.mkdir(parents=True, exist_ok=True)
    result = {"turns": [], "embeddings": []}
    samples = 0
    expected = int(meeting["duration_s"])
    for number, audio in enumerate(audio_blocks(recording), 1):
        checkpoint = checkpoints / f"{BLOCK_SECONDS}s-{MODEL_HASH}-{number:03}.json"
        if checkpoint.exists():
            partial = json.loads(checkpoint.read_text(encoding="utf-8"))
            print(f"  bloc {number} recuperat", flush=True)
        else:
            print(f"  bloc {number}/{math.ceil(expected / BLOCK_SECONDS)} la {samples / RATE:.0f}s", flush=True)
            block_id = f"B{number:03}"
            partial = analyze(meeting, audio, models, {}, progress_callback())
            offset = samples / RATE
            for key in ("turns", "embeddings"):
                for item in partial[key]:
                    item["cluster_id"] = f"{block_id}-{item['cluster_id']}"
                    item["start_s"] += offset
                    item["end_s"] += offset
                    if key == "embeddings":
                        item["sample_id"] = f"{block_id}-{item['sample_id']}"
            write_json(checkpoint, partial)
            print(f"  bloc {number} salvat, {len(partial['embeddings'])} vectori", flush=True)
        samples += len(audio)
        for key in result:
            result[key].extend(partial[key])
    duration = samples / RATE
    if abs(duration - expected) > max(10, 0.04 * expected):
        raise ValueError(f"Durată {duration:.1f}s, inventar {expected}s.")
    result.update(meeting=meeting, recording_sha256=audio_hash, duration_s=duration,
                  segmentation_block_seconds=BLOCK_SECONDS,
                  suggestions=suggestions(result["embeddings"], profiles))
    return result


def export(meetings, status, output):
    archive = output / "sibiu-2026-local.zip"
    with ZipFile(archive.with_suffix(".tmp"), "w", compression=ZIP_DEFLATED) as bundle:
        bundle.write(INVENTORY, "meetings.csv")
        bundle.write(output / "status.json", "status.json")
        for meeting in meetings:
            mid = meeting["meeting_id"]
            if status.get(mid, {}).get("status") == "ok":
                bundle.write(output / f"{mid}.json", f"meetings/{mid}.json")
        with bundle.open("auto-embeddings.jsonl", "w") as combined:
            for meeting in meetings:
                mid = meeting["meeting_id"]
                if status.get(mid, {}).get("status") != "ok":
                    continue
                result = json.loads((output / f"{mid}.json").read_text(encoding="utf-8"))
                for row in result["embeddings"]:
                    row["recording_sha256"] = result["recording_sha256"]
                    combined.write((json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    archive.with_suffix(".tmp").replace(archive)


def run(runtime):
    meetings = list(csv.DictReader(INVENTORY.read_text(encoding="utf-8-sig").splitlines()))
    output = runtime / "sibiu-2026-local"
    inputs = runtime / "sibiu-colab-retry-inputs"
    output.mkdir(exist_ok=True)
    paths = {m["meeting_id"]: inputs / f"{m['meeting_id']}.mp4" for m in meetings}
    missing = [path.name for path in paths.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    profiles = speaker_profiles(inputs / "reference-embeddings.jsonl")
    model_dir = runtime / "models" / "diarization"
    models = None
    status = {}
    for meeting in meetings:
        mid = meeting["meeting_id"]
        target = output / f"{mid}.json"
        recording = paths[mid]
        audio_hash = digest(recording)
        try:
            prior = target if target.exists() else runtime / "sibiu-colab-retry-results" / f"{mid}.json"
            result = json.loads(prior.read_text(encoding="utf-8")) if prior.exists() else None
            if result and result["recording_sha256"] == audio_hash and result["meeting"] == meeting:
                print(f"{mid} recuperat", flush=True)
            else:
                print(f"{mid} începe · {meeting['duration_s']}s", flush=True)
                if models is None:
                    models = make_models(model_dir / "titanet_large.onnx", model_dir / "segmentation.onnx")
                result = process(meeting, recording, audio_hash, models, profiles, output)
            write_json(target, result)
            status[mid] = dict(status="ok", turns=len(result["turns"]), embeddings=len(result["embeddings"]))
        except Exception as error:
            status[mid] = dict(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(output / "status.json", status)
        print(mid, status[mid], flush=True)
    export(meetings, status, output)
    ok = sum(item["status"] == "ok" for item in status.values())
    print(f"{ok}/{len(meetings)} ședințe. Arhivă: {output / 'sibiu-2026-local.zip'}", flush=True)
    return ok == len(meetings)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, required=True, help="Directory with recordings and models")
    sys.exit(not run(parser.parse_args().runtime_dir.resolve()))
