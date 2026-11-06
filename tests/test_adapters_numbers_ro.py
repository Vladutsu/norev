import json
import unittest

from norev.adapters.llm.numbers_ro import numbers_to_digits
from norev.adapters.llm.translator import TurnTranslator
from norev.core.models import Segment, Transcript
from norev.testing.fakes import FakeLlmClient


class NumbersToDigitsTest(unittest.TestCase):
    def check(self, cases):
        for words, digits in cases:
            with self.subTest(words):
                self.assertEqual(numbers_to_digits(words), digits)

    def test_the_known_case(self):
        self.check([("sunați la o sută doisprezece imediat.", "sunați la 112 imediat.")])

    def test_hundreds_thousands_and_compounds(self):
        self.check([
            ("Dimineața două sute zece, aseară o sută optzeci.", "Dimineața 210, aseară 180."),
            ("o sută șaizeci cu nouăzeci și cinci", "160 cu 95"),
            ("patru mii cinci sute de euro", "4500 de euro"),
            ("douăsprezece mii cinci sute de lei", "12500 de lei"),
            ("nouăzeci și cinci de mii de euro", "95000 de euro"),
            ("o mie de miligrame", "1000 de miligrame"),
            ("treizeci și opt și ceva", "38 și ceva"),
            ("pe douăzeci noiembrie, la ora unsprezece", "pe 20 noiembrie, la ora 11"),
        ])

    def test_ambiguous_words_are_left_alone(self):
        self.check([
            ("o casă nouă", "o casă nouă"),  # adjective "new"
            ("nouă zile", "nouă zile"),  # could be "new" too: left for the model
            ("optzeci la sută", "80 la sută"),  # "la sută" is not 100
            ("o dată pe zi", "o dată pe zi"),
            ("un bilet de trimitere", "un bilet de trimitere"),
            ("douăzeci și cinci de licențe", "25 de licențe"),  # "de" only joins a scale word
            ("Bună ziua, doamna Popescu.", "Bună ziua, doamna Popescu."),
            ("Revin a doua zi.", "Revin a doua zi."),  # ordinal: the next day
            ("A doua propoziție.", "A doua propoziție."),
        ])

    def test_punctuation_ends_a_number(self):
        self.check([("două, trei", "2, 3")])


class TranslatorSendsDigitsTest(unittest.TestCase):
    TRANSCRIPT = Transcript((Segment(0, 1, "Sunați la o sută doisprezece.", "S1"), Segment(1, 2, "Bine.", "S2")), language="ro")

    def test_romanian_numbers_are_sent_as_digits(self):
        llm = FakeLlmClient([json.dumps({"turns": [{"id": "1", "text": "Call 112."}, {"id": "2", "text": "Fine."}]})])
        TurnTranslator(llm).translate(self.TRANSCRIPT, "ro")
        self.assertIn('"Sunați la 112."', llm.calls[0][0])
        self.assertNotIn("o sută doisprezece", llm.calls[0][0])

    def test_an_untranslated_turn_keeps_its_original_words(self):
        llm = FakeLlmClient([json.dumps({"turns": [{"id": "2", "text": "Fine."}]}), json.dumps({"turns": []})])
        result = TurnTranslator(llm).translate(self.TRANSCRIPT, "ro")
        self.assertEqual(result.segments[0].text, "Sunați la o sută doisprezece.")
        self.assertTrue(result.segments[0].untranslated)

    def test_other_languages_are_sent_as_they_are(self):
        llm = FakeLlmClient([json.dumps({"turns": [{"id": "1", "text": "x"}, {"id": "2", "text": "y"}]})])
        TurnTranslator(llm).translate(self.TRANSCRIPT, "de")
        self.assertIn("o sută doisprezece", llm.calls[0][0])


if __name__ == "__main__":
    unittest.main()
