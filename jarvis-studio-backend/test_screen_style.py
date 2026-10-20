"""The home screen's style (modern / Iron Man) can be switched by voice."""

import unittest

from actions import _screen_patch


class ScreenStyleTest(unittest.TestCase):
    def test_ironman_words(self):
        for said in ("iron man", "the ironman one", "arc reactor", "classic hud"):
            self.assertEqual(_screen_patch("set_style", {"value": said})[0], {"style": "ironman"}, said)

    def test_modern_words(self):
        for said in ("modern", "the orb one", "back to normal"):
            self.assertEqual(_screen_patch("set_style", {"value": said})[0], {"style": "modern"}, said)

    def test_unknown_asks(self):
        patch, msg = _screen_patch("set_style", {"value": "banana"})
        self.assertIsNone(patch)
        self.assertIn("Iron Man", msg)


if __name__ == "__main__":
    unittest.main()
