"""Recording the command from the wake word's own stream (wake_word/capture.py)."""
import unittest

import numpy as np

from wake_word.capture import (
    CommandCapture, NoiseFloor, PreRoll, command_after_wake, heard_wake_phrase, is_noise,
)

CHUNK = 1280                      # 80 ms at 16 kHz


def tone(level: float) -> np.ndarray:
    return (np.ones(CHUNK) * level * 32767).astype(np.int16)


class PreRollTests(unittest.TestCase):
    def test_keeps_only_the_last_three_seconds_in_order(self):
        pre = PreRoll()
        for i in range(60):                              # 4.8 s of numbered chunks
            pre.write(np.full(CHUNK, i, dtype=np.int16))
        snap = pre.snapshot()
        self.assertEqual(len(snap), 48_000)
        self.assertEqual(snap[-1], 59)
        self.assertLess(snap[0], snap[-1])


class CaptureTests(unittest.TestCase):
    def _run(self, levels):
        room = NoiseFloor()
        for _ in range(50):
            room.loud(tone(0.02))                        # the room before the wake
        room.hold()
        cap = CommandCapture(np.zeros(16, dtype=np.int16))
        for n, level in enumerate(levels, 1):
            if cap.add(tone(level), room.loud(tone(level))):
                return cap, n * 0.08
        return cap, None

    def test_a_command_ends_after_a_pause(self):
        levels = [0.02] * 8 + [0.06] * 20 + [0.02] * 40
        cap, ended = self._run(levels)
        self.assertTrue(cap.speech)
        # Speech stops at 2.24 s; the capture ends SILENCE_S later.
        self.assertAlmostEqual(ended, 2.24 + CommandCapture.SILENCE_S, delta=0.1)

    def test_nothing_said_ends_without_speech(self):
        cap, ended = self._run([0.02] * 80)
        self.assertFalse(cap.speech)
        self.assertAlmostEqual(ended, CommandCapture.NO_SPEECH_S, delta=0.1)

    def test_the_wake_words_tail_is_not_a_command(self):
        # "…vis" right after the detection, then the user pauses before speaking.
        levels = [0.06] * 5 + [0.02] * 15 + [0.06] * 10 + [0.02] * 30
        cap, ended = self._run(levels)
        self.assertTrue(cap.speech)
        self.assertGreater(ended, 2.4)                   # waited through the pause

    def test_a_lone_blip_is_not_speech(self):
        cap, _ = self._run([0.02] * 10 + [0.08] + [0.02] * 60)
        self.assertFalse(cap.speech)


class PhraseTests(unittest.TestCase):
    def test_the_command_follows_the_last_wake_phrase(self):
        self.assertEqual(command_after_wake("we watched Iron Man. Hey Jarvis, open Spotify."),
                         "open Spotify.")
        self.assertEqual(command_after_wake("Kay, Jarvis. What's the time?"), "What's the time?")
        self.assertEqual(command_after_wake("Hey Jarvis."), "")

    def test_talk_that_only_mentions_jarvis_is_not_a_wake(self):
        self.assertIsNone(command_after_wake("Jarvis is the AI in Iron Man"))
        self.assertFalse(heard_wake_phrase("I think Jarvis was voiced by Paul Bettany"))

    def test_whispers_silence_phrases_are_noise(self):
        self.assertTrue(is_noise("Thank you."))
        self.assertTrue(is_noise("[Music]"))
        self.assertFalse(is_noise("open notepad"))


if __name__ == "__main__":
    unittest.main()
