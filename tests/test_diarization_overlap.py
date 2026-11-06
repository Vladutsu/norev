import importlib
import sys
import unittest

from norev.adapters.diarization.overlap import assign_speakers, split_by_speaker


class AssignSpeakersTest(unittest.TestCase):
    def test_segment_inside_one_turn(self):
        self.assertEqual(assign_speakers([(1.0, 2.0)], [(0.0, 5.0, "A")]), ["A"])

    def test_largest_overlap_wins(self):
        turns = [(0.0, 2.5, "A"), (2.5, 10.0, "B")]
        self.assertEqual(assign_speakers([(2.0, 4.0)], turns), ["B"])

    def test_overlap_adds_up_over_turns_of_the_same_speaker(self):
        # A: 1.0 + 1.0 = 2.0 s, B: 1.5 s in one turn
        turns = [(0.0, 1.0, "A"), (1.0, 2.5, "B"), (2.5, 3.5, "A")]
        self.assertEqual(assign_speakers([(0.0, 3.5)], turns), ["A"])

    def test_tie_goes_to_the_speaker_who_starts_first_in_the_segment(self):
        turns = [(2.0, 4.0, "A"), (0.0, 2.0, "B")]
        self.assertEqual(assign_speakers([(1.0, 3.0)], turns), ["B"])

    def test_tie_with_same_start_goes_to_smaller_speaker_id(self):
        # overlapping speech: both talk over the whole segment
        turns = [(0.0, 5.0, "speaker_1"), (0.0, 5.0, "speaker_0")]
        self.assertEqual(assign_speakers([(1.0, 2.0)], turns), ["speaker_0"])

    def test_order_of_turns_does_not_matter(self):
        turns = [(0.0, 2.0, "A"), (2.0, 4.0, "B"), (1.0, 3.0, "C")]
        segments = [(0.5, 1.5), (1.0, 3.0), (2.5, 3.9)]
        self.assertEqual(assign_speakers(segments, turns), assign_speakers(segments, turns[::-1]))

    def test_segment_in_a_gap_between_turns_gets_none(self):
        turns = [(0.0, 1.0, "A"), (3.0, 4.0, "B")]
        self.assertEqual(assign_speakers([(1.2, 2.8)], turns), [None])

    def test_touching_boundaries_are_not_overlap(self):
        turns = [(0.0, 1.0, "A"), (2.0, 3.0, "B")]
        self.assertEqual(assign_speakers([(1.0, 2.0)], turns), [None])

    def test_segment_partly_in_a_gap_takes_the_speaker_it_touches(self):
        turns = [(0.0, 1.0, "A"), (5.0, 6.0, "B")]
        self.assertEqual(assign_speakers([(0.8, 3.0)], turns), ["A"])

    def test_zero_length_segment_gets_none(self):
        self.assertEqual(assign_speakers([(1.0, 1.0)], [(0.0, 2.0, "A")]), [None])

    def test_no_turns_gives_none_for_every_segment(self):
        self.assertEqual(assign_speakers([(0.0, 1.0), (1.0, 2.0)], []), [None, None])

    def test_keeps_segment_order_and_count(self):
        turns = [(0.0, 2.0, "A"), (2.0, 4.0, "B")]
        segments = [(3.0, 3.5), (0.0, 1.0), (10.0, 11.0), (2.5, 3.0)]
        self.assertEqual(assign_speakers(segments, turns), ["B", "A", None, "B"])

    def test_no_segments(self):
        self.assertEqual(assign_speakers([], [(0.0, 1.0, "A")]), [])


class SplitBySpeakerTest(unittest.TestCase):
    WORDS = [(0.0, 0.5, " Andrei,"), (0.5, 1.0, " ready?"), (1.2, 1.5, " Yes,"), (1.5, 2.0, " done.")]

    def test_one_whisper_segment_with_two_speakers_is_split_at_the_change(self):
        turns = [(0.0, 1.0, "A"), (1.1, 2.0, "B")]
        self.assertEqual(
            split_by_speaker(self.WORDS, turns),
            [(0.0, 1.0, "A", "Andrei, ready?"), (1.2, 2.0, "B", "Yes, done.")],
        )

    def test_word_in_a_diarization_gap_stays_with_the_speaker_before(self):
        turns = [(0.0, 1.0, "A"), (1.6, 2.0, "B")]  # "Yes," (1.2-1.5) overlaps no turn
        self.assertEqual(
            split_by_speaker(self.WORDS, turns),
            [(0.0, 1.5, "A", "Andrei, ready? Yes,"), (1.5, 2.0, "B", "done.")],
        )

    def test_first_words_in_a_gap_go_to_the_first_speaker(self):
        turns = [(0.6, 2.0, "A")]
        self.assertEqual(split_by_speaker(self.WORDS, turns), [(0.0, 2.0, "A", "Andrei, ready? Yes, done.")])

    def test_no_turns_gives_nothing(self):
        self.assertEqual(split_by_speaker(self.WORDS, []), [])


class PrototypeImportTest(unittest.TestCase):
    def test_imports_without_the_diarization_dependencies(self):
        module = importlib.import_module("norev.adapters.diarization.sortformer")
        self.assertTrue(hasattr(module, "SortformerDiarizer"))
        self.assertNotIn("nemo", sys.modules)
        self.assertNotIn("torch", sys.modules)


if __name__ == "__main__":
    unittest.main()
