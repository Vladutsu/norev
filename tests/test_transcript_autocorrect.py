import json
import unittest
from pathlib import Path

from norev.adapters.domains.loader import optional_correction
from norev.core.models import Segment, Transcript
from norev.testing.fakes import FakeLlmClient
from norev.usecases.transcript_autocorrect import autocorrect, merge, safe, vocabulary_words

FOLDER = Path(__file__).resolve().parents[1] / 'domains/local_council'
ALLOWED = vocabulary_words(['Zeno Karl Pinter', 'Laura Ionela Barac', 'Cisnădie'])


class MergeTest(unittest.TestCase):
    def test_misrecognised_words_listed_names_and_punctuation_are_corrected(self):
        self.assertEqual(merge('dureria nu are confesiune', 'durerea nu are confesiune', ALLOWED), 'durerea nu are confesiune')
        self.assertEqual(merge('predau conducerea domnului Consilier Zenopint', 'predau conducerea domnului consilier Zeno Pinter.', ALLOWED),
                         'predau conducerea domnului consilier Zeno Pinter.')
        self.assertEqual(merge('pe domnul Abaraf n-am văzut-o', 'pe doamna Barac n-am văzut-o', ALLOWED),
                         'pe domnul Barac n-am văzut-o')  # the name is fixed, "domnul" is a real word and stays
        self.assertEqual(merge('Am venit cu amendamentul Am zis', 'Am venit cu amendamentul. Am zis', ALLOWED),
                         'Am venit cu amendamentul. Am zis')

    def test_real_words_paraphrases_numbers_and_negations_keep_what_whisper_wrote(self):
        # each "fix" below was proposed by the model on a real council meeting
        for old, new in [('Cine este împotrivă?', 'Cine este împotriva?'), ('Ideea este', 'Idea este'),
                         ('o taxă modică', 'o taxă moderată'), ('familii care se destramă', 'familii care se despart'),
                         ('de toate confeziunile', 'de toate confețiunile'), ('Sibiul este un simbol', 'Sibiu este un simbol'),
                         ('13 voturi pentru', '14 voturi pentru'), ('Avem trei abțineri', 'Avem patru abțineri'),
                         ('Proiectul nu a fost adoptat', 'Proiectul a fost adoptat'), ('a vorbit domnul Floril', 'a vorbit domnul Popescu')]:
            with self.subTest(old=old):
                self.assertIsNone(merge(old, new, ALLOWED))
        self.assertTrue(safe('Sebiu a cunoscut', 'Sibiu a cunoscut', vocabulary_words(['Sibiu'])))
        self.assertFalse(safe('jocuri de noroc credează drame', 'jocuri de noroc, cred drame', ALLOWED))


class AutocorrectTest(unittest.TestCase):
    def test_only_safe_fixes_of_the_fragment_are_applied_times_and_speakers_kept(self):
        transcript = Transcript((Segment(0, 2, 'Vai intra.', 'S1'), Segment(2, 4, '13 voturi pentru.', 'S2'),
                                 Segment(4, 6, 'dureria nu are confesiune', 'S3')), 'ro')
        answer = {'fixes': [{'i': 0, 'text': 'Va intra.'}, {'i': 1, 'text': '15 voturi pentru.'},
                            {'i': 2, 'text': 'durerea nu are confesiune'}, {'i': 9, 'text': 'inventat'}]}
        llm = FakeLlmClient([json.dumps(answer)])
        fixed, changed = autocorrect(llm, transcript, optional_correction(FOLDER))
        self.assertEqual(changed, 1)  # "Vai" is a Romanian word: it stays; the number never changes
        self.assertEqual([s.text for s in fixed.segments], ['Vai intra.', '13 voturi pentru.', 'durerea nu are confesiune'])
        self.assertEqual([(s.start, s.end, s.speaker_id) for s in fixed.segments],
                         [(s.start, s.end, s.speaker_id) for s in transcript.segments])
        prompt = llm.calls[0][0]
        self.assertIn('[0] Vai intra.', prompt)
        self.assertIn('Zeno Karl Pinter', prompt)  # the Domain's names reach the model

    def test_a_broken_answer_leaves_the_fragment_as_whisper_wrote_it(self):
        transcript = Transcript((Segment(0, 2, 'Vai intra.'),), 'ro')
        fixed, changed = autocorrect(FakeLlmClient(['{"fixes": [']), transcript, optional_correction(FOLDER))
        self.assertEqual((fixed, changed), (transcript, 0))

    def test_the_vocabulary_file_has_no_comments(self):
        _, _, vocabulary = optional_correction(FOLDER)
        self.assertIn('Zeno Karl Pinter', vocabulary)
        self.assertFalse(any(line.startswith('#') for line in vocabulary))


if __name__ == '__main__':
    unittest.main()
