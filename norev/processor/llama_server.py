"""llama-server for one analysis, as norev.sh does it: started after Whisper and diarization freed the
GPU, stopped right after, so the two never hold VRAM together (#22, #43).

Same environment variables and server options as norev.sh (README.md). A server already answering
at LLAMACPP_URL is used as it is and left running.
"""
import os
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from LLM.AI_Model.client import llamacpp_base_url


class LlamaServerError(RuntimeError):
    """llama-server is missing, exited or never became ready. The message names files, never Session data."""


@dataclass(frozen=True)
class LlamaServerSettings:
    binary: Path
    model: Path
    url: str
    port: int
    ctx: int
    parallel: int
    gpu_layers: int
    log_file: Path
    ready_timeout: float = 120.0

    @classmethod
    def from_environment(cls, env: Mapping[str, str] | None = None) -> "LlamaServerSettings":
        env = os.environ if env is None else env
        home = Path.home()
        port = int(env.get("LLAMACPP_PORT", "8080"))
        model = env.get("NOREV_EXTRACT_MODEL") or env.get("LLAMACPP_MODEL") or str(home / "models" / "qwen3-8b" / "Qwen3-8B-Q4_K_M.gguf")
        return cls(
            binary=Path(env.get("LLAMA_SERVER") or home / "Repositories" / "llama.cpp" / "build" / "bin" / "llama-server"),
            model=Path(model),
            url=llamacpp_base_url(env.get("LLAMACPP_URL") or f"http://127.0.0.1:{port}"),
            port=port,
            ctx=int(env.get("LLAMACPP_CTX", "4096")),
            parallel=max(1, int(env.get("LLM_PARALLEL", "1"))),
            gpu_layers=int(env.get("LLAMACPP_GPU_LAYERS", "99")),
            log_file=Path(env.get("NOREV_LLAMA_LOG") or home / ".norev" / "llama-server.log"),
        )

    @property
    def model_name(self) -> str:
        return self.model.stem

    def command(self) -> list[str]:
        cmd = [str(self.binary), "-m", str(self.model), "--host", "127.0.0.1", "--port", str(self.port),
               "-c", str(self.ctx * self.parallel), "--parallel", str(self.parallel), "-ngl", str(self.gpu_layers),
               "-fa", "on", "-ctk", "q8_0", "-ctv", "q8_0", "--alias", self.model_name]
        if "Qwen3" in self.model.name:  # Qwen3 "thinks" before answering by default, which only slows the stages
            cmd += ["--reasoning", "off"]
        return cmd


def healthy(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=timeout) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


@contextmanager
def llama_server(settings: LlamaServerSettings, *, is_healthy: Callable[[str], bool] = healthy,
                 popen=subprocess.Popen, sleep=time.sleep) -> Iterator[str]:
    """Yields the model alias to call; the server stops when the block ends, also on error."""
    if is_healthy(settings.url):
        yield os.environ.get("LLM_MODEL") or settings.model_name
        return
    if not settings.binary.is_file():
        raise LlamaServerError(f"llama-server not found at {settings.binary}; set LLAMA_SERVER")
    if not settings.model.is_file():
        raise LlamaServerError(f"GGUF model not found at {settings.model}; set LLAMACPP_MODEL")
    settings.log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(settings.log_file, "ab") as log:
        process = popen(settings.command(), stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + settings.ready_timeout
        while not is_healthy(settings.url):
            if process.poll() is not None:
                raise LlamaServerError(f"llama-server exited during startup; see {settings.log_file}")
            if time.monotonic() > deadline:
                raise LlamaServerError(f"llama-server did not become ready; see {settings.log_file}")
            sleep(1)
        yield settings.model_name
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
