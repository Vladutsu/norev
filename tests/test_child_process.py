"""#65: Whisper and diarization in a child process that close() ends, with a fake adapter (no GPU or model)."""
import os
import unittest

from norev.adapters.child_process import ChildProcessAdapter


class Store:
    def load_audio(self, audio_ref):
        return {"a1": b"RIFF one", "a2": b"RIFF second"}[audio_ref]


class EchoAdapter:
    """In the child: what it got from the parent, and in which process."""

    def __init__(self, store):
        self._store = store

    def transcribe(self, audio_ref, language=None):
        return self._store.load_audio(audio_ref), language, os.getpid()

    def diarize(self, audio_ref):
        raise ValueError("no Speakers")


def echo(store):
    return EchoAdapter(store)


class ChildProcessAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = ChildProcessAdapter(Store(), echo)
        self.addCleanup(self.adapter.close)

    def test_the_adapter_runs_in_one_child_with_the_audio_from_the_parent_store(self):
        audio, language, pid = self.adapter.transcribe("a1", "ro")
        self.assertEqual((audio, language), (b"RIFF one", "ro"))
        self.assertNotEqual(pid, os.getpid())
        self.assertEqual(self.adapter.transcribe("a2")[::2], (b"RIFF second", pid))  # same child, no reload

    def test_close_ends_the_child_and_the_next_call_starts_a_new_one(self):
        first = self.adapter.transcribe("a1")[2]
        process = self.adapter._process
        self.adapter.close()
        self.assertIsNone(self.adapter._process)
        with self.assertRaises(ValueError):  # closed: the process object is released with the child
            process.is_alive()
        self.assertNotEqual(self.adapter.transcribe("a1")[2], first)

    def test_an_error_in_the_child_is_raised_in_the_parent(self):
        with self.assertRaises(ValueError):
            self.adapter.diarize("a1")
        self.assertEqual(self.adapter.transcribe("a1")[0], b"RIFF one")  # the child still serves

    def test_close_before_any_call_does_nothing(self):
        ChildProcessAdapter(Store(), echo).close()


if __name__ == "__main__":
    unittest.main()
