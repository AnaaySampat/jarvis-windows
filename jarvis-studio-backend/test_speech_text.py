"""What the voice says for a Markdown reply (tts/speech_text.py)."""
import unittest

from tts.speech_text import speakable
from tts.tts_engine import _strip_tags


class SpeakableTests(unittest.TestCase):
    def test_a_table_is_read_row_by_row(self):
        text = "Prices:\n| Fruit | Price |\n|---|---:|\n| Apple | $2 |\n| Pear | $3 |"
        self.assertEqual(_strip_tags(text), "Prices: Fruit, Price. Apple, $2. Pear, $3.")

    def test_one_streamed_row_alone_is_still_read_as_cells(self):
        self.assertEqual(speakable("| Apple | $2 |"), "Apple, $2.")
        self.assertEqual(speakable("|---|---|"), "")

    def test_emphasis_links_and_lists_lose_their_punctuation(self):
        self.assertEqual(
            _strip_tags("## Plan\n- **Buy** milk\n1. Read [the docs](https://x.y)"),
            "Plan Buy milk Read the docs")

    def test_arithmetic_and_identifiers_survive(self):
        self.assertEqual(speakable("2*3*4 and snake_case_name"), "2*3*4 and snake_case_name")

    def test_tags_still_go_first(self):
        self.assertEqual(_strip_tags('Here. [TABLE]{"rows":[["*a*"]]}[/TABLE] Done.'),
                         "Here. Done.")


if __name__ == "__main__":
    unittest.main()
