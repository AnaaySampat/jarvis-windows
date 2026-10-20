"""The home screen's core style (orb / arc reactor) can be switched by voice."""

import unittest

from actions import _screen_patch


class ScreenCoreTest(unittest.TestCase):
    def test_reactor_words(self):
        for said in ("reactor", "the iron man one", "ironman helmet", "classic"):
            self.assertEqual(_screen_patch("set_core", {"value": said})[0], {"core": "reactor"}, said)

    def test_orb_words(self):
        self.assertEqual(_screen_patch("set_core", {"value": "the orb"})[0], {"core": "orb"})

    def test_unknown_asks(self):
        patch, msg = _screen_patch("set_core", {"value": "banana"})
        self.assertIsNone(patch)
        self.assertIn("orb", msg)


if __name__ == "__main__":
    unittest.main()
