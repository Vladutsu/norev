"""Where norev keeps its data, key, models and logs on this machine (#48).

Everything lives in the User's profile, never in the app folder:

  %LOCALAPPDATA%\\norev\\norev.db          encrypted store (same default as the store itself, #26)
  %LOCALAPPDATA%\\norev\\keys\\norev.key   store key, in its own folder
  %LOCALAPPDATA%\\norev\\models\\          GGUF models; Whisper and diarization under models\\hf
  %LOCALAPPDATA%\\norev\\llama.cpp\\       llama-server.exe and its DLLs
  %LOCALAPPDATA%\\norev\\logs\\            llama-server.log

NOREV_HOME moves all of it; every single path can still be set by its own variable.
On Linux (the dev container) the root is $XDG_DATA_HOME/norev or ~/.local/share/norev.
"""
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping

DEFAULT_GGUF = "Qwen3-8B-Q4_K_M.gguf"


@dataclass(frozen=True)
class AppPaths:
    root: Path
    windows: bool

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None, windows: bool | None = None) -> "AppPaths":
        env = os.environ if env is None else env
        windows = sys.platform == "win32" if windows is None else windows
        if env.get("NOREV_HOME"):
            root = Path(env["NOREV_HOME"])
        elif env.get("LOCALAPPDATA"):
            root = Path(env["LOCALAPPDATA"]) / "norev"
        else:
            root = Path(env.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "norev"
        return cls(root=root, windows=windows)

    @property
    def store(self) -> Path:
        return self.root / "norev.db"

    @property
    def key(self) -> Path:
        return self.root / "keys" / "norev.key"

    @property
    def models(self) -> Path:
        return self.root / "models"

    @property
    def hf_home(self) -> Path:
        return self.models / "hf"

    @property
    def gguf(self) -> Path:
        return self.models / DEFAULT_GGUF

    @property
    def llama_server(self) -> Path:
        return self.root / "llama.cpp" / ("llama-server.exe" if self.windows else "llama-server")

    @property
    def logs(self) -> Path:
        return self.root / "logs"


def apply_defaults(paths: AppPaths, env: MutableMapping[str, str], *, offline: bool = True) -> None:
    """Fill the variables the rest of norev reads, without overriding what the User set.

    offline=True keeps Hugging Face from downloading anything; only `setup-models` turns it off.
    The key variable is set only when the key exists, so a missing key is reported, not opened.
    """
    env.setdefault("HF_HOME", str(paths.hf_home))
    if offline:
        env.setdefault("HF_HUB_OFFLINE", "1")
    env.setdefault("NOREV_STORE_PATH", str(paths.store))
    if paths.key.is_file():
        env.setdefault("NOREV_STORE_KEY_FILE", str(paths.key))
    env.setdefault("LLAMA_SERVER", str(paths.llama_server))
    env.setdefault("NOREV_EXTRACT_MODEL", env.get("LLAMACPP_MODEL") or str(paths.gguf))


def cuda_dll_dirs(site_packages: list[Path]) -> list[Path]:
    """The bin folders of the nvidia-* wheels (cuBLAS, cuDNN): on Windows they must be on the DLL path."""
    found = []
    for base in site_packages:
        nvidia = base / "nvidia"
        if nvidia.is_dir():
            found.extend(sorted(p for p in nvidia.glob("*/bin") if p.is_dir()))
    return found
