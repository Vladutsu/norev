"""Hardware tier: what this laptop can run, decided once at install and saved next to the store.

`detect()` asks the machine (RAM, GPU vendor, VRAM); `plan()` turns that into the settings the rest
of norev already reads (WHISPER_*, LLAMACPP_*, LLM_PARALLEL, DIARIZATION_DEVICE, the GGUF models).
`plan()` is pure, so every tier is tested without the hardware. Nothing here opens a network
connection. Variables the User sets in the environment always win over the saved profile.

The tiers keep the accuracy of the reference setup (Qwen3-8B Q4_K_M, Whisper large-v3-turbo, beam 5)
wherever the memory allows it; only machines that cannot hold the 8B model get the 4B one.
"""
import json
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

GB = 1024 ** 3


@dataclass(frozen=True)
class Model:
    repo: str
    file: str
    size: int
    sha256: str


# Reference model: best in the test Sessions (README). Pinned by content hash.
LARGE = Model("Qwen/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", 5027783488,
              "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785")
# Only for machines that cannot hold LARGE: same family, so the prompts behave the closest.
# shortcut: not measured on the Romanian test Sessions yet; measure before relying on it (#25)
SMALL = Model("unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2497281120,
              "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597")

# Memory thresholds in GB, as the OS reports them (a 16 GB laptop reports ~15.3, a 12 GB one ~11.7)
LARGE_MIN_RAM = 11.5           # 8B (5 GB) + KV cache + OS + the app window, on RAM alone
LARGE_MIN_RAM_PLUS_VRAM = 13   # e.g. 8 GB RAM + a 6 GB GPU: llama.cpp puts what fits on the GPU
DEDICATED_MIN_VRAM = 2         # below this a "GPU" is integrated (shared memory carve-out)
# GPUs report a bit less than their nominal size (an 8 GB card: 8188 MiB = 7.996 GB), so the
# thresholds sit just under 8 / 6 / 4 GB
FAST_GPU_VRAM = 7.5            # 8B fully on the GPU with 4 parallel slots (measured 5.9 GB, README)
WHISPER_FP16_VRAM = 5.5
WHISPER_INT8_VRAM = 3.5


@dataclass(frozen=True)
class Gpu:
    vendor: str                 # nvidia | amd | intel | apple | other
    name: str
    vram_gb: float              # dedicated memory; 0 when unknown

    @property
    def integrated(self) -> bool:
        return self.vendor == "apple" or (self.vendor != "nvidia" and self.vram_gb < DEDICATED_MIN_VRAM)


@dataclass(frozen=True)
class Hardware:
    system: str                 # windows | linux | darwin
    arch: str                   # x64 | arm64
    ram_gb: float
    gpus: tuple[Gpu, ...] = ()

    @property
    def gpu(self) -> Gpu | None:
        """The GPU norev uses: NVIDIA first (CUDA), then the one with the most dedicated memory."""
        if not self.gpus:
            return None
        return max(self.gpus, key=lambda g: (g.vendor == "nvidia", not g.integrated, g.vram_gb))


@dataclass(frozen=True)
class Profile:
    tier: str                   # gpu | gpu-small | integrated | cpu  (shown to the User by doctor)
    model: Model
    llama_backend: str          # cuda | vulkan | metal | cpu: which llama.cpp build to download
    gpu_layers: str             # "auto": llama.cpp fits the layers to free VRAM (--fit); "0": CPU only
    parallel: int
    whisper_device: str
    whisper_compute_type: str
    diarization_device: str
    cuda_wheels: bool           # nvidia-cublas/cudnn for Whisper on the GPU (requirements-cuda.txt)
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def env(self, models_dir: Path) -> dict[str, str]:
        gguf = str(Path(models_dir) / self.model.file)
        return {
            "LLAMACPP_MODEL": gguf,
            "NOREV_EXTRACT_MODEL": gguf,
            "NOREV_TRANSLATE_MODEL": gguf,
            "NOREV_DOMAIN_MODEL": self.model.file,  # the Corporate Domain stage uses the same model: no reload
            "LLAMACPP_GPU_LAYERS": self.gpu_layers,
            "LLAMACPP_CTX": "4096",
            "LLM_PARALLEL": str(self.parallel),
            "WHISPER_MODEL": "large-v3-turbo",  # every tier: smaller Whisper models lose Romanian words
            "WHISPER_BEAM_SIZE": "5",           # #66: beam 1 lost words a Domain needs
            "WHISPER_DEVICE": self.whisper_device,
            "WHISPER_COMPUTE_TYPE": self.whisper_compute_type,
            "DIARIZATION_DEVICE": self.diarization_device,
        }


def plan(hw: Hardware) -> Profile:
    gpu = hw.gpu
    nvidia = gpu is not None and gpu.vendor == "nvidia"
    dedicated = gpu.vram_gb if gpu is not None and not gpu.integrated else 0.0
    nvidia_vram = gpu.vram_gb if gpu is not None and nvidia else 0.0
    reasons = []

    large = hw.ram_gb >= LARGE_MIN_RAM or hw.ram_gb + dedicated >= LARGE_MIN_RAM_PLUS_VRAM
    model = LARGE if large else SMALL
    if not large:
        reasons.append(f"{hw.ram_gb:.1f} GB RAM și {dedicated:.0f} GB VRAM nu ajung pentru Qwen3-8B: "
                       f"folosesc Qwen3-4B (mai puțin precis)")

    if nvidia:
        backend = "cuda"
    elif hw.system == "darwin":
        backend = "metal"
    elif gpu is not None:
        backend = "vulkan"  # Intel and AMD, integrated or dedicated; falls back to CPU if it does not start
    else:
        backend = "cpu"
    if backend == "cuda" and hw.arch != "x64":
        backend = "vulkan"  # no CUDA build of llama.cpp for this architecture among the pinned ones

    fully_on_gpu = dedicated >= FAST_GPU_VRAM or (model is SMALL and dedicated >= WHISPER_FP16_VRAM)
    if fully_on_gpu:
        tier, parallel = "gpu", 4
    elif dedicated >= WHISPER_INT8_VRAM:
        tier, parallel = "gpu-small", 1  # README: at the limit on 6 GB, one slot
    elif gpu is not None:
        tier, parallel = "integrated", 2 if large else 1
    else:
        tier, parallel = "cpu", 2 if large else 1
    gpu_layers = "0" if backend == "cpu" else "auto"

    if nvidia_vram >= WHISPER_FP16_VRAM:
        whisper = ("cuda", "float16")
    elif nvidia_vram >= WHISPER_INT8_VRAM:
        whisper = ("cuda", "int8_float16")  # same accuracy in practice, half the VRAM
    else:
        whisper = ("cpu", "int8")
        reasons.append("Whisper rulează pe procesor (int8): aceeași precizie, mai lent")
    diarization = "cuda" if nvidia_vram >= WHISPER_INT8_VRAM else "cpu"

    return Profile(tier=tier, model=model, llama_backend=backend, gpu_layers=gpu_layers, parallel=parallel,
                   whisper_device=whisper[0], whisper_compute_type=whisper[1], diarization_device=diarization,
                   cuda_wheels=whisper[0] == "cuda" or diarization == "cuda", reasons=tuple(reasons))


# --- detection (best effort: a probe that fails means "not found", never a crash)

def _run(args: list[str], timeout: float = 15) -> str:
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=flags)
        return result.stdout if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _system() -> str:
    return {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux")


def _arch() -> str:
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x64"


def _ram_gb(system: str) -> float:
    if system == "windows":
        import ctypes

        class MemoryStatus(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

        status = MemoryStatus()
        status.dwLength = ctypes.sizeof(MemoryStatus)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))  # type: ignore[attr-defined]
        return status.ullTotalPhys / GB
    if system == "darwin":
        out = _run(["sysctl", "-n", "hw.memsize"]).strip()
        return int(out) / GB if out.isdigit() else 0.0
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024 / GB
    except OSError:
        pass
    return 0.0


def _vendor(name: str) -> str:
    n = name.lower()
    for vendor, words in (("nvidia", ("nvidia", "geforce", "quadro", "rtx")), ("amd", ("amd", "radeon")),
                          ("intel", ("intel",)), ("apple", ("apple",))):
        if any(w in n for w in words):
            return vendor
    return "other"


def parse_nvidia_smi(out: str) -> list[Gpu]:
    """`nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits` -> GPUs (VRAM in MiB)."""
    gpus = []
    for line in out.splitlines():
        name, _, mib = line.rpartition(",")
        if name.strip() and mib.strip().isdigit():
            gpus.append(Gpu("nvidia", name.strip(), int(mib) / 1024))
    return gpus


def parse_windows_adapters(out: str) -> list[Gpu]:
    """Lines `<DriverDesc>|<HardwareInformation.qwMemorySize in bytes>` from the display adapter registry keys."""
    gpus = []
    for line in out.splitlines():
        name, _, size = line.strip().partition("|")
        if name and _vendor(name) != "nvidia":  # NVIDIA comes from nvidia-smi, with the exact VRAM
            gpus.append(Gpu(_vendor(name), name, int(size) / GB if size.strip().isdigit() else 0.0))
    return gpus


WINDOWS_ADAPTERS = (
    "Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}\\0*' "
    "-ErrorAction SilentlyContinue | ForEach-Object { \"$($_.DriverDesc)|$($_.'HardwareInformation.qwMemorySize')\" }"
)
PCI_VENDORS = {"0x10de": "nvidia", "0x1002": "amd", "0x8086": "intel"}


def _linux_gpus() -> list[Gpu]:
    gpus = []
    for card in sorted(Path("/sys/class/drm").glob("card[0-9]")):
        try:
            vendor = PCI_VENDORS.get((card / "device" / "vendor").read_text().strip(), "other")
        except OSError:
            continue
        if vendor == "nvidia":
            continue  # from nvidia-smi
        vram = card / "device" / "mem_info_vram_total"  # amdgpu only
        try:
            size = int(vram.read_text()) / GB if vram.is_file() else 0.0
        except (OSError, ValueError):
            size = 0.0
        gpus.append(Gpu(vendor, card.name, size))
    return gpus


def detect() -> Hardware:
    system = _system()
    gpus: list[Gpu] = []
    smi = shutil.which("nvidia-smi")
    if smi:
        gpus += parse_nvidia_smi(_run([smi, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"]))
    if system == "windows":
        gpus += parse_windows_adapters(_run(["powershell", "-NoProfile", "-Command", WINDOWS_ADAPTERS]))
    elif system == "linux":
        gpus += _linux_gpus()
    elif system == "darwin" and _arch() == "arm64":
        gpus.append(Gpu("apple", "Apple Silicon", 0.0))
    return Hardware(system=system, arch=_arch(), ram_gb=round(_ram_gb(system), 1), gpus=tuple(gpus))


# --- the saved profile (NOREV_HOME/hardware.json)

def save(path: Path, hw: Hardware, profile: Profile, env: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"format": 1, "hardware": asdict(hw), "tier": profile.tier, "model": profile.model.file,
            "llama_backend": profile.llama_backend, "reasons": list(profile.reasons), "env": env}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if data.get("format") == 1 and isinstance(data.get("env"), dict) else None


def summary(hw: Hardware, profile: Profile) -> str:
    gpu = hw.gpu
    gpu_text = "fără placă video" if gpu is None else (
        f"{gpu.name} ({'integrată' if gpu.integrated else f'{gpu.vram_gb:.0f} GB VRAM'})")
    lines = [f"Calculator: {hw.ram_gb:.1f} GB RAM, {gpu_text}",
             f"Nivel: {profile.tier} | LLM: {profile.model.file} pe {profile.llama_backend} "
             f"({profile.parallel} în paralel) | Whisper large-v3-turbo pe {profile.whisper_device} "
             f"({profile.whisper_compute_type})"]
    lines += [f"  - {r}" for r in profile.reasons]
    return "\n".join(lines)


def _env_name(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", name))


def apply(path: Path, env) -> dict | None:
    """Fill the saved profile's variables into env, never over what the User set."""
    data = load(path)
    if data:
        for name, value in data["env"].items():
            if _env_name(name) and isinstance(value, str):
                env.setdefault(name, value)
    return data


if __name__ == "__main__":  # python -m norev.launcher.hardware: show what would be picked, change nothing
    found = detect()
    print(summary(found, plan(found)))
    print(json.dumps(asdict(found), indent=2))
