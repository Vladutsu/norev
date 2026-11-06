import tempfile
import threading
import unittest
import wave
from pathlib import Path

from LLM.Recorder.recorder import (
    SAMPLE_RATE,
    SAMPLE_WIDTH,
    parse_alsa_capture_devices,
    parse_dshow_devices,
    record,
)

HALF_SECOND = b"\x01\x00" * (SAMPLE_RATE // 2)


def _chunks(count: int):
    for _ in range(count):
        yield HALF_SECOND


class RecordTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.wav_path = Path(self._dir.name) / "nested" / "session.wav"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _wav(self) -> tuple[int, int, int, int]:
        with wave.open(str(self.wav_path), "rb") as wav:
            return wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()

    def test_writes_16khz_mono_wav_until_source_ends(self) -> None:
        seconds = record(_chunks(4), self.wav_path, threading.Event())
        self.assertEqual(seconds, 2.0)
        self.assertEqual(self._wav(), (1, SAMPLE_WIDTH, SAMPLE_RATE, 2 * SAMPLE_RATE))

    def test_max_seconds_cuts_exactly(self) -> None:
        seconds = record(_chunks(10), self.wav_path, threading.Event(), max_seconds=1.25)
        self.assertEqual(seconds, 1.25)
        self.assertEqual(self._wav()[3], int(1.25 * SAMPLE_RATE))

    def test_stop_event_ends_after_current_chunk(self) -> None:
        stop = threading.Event()

        def source():
            yield HALF_SECOND
            stop.set()  # e.g. the User pressed Enter while this chunk was captured
            yield HALF_SECOND
            yield HALF_SECOND

        self.assertEqual(record(source(), self.wav_path, stop), 1.0)

    def test_ctrl_c_keeps_audio_recorded_so_far(self) -> None:
        def source():
            yield HALF_SECOND
            raise KeyboardInterrupt

        self.assertEqual(record(source(), self.wav_path, threading.Event()), 0.5)
        self.assertEqual(self._wav()[3], SAMPLE_RATE // 2)

    def test_header_is_valid_while_recording(self) -> None:
        """If the process dies mid-Session, the WAV written so far must still be readable."""
        seen: list[int] = []

        def source():
            yield HALF_SECOND
            yield HALF_SECOND
            seen.append(self._wav()[3])  # read by another handle before the writer closes

        record(source(), self.wav_path, threading.Event())
        self.assertEqual(seen, [SAMPLE_RATE])


class DeviceListTest(unittest.TestCase):
    def test_dshow_lists_audio_devices_only(self) -> None:
        # FFmpeg log fragments as PyAV captures them, joined
        log = (
            '"Integrated Camera" (video)\n'
            '  Alternative name "@device_pnp_\\\\?\\usb#vid_13d3"\n'
            '"Microphone Array (Realtek(R) Audio)" (audio)\n'
            '  Alternative name "@device_cm_{33D9A762}\\wave_{70630C6C}"\n'
            '"Microfon (USB Headset)" (audio)\n'
        )
        self.assertEqual(
            parse_dshow_devices(log),
            ["Microphone Array (Realtek(R) Audio)", "Microfon (USB Headset)"],
        )

    def test_alsa_lists_capture_pcms_as_plughw(self) -> None:
        pcm = (
            "00-00: ALC3246 Analog : ALC3246 Analog : playback 1 : capture 1\n"
            "00-03: HDMI 0 : HDMI 0 : playback 1\n"
            "01-00: USB Audio : USB Audio : capture 1\n"
        )
        self.assertEqual(parse_alsa_capture_devices(pcm), ["plughw:0,0", "plughw:1,0"])


if __name__ == "__main__":
    unittest.main()
