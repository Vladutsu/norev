"""Where a quote of a Proposed Action or Suggestion was said, so review can show it in the Transcript."""
import unittest

from norev.core.models import Segment, Transcript
from norev.ui.backend import quote_span

SPOKEN = Transcript(segments=(
    Segment(0.0, 2.0, "Maria, te ocupi de documentație?", "S1"),
    Segment(2.0, 4.5, "Da, o actualizez până pe 20 noiembrie.", "S2"),
    Segment(4.5, 6.0, "Mulțumesc.", "S1"),
), language="ro")
# the translation joins a Speaker's turn into one Segment, with the turn's times
READ = Transcript(segments=(
    Segment(0.0, 2.0, "Maria, can you take care of the documentation?", "S1"),
    Segment(2.0, 4.5, "Yes, I will update it by November 20th.", "S2"),
    Segment(4.5, 6.0, "Thank you.", "S1"),
), language="en")


class QuoteSpanTest(unittest.TestCase):
    def test_a_quote_over_a_question_and_its_answer_spans_both_segments(self):
        excerpt = "Maria, can you take care of the documentation? Yes, I will update it by November 20th."
        self.assertEqual(quote_span(excerpt, READ, SPOKEN), {"start": 0.0, "end": 4.5})

    def test_case_and_punctuation_do_not_matter(self):
        self.assertEqual(quote_span("yes i will UPDATE it", READ, SPOKEN), {"start": 2.0, "end": 4.5})

    def test_the_spoken_transcript_is_searched_when_the_read_one_lacks_the_words(self):
        self.assertEqual(quote_span("„o actualizez până pe 20 noiembrie”", READ, SPOKEN), {"start": 2.0, "end": 4.5})

    def test_no_span_when_the_words_are_not_said_in_that_order(self):
        for excerpt in ("I will update the documentation", "documentation Maria", "update it by November 21st"):
            with self.subTest(excerpt=excerpt):
                self.assertIsNone(quote_span(excerpt, READ, SPOKEN))
        self.assertIsNone(quote_span("documentați", SPOKEN))  # a whole word, never part of one

    def test_no_span_for_an_empty_quote_or_no_transcript(self):
        self.assertIsNone(quote_span("", READ, SPOKEN))
        self.assertIsNone(quote_span(" „” ", READ, SPOKEN))
        self.assertIsNone(quote_span("Thank you"))


if __name__ == "__main__":
    unittest.main()
