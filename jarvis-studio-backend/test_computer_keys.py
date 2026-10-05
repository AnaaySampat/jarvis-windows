"""Scancode resolution for the game/held-key input path (computer.key_down/up).

Guards the one bit of non-trivial logic there: name → (scancode, extended) via the
OS keyboard layout. Windows-only (uses the real MapVirtualKey), which is where this
code runs. Skips cleanly elsewhere.
"""

import sys
import unittest

if sys.platform != "win32":  # pragma: no cover - CI on non-Windows
    raise unittest.SkipTest("computer key scancodes are Windows-only")

from actions import computer as c


class KeyScanTests(unittest.TestCase):
    def test_letters_map_to_set1_scancodes(self):
        self.assertEqual(c._key_to_scan("w"), (0x11, False))
        self.assertEqual(c._key_to_scan("a"), (0x1E, False))
        self.assertEqual(c._key_to_scan("s"), (0x1F, False))
        self.assertEqual(c._key_to_scan("d"), (0x20, False))

    def test_arrows_are_extended(self):
        for name in ("up", "down", "left", "right"):
            scan, ext = c._key_to_scan(name)
            self.assertTrue(scan and ext, f"{name} should be an extended key")

    def test_aliases_and_named_keys(self):
        self.assertEqual(c._key_to_scan("arrow left"), c._key_to_scan("left"))
        self.assertEqual(c._key_to_scan("escape"), c._key_to_scan("esc"))
        # LShift lives at Set-1 scancode 0x2A, not extended.
        self.assertEqual(c._key_to_scan("shift"), (0x2A, False))
        space = c._key_to_scan("space")
        self.assertTrue(space and space[1] is False)

    def test_unknown_keys_return_none(self):
        self.assertIsNone(c._key_to_scan("nope key"))
        self.assertIsNone(c._key_to_scan(""))

    def test_release_all_keys_clears_tracking(self):
        c._held_keys.add((0x11, False))
        c.release_all_keys()
        self.assertEqual(c._held_keys, set())


if __name__ == "__main__":
    unittest.main()
