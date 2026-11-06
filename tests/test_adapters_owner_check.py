import unittest

from norev.adapters.llm.owner_check import checked_owner, commitment_sentences, speakers_of_name
from norev.core.models import Segment

AGENT, CLIENT, THIRD = "S1", "S2", "S3"
TURNS = [
    Segment(0, 1, "Good day, Mr. Ionescu. Have you looked at the apartment?", AGENT),
    Segment(1, 2, "Yes, we liked it, but the price is high.", CLIENT),
    Segment(2, 3, "I can talk to the owner. I'll call him tomorrow.", AGENT),
    Segment(3, 4, "And I'll talk to the bank next week.", CLIENT),
    Segment(4, 5, "Maria, can you prepare the contract?", AGENT),
    Segment(5, 6, "Yes, I'll prepare it by Friday.", THIRD),
    Segment(6, 7, "I'll prescribe you a pill. I want you to rest.", AGENT),
]


class CommitmentSentencesTest(unittest.TestCase):
    def test_first_person_sentences_where_the_speaker_acts(self):
        self.assertEqual(commitment_sentences("May I speak with the owner. I'll call him tomorrow."),
                         ["May I speak with the owner.", "I'll call him tomorrow."])
        self.assertEqual(commitment_sentences("I'll send you by email the list."), ["I'll send you by email the list."])
        self.assertEqual(commitment_sentences("I'll send it to you tomorrow."), ["I'll send it to you tomorrow."])

    def test_sentences_that_hand_the_action_to_the_listener_do_not_count(self):
        self.assertEqual(commitment_sentences("I'll prescribe you a pill. I want you to rest."), [])
        self.assertEqual(commitment_sentences("Please send the list."), [])


class SpeakersOfNameTest(unittest.TestCase):
    def test_greeting_and_question_by_name_tie_the_next_speaker(self):
        self.assertEqual(speakers_of_name(TURNS, "Ionescu"), {CLIENT})
        self.assertEqual(speakers_of_name(TURNS, "Maria"), {THIRD})

    def test_a_greeting_that_answers_a_greeting_is_for_the_previous_speaker(self):
        turns = [Segment(0, 1, "Good day, Mrs. Georgescu.", "S1"), Segment(1, 2, "Good day, Doctor. My daughter came too.", "S2"),
                 Segment(2, 3, "Hello.", "S3")]
        self.assertEqual(speakers_of_name(turns, "Doctor"), {"S1"})

    def test_thanks_and_mentions_tie_nobody(self):
        turns = [Segment(0, 1, "Thank you, Mihai. Let's go on.", "S1"), Segment(1, 2, "I'll write to Elena today.", "S2"),
                 Segment(2, 3, "Fine.", "S3")]
        self.assertEqual(speakers_of_name(turns, "Mihai"), set())
        self.assertEqual(speakers_of_name(turns, "Elena"), set())


class CheckedOwnerTest(unittest.TestCase):
    def test_i_will_given_to_the_person_spoken_to_becomes_unknown(self):
        self.assertIsNone(checked_owner("Mr. Ionescu", "I'll call him tomorrow.", TURNS))
        self.assertIsNone(checked_owner("Maria", "And I'll talk to the bank next week.", TURNS))  # Maria is S3, the client said it

    def test_the_right_owner_is_kept(self):
        self.assertEqual(checked_owner("Maria", "Yes, I'll prepare it by Friday.", TURNS), "Maria")
        self.assertEqual(checked_owner("Mr. Ionescu", "And I'll talk to the bank next week.", TURNS), "Mr. Ionescu")

    def test_nothing_changes_without_certainty(self):
        self.assertEqual(checked_owner("Elena", "I'll call him tomorrow.", TURNS), "Elena")  # Elena tied to nobody
        self.assertEqual(checked_owner("Ionescu", "I want you to rest.", TURNS), "Ionescu")  # an instruction, not a commitment
        self.assertEqual(checked_owner("Ionescu", "I'll do something never said.", TURNS), "Ionescu")  # not in the Transcript
        self.assertIsNone(checked_owner(None, "I'll call him tomorrow.", TURNS))
        turns = TURNS + [Segment(7, 8, "Ionescu, are you there?", AGENT), Segment(8, 9, "Yes.", THIRD)]
        self.assertEqual(checked_owner("Mr. Ionescu", "I'll call him tomorrow.", turns), "Mr. Ionescu")  # tied to two Speakers

    def test_never_puts_a_speaker_id_or_another_name(self):
        for owner in ("Mr. Ionescu", "Maria", "Elena", None):
            self.assertIn(checked_owner(owner, "I'll call him tomorrow.", TURNS), (owner, None))


if __name__ == "__main__":
    unittest.main()
