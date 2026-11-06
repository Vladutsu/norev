import json
import unittest

from norev.adapters.llm.speaker_names import SpeakerName, SpeakerNamer, Turn, names_from_turns, speaker_owners
from norev.testing.fakes import FakeLlmClient

LEAD, ANDREI, MARIA, RADU = "S1", "S2", "S3", "S4"

MEETING = [
    Turn(LEAD, "We're starting the sprint. Andrei, how are you doing with the payment tests?"),  # 0
    Turn(ANDREI, "I'm almost done, a few refund cases left."),  # 1
    Turn(LEAD, "Alright, Maria, what's the mobile team up to?"),  # 2
    Turn(MARIA, "I fixed the login crash. The password reset bug remains."),  # 3
    Turn(LEAD, "Who is handling the password reset bug?"),  # 4
    Turn(MARIA, "I'm handling the bug and will fix it by Wednesday."),  # 5
    Turn(ANDREI, "I'll write the API documentation by Friday."),  # 6
    Turn(RADU, "I can check the test server by tomorrow."),  # 7
    Turn(LEAD, "Thank you! Radu, can you prepare the presentation?"),  # 8
    Turn(RADU, "Yes, I'll take care of the presentation by November 18."),  # 9
    Turn(LEAD, "I'll write to Elena about the certificate today."),  # 10
]
NAMES = {
    ANDREI: SpeakerName("Andrei", 0),
    MARIA: SpeakerName("Maria", 2),
    RADU: SpeakerName("Radu", 8),
}


def _action(action_id, excerpt, owner=None):
    return {"id": action_id, "owner": owner, "transcript_excerpt": excerpt}


class NamesFromTurnsTest(unittest.TestCase):
    def test_a_question_by_name_names_who_answers_next(self):
        self.assertEqual(names_from_turns(MEETING, ["Andrei", "Maria", "Radu", "Elena"]), NAMES)

    def test_name_at_the_end_of_a_question(self):
        turns = [Turn(LEAD, "Can you take the release, Radu?"), Turn(RADU, "Sure.")]
        self.assertEqual(names_from_turns(turns, ["Radu"]), {RADU: SpeakerName("Radu", 0)})

    def test_a_name_that_is_not_a_question_names_nobody(self):
        turns = [Turn(LEAD, "Thank you, Maria."), Turn(RADU, "Next point: the budget.")]
        self.assertEqual(names_from_turns(turns, ["Maria"]), {})

    def test_no_name_when_the_next_turn_is_the_same_speaker(self):
        turns = [Turn(LEAD, "Andrei, are you there?"), Turn(LEAD, "Let's move on.")]
        self.assertEqual(names_from_turns(turns, ["Andrei"]), {})

    def test_self_introduction(self):
        turns = [Turn(ANDREI, "Hi, I'm Andrei from the backend team."), Turn(LEAD, "Welcome.")]
        self.assertEqual(names_from_turns(turns, ["Andrei"]), {ANDREI: SpeakerName("Andrei", 0)})

    def test_mentioning_someone_else_does_not_name_the_speaker(self):
        turns = [Turn(LEAD, "I'll write to Elena today."), Turn(ANDREI, "Good.")]
        self.assertEqual(names_from_turns(turns, ["Elena"]), {})

    def test_a_speaker_with_two_names_keeps_none(self):
        # diarization merged two people into S3
        turns = [
            Turn(LEAD, "Maria, any news?"), Turn(MARIA, "Login is fixed."),
            Turn(LEAD, "Radu, can you check the server?"), Turn(MARIA, "Yes."),
        ]
        self.assertEqual(names_from_turns(turns, ["Maria", "Radu"]), {})

    def test_a_name_given_to_two_speakers_is_kept_by_none(self):
        turns = [Turn(LEAD, "Andrei, ready?"), Turn(ANDREI, "Yes."), Turn(LEAD, "Andrei, and the docs?"), Turn(RADU, "Soon.")]
        self.assertEqual(names_from_turns(turns, ["Andrei"]), {})

    def test_only_listed_person_names_count(self):
        turns = [Turn(LEAD, "Alright, everyone, ready?"), Turn(ANDREI, "Yes.")]
        self.assertEqual(names_from_turns(turns, []), {})


class SpeakerOwnersTest(unittest.TestCase):
    def test_named_speaker_who_spoke_before_gets_their_commitment(self):
        actions = [
            _action("A1", "I'm handling the bug and will fix it by Wednesday"),
            _action("A2", "I'll write the API documentation by Friday"),
        ]
        self.assertEqual(speaker_owners(MEETING, actions, NAMES), {"A1": ("Maria", MARIA), "A2": ("Andrei", ANDREI)})

    def test_commitment_before_the_name_was_known_keeps_no_owner(self):
        # Radu says it in turn 7, but is named only in turn 8
        self.assertEqual(speaker_owners(MEETING, [_action("A1", "I can check the test server by tomorrow")], NAMES), {})

    def test_commitment_after_the_name_is_known(self):
        actions = [_action("A1", "I'll take care of the presentation by November 18")]
        self.assertEqual(speaker_owners(MEETING, actions, NAMES), {"A1": ("Radu", RADU)})

    def test_a_speaker_who_never_spoke_before_keeps_no_owner(self):
        turns = [Turn(LEAD, "Andrei, can you do the release?"), Turn(ANDREI, "Yes, I'll do the release on Monday.")]
        names = names_from_turns(turns, ["Andrei"])
        self.assertEqual(names, {ANDREI: SpeakerName("Andrei", 0)})
        self.assertEqual(speaker_owners(turns, [_action("A1", "I'll do the release on Monday")], names), {})

    def test_unnamed_speaker_keeps_no_owner(self):
        self.assertEqual(speaker_owners(MEETING, [_action("A1", "I'll write to Elena about the certificate today")], NAMES), {})

    def test_the_speaker_replaces_the_owner_the_extractor_guessed_for_i(self):
        actions = [_action("A1", "I'll write the API documentation by Friday", owner="Elena")]
        self.assertEqual(speaker_owners(MEETING, actions, NAMES), {"A1": ("Andrei", ANDREI)})

    def test_an_owner_is_left_alone_when_the_rule_does_not_hold(self):
        # unnamed lead; Radu before he was named
        actions = [
            _action("A1", "I'll write to Elena about the certificate today", owner="Radu"),
            _action("A2", "I can check the test server by tomorrow", owner="Maria"),
        ]
        self.assertEqual(speaker_owners(MEETING, actions, NAMES), {})

    def test_not_a_first_person_commitment(self):
        turns = MEETING + [Turn(ANDREI, "Please send the invoice to the client.")]
        self.assertEqual(speaker_owners(turns, [_action("A1", "Please send the invoice to the client")], NAMES), {})

    def test_excerpt_found_in_no_turn_or_in_two_turns_keeps_no_owner(self):
        turns = MEETING + [Turn(MARIA, "I'll write the API documentation by Friday.")]
        actions = [_action("A1", "I'll write the API documentation by Friday"), _action("A2", "I'll do something never said")]
        self.assertEqual(speaker_owners(turns, actions, NAMES), {})

    def test_excerpt_spanning_the_question_and_the_answer_counts_the_answer(self):
        actions = [_action("A1", "Who is handling the password reset bug? I'm handling the bug and will fix it by Wednesday.")]
        self.assertEqual(speaker_owners(MEETING, actions, NAMES), {"A1": ("Maria", MARIA)})

    def test_first_person_sentences_from_two_speakers_keep_no_owner(self):
        actions = [_action("A1", "I'll write the API documentation by Friday. I can check the test server by tomorrow.")]
        self.assertEqual(speaker_owners(MEETING, actions, NAMES), {})

    def test_excerpt_matches_despite_case_and_punctuation(self):
        actions = [_action("A1", "i'm handling the bug, and will fix it by wednesday!")]
        self.assertEqual(speaker_owners(MEETING, actions, NAMES), {"A1": ("Maria", MARIA)})


class SpeakerNamerTest(unittest.TestCase):
    def test_lists_names_with_the_llm_and_maps_them_in_code(self):
        llm = FakeLlmClient([json.dumps({"names": ["Andrei", "Maria", "Radu", "Elena"]})])
        names = SpeakerNamer(llm, model="m").name(MEETING)
        self.assertEqual(names, NAMES)
        [(prompt, model, schema)] = llm.calls
        self.assertEqual(model, "m")
        self.assertIn("Andrei, how are you doing", prompt)
        self.assertNotIn("[S1]", prompt, "the model does not decide who is who")
        self.assertEqual(schema["required"], ["names"])

    def test_a_name_that_is_not_in_the_transcript_is_dropped(self):
        turns = [Turn(LEAD, "Bob, ready?"), Turn(ANDREI, "Yes.")]
        llm = FakeLlmClient([json.dumps({"names": ["Robert"]})])
        self.assertEqual(SpeakerNamer(llm).name(turns), {})

    def test_answer_that_is_not_json_gives_no_names(self):
        self.assertEqual(SpeakerNamer(FakeLlmClient(["not json"])).name(MEETING), {})

    def test_long_transcript_is_sent_in_chunks(self):
        llm = FakeLlmClient([json.dumps({"names": ["Andrei"]}), json.dumps({"names": []}), json.dumps({"names": ["Maria"]})] + [json.dumps({"names": []})] * 20)
        names = SpeakerNamer(llm, chunk_chars=120).name(MEETING)
        self.assertGreater(len(llm.calls), 1)
        self.assertIn(ANDREI, names)


if __name__ == "__main__":
    unittest.main()
