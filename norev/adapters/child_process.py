"""A GPU adapter (Transcriber or Diarizer) run in a child process (#65).

`close()` frees a model, but the CUDA context of CTranslate2 or torch stays until its process ends:
0.6-1.1 GB that llama-server then lacks on a 6 GB card. Here the Central Processor's `close()` (called
when another model needs the GPU) ends the child, so everything it held on the GPU goes with it.

The parent reads the Session audio from the store and sends the bytes through a pipe: the child gets no
store or key, and the audio is never written to a file (ADR 0004).
"""
import multiprocessing
from collections.abc import Callable
from typing import Any


class _Audio:
    """The one-method store the adapter sees in the child: the audio of the current call."""

    audio = b""

    def load_audio(self, audio_ref: str) -> bytes:
        return self.audio


def _serve(conn, make_adapter) -> None:
    store = _Audio()
    adapter = make_adapter(store)
    while True:
        message = conn.recv()
        if message is None:
            return
        method, audio, args = message
        store.audio = audio
        try:
            conn.send((True, getattr(adapter, method)("audio", *args)))
        except Exception as e:  # sent back as it is: the parent handles it as if the adapter ran there
            try:
                conn.send((False, e))
            except Exception:  # not picklable: its class name only
                conn.send((False, RuntimeError(type(e).__name__)))


class ChildProcessAdapter:
    """`make_adapter(store) -> adapter` must be a module-level function (the child imports it).
    The child starts on the first call and stays until `close()`."""

    def __init__(self, store, make_adapter: Callable[[Any], Any]) -> None:
        self._store = store
        self._make_adapter = make_adapter
        self._process = None
        self._conn = None

    def transcribe(self, audio_ref: str, language: str | None = None):
        return self._call("transcribe", audio_ref, language)

    def diarize(self, audio_ref: str):
        return self._call("diarize", audio_ref)

    def close(self) -> None:
        """Ends the child, so its GPU memory and CUDA context are free before the next model loads."""
        if self._process is None:
            return
        process, conn, self._process, self._conn = self._process, self._conn, None, None
        try:
            conn.send(None)
        except OSError:
            pass
        process.join(timeout=10)
        if process.is_alive():
            process.kill()
            process.join()
        conn.close()
        process.close()

    def _call(self, method: str, audio_ref: str, *args):
        audio = self._store.load_audio(audio_ref)
        if self._process is None:
            ctx = multiprocessing.get_context("spawn")  # a fresh process: no CUDA state from the parent
            self._conn, child = ctx.Pipe()
            self._process = ctx.Process(target=_serve, args=(child, self._make_adapter), daemon=True)
            self._process.start()
            child.close()
        try:
            self._conn.send((method, audio, args))
            ok, result = self._conn.recv()
        except (EOFError, OSError):  # the child died (a driver crash, killed): the next call starts a new one
            self.close()
            raise RuntimeError(f"the {method} process ended") from None
        if not ok:
            raise result
        return result


def whisper(store):
    from norev.adapters.whisper.transcriber import WhisperTranscriber

    return WhisperTranscriber(store)


def sortformer(store):
    from norev.adapters.diarization.sortformer import SortformerDiarizer

    return SortformerDiarizer(store)
