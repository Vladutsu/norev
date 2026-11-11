import unittest
from datetime import datetime, timezone

from norev.adapters.diarization.sherpa import cluster
from norev.core.models import Segment, Session, SpeakerTurn, Transcript
from norev.testing.fakes import FakeTranscriber
from norev.usecases.transcribe import transcribe_session
from test_ui_session_backend import Case


class ClusterTest(unittest.TestCase):
    def test_close_voices_merge_and_distant_ones_stay_apart(self):
        a, a2, b = [1.0, 0.0, 0.1], [0.98, 0.05, 0.12], [0.0, 1.0, 0.0]
        labels = cluster([a, b, a2], 0.6)
        self.assertEqual(labels[0], labels[2])
        self.assertNotEqual(labels[0], labels[1])
        self.assertEqual(len(set(cluster([a, b, a2], 2.0))), 1)
        self.assertEqual(cluster([], 0.6), [])


class SpansDiarizer:
    wants_spans = True

    def __init__(self):
        self.spans = None

    def diarize(self, audio_ref, spans=None):
        self.spans = spans
        return [SpeakerTurn(start, end, 'x' if start < 5 else 'y') for start, end in spans]


class SpansTest(Case):
    def test_a_segment_diarizer_gets_the_transcript_segments(self):
        self.store.save_session(Session('s1', datetime(2026, 4, 27, tzinfo=timezone.utc), 'local_council', audio_ref=self.store.save_audio('s1', b'a')))
        transcript = Transcript((Segment(0, 4, 'Supun la vot.'), Segment(6, 9, 'Cinci.')), 'ro')
        diarizer = SpansDiarizer()
        result = transcribe_session('s1', self.store, FakeTranscriber({self.store.get_session('s1').audio_ref: transcript}), diarizer)
        self.assertEqual(diarizer.spans, [(0, 4), (6, 9)])
        self.assertEqual([s.speaker_id for s in result.segments], ['S1', 'S2'])


if __name__ == '__main__':
    unittest.main()
