"""Diarizer port: sherpa-onnx on the CPU, any number of Speakers (a council meeting has 20+).

With the Transcript's segments (`spans`), each segment gets one TitaNet speaker embedding and the
segments are grouped by average-linkage clustering on cosine distance. Measured on a 51-min council
meeting: at 0.60 the chair's procedural lines stay one Speaker (32/35) while 11 other speakers stay
apart, which sherpa's own pipeline (short pyannote windows) and smaller embedding models did not do.
Without spans it falls back to sherpa's pipeline (pyannote segmentation 3.0 + the same embeddings).
`pip install sherpa-onnx`; `norev.cmd setup-models` downloads the models once into models\\diarization.
Runs offline after that. The audio is decoded in memory, never written to a file (ADR 0004).
"""
import io
import os
from pathlib import Path

from norev.core.models import SpeakerTurn
from norev.core.ports import SessionStore

SAMPLE_RATE = 16000
SEGMENTATION_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-segmentation-models/"
                    "sherpa-onnx-pyannote-segmentation-3-0.tar.bz2")
EMBEDDING_URL = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/speaker-recongition-models/"
                 "nemo_en_titanet_large.onnx")
SEGMENTATION = "segmentation.onnx"
EMBEDDING = "titanet_large.onnx"
MIN_SECONDS = 0.5  # shorter segments ("Da.", "5.") give no reliable embedding: they keep no Speaker


def threshold() -> float:
    return float(os.getenv("NOREV_DIARIZATION_THRESHOLD", "0.6"))


def cluster(embeddings, limit: float) -> list[int]:
    """Average-linkage agglomerative clustering on cosine distance, merged while the closest pair is
    within `limit`. Returns one cluster number per row; the order of rows decides nothing."""
    import numpy as np

    x = np.asarray(embeddings, dtype=np.float64)
    if len(x) == 0:
        return []
    x = x / np.linalg.norm(x, axis=1, keepdims=True)
    dist = 1.0 - x @ x.T
    np.fill_diagonal(dist, np.inf)
    size = np.ones(len(x))
    label = np.arange(len(x))
    while True:
        i, j = np.unravel_index(np.argmin(dist), dist.shape)
        if not dist[i, j] <= limit:
            break
        merged = (size[i] * dist[i] + size[j] * dist[j]) / (size[i] + size[j])
        dist[i, :] = merged
        dist[:, i] = merged
        dist[i, i] = np.inf
        dist[j, :] = np.inf
        dist[:, j] = np.inf
        size[i] += size[j]
        label[label == j] = i
    return [int(v) for v in label]


def models_dir() -> Path:
    if os.getenv("NOREV_DIARIZATION_DIR"):
        return Path(os.environ["NOREV_DIARIZATION_DIR"])
    from norev.launcher.paths import AppPaths

    return AppPaths.from_environment().models / "diarization"


def download_models(target: Path) -> None:
    """The only network use: the two model files, once."""
    import tarfile
    import tempfile
    import urllib.request

    target.mkdir(parents=True, exist_ok=True)
    if not (target / EMBEDDING).is_file():
        urllib.request.urlretrieve(EMBEDDING_URL, target / EMBEDDING)
    if not (target / SEGMENTATION).is_file():
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "segmentation.tar.bz2"
            urllib.request.urlretrieve(SEGMENTATION_URL, archive)
            with tarfile.open(archive) as tar:
                member = next(m for m in tar.getmembers() if m.name.endswith("/model.onnx"))
                with tar.extractfile(member) as src:
                    (target / SEGMENTATION).write_bytes(src.read())


class SherpaDiarizer:
    wants_spans = True  # transcribe_session passes the Transcript's segments

    def __init__(self, store: SessionStore, model_dir: Path | None = None) -> None:
        self._store = store
        self._dir = model_dir or models_dir()
        self._model = None
        self._extractor = None

    def diarize(self, audio_ref: str, spans=None) -> list[SpeakerTurn]:
        """Who spoke when; speaker labels are cluster numbers, never a guess at who the person is."""
        from faster_whisper import decode_audio

        audio = decode_audio(io.BytesIO(self._store.load_audio(audio_ref)), sampling_rate=SAMPLE_RATE)
        if spans is not None:
            return self._by_segment(audio, spans)
        result = self._load().process(audio).sort_by_start_time()
        turns = [SpeakerTurn(float(r.start), float(r.end), str(r.speaker)) for r in result]
        return sorted(turns, key=lambda t: (t.start, t.end, t.speaker_id))

    def _by_segment(self, audio, spans) -> list[SpeakerTurn]:
        extractor = self._embeddings()
        kept, vectors = [], []
        for start, end in spans:
            if end - start < MIN_SECONDS:
                continue
            stream = extractor.create_stream()
            stream.accept_waveform(SAMPLE_RATE, audio[int(start * SAMPLE_RATE):int(end * SAMPLE_RATE)])
            stream.input_finished()
            kept.append((float(start), float(end)))
            vectors.append(extractor.compute(stream))
        labels = cluster(vectors, threshold())
        return sorted((SpeakerTurn(start, end, str(label)) for (start, end), label in zip(kept, labels)),
                      key=lambda t: (t.start, t.end, t.speaker_id))

    def _embeddings(self):
        if self._extractor is None:
            import sherpa_onnx

            if not (self._dir / EMBEDDING).is_file():
                raise FileNotFoundError(f"{self._dir / EMBEDDING}: run norev.cmd setup-models")
            self._extractor = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                model=str(self._dir / EMBEDDING), num_threads=max(1, (os.cpu_count() or 2) - 1)))
        return self._extractor

    def close(self) -> None:
        self._model = None
        self._extractor = None

    def _load(self):
        if self._model is None:
            import sherpa_onnx

            for name in (SEGMENTATION, EMBEDDING):
                if not (self._dir / name).is_file():
                    raise FileNotFoundError(f"{self._dir / name}: run norev.cmd setup-models")
            threads = max(1, (os.cpu_count() or 2) - 1)
            config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
                segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                    pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(
                        model=str(self._dir / SEGMENTATION)),
                    num_threads=threads,
                ),
                embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(self._dir / EMBEDDING), num_threads=threads),
                clustering=sherpa_onnx.FastClusteringConfig(
                    num_clusters=int(os.getenv("NOREV_SPEAKER_COUNT", "-1")),
                    threshold=threshold(),
                ),
                min_duration_on=0.3,
                min_duration_off=0.5,
            )
            if not config.validate():
                raise RuntimeError("invalid sherpa-onnx diarization config")
            self._model = sherpa_onnx.OfflineSpeakerDiarization(config)
        return self._model
