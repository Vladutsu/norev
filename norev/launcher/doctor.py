"""First-start check (#48): what norev needs on this machine, and how to add what is missing.

No check downloads anything or opens a network connection, and no check can crash the run: a
probe that fails is reported as a failed check. Messages are for the User, in Romanian.
"""
import importlib.util
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from norev.adapters.store.encrypted import StoreKeyError, read_key_file
from norev.launcher.paths import AppPaths

DIARIZATION_MODEL = "nvidia/diar_streaming_sortformer_4spk-v2"  # same as norev.adapters.diarization.sortformer


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    required: bool = True  # required: the real Session flow cannot run without it


def _cuda_devices() -> int:
    import ctranslate2

    return ctranslate2.get_cuda_device_count()


def _whisper_cached(name: str, hf_home: Path) -> Path | None:
    if Path(name).is_dir():
        return Path(name)
    from faster_whisper.utils import download_model

    try:
        return Path(download_model(name, local_files_only=True, cache_dir=str(hf_home / "hub")))
    except Exception:
        return None


def _hf_cached(repo: str, hf_home: Path) -> Path | None:
    from huggingface_hub import snapshot_download

    try:
        return Path(snapshot_download(repo, local_files_only=True, cache_dir=str(hf_home / "hub")))
    except Exception:
        return None


@dataclass
class Probes:
    """Everything the checks ask the machine; tests replace these."""

    find_spec: Callable[[str], object] = importlib.util.find_spec
    which: Callable[[str], str | None] = shutil.which
    cuda_devices: Callable[[], int] = _cuda_devices
    whisper_cached: Callable[[str, Path], Path | None] = _whisper_cached
    hf_cached: Callable[[str, Path], Path | None] = _hf_cached
    version: tuple = field(default_factory=lambda: tuple(sys.version_info[:2]))
    in_venv: bool = field(default_factory=lambda: sys.prefix != sys.base_prefix)


def run_checks(paths: AppPaths, env: Mapping[str, str] | None = None, probes: Probes | None = None) -> list[Check]:
    env = os.environ if env is None else env
    probes = probes or Probes()
    checks = []
    for check in (_python, _packages, _gpu, _llama_server, _gguf, _whisper, _speakers, _key):
        try:
            checks.extend(check(paths, env, probes))
        except Exception as e:  # a probe failing is a finding, not a crash
            checks.append(Check(check.__name__.strip("_"), False, f"verificarea a eșuat ({type(e).__name__})"))
    return checks


def _python(paths, env, probes):
    ok = probes.version >= (3, 11)
    yield Check("Python", ok, f"{probes.version[0]}.{probes.version[1]}",
                "" if ok else "Instalează Python 3.12 de pe python.org, apoi rulează din nou install.cmd.")
    if not probes.in_venv:
        yield Check("Mediu .venv", False, "norev nu rulează din .venv",
                    "Pornește prin norev.cmd (sau rulează install.cmd).", required=False)


def _packages(paths, env, probes):
    for module, label, fix_file, required in (
        ("faster_whisper", "Transcriere (faster-whisper)", "requirements.txt", True),
        ("cryptography", "Criptare (cryptography)", "requirements.txt", True),
        ("webview", "Fereastra aplicației (pywebview)", "requirements-ui.txt", True),
    ):
        ok = probes.find_spec(module) is not None
        yield Check(label, ok, "instalat" if ok else "lipsește",
                    "" if ok else f"Rulează install.cmd (instalează {fix_file}).", required)


def _gpu(paths, env, probes):
    if probes.find_spec("ctranslate2") is None:
        yield Check("Placă video (CUDA)", False, "nu se poate verifica: lipsește ctranslate2",
                    "Rulează install.cmd.", required=False)
        return
    devices = probes.cuda_devices()
    if devices > 0:
        yield Check("Placă video (CUDA)", True, f"{devices} GPU NVIDIA disponibil(e)")
    else:
        hint = "driverul NVIDIA pare instalat" if probes.which("nvidia-smi") else "nu am găsit driverul NVIDIA (nvidia-smi)"
        yield Check("Placă video (CUDA)", False, f"nicio placă CUDA: transcrierea merge pe procesor, mult mai încet ({hint})",
                    "Instalează driverul NVIDIA actual; fără GPU norev funcționează, dar lent.", required=False)


def _llama_server(paths, env, probes):
    server = Path(env.get("LLAMA_SERVER") or paths.llama_server)
    ok = server.is_file()
    yield Check("llama-server", ok, str(server),
                "" if ok else f"Descarcă llama.cpp pentru Windows (CUDA) de pe github.com/ggml-org/llama.cpp/releases "
                              f"și dezarhivează-l în {server.parent}.")


def _gguf(paths, env, probes):
    model = Path(env.get("NOREV_EXTRACT_MODEL") or env.get("LLAMACPP_MODEL") or paths.gguf)
    ok = model.is_file() and model.stat().st_size > 0
    others = sorted(p.name for p in model.parent.glob("*.gguf")) if model.parent.is_dir() else []
    fix = ""
    if not ok:
        fix = f"Copiază modelul {model.name} în {model.parent}."
        if others:
            fix += f" Am găsit acolo: {', '.join(others)} (setează NOREV_EXTRACT_MODEL pentru altul)."
    yield Check("Model LLM (GGUF)", ok, str(model), fix)


def _whisper(paths, env, probes):
    if probes.find_spec("faster_whisper") is None:
        return
    name = env.get("WHISPER_MODEL", "large-v3-turbo")
    hf_home = Path(env.get("HF_HOME") or paths.hf_home)
    found = probes.whisper_cached(name, hf_home)
    yield Check("Model Whisper", found is not None, str(found) if found else f"{name}: nu e pe acest PC",
                "" if found else "Rulează: norev.cmd setup-models (descarcă modelul o singură dată, ~1.6 GB).")


def _speakers(paths, env, probes):
    if probes.find_spec("nemo") is None:
        yield Check("Vorbitori (diarizare)", False, "neinstalată: Transcript-ul nu va avea Vorbitori",
                    "Opțional: install.cmd --speakers, apoi norev.cmd setup-models.", required=False)
        return
    hf_home = Path(env.get("HF_HOME") or paths.hf_home)
    found = probes.hf_cached(DIARIZATION_MODEL, hf_home)
    yield Check("Model diarizare", found is not None, str(found) if found else f"{DIARIZATION_MODEL}: nu e pe acest PC",
                "" if found else "Rulează: norev.cmd setup-models.", required=False)


def _key(paths, env, probes):
    key = Path(env.get("NOREV_STORE_KEY_FILE") or paths.key)
    store = Path(env.get("NOREV_STORE_PATH") or paths.store)
    if key.is_file():
        try:
            read_key_file(key)
        except StoreKeyError:
            sealed = key.suffix == ".dpapi"
            yield Check("Cheia store-ului", False,
                        f"{key}: {'contul Windows curent nu o poate deschide' if sealed else 'fișier deteriorat'}",
                        "Pune înapoi backup-ul cheii: norev.cmd import-key <fișierul de backup>.")
            return
        sealed = key.suffix == ".dpapi"
        yield Check("Cheia store-ului", True, f"{key}" + (" (protejată de contul Windows)" if sealed else ""))
        if paths.windows and not sealed:
            yield Check("Protecția cheii", False, "cheia e un fișier neprotejat",
                        "Rulează: norev.cmd protect-key (o singură dată; păstrează datele).", required=False)
    elif store.is_file():
        yield Check("Cheia store-ului", False, f"lipsește {key}, dar există date în {store}",
                    "Pune înapoi backup-ul cheii: norev.cmd import-key <fișier>. O cheie nouă NU poate citi datele existente.")
    else:
        yield Check("Cheia store-ului", False, f"lipsește {key}",
                    "Rulează: norev.cmd new-key, apoi norev.cmd export-key <stick USB> pentru backup.")


def report(checks: list[Check]) -> str:
    lines = []
    for c in checks:
        mark = "OK " if c.ok else ("LIPSEȘTE" if c.required else "ATENȚIE")
        lines.append(f"[{mark:^8}] {c.name}: {c.detail}")
        if c.fix:
            lines.append(f"           → {c.fix}")
    missing = [c.name for c in checks if c.required and not c.ok]
    lines.append("")
    lines.append("Totul e pregătit." if not missing else f"Lipsesc: {', '.join(missing)}.")
    return "\n".join(lines)


def ready(checks: list[Check]) -> bool:
    return all(c.ok for c in checks if c.required)
