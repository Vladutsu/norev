"""Records Session audio from an input device (microphone) to a 16 kHz mono WAV file.

Capture uses the FFmpeg devices bundled with PyAV, which is already installed for faster-whisper,
so no dependency is added: DirectShow on Windows, ALSA on Linux. PyAV is imported inside the
functions that need it, so this module imports on a machine (or CI) without audio devices.
The PyAV Linux wheel has ALSA but no PulseAudio, so record on the host OS, not in WSL or a container.
"""
import re
import sys
import threading
import wave
from collections.abc import Iterable, Iterator
from pathlib import Path

SAMPLE_RATE = 16000  # Whisper's working rate; one hour is about 115 MB
SAMPLE_WIDTH = 2  # 16-bit PCM
CHANNELS = 1

# FFmpeg input format per platform
_INPUT_FORMATS = {"win32": "dshow", "linux": "alsa"}


class Recorder:
    """Records one Session to a WAV file.

        recorder = Recorder(Path("session.wav"), device="Microphone Array (Realtek(R) Audio)")
        seconds = recorder.record()  # blocks until recorder.stop() (from another thread) or Ctrl+C

    `source` replaces the device with any iterable of 16 kHz mono s16 PCM blocks (tests, fakes).
    """

    def __init__(
        self, wav_path: Path, device: str | None = None, source: Iterable[bytes] | None = None
    ) -> None:
        self.wav_path = Path(wav_path)
        self._device = device
        self._source = source
        self._stop = threading.Event()

    def stop(self) -> None:
        """Ends record() after the block being captured (at most about half a second)."""
        self._stop.set()

    def record(self) -> float:
        """Records until stop(), Ctrl+C or the end of the source. Returns the duration in seconds."""
        source = self._source if self._source is not None else capture(self._device or default_device())
        try:
            return write_wav(source, self.wav_path, self._stop)
        finally:
            close = getattr(source, "close", None)
            if close is not None:
                close()


def write_wav(blocks: Iterable[bytes], wav_path: Path, stop: threading.Event) -> float:
    """Writes PCM blocks to a 16 kHz mono 16-bit WAV until `stop` is set, the blocks end or Ctrl+C.
    Returns the duration in seconds.

    `wave` rewrites the header after every block, so the file stays valid if the process is killed
    (e.g. Ctrl+C in Git Bash's mintty terminates the process instead of raising KeyboardInterrupt).
    """
    # shortcut: plain WAV on disk; ADR 0004 encrypts Session audio at rest once the SessionStore exists
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(CHANNELS)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        try:
            for block in blocks:
                wav.writeframes(block)
                written += len(block)
                if stop.is_set():
                    break
        except KeyboardInterrupt:
            pass  # Ctrl+C ends the recording; the audio so far is kept
    return written / (SAMPLE_WIDTH * CHANNELS) / SAMPLE_RATE


def input_format() -> str:
    try:
        return _INPUT_FORMATS[sys.platform]
    except KeyError:
        raise RuntimeError(f"Recording is not supported on {sys.platform} (supported: Windows, Linux).") from None


def list_input_devices() -> list[str]:
    """Input device names; --device N picks the N-th."""
    if input_format() == "dshow":
        return parse_dshow_devices(_dshow_device_log())
    try:
        pcm = Path("/proc/asound/pcm").read_text(encoding="utf-8")
    except OSError:
        pcm = ""
    return ["default", *parse_alsa_capture_devices(pcm)]


def default_device() -> str:
    devices = list_input_devices()
    if not devices:
        raise RuntimeError("No input device found.")
    return devices[0]


def resolve_device(spec: str | None) -> str:
    """None: the default device. "N": the N-th of list_input_devices(). Anything else: a device name."""
    if not spec:
        return default_device()
    if not spec.isdigit():
        return spec
    devices = list_input_devices()
    index = int(spec)
    if index >= len(devices):
        raise RuntimeError(f"No input device {index}; there are {len(devices)} (see --list-devices).")
    return devices[index]


def parse_dshow_devices(log_text: str) -> list[str]:
    """Audio device names from FFmpeg's `-f dshow -list_devices true` output."""
    return list(dict.fromkeys(re.findall(r'"([^"]+)"\s*\(audio\)', log_text)))


def parse_alsa_capture_devices(proc_asound_pcm: str) -> list[str]:
    """`plughw:C,D` for each capture PCM in /proc/asound/pcm ("00-00: ... : capture 1").
    plughw, not hw, so ALSA converts to whatever rate and channels the card supports."""
    devices = []
    for line in proc_asound_pcm.splitlines():
        match = re.match(r"(\d+)-(\d+):", line)
        if match and "capture" in line:
            devices.append(f"plughw:{int(match[1])},{int(match[2])}")
    return devices


def _dshow_device_log() -> str:
    import av
    import av.logging

    previous = av.logging.get_level()
    av.logging.set_level(av.logging.INFO)  # PyAV keeps FFmpeg logging off by default
    try:
        with av.logging.Capture() as logs:
            try:
                av.open("dummy", format="dshow", options={"list_devices": "true"})
            except av.FFmpegError:
                pass  # FFmpeg always "fails" after printing the list
    finally:
        av.logging.set_level(previous)
    return "".join(message for _, _, message in logs)


def capture(device: str) -> Iterator[bytes]:
    """Opens the device now (so a wrong name fails before any file is written) and returns an
    iterator of 16 kHz mono s16 PCM blocks, about one per half second, until closed."""
    import av

    fmt = input_format()
    try:
        container = av.open(f"audio={device}" if fmt == "dshow" else device, format=fmt)
    except av.FFmpegError as exc:
        raise RuntimeError(f"Could not open input device {device!r}: {exc}") from exc
    return _pcm_blocks(container)


def _pcm_blocks(container) -> Iterator[bytes]:
    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    try:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                # the plane buffer is padded past the last sample
                yield bytes(out.planes[0])[: out.samples * SAMPLE_WIDTH]
    finally:
        container.close()
