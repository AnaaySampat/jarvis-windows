"""PDF output is readable across paragraphs, titles and Windows filename punctuation."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from actions import skills


@unittest.skipUnless(importlib.util.find_spec("fpdf") and importlib.util.find_spec("pypdf"),
                     "PDF round-trip tests require optional fpdf2 and pypdf packages")
class PdfRoundTripTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.enterContext(mock.patch.object(skills.storage, "documents_dir", return_value=self.output))
        self.enterContext(mock.patch.object(skills, "_open_file"))

    def create_and_read(self, payload):
        ok, message = skills.text_to_pdf(payload)
        self.assertTrue(ok, message)
        outputs = list(self.output.glob("*.pdf"))
        self.assertEqual(len(outputs), 1)
        ok, message, text = skills.read_pdf(str(outputs[0]))
        self.assertTrue(ok, message)
        return outputs[0], text

    def test_titled_multiline_document_retains_every_paragraph(self):
        _, text = self.create_and_read({"title": "Shopping list", "text": "Milk\nBread\n\nTotal: 42"})
        for expected in ("Shopping list", "Milk", "Bread", "Total: 42"):
            self.assertIn(expected, text)

    def test_multiline_document_without_title(self):
        _, text = self.create_and_read("First line\nSecond line")
        self.assertIn("First line", text)
        self.assertIn("Second line", text)

    def test_smart_punctuation_in_title_and_body_is_readable(self):
        _, text = self.create_and_read({"title": "JARVIS \u2014 caf\u00e9", "text": "\u201cHello\u201d\nDon't forget\u2026"})
        self.assertIn("JARVIS - caf\u00e9", text)
        self.assertIn('"Hello"', text)
        self.assertIn("Don't forget...", text)

    def test_title_with_windows_filename_characters_stays_in_output_folder(self):
        path, text = self.create_and_read({"title": 'Plan: work / home \\ notes? <2026> | "draft"', "text": "An ordinary report."})
        self.assertEqual(path.parent, self.output)
        self.assertFalse(any(character in path.name for character in '<>:"/\\|?*'))
        self.assertIn("An ordinary report.", text)


if __name__ == "__main__":
    unittest.main()
