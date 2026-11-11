"""Live feedback is bounded and independent from the final recording."""
import queue
import unittest
from unittest.mock import patch

from norev.ui._live import LiveFeedback, _CHUNK_BYTES, _worker, monitor


class FakeQueue(queue.Queue):
    def cancel_join_thread(self): pass
    def close(self): pass


class FakeProcess:
    def __init__(self, **kwargs):
        self.exitcode = None
        self.started = False
        self.terminated = False
    def start(self): self.started = True
    def is_alive(self): return self.started and not self.terminated
    def terminate(self): self.terminated = True
    def kill(self): self.terminated = True
    def join(self, timeout=None): pass
    def close(self): pass


class Context:
    def Queue(self, maxsize=0): return FakeQueue(maxsize)
    def Process(self, **kwargs): return FakeProcess(**kwargs)


class LiveFeedbackTest(unittest.TestCase):
    def test_recording_is_unchanged_when_feedback_queue_is_full(self):
        live = LiveFeedback('ro')
        blocks = [b'\x01\x00' * (_CHUNK_BYTES // 2)] * 3
        with patch('norev.ui._live.multiprocessing.get_context', return_value=Context()):
            self.assertEqual(list(monitor(iter(blocks), live)), blocks)
            self.assertEqual(live.audio.qsize(), 1)
            live.results.put('Bună ziua')
            self.assertEqual(live.read(), [{'speaker_id': None, 'text': 'Bună ziua'}])
            process = live.process
            live.close()
            self.assertTrue(process.terminated)  # before close returns: the final Transcript gets the GPU

    def test_no_audio_starts_no_model(self):
        live = LiveFeedback('ro')
        live.feed(b'')
        live.feed(b'\x00' * 20)
        self.assertEqual(live.read(), [])
        self.assertIsNone(live.process)
        live.close()

    def test_process_start_failure_keeps_audio_and_reports_unavailable(self):
        class BrokenContext(Context):
            def Process(self, **kwargs):
                raise OSError("unavailable")
        live = LiveFeedback('ro')
        block = b'\x00' * _CHUNK_BYTES
        with patch('norev.ui._live.multiprocessing.get_context', return_value=BrokenContext()):
            self.assertEqual(list(monitor([block], live)), [block])
            self.assertIn('nu este disponibilă', live.read()[0]['text'])
            live.close()

    def test_failed_live_model_does_not_stop_recording(self):
        live = LiveFeedback('ro')
        with patch('norev.ui._live.multiprocessing.get_context', return_value=Context()):
            live.feed(b'\x00' * _CHUNK_BYTES)
            live.results.put(None)
            self.assertIn('nu este disponibilă', live.read()[0]['text'])
            self.assertEqual(list(monitor([b'\x01\x00'], live)), [b'\x01\x00'])
            live.close()


    def test_worker_sends_the_fake_transcriber_text_of_each_synthetic_chunk(self):
        audio, results = queue.Queue(), queue.Queue()
        speech, silence = b'\x10\x00' * (_CHUNK_BYTES // 2), b'\x00' * _CHUNK_BYTES
        for chunk in (speech, silence, speech, None):
            audio.put(chunk)
        heard = []

        def fake_transcriber(language):
            heard.append(language)
            return lambda data: 'unu doi' if any(data) else ''

        _worker(audio, results, 'ro', make_transcriber=fake_transcriber)
        self.assertEqual(heard, ['ro'])  # one model per recording
        self.assertEqual([results.get_nowait() for _ in range(results.qsize())], ['unu doi', 'unu doi'])

    def test_worker_reports_a_missing_model_without_its_message(self):
        audio, results = queue.Queue(), queue.Queue()

        def no_model(language):
            raise OSError('model not found in /home/user/models')

        _worker(audio, results, 'ro', make_transcriber=no_model)
        self.assertIsNone(results.get_nowait())


if __name__ == '__main__':
    unittest.main()
