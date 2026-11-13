"""Conservative TitaNet matching against a local, opt-in profile file."""
import hashlib
import json
import math
from pathlib import Path

from norev.adapters.diarization.sherpa import MAX_EMBEDDING_SECONDS, SAMPLE_RATE

DIMENSION = 192
MIN_SCORE = .70
MIN_MARGIN = .15
MIN_SECONDS = 4


def load_profiles(path: Path, model: Path):
    import numpy as np

    data = json.loads(path.read_text(encoding="utf-8-sig"))
    with model.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if data["model_sha256"] != digest or data["dimension"] != DIMENSION:
        raise ValueError("Voice profiles do not match the installed TitaNet model")
    names, vectors = [], []
    for profile in data["profiles"]:
        name = profile["speaker_name"]
        vector = np.asarray(profile["embedding"], dtype=float)
        if (not isinstance(name, str) or not name.strip() or any(not c.isprintable() for c in name)
                or "[" in name or "]" in name or name in names
                or vector.shape != (DIMENSION,) or not np.isfinite(vector).all()
                or not math.isclose(float(np.linalg.norm(vector)), 1, abs_tol=1e-5)):
            raise ValueError("Invalid voice profile name or vector")
        names.append(name)
        vectors.append(vector)
    if len(names) < 2:
        raise ValueError("Voice matching requires at least two distinct profiles")
    return names, np.asarray(vectors)


def profile_names(path) -> list[str]:
    """The names in the profile file, for the User to pick from; [] when there is none or it is unreadable."""
    try:
        return sorted(p["speaker_name"] for p in json.loads(Path(path).read_text(encoding="utf-8-sig"))["profiles"]
                      if isinstance(p.get("speaker_name"), str))
    except (OSError, ValueError, KeyError, TypeError):
        return []


def match_vector(vector, profiles):
    import numpy as np

    names, references = profiles
    vector = np.asarray(vector, dtype=float)
    norm = np.linalg.norm(vector)
    if not np.isfinite(norm) or norm == 0:
        return None
    scores = references @ (vector / norm)
    runner_up, best = np.argsort(scores)[-2:]
    if scores[best] >= MIN_SCORE and scores[best] - scores[runner_up] >= MIN_MARGIN:
        return names[best]
    return None


def match_spans(path, model, extractor, audio, spans):
    profiles = load_profiles(path, model)
    names = []
    for start, end in spans:
        left = max(0, round(start * SAMPLE_RATE))
        right = min(len(audio), round(end * SAMPLE_RATE))
        duration = (right - left) / SAMPLE_RATE
        if duration < MIN_SECONDS:
            names.append(None)
            continue
        # Score all of a long sentence. A single doubtful or conflicting window leaves it anonymous.
        count = math.ceil(duration / MAX_EMBEDDING_SECONDS)
        matched = set()
        for i in range(count):
            a = left + (right - left) * i // count
            b = left + (right - left) * (i + 1) // count
            stream = extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio[a:b])
            stream.input_finished()
            matched.add(match_vector(extractor.compute(stream), profiles))
            if None in matched or len(matched) > 1:
                break
        names.append(next(iter(matched)) if len(matched) == 1 else None)
    return names
