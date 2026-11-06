"""Whisper and Sortformer adapters (#24): what can be checked on a CPU, with no model."""
import importlib
import subprocess
import sys
import unittest

from norev.adapters.whisper.transcriber import sentence_segments
from norev.core.ports import Diarizer, Transcriber
from norev.testing.fakes import FakeDiarizer, InMemorySessionStore


class ImportWithoutGpuDependenciesTest(unittest.TestCase):
    def test_adapters_import_without_faster_whisper_torch_or_nemo(self):
        # a fresh interpreter, so modules other tests imported do not hide a top-level import
        code = (
            "import sys; import norev.adapters.whisper.transcriber, norev.adapters.diarization.sortformer;"
            "print(sorted(m for m in ('faster_whisper', 'torch', 'nemo', 'ctranslate2') if m in sys.modules))"
        )
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "[]")

    def test_adapters_implement_the_ports(self):
        store = InMemorySessionStore()
        whisper = importlib.import_module("norev.adapters.whisper.transcriber").WhisperTranscriber(store)
        sortformer = importlib.import_module("norev.adapters.diarization.sortformer").SortformerDiarizer(store)
        self.assertIsInstance(whisper, Transcriber)
        self.assertIsInstance(sortformer, Diarizer)
        self.assertIsInstance(FakeDiarizer({}), Diarizer)


class SentenceSegmentsTest(unittest.TestCase):
    WORDS = [(0.0, 0.4, " Bună"), (0.4, 0.9, " ziua."), (1.2, 1.5, " Ce"), (1.5, 2.0, " faceți?"), (2.4, 2.8, " Bine")]

    def test_one_segment_per_sentence_with_word_times(self):
        self.assertEqual(
            [(s.start, s.end, s.text) for s in sentence_segments(self.WORDS)],
            [(0.0, 0.9, "Bună ziua."), (1.2, 2.0, "Ce faceți?"), (2.4, 2.8, "Bine")],
        )

    def test_segments_have_no_speaker_yet(self):
        self.assertTrue(all(s.speaker_id is None for s in sentence_segments(self.WORDS)))

    def test_no_words(self):
        self.assertEqual(sentence_segments([]), [])


if __name__ == "__main__":
    unittest.main()
