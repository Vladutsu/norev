"""llama-server for one analysis: start, wait until healthy, stop (#48).

The Python version of the lifecycle in norev.sh, so the same flow runs on Windows. Only
127.0.0.1 is ever contacted. A server already answering on the port is reused and left running.
"""
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


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
