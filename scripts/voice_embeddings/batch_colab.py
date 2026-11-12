"""CPU diarization and voice embeddings for the official Sibiu recordings."""

import csv
from hashlib import file_digest
from io import BytesIO
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile

import av
import numpy as np
import sherpa_onnx


RATE = 16000
SEGMENTATION_HASH = "220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079"


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def decode_audio(path, sampling_rate=RATE):
    """Decode mono PCM with the same PyAV resampling as the pilot."""
    pcm = BytesIO()
    resampler = av.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    with av.open(str(path), metadata_errors="ignore") as container:
        frames = iter(container.decode(audio=0))
        while True:
            try:
                frame = next(frames)
            except av.error.InvalidDataError:
                continue
            except StopIteration:
                break
            frame.pts = None
            for output in resampler.resample(frame):
                pcm.write(output.to_ndarray())
        for output in resampler.resample(None):
            pcm.write(output.to_ndarray())
    if not pcm.tell():
        raise ValueError("Fișierul nu conține audio decodabil.")
    return np.frombuffer(pcm.getbuffer(), dtype=np.int16).astype(np.float32) / 32768


def unit(vector):
    vector = np.asarray(vector, dtype=np.float64)
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm == 0:
        raise ValueError("Vector vocal invalid.")
    return vector / norm


def write_json(path, value):
    partial = path.with_suffix(".tmp")
    partial.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    partial.replace(path)


def speaker_profiles(path):
    from embeddings import load, validate

    if not path or not path.is_file():
        return {}
    rows = load(path)
    validate(rows, embeddings=True)
    grouped = {}
    for row in rows:
        if row["split"] == "enroll":
            grouped.setdefault(row["speaker_id"], []).append(row["embedding"])
    if len(grouped) < 2:
        raise ValueError("Pentru sugestii cu marjă trebuie minimum două profiluri de referință.")
    return {speaker: unit(np.mean(vectors, axis=0)) for speaker, vectors in grouped.items()}


def make_models(model, segmentation):
    from embeddings import MODEL_HASH

    if digest(model) != MODEL_HASH or digest(segmentation) != SEGMENTATION_HASH:
        raise ValueError("SHA-256 diferit pentru unul dintre modelele ONNX.")
    embedding = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), provider="cpu", num_threads=4)
    config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
        segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
            pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation)),
            provider="cpu", num_threads=4,
        ),
        embedding=embedding,
        clustering=sherpa_onnx.FastClusteringConfig(num_clusters=-1, threshold=0.6),
        min_duration_on=0.3,
        min_duration_off=0.5,
    )
    if not config.validate():
        raise RuntimeError("Configurarea diarizării nu este validă.")
    return sherpa_onnx.OfflineSpeakerDiarization(config), sherpa_onnx.SpeakerEmbeddingExtractor(embedding)


def analyze(meeting, audio, models, profiles, progress=None):
    from embeddings import MODEL_HASH

    diarizer, extractor = models
    turns = [
        dict(cluster_id=f"S{turn.speaker + 1}", start_s=max(0.0, turn.start),
             end_s=min(len(audio) / RATE, turn.end))
        for turn in diarizer.process(audio, callback=progress).sort_by_start_time()
    ]
    rows = []
    for turn_index, turn in enumerate(turns, 1):
        start, end = round(turn["start_s"] * RATE), round(turn["end_s"] * RATE)
        if end - start < 3 * RATE:
            continue
        if end - start > 8 * RATE:
            start = (start + end - 8 * RATE) // 2
            end = start + 8 * RATE
        stream = extractor.create_stream()
        stream.accept_waveform(RATE, audio[start:end])
        stream.input_finished()
        vector = unit(extractor.compute(stream))
        if len(vector) != 192:
            raise ValueError("Dimensiune TitaNet neașteptată.")
        rows.append(dict(
            sample_id=f"{meeting['meeting_id']}-auto-{turn_index:04d}", meeting_id=meeting["meeting_id"],
            source_url=meeting["source_url"], cluster_id=turn["cluster_id"], start_s=start / RATE,
            end_s=end / RATE, model_sha256=MODEL_HASH, embedding=vector.tolist(),
        ))
    return {"turns": turns, "embeddings": rows, "suggestions": suggestions(rows, profiles)}


def suggestions(rows, profiles):
    grouped = {}
    for row in rows:
        grouped.setdefault(row["cluster_id"], []).append(row)
    output = {}
    for cluster, members in grouped.items():
        selected = sorted(members, key=lambda row: row["end_s"] - row["start_s"], reverse=True)[:5]
        if profiles:
            centre = unit(np.mean([row["embedding"] for row in selected], axis=0))
            scores = sorted(((float(centre @ vector), speaker) for speaker, vector in profiles.items()), reverse=True)
            score, speaker = scores[0]
            margin = score - scores[1][0]
            output[cluster] = dict(suggested_speaker_id=speaker if score >= 0.55 and margin >= 0.10 else None,
                                   cosine=score, margin=margin)
    return output


def find_recording(meeting, inputs, temp):
    local = [p for p in inputs.glob(meeting["meeting_id"] + ".*")
             if p.suffix.lower() in {".mp4", ".webm", ".m4a", ".mp3", ".wav", ".mkv", ".opus", ".aac"}]
    if len(local) > 1:
        raise ValueError("Există mai multe fișiere locale pentru această ședință.")
    if local:
        return local[0]
    command = [
        sys.executable, "-m", "yt_dlp", "--no-playlist", "--abort-on-unavailable-fragments",
        "--no-progress", "--socket-timeout", "20", "--retries", "2", "--fragment-retries", "2",
        "--impersonate", "chrome",
        "-N", "12", "--referer", "https://sibiu.ro/consiliu/sedinte_live",
        "-f", "bestaudio/best", "-o", str(temp / "audio.%(ext)s"), "--", meeting["source_url"],
    ]
    run = subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=900)
    if run.returncode:
        raise RuntimeError((run.stderr or run.stdout).strip()[-1200:])
    (path,) = temp.glob("audio.*")
    return path


def batch(inventory, inputs, output, model, segmentation, reference, only=""):
    meetings = list(csv.DictReader(inventory.read_text(encoding="utf-8-sig").splitlines()))
    if not meetings:
        raise ValueError("Inventarul nu conține ședințe.")
    if len({row["meeting_id"] for row in meetings}) != len(meetings):
        raise ValueError("Inventarul conține ID-uri de ședință duplicate.")
    for row in meetings:
        meeting_id = row["meeting_id"]
        if not meeting_id.startswith("sibiu-2026-") or not all(c.isascii() and (c.isalnum() or c == "-") for c in meeting_id):
            raise ValueError(f"ID de ședință invalid: {meeting_id}")
    selected = {meeting.strip() for meeting in only.split(",") if meeting.strip()}
    if selected - {row["meeting_id"] for row in meetings}:
        raise ValueError("Filtrul include ID-uri absente din inventar.")
    profiles = speaker_profiles(reference)
    models = None
    output.mkdir(parents=True, exist_ok=True)
    status_file = output / "status.json"
    status = json.loads(status_file.read_text(encoding="utf-8")) if status_file.exists() else {}
    results = []
    for meeting in meetings:
        meeting_id = meeting["meeting_id"]
        target = output / f"{meeting_id}.json"
        if selected and meeting_id not in selected and not target.exists():
            continue
        try:
            result = json.loads(target.read_text(encoding="utf-8")) if target.exists() else None
            if result is None or result["meeting"] != meeting:
                if selected and meeting_id not in selected:
                    continue
                print(f"Procesez {meeting_id}", flush=True)
                if models is None:
                    models = make_models(model, segmentation)
                with TemporaryDirectory() as directory:
                    recording = find_recording(meeting, inputs, Path(directory))
                    audio = decode_audio(recording)
                    duration = len(audio) / RATE
                    expected = float(meeting["duration_s"])
                    if abs(duration - expected) > max(10, 0.04 * expected):
                        raise ValueError(f"Durată {duration:.1f}s, inventar {expected:.1f}s. Verifică sursa.")
                    result = analyze(meeting, audio, models, profiles)
                    result.update(meeting=meeting, recording_sha256=digest(recording), duration_s=duration)
                    del audio
                write_json(target, result)
            elif result["suggestions"] != (current := suggestions(result["embeddings"], profiles)):
                result["suggestions"] = current
                write_json(target, result)
            status[meeting_id] = dict(status="ok", turns=len(result["turns"]), embeddings=len(result["embeddings"]))
            results.append(target)
        except Exception as error:
            status[meeting_id] = dict(status="failed", error=f"{type(error).__name__}: {error}")
        write_json(status_file, status)
        print(meeting_id, status[meeting_id], flush=True)
    archive = output / "sibiu-2026-colab.zip"
    with ZipFile(archive.with_suffix(".tmp"), "w", compression=ZIP_DEFLATED) as bundle:
        bundle.write(inventory, "meetings.csv")
        bundle.write(status_file, "status.json")
        for target in results:
            bundle.write(target, f"meetings/{target.name}")
        with bundle.open("auto-embeddings.jsonl", "w") as combined:
            for target in results:
                result = json.loads(target.read_text(encoding="utf-8"))
                for row in result["embeddings"]:
                    row["recording_sha256"] = result["recording_sha256"]
                    combined.write((json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8"))
    archive.with_suffix(".tmp").replace(archive)
    return archive, len(results), len(meetings)
