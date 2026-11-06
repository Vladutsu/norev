"""Records Session audio from an input device (microphone) to a 16 kHz mono WAV file.

Uses the FFmpeg capture devices bundled with PyAV (already installed for faster-whisper), so no
extra dependency: DirectShow on Windows, ALSA on Linux. The PyAV Linux wheel has no PulseAudio
support, so under WSL there is usually no capture device: record on the Windows side instead.
"""
import os
import re
import sys
import threading
import wave
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

SAMPLE_RATE = 16000  # Whisper's working rate; one hour is about 115 MB
SAMPLE_WIDTH = 2  # 16-bit PCM

# FFmpeg input format per platform
_INPUT_FORMATS = {"win32": "dshow", "linux": "alsa"}


@dataclass(frozen=True)
class RecorderSettings:
    device: str  # empty: the platform default (first DirectShow microphone, ALSA "default")
    output_dir: Path

    DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[2] / "Outputs" / "Recordings"

    @classmethod
    def from_environment(cls) -> "RecorderSettings":
        return cls(
            device=os.getenv("NOREV_INPUT_DEVICE", "").strip(),
            output_dir=cls.DEFAULT_OUTPUT_DIR,
        )


def input_format() -> str:
    try:
        return _INPUT_FORMATS[sys.platform]
    except KeyError:
        raise RuntimeError(f"Recording is not supported on {sys.platform} (supported: Windows, Linux).") from None


def list_input_devices() -> list[str]:
    """Device names to pass as --device / NOREV_INPUT_DEVICE."""
    if input_format() == "dshow":
        return parse_dshow_devices(_dshow_device_log())
    try:
        pcm = Path("/proc/asound/pcm").read_text(encoding="utf-8")
    except OSError:
        pcm = ""
    return ["default", *parse_alsa_capture_devices(pcm)]


def default_device() -> str:
    if input_format() == "alsa":
        return "default"
    devices = list_input_devices()
    if not devices:
        raise RuntimeError("No microphone found (DirectShow lists no audio input device).")
    return devices[0]


def parse_dshow_devices(log_text: str) -> list[str]:
    """Audio device names from FFmpeg's `-list_devices true -f dshow` output."""
    return list(dict.fromkeys(re.findall(r'"([^"]+)"\s*\(audio\)', log_text)))


def parse_alsa_capture_devices(proc_asound_pcm: str) -> list[str]:
    """`plughw:C,D` for each capture PCM in /proc/asound/pcm ("00-00: ... : capture 1").
    plughw, not hw, so ALSA converts to whatever rate/channels the card supports."""
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
    iterator of 16 kHz mono s16 PCM chunks, roughly one per half second, until closed."""
    import av

    fmt = input_format()
    try:
        container = av.open(f"audio={device}" if fmt == "dshow" else device, format=fmt)
    except av.FFmpegError as exc:
        raise RuntimeError(f"Could not open input device {device!r}: {exc}") from exc
    return _pcm_chunks(container)


def _pcm_chunks(container) -> Iterator[bytes]:
    import av

    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    try:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                # the plane buffer is padded past the last sample
                yield bytes(out.planes[0])[: out.samples * SAMPLE_WIDTH]
    finally:
        container.close()


def record(
    chunks: Iterable[bytes],
    wav_path: Path,
    stop: threading.Event,
    max_seconds: float | None = None,
) -> float:
    """Writes 16 kHz mono s16 PCM chunks to wav_path until `stop` is set, max_seconds is reached,
    the source ends or Ctrl+C. Returns the recorded duration in seconds.

    The WAV header is rewritten after every chunk, so the file stays valid if the process dies.
    """
    # shortcut: plain WAV on disk; ADR 0004 wants Session audio encrypted at rest (no store yet)
    wav_path.parent.mkdir(parents=True, exist_ok=True)
    max_bytes = int(max_seconds * SAMPLE_RATE) * SAMPLE_WIDTH if max_seconds else None
    written = 0
    with wave.open(str(wav_path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(SAMPLE_WIDTH)
        wav.setframerate(SAMPLE_RATE)
        try:
            for chunk in chunks:
                if max_bytes is not None:
                    chunk = chunk[: max_bytes - written]
                wav.writeframes(chunk)
                written += len(chunk)
                if stop.is_set() or (max_bytes is not None and written >= max_bytes):
                    break
        except KeyboardInterrupt:
            pass  # Ctrl+C ends the recording; the audio so far is kept
    return written / SAMPLE_WIDTH / SAMPLE_RATE
