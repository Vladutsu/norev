"""llama-server for one analysis: start, wait until healthy, stop (#48).

The Python version of the lifecycle in norev.sh, so the same flow runs on Windows. Only
127.0.0.1 is ever contacted. A server already answering on the port is reused and left running.
"""
import dataclasses
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger(__name__)


class LlamaError(RuntimeError):
    """llama-server is missing, the model is missing, or it did not start; the message says which."""


def healthy(url: str) -> bool:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
    try:
        with opener.open(f"{url}/health", timeout=1) as response:
            return response.status == 200
    except Exception:
        return False


@dataclass
class LlamaServer:
    binary: Path
    model: Path
    log_file: Path
    port: int = 8080
    ctx: int = 4096
    parallel: int = 4
    gpu_layers: int = 99
    timeout: float = 120
    popen: Callable = subprocess.Popen
    health: Callable[[str], bool] = healthy
    sleep: Callable[[float], None] = time.sleep

    def __post_init__(self) -> None:
        self._process = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def external(self) -> bool:
        """After __enter__: True when a server already answering on the port was reused."""
        return self._process is None

    @property
    def alias(self) -> str:
        return Path(self.model).stem

    def args(self) -> list[str]:
        """Same flags as norev.sh: every slot gets `ctx` tokens; Qwen3 answers without "thinking"."""
        args = [str(self.binary), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
                "-c", str(self.ctx * self.parallel), "--parallel", str(self.parallel), "-ngl", str(self.gpu_layers),
                "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--alias", self.alias]
        if "Qwen3" in Path(self.model).name:
            args += ["--reasoning", "off"]
        return args

    def __enter__(self) -> "LlamaServer":
        if self.health(self.url):
            return self  # already running: reuse it, and leave it running
        if not Path(self.binary).is_file():
            raise LlamaError(f"llama-server nu există la {self.binary}")
        if not Path(self.model).is_file():
            raise LlamaError(f"modelul GGUF nu există la {self.model}")
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        log = open(self.log_file, "ab")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
        try:
            self._process = self.popen(self.args(), stdout=log, stderr=subprocess.STDOUT, creationflags=flags)
        finally:
            log.close()
        waited = 0.0
        while waited < self.timeout:
            if self.health(self.url):
                return self
            if self._process.poll() is not None:
                self._process = None
                raise LlamaError(f"llama-server s-a oprit la pornire; vezi {self.log_file}")
            self.sleep(1)
            waited += 1
        self.stop()
        raise LlamaError(f"llama-server nu a pornit în {int(self.timeout)} s; vezi {self.log_file}")

    def __exit__(self, *exc) -> None:
        self.stop()

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait()
        self._process = None


class ModelMissing(LlamaError):
    """The GGUF model a stage asks for is not on this machine; there is no fallback to another one."""


class LlamaModels:
    """One llama-server at a time, started with the model the call names (#62).

    Each stage names its model (translation, extraction, the Domain's `[model]`); `load` stops the
    running server first when another model is asked for, so two models never share the GPU. A server
    started outside norev is reused only while every call asks for the model it was found with.
    """

    def __init__(self, template: LlamaServer, models_dir: Path) -> None:
        # shortcut: every model gets LLAMACPP_CTX per slot and domain.toml `context` is not used yet; decide per model ctx after the 6 GB measurements (#62)
        self.template = template  # binary, port, ctx, parallel, gpu layers: the same for every model
        self.models_dir = Path(models_dir)
        self._server: LlamaServer | None = None
        self._lock = threading.Lock()  # parallel calls for the same model start it once

    def resolve(self, name: str | None) -> Path:
        """A path as given, else a file under models_dir by name (with or without .gguf)."""
        if not name:
            raise ModelMissing("niciun model GGUF cerut pentru această etapă")
        path = Path(name)
        if path.is_file():
            return path.resolve()
        file = path.name if path.suffix == ".gguf" else f"{path.name}.gguf"
        found = sorted(self.models_dir.rglob(file)) if self.models_dir.is_dir() else []
        if len(found) > 1:
            raise ModelMissing(f"modelul {file} apare de mai multe ori în {self.models_dir}: {', '.join(map(str, found))}")
        if not found:
            raise ModelMissing(f"modelul GGUF {file} nu există (căutat ca {path} și în {self.models_dir})")
        return found[0].resolve()

    def load(self, name: str | None) -> str:
        """Start llama-server with this model unless it is already running; return its alias."""
        model = self.resolve(name)
        with self._lock:
            if self._server is not None and self._server.model == model:
                return self._server.alias
            if self._server is not None and self._server.external:
                raise LlamaError(f"un llama-server pornit din afara norev rulează pe {self._server.url}, iar "
                                 f"etapa aceasta cere alt model ({model.name}); oprește-l")
            self.stop()
            server = dataclasses.replace(self.template, model=model)
            started = time.monotonic()
            server.__enter__()
            self._server = server
            log.info("llama-server %s %s în %.1f s", server.alias,
                     "refolosit" if server.external else "pornit", time.monotonic() - started)
            return server.alias

    def stop(self) -> None:
        if self._server is not None:
            self._server.stop()
            self._server = None


def server_from_environment(log_file: Path, env=None) -> LlamaServer:
    """The server norev.sh would start, from the variables apply_defaults filled (README.md)."""
    env = os.environ if env is None else env
    return LlamaServer(
        binary=Path(env["LLAMA_SERVER"]),
        model=Path(env["NOREV_EXTRACT_MODEL"]),
        log_file=log_file,
        port=int(env.get("LLAMACPP_PORT", "8080")),
        ctx=int(env.get("LLAMACPP_CTX", "4096")),
        parallel=int(env.get("LLM_PARALLEL", "4")),
        gpu_layers=int(env.get("LLAMACPP_GPU_LAYERS", "99")),
    )


def models_from_environment(log_file: Path, env=None) -> LlamaModels:
    """llama-server for each stage's model (#62): same flags as norev.sh, bare names under NOREV_MODELS_DIR."""
    env = os.environ if env is None else env
    return LlamaModels(server_from_environment(log_file, env), Path(env["NOREV_MODELS_DIR"]))
