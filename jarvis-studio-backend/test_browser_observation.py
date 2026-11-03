"""Page observations retain completion evidence even without clickable controls."""
import unittest
from types import SimpleNamespace
from unittest import mock

from actions import browser


class BrowserObservationTests(unittest.TestCase):
    def test_static_confirmation_retains_its_current_heading_url_and_title(self):
        frame = SimpleNamespace(evaluate=lambda *_: {
            "els": [], "heading": "Your response has been recorded", "rest": 0,
        })
        page = SimpleNamespace(main_frame=frame, frames=[frame],
                               title=lambda: "Confirmation", url="https://example.test/thanks")
        worker = SimpleNamespace(_last_title="Previous form", with_page=lambda action: action(page))
        with mock.patch.object(browser, "_get_worker", return_value=worker), \
                mock.patch.object(browser, "_settle"):
            ok, _message, listing = browser.list_elements()
        self.assertTrue(ok)
        self.assertIn("Page: Confirmation — https://example.test/thanks", listing)
        self.assertIn("Heading: Your response has been recorded", listing)
        self.assertEqual(worker._last_title, "Confirmation")


if __name__ == "__main__":
    unittest.main()
