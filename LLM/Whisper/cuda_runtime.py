import os
import threading
from ctypes import CDLL, RTLD_GLOBAL
from pathlib import Path
from site import getsitepackages, getusersitepackages


class CudaRuntimeLoader:
    def __init__(self) -> None:
        self._preload_lock = threading.RLock()
        self._cuda_runtime_preloaded = False

    def preload(self) -> None:
        if self._cuda_runtime_preloaded:
            return

        with self._preload_lock:
            if self._cuda_runtime_preloaded:
                return

            lib_dirs = self._candidate_cuda_lib_dirs()
            if not lib_dirs:
                return

            self._prepend_ld_library_path(lib_dirs)

            libs = (
                "libcublas.so.12",
                "libcublasLt.so.12",
                "libcudnn.so.9",
                "libcudnn_ops.so.9",
            )

            for lib_name in libs:
                for lib_dir in lib_dirs:
                    lib_path = lib_dir / lib_name
                    if not lib_path.exists():
                        continue
                    CDLL(str(lib_path), mode=RTLD_GLOBAL)
                    break

            self._cuda_runtime_preloaded = True

    @staticmethod
    def _candidate_cuda_lib_dirs() -> list[Path]:
        dirs: list[Path] = []

        all_site_dirs = list(getsitepackages())
        try:
            all_site_dirs.append(getusersitepackages())
        except Exception:
            pass

        relative_dirs = (
            ("nvidia", "cublas", "lib"),
            ("nvidia", "cudnn", "lib"),
            ("nvidia", "cuda_nvrtc", "lib"),
        )

        for site_dir in all_site_dirs:
            base = Path(site_dir)
            for rel in relative_dirs:
                candidate = base.joinpath(*rel)
                if candidate.is_dir():
                    dirs.append(candidate)

        unique_dirs: list[Path] = []
        seen = set()
        for item in dirs:
            resolved = str(item.resolve())
            if resolved in seen:
                continue
            seen.add(resolved)
            unique_dirs.append(item)

        return unique_dirs

    @staticmethod
    def _prepend_ld_library_path(paths: list[Path]) -> None:
        existing = os.environ.get("LD_LIBRARY_PATH", "")
        existing_parts = [p for p in existing.split(":") if p]
        merged = [str(p) for p in paths] + existing_parts

        ordered: list[str] = []
        seen = set()
        for item in merged:
            if item in seen:
                continue
            seen.add(item)
            ordered.append(item)

        os.environ["LD_LIBRARY_PATH"] = ":".join(ordered)
