import itertools
import tempfile
import threading
import time
import unittest
import wave
from pathlib import Path

from norev.adapters.recorder.recorder import SAMPLE_RATE, SAMPLE_WIDTH, Recorder

HALF_SECOND = b"\x01\x00" * (SAMPLE_RATE // 2)  # fake audio block: 8000 samples of 16-bit PCM


def _blocks(count: int):
    for _ in range(count):
        yield HALF_SECOND


class RecorderWavTest(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.wav_path = Path(self._dir.name) / "nested" / "session.wav"

    def tearDown(self) -> None:
        self._dir.cleanup()

    def _header(self) -> tuple[int, int, int, int]:
        with wave.open(str(self.wav_path), "rb") as wav:
            return wav.getnchannels(), wav.getsampwidth(), wav.getframerate(), wav.getnframes()

    def test_header_sample_rate_and_length(self) -> None:
        seconds = Recorder(self.wav_path, source=_blocks(4)).record()
        self.assertEqual(seconds, 2.0)
        self.assertEqual(self._header(), (1, SAMPLE_WIDTH, SAMPLE_RATE, 2 * SAMPLE_RATE))
        self.assertEqual(self.wav_path.read_bytes()[:4], b"RIFF")

    def test_records_into_memory_without_a_file(self) -> None:
        import io

        buffer = io.BytesIO()
        Recorder(buffer, source=[b"\x00\x00" * SAMPLE_RATE]).record()
        with wave.open(io.BytesIO(buffer.getvalue())) as wav:
            self.assertEqual((wav.getframerate(), wav.getnframes()), (SAMPLE_RATE, SAMPLE_RATE))

    def test_stop_from_another_thread(self) -> None:
        def endless():
            for _ in itertools.count():
                time.sleep(0.001)
                yield HALF_SECOND

        recorder = Recorder(self.wav_path, source=endless())
        threading.Timer(0.05, recorder.stop).start()
        seconds = recorder.record()  # returns only because stop() was called
        self.assertGreater(seconds, 0)
        self.assertEqual(self._header()[3], int(seconds * SAMPLE_RATE))

    def test_ctrl_c_keeps_audio_recorded_so_far(self) -> None:
        def interrupted():
            yield HALF_SECOND
            raise KeyboardInterrupt

        self.assertEqual(Recorder(self.wav_path, source=interrupted()).record(), 0.5)
        self.assertEqual(self._header()[3], SAMPLE_RATE // 2)

    def test_header_is_valid_while_recording(self) -> None:
        """If the process is killed mid-Session, the WAV written so far must still be readable."""
        frames_seen: list[int] = []

        def watched():
            yield HALF_SECOND
            yield HALF_SECOND
            frames_seen.append(self._header()[3])  # read by another handle before the writer closes

        Recorder(self.wav_path, source=watched()).record()
        self.assertEqual(frames_seen, [SAMPLE_RATE])


if __name__ == "__main__":
    unittest.main()
