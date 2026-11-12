"""Extract and validate the Sibiu 2026 voice-embedding delivery."""

import argparse
from datetime import date
from hashlib import file_digest
import json
import math
from pathlib import Path


MODEL_HASH = "d51abcf31717ef28162f26acb9d44dd4127c3d44c9b8624f699f3425daca8e77"
DIMENSION = 192
SAMPLE_RATE = 16000
CLIP_FIELDS = {
    "sample_id", "meeting_id", "source_url", "start_s", "end_s",
    "split", "speaker_id", "speaker_name", "evidence",
}


def load(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def finite_number(value):
    return type(value) in (int, float) and math.isfinite(value)


def validate(rows, embeddings):
    if not rows:
        raise ValueError("Empty input")
    samples, meetings, speakers = set(), {}, {}
    required = CLIP_FIELDS | {"model_sha256", "embedding"} if embeddings else CLIP_FIELDS
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or set(row) != required:
            raise ValueError(f"Row {index}: unexpected or missing fields")
        sample, meeting, url, split = (row[k] for k in ("sample_id", "meeting_id", "source_url", "split"))
        for key in ("sample_id", "meeting_id", "source_url", "evidence"):
            if not isinstance(row[key], str) or not row[key].strip():
                raise ValueError(f"Row {index}: empty or invalid {key}")
        if not meeting.startswith("sibiu-2026-") or date.fromisoformat(meeting[6:]).isoformat() != meeting[6:]:
            raise ValueError(f"{sample}: invalid meeting ID")
        if not url.startswith("https://") or split not in ("enroll", "calibrate", "test"):
            raise ValueError(f"{sample}: invalid source URL or split")
        if sample in samples:
            raise ValueError(f"{sample}: duplicate sample ID")
        samples.add(sample)
        if meetings.setdefault(meeting, (url, split)) != (url, split):
            raise ValueError(f"{meeting}: use one recording and one split per meeting")
        start, end = row["start_s"], row["end_s"]
        if not all(map(finite_number, (start, end))) or start < 0 or not 3 <= end - start <= 12:
            raise ValueError(f"{sample}: expected an interval of 3–12 seconds")
        speaker, name = row["speaker_id"], row["speaker_name"]
        if speaker is None:
            if name is not None or split == "enroll":
                raise ValueError(f"{sample}: enrollment requires a confirmed speaker")
        elif (not isinstance(speaker, str) or not speaker.strip() or not isinstance(name, str)
              or not name.strip() or speakers.setdefault(speaker, name) != name):
            raise ValueError(f"{sample}: invalid or inconsistent speaker identity")
        if embeddings:
            vector = row["embedding"]
            if (row["model_sha256"] != MODEL_HASH or not isinstance(vector, list)
                or len(vector) != DIMENSION or not all(map(finite_number, vector))
                or not math.isclose(math.hypot(*vector), 1, abs_tol=1e-5)):
                raise ValueError(f"{sample}: invalid model hash, embedding dimension, or normalization")


def extract(model, clips, meeting, audio_path):
    from batch_colab import decode_audio
    import sherpa_onnx

    with model.open("rb") as source:
        if file_digest(source, "sha256").hexdigest() != MODEL_HASH:
            raise ValueError("Model SHA-256 differs from the pilot model")
    rows = load(clips)
    validate(rows, embeddings=False)
    selected = [row for row in rows if row["meeting_id"] == meeting]
    if not selected:
        raise ValueError(f"No clips for {meeting}")
    audio = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
    config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), provider="cpu", num_threads=4)
    extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
    for row in selected:
        start, end = (round(row[k] * SAMPLE_RATE) for k in ("start_s", "end_s"))
        if end > len(audio):
            raise ValueError(f"{row['sample_id']}: clip ends after the recording")
        stream = extractor.create_stream()
        stream.accept_waveform(SAMPLE_RATE, audio[start:end])
        stream.input_finished()
        vector = list(map(float, extractor.compute(stream)))
        norm = math.hypot(*vector)
        if not math.isfinite(norm) or norm == 0:
            raise ValueError(f"{row['sample_id']}: extractor returned an invalid vector")
        row.update(model_sha256=MODEL_HASH, embedding=[value / norm for value in vector])
    validate(selected, embeddings=True)
    return selected


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("extract")
    run.add_argument("model", type=Path)
    run.add_argument("clips", type=Path)
    run.add_argument("meeting")
    run.add_argument("audio", type=Path)
    run.add_argument("output", type=Path)
    commands.add_parser("validate").add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "extract":
            rows = extract(args.model, args.clips, args.meeting, args.audio)
            with args.output.open("x", encoding="utf-8") as target:
                target.writelines(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows)
        else:
            rows = load(args.output)
            validate(rows, embeddings=True)
    except (ValueError, OSError) as error:
        parser.exit(1, f"{error}\n")
    print(f"OK: {len(rows)} rows")
