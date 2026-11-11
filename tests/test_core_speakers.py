import importlib
import sys
import unittest

from norev.core.models import Segment, Speaker, SpeakerTurn, Transcript
from norev.core.speakers import assign_speakers, attach_speakers, speakers_in


class AssignSpeakersTest(unittest.TestCase):
    def test_segment_inside_one_turn(self):
        self.assertEqual(assign_speakers([(1.0, 2.0)], [SpeakerTurn(0.0, 5.0, "A")]), ["A"])

    def test_largest_overlap_wins(self):
        turns = [SpeakerTurn(0.0, 2.5, "A"), SpeakerTurn(2.5, 10.0, "B")]
        self.assertEqual(assign_speakers([(2.0, 4.0)], turns), ["B"])

    def test_overlap_adds_up_over_turns_of_the_same_speaker(self):
        # A: 1.0 + 1.0 = 2.0 s, B: 1.5 s in one turn
        turns = [SpeakerTurn(0.0, 1.0, "A"), SpeakerTurn(1.0, 2.5, "B"), SpeakerTurn(2.5, 3.5, "A")]
        self.assertEqual(assign_speakers([(0.0, 3.5)], turns), ["A"])

    def test_tie_goes_to_the_speaker_who_starts_first_in_the_segment(self):
        turns = [SpeakerTurn(2.0, 4.0, "A"), SpeakerTurn(0.0, 2.0, "B")]
        self.assertEqual(assign_speakers([(1.0, 3.0)], turns), ["B"])

    def test_tie_with_same_start_goes_to_smaller_speaker_id(self):
        # overlapping speech: both talk over the whole segment
        turns = [SpeakerTurn(0.0, 5.0, "speaker_1"), SpeakerTurn(0.0, 5.0, "speaker_0")]
        self.assertEqual(assign_speakers([(1.0, 2.0)], turns), ["speaker_0"])

    def test_order_of_turns_does_not_matter(self):
        turns = [SpeakerTurn(0.0, 2.0, "A"), SpeakerTurn(2.0, 4.0, "B"), SpeakerTurn(1.0, 3.0, "C")]
        segments = [(0.5, 1.5), (1.0, 3.0), (2.5, 3.9)]
        self.assertEqual(assign_speakers(segments, turns), assign_speakers(segments, turns[::-1]))

    def test_segment_in_a_gap_between_turns_gets_none(self):
        turns = [SpeakerTurn(0.0, 1.0, "A"), SpeakerTurn(3.0, 4.0, "B")]
        self.assertEqual(assign_speakers([(1.2, 2.8)], turns), [None])

    def test_touching_boundaries_are_not_overlap(self):
        turns = [SpeakerTurn(0.0, 1.0, "A"), SpeakerTurn(2.0, 3.0, "B")]
        self.assertEqual(assign_speakers([(1.0, 2.0)], turns), [None])

    def test_segment_partly_in_a_gap_takes_the_speaker_it_touches(self):
        turns = [SpeakerTurn(0.0, 1.0, "A"), SpeakerTurn(5.0, 6.0, "B")]
        self.assertEqual(assign_speakers([(0.8, 3.0)], turns), ["A"])

    def test_zero_length_segment_gets_none(self):
        self.assertEqual(assign_speakers([(1.0, 1.0)], [SpeakerTurn(0.0, 2.0, "A")]), [None])

    def test_no_turns_gives_none_for_every_segment(self):
        self.assertEqual(assign_speakers([(0.0, 1.0), (1.0, 2.0)], []), [None, None])

    def test_keeps_segment_order_and_count(self):
        turns = [SpeakerTurn(0.0, 2.0, "A"), SpeakerTurn(2.0, 4.0, "B")]
        segments = [(3.0, 3.5), (0.0, 1.0), (10.0, 11.0), (2.5, 3.0)]
        self.assertEqual(assign_speakers(segments, turns), ["B", "A", None, "B"])

    def test_no_segments(self):
        self.assertEqual(assign_speakers([], [SpeakerTurn(0.0, 1.0, "A")]), [])


class AttachSpeakersTest(unittest.TestCase):
    TRANSCRIPT = Transcript(
        segments=(
            Segment(0.0, 2.0, "Bună ziua."),
            Segment(2.2, 4.0, "Bună ziua, ce faceți?"),
            Segment(4.5, 5.0, "(tuse)"),
            Segment(5.2, 7.0, "Bine, mulțumesc."),
        ),
        language="ro",
    )
    TURNS = [SpeakerTurn(0.0, 2.1, "speaker_1"), SpeakerTurn(2.1, 4.2, "speaker_0"), SpeakerTurn(5.0, 7.0, "speaker_1")]

    def test_sets_speakers_and_keeps_times_text_and_language(self):
        result = attach_speakers(self.TRANSCRIPT, self.TURNS)
        self.assertEqual(
            [(s.start, s.end, s.text, s.speaker_id) for s in result.segments],
            [(0.0, 2.0, "Bună ziua.", "S1"), (2.2, 4.0, "Bună ziua, ce faceți?", "S2"),
             (4.5, 5.0, "(tuse)", None), (5.2, 7.0, "Bine, mulțumesc.", "S1")],
        )
        self.assertEqual(result.language, "ro")

    def test_ids_follow_first_appearance_not_the_diarizer_labels(self):
        result = attach_speakers(self.TRANSCRIPT, self.TURNS)
        self.assertEqual(result.segments[0].speaker_id, "S1")  # diarizer said speaker_1

    def test_intervention_without_any_turn_keeps_no_speaker(self):
        result = attach_speakers(self.TRANSCRIPT, [])
        self.assertTrue(all(s.speaker_id is None for s in result.segments))
        self.assertEqual(speakers_in(result), [])

    def test_speakers_in_order_of_first_appearance_without_roles(self):
        result = attach_speakers(self.TRANSCRIPT, self.TURNS)
        self.assertEqual(speakers_in(result), [Speaker("S1"), Speaker("S2")])
        self.assertTrue(all(s.role is None and not s.role_confirmed for s in speakers_in(result)))


if __name__ == "__main__":
    unittest.main()
