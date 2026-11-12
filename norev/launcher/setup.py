"""One-time setup, run by install.cmd / install.sh (and by the first start if it was skipped).

  1. detect the hardware and save the tier (hardware.py) in NOREV_HOME/hardware.json
  2. install the packages that tier needs: CUDA libraries only for NVIDIA, a CPU-only torch for
     diarization elsewhere (it is ~200 MB instead of ~6 GB)
  3. download, once: Whisper large-v3-turbo, the diarization model, the council GGUF (Hugging Face,
     checked by SHA-256) and the llama.cpp build for the tier's backend (GitHub, checked by SHA-256)
  4. if that llama.cpp build does not run on this machine (no Vulkan driver, ...), fall back to the
     CPU build, so the app always starts

Only these downloads go out to the network, never Session data (ADR 0001). Every step skips what
is already there, so running setup again is safe and only fills what is missing.
"""
import dataclasses
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from norev.launcher import hardware
from norev.launcher.paths import AppPaths

ROOT = Path(__file__).resolve().parent.parent.parent
# Pinned llama.cpp build: since 2026 every build is a GitHub prerelease and "latest" holds no binaries.
# shortcut: pinned by hand; bump it after checking the flags in norev/launcher/llama.py still exist
LLAMA_BUILD = os.environ.get("NOREV_LLAMA_BUILD", "b12249")
LLAMA_RELEASE = "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/{build}"

# (system, arch, backend) -> (llama.cpp asset, CUDA runtime asset or None); {b} is the build
LLAMA_ASSETS = {
    ("windows", "x64", "cuda"): ("llama-{b}-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"),
    ("windows", "x64", "vulkan"): ("llama-{b}-bin-win-vulkan-x64.zip", None),
    ("windows", "x64", "cpu"): ("llama-{b}-bin-win-cpu-x64.zip", None),
    ("windows", "arm64", "vulkan"): ("llama-{b}-bin-win-vulkan-arm64.zip", None),
    ("windows", "arm64", "cpu"): ("llama-{b}-bin-win-cpu-arm64.zip", None),
    ("linux", "x64", "cuda"): ("llama-{b}-bin-ubuntu-cuda-12.8-x64.tar.gz", "cudart-llama-{b}-bin-ubuntu-cuda-12.8-x64.tar.gz"),
    ("linux", "x64", "vulkan"): ("llama-{b}-bin-ubuntu-vulkan-x64.tar.gz", None),
    ("linux", "x64", "cpu"): ("llama-{b}-bin-ubuntu-x64.tar.gz", None),
    ("linux", "arm64", "vulkan"): ("llama-{b}-bin-ubuntu-vulkan-arm64.tar.gz", None),
    ("linux", "arm64", "cpu"): ("llama-{b}-bin-ubuntu-arm64.tar.gz", None),
    ("darwin", "arm64", "metal"): ("llama-{b}-bin-macos-arm64.tar.gz", None),
    ("darwin", "x64", "cpu"): ("llama-{b}-bin-macos-x64.tar.gz", None),
}


class SetupError(RuntimeError):
    """A step could not finish; the message says which and what to do (Romanian, for the User)."""


def say(text: str) -> None:
    print(f"[norev] {text}", flush=True)


# --- 2. packages

def pip(*args: str) -> None:
    code = subprocess.run([sys.executable, "-m", "pip", "install", "--disable-pip-version-check", *args]).returncode
    if code:
        raise SetupError(f"pip install {' '.join(args)} a eșuat (cod {code}); verifică internetul și rulează din nou")


def install_packages(profile: hardware.Profile, speakers: bool, find_spec=None) -> None:
    import importlib.util

    find_spec = find_spec or importlib.util.find_spec
    if profile.cuda_wheels:
        say("Instalez bibliotecile CUDA pentru Whisper (placă NVIDIA) ...")
        pip("-r", str(ROOT / "requirements-cuda.txt"))
    if speakers and find_spec("sherpa_onnx") is None:
        # sherpa-onnx runs on the CPU on every tier and has no Speaker limit (a council meeting has 20+)
        say("Instalez diarizarea (Vorbitori, sherpa-onnx) ...")
        # one command with requirements.txt, so pip keeps its pins (see requirements-diarization.txt)
        pip("-r", str(ROOT / "requirements.txt"), "-r", str(ROOT / "requirements-diarization.txt"))


# --- 3. downloads

def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: Path, expected_sha256: str | None) -> Path:
    """Download to target (via a .part file), refusing a file whose hash is not the expected one."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": "norev-setup"})
    with urllib.request.urlopen(request, timeout=60) as response, open(part, "wb") as out:
        total = int(response.headers.get("Content-Length") or 0)
        done, shown = 0, -1
        while block := response.read(1 << 20):
            out.write(block)
            done += len(block)
            if total and (percent := done * 100 // total) // 10 != shown:
                shown = percent // 10
                print(f"    {target.name}: {percent}%", flush=True)
    if expected_sha256 and sha256(part) != expected_sha256:
        part.unlink()
        raise SetupError(f"{target.name}: fișierul descărcat nu are amprenta așteptată; nu îl folosesc")
    part.replace(target)
    return target


def fetch_gguf(model: hardware.Model, models_dir: Path) -> Path:
    target = models_dir / model.file
    if target.is_file() and target.stat().st_size == model.size:
        say(f"Modelul LLM {model.file} e deja descărcat.")
        return target
    say(f"Descarc modelul LLM {model.file} ({model.size / hardware.GB:.1f} GB) ...")
    return download(f"https://huggingface.co/{model.repo}/resolve/main/{model.file}", target, model.sha256)


def fetch_speech_models(paths: AppPaths, find_spec=None) -> None:
    import importlib.util

    from faster_whisper.utils import download_model

    from norev.launcher.doctor import DIARIZATION_MODEL

    find_spec = find_spec or importlib.util.find_spec
    cache = str(paths.hf_home / "hub")
    name = os.environ.get("WHISPER_MODEL", "large-v3-turbo")
    say(f"Descarc modelul Whisper {name} (~1.6 GB, o singură dată) ...")
    download_model(name, cache_dir=cache)
    if find_spec("sherpa_onnx") is not None:
        fetch_sherpa_models(paths.models / "diarization")
    if find_spec("nemo") is not None:  # Sortformer, only when installed by hand (NOREV_DIARIZER=sortformer)
        from huggingface_hub import snapshot_download

        say(f"Descarc modelul de diarizare {DIARIZATION_MODEL} ...")
        snapshot_download(DIARIZATION_MODEL, cache_dir=cache)


def fetch_sherpa_models(target: Path) -> None:
    """The two sherpa-onnx diarization models (segmentation and TitaNet embeddings), once."""
    from norev.adapters.diarization import sherpa

    target.mkdir(parents=True, exist_ok=True)
    if not (target / sherpa.EMBEDDING).is_file():
        say("Descarc modelul de diarizare TitaNet (sherpa-onnx) ...")
        download(sherpa.EMBEDDING_URL, target / sherpa.EMBEDDING, None)
    if not (target / sherpa.SEGMENTATION).is_file():
        say("Descarc modelul de segmentare pyannote (sherpa-onnx) ...")
        with tempfile.TemporaryDirectory(dir=target) as tmp:
            archive = download(sherpa.SEGMENTATION_URL, Path(tmp) / "segmentation.tar.bz2", None)
            with tarfile.open(archive) as tar:
                member = next((m for m in tar.getmembers() if m.name.endswith("/model.onnx")), None)
                if member is None:
                    raise SetupError("modelul de segmentare nu e în arhivă")
                with tar.extractfile(member) as src:
                    (target / sherpa.SEGMENTATION).write_bytes(src.read())


def release_assets(build: str, opener=urllib.request.urlopen) -> dict[str, tuple[str, str | None]]:
    """name -> (download URL, sha256 or None) for one llama.cpp build."""
    request = urllib.request.Request(LLAMA_RELEASE.format(build=build),
                                     headers={"User-Agent": "norev-setup", "Accept": "application/vnd.github+json"})
    with opener(request, timeout=60) as response:
        data = json.load(response)
    return {a["name"]: (a["browser_download_url"], (a.get("digest") or "").removeprefix("sha256:") or None)
            for a in data.get("assets", [])}


def asset_names(system: str, arch: str, backend: str, build: str) -> tuple[str, str | None]:
    key = (system, arch, backend)
    if key not in LLAMA_ASSETS:
        key = (system, arch, "cpu")
    if key not in LLAMA_ASSETS:
        raise SetupError(f"nu există llama.cpp pentru {system}/{arch}")
    main, runtime = LLAMA_ASSETS[key]
    return main.format(b=build), runtime.format(b=build) if runtime else None


def _extract(archive: Path, into: Path) -> None:
    """Unpack a release archive, refusing any entry that would land outside `into`."""
    into.mkdir(parents=True, exist_ok=True)
    root = into.resolve()
    if archive.name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                if not (root / name).resolve().is_relative_to(root):
                    raise SetupError(f"arhiva {archive.name} are o cale nesigură: {name}")
            z.extractall(into)
    else:
        with tarfile.open(archive) as t:
            t.extractall(into, filter="data")  # "data" refuses absolute paths, links out, devices


def _binary_dir(folder: Path, exe: str) -> Path:
    found = sorted(folder.rglob(exe))
    if not found:
        raise SetupError(f"llama-server nu e în arhiva llama.cpp ({folder})")
    return found[0].parent


def install_llama(paths: AppPaths, hw: hardware.Hardware, backend: str, build: str = LLAMA_BUILD,
                  assets=release_assets) -> Path:
    """Download and unpack the build into NOREV_HOME/llama.cpp/<build>-<backend>/; return llama-server."""
    folder = paths.root / "llama.cpp" / f"{build}-{backend}"
    exe = "llama-server.exe" if hw.system == "windows" else "llama-server"
    if folder.is_dir() and list(folder.rglob(exe)):
        return _binary_dir(folder, exe) / exe
    main, runtime = asset_names(hw.system, hw.arch, backend, build)
    available = assets(build)
    with tempfile.TemporaryDirectory(dir=paths.root) as tmp:
        staging = Path(tmp) / "unpacked"
        for name in filter(None, (main, runtime)):
            if name not in available:
                raise SetupError(f"llama.cpp {build} nu are {name}; setează NOREV_LLAMA_BUILD la un build care îl are")
            url, digest = available[name]
            say(f"Descarc {name} ...")
            archive = download(url, Path(tmp) / name, digest)
            _extract(archive, staging / ("runtime" if name == runtime else "main"))
        bin_dir = _binary_dir(staging / "main", exe)
        if runtime:  # the CUDA runtime libraries go next to llama-server, where it looks for them
            for lib in (staging / "runtime").rglob("*"):
                if lib.is_file() and (lib.suffix == ".dll" or ".so" in lib.name):
                    shutil.copy2(lib, bin_dir / lib.name)
        if folder.exists():
            shutil.rmtree(folder)
        folder.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(bin_dir), str(folder))
    server = folder / exe
    if hw.system != "windows":
        server.chmod(0o755)
    return server


def server_runs(server: Path) -> bool:
    """`llama-server --version` exits 0 when the build loads on this machine (drivers, CPU features)."""
    env = dict(os.environ)
    env["LD_LIBRARY_PATH"] = os.pathsep.join(filter(None, [str(server.parent), env.get("LD_LIBRARY_PATH")]))
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    try:
        return subprocess.run([str(server), "--version"], capture_output=True, timeout=60, env=env,
                              creationflags=flags).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --- 1-4 together

def run(paths: AppPaths, *, speakers: bool = True, downloads: bool = True,
        detect=hardware.detect, packages=install_packages, speech=fetch_speech_models, gguf=fetch_gguf,
        llama=install_llama, runs=server_runs) -> dict | None:
    """Detect again every time: a new GPU or more RAM gives a new tier on the next setup."""
    profile_file = paths.hardware
    hw = detect()
    profile = dataclasses.replace(hardware.plan(hw), model=hardware.SMALL)
    print(hardware.summary(hw, profile), flush=True)

    env = profile.env(paths.models)
    hardware.save(profile_file, hw, profile, env)  # saved early: a failed download still leaves a profile
    packages(profile, speakers)
    if not downloads:
        return hardware.load(profile_file)

    speech(paths)
    gguf(profile.model, paths.models)
    backend = profile.llama_backend
    server = llama(paths, hw, backend)
    if backend != "cpu" and not runs(server):
        say(f"llama.cpp pentru {backend} nu pornește pe acest calculator; folosesc varianta pentru procesor.")
        backend = "cpu"
        server = llama(paths, hw, backend)
        env["LLAMACPP_GPU_LAYERS"] = "0"
    env["LLAMA_SERVER"] = str(server)
    hardware.save(profile_file, hw, dataclasses.replace(profile, llama_backend=backend), env)
    say("Configurarea e gata.")
    return hardware.load(profile_file)
