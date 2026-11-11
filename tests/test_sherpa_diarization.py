import unittest
from datetime import datetime, timezone

from norev.adapters.diarization.sherpa import MIN_SPEAKER_SECONDS, cluster, group
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


A, A2, B, C = [1.0, 0.0, 0.0], [0.97, 0.2, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]


class GroupTest(unittest.TestCase):
    def test_close_mean_voices_are_one_speaker_and_distant_ones_stay_apart(self):
        labels = group([A, B, A2, B], [20, 20, 20, 20], merge=0.4)
        self.assertEqual(labels, [0, 1, 0, 1])  # numbered in order of first appearance
        self.assertEqual(len(set(group([A, B, A2], [20, 20, 20], merge=1.5))), 1)

    def test_a_short_reply_joins_the_nearest_speaker_instead_of_becoming_one(self):
        # "5." is too short to shape a Speaker: it is attached, not counted
        labels = group([A, B, C, A2], [30, 30, 0.8, 30], merge=0.4)
        self.assertEqual(len(set(labels)), 2)
        self.assertIn(labels[2], (labels[0], labels[1]))

    def test_a_voice_heard_only_briefly_in_the_whole_meeting_is_not_a_speaker(self):
        labels = group([A, B, C], [60, 60, MIN_SPEAKER_SECONDS - 1], merge=0.4)
        self.assertEqual(len(set(labels)), 2)
        self.assertEqual(len(set(group([A, B, C], [60, 60, MIN_SPEAKER_SECONDS], merge=0.4))), 3)

    def test_a_short_recording_still_gets_speakers(self):
        self.assertEqual(group([A, B], [3, 3], merge=0.4), [0, 1])  # nobody reaches the minimum: all are kept
        self.assertEqual(group([A], [0.7], merge=0.4), [0])
        self.assertEqual(group([], [], merge=0.4), [])

    def test_the_merge_distance_comes_from_the_environment(self):
        from unittest import mock
        with mock.patch.dict("os.environ", {"NOREV_SPEAKER_MERGE": "1.5"}):
            self.assertEqual(len(set(group([A, B], [20, 20]))), 1)


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
