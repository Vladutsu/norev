"""Ephemeral, local-only recording feedback; never used for the final Transcript."""
import multiprocessing
import queue

# About five seconds per chunk; a bounded queue keeps memory flat when the model is slower than speech.
_CHUNK_BYTES = 5 * 16000 * 2
_UNAVAILABLE = "Transcrierea live nu este disponibilă; înregistrarea continuă."


def _whisper(language):
    """The live Transcriber: the app's Whisper model and device (WHISPER_DEVICE, the GPU by default).
    It runs in a child process that Stop ends, so its VRAM and CUDA context are free before the final Transcript.
    Not enough VRAM or no model: the worker reports None and the Recording screen says live is unavailable."""
    import numpy as np
    from faster_whisper import WhisperModel
    from LLM.Whisper.settings import WhisperSettings

    settings = WhisperSettings.from_environment()
    model = WhisperModel(settings.model_name, device=settings.model_device, compute_type=settings.model_compute_type,
                         cpu_threads=4, local_files_only=True)

    def transcribe(data):
        samples = np.frombuffer(data, dtype='<i2').astype(np.float32) / 32768.0
        segments, _ = model.transcribe(samples, language=None if language in ('', 'auto') else language,
                                       beam_size=1, vad_filter=True, condition_on_previous_text=False)
        return ' '.join(s.text.strip() for s in segments if s.text.strip())
    return transcribe


def _worker(audio, results, language, make_transcriber=_whisper):
    """Child process: PCM chunks in, text out, until None. `make_transcriber(language)` -> (bytes) -> str."""
    try:
        transcribe = make_transcriber(language)
        while True:
            data = audio.get()
            if data is None:
                return
            text = transcribe(data)
            if text:
                results.put(text)
    except Exception:
        results.put(None)  # Never log Session data or the model's exception message.


class LiveFeedback:
    def __init__(self, language):
        self.language = language
        self.pending = bytearray()
        self.segments = []
        self.process = None
        self.audio = self.results = None
        self.failed = False

    def feed(self, block):
        if self.failed or not block:
            return
        self.pending.extend(block)
        while len(self.pending) >= _CHUNK_BYTES:
            chunk = bytes(self.pending[:_CHUNK_BYTES])
            del self.pending[:_CHUNK_BYTES]
            if self.process is None:
                try:
                    ctx = multiprocessing.get_context('spawn')
                    self.audio, self.results = ctx.Queue(maxsize=1), ctx.Queue()
                    process = ctx.Process(target=_worker, args=(self.audio, self.results, self.language), daemon=True)
                    process.start()
                    self.process = process
                except Exception:
                    self.failed = True
                    for q in (self.audio, self.results):
                        if q is not None:
                            q.cancel_join_thread()
                            q.close()
                    self.audio = self.results = None
                    return
            try:
                self.audio.put_nowait(chunk)
            except queue.Full:
                pass  # Feedback may skip audio when the model is slower than the recording.
            except (OSError, ValueError):
                self.failed = True
                return

    def read(self):
        if self.results is not None:
            while True:
                try:
                    text = self.results.get_nowait()
                except queue.Empty:
                    break
                if text is None:
                    self.failed = True
                else:
                    self.segments.append({'speaker_id': None, 'text': text})
        if self.process is not None and self.process.exitcode is not None:
            self.failed = True
        return [*self.segments, *([{'speaker_id': None, 'text': _UNAVAILABLE}] if self.failed else [])]

    def close(self):
        """Ends the live model before returning, so the final Transcript gets the whole GPU. Its text is not needed."""
        self.pending.clear()
        if self.process is None:
            return
        self.process.terminate()
        self.process.join(timeout=5)
        if self.process.is_alive():
            self.process.kill()
            self.process.join()
        self.process.close()
        for q in (self.audio, self.results):
            if q is not None:
                q.cancel_join_thread()
                q.close()


def monitor(blocks, live):
    try:
        for block in blocks:
            live.feed(block)
            yield block
    finally:
        close = getattr(blocks, 'close', None)
        if close is not None:
            close()
