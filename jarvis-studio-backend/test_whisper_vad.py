"""Speech/silence gate for the Whisper mic recorder."""

import unittest

from transcription.whisper_transcriber import (
    SILENCE_THRESHOLD,
    _calibrate_threshold,
    _frame_is_speech,
)


class CalibrateThresholdTests(unittest.TestCase):
    def test_speech_during_calib_does_not_inflate_threshold(self):
        # User starts talking immediately after wake — all frames are loud.
        _, threshold = _calibrate_threshold([1500.0, 1800.0, 2200.0, 1900.0])
        self.assertEqual(threshold, float(SILENCE_THRESHOLD))

    def test_mixed_room_and_speech_uses_quiet_frames(self):
        noise_floor, threshold = _calibrate_threshold([120.0, 140.0, 1800.0, 2100.0])
        self.assertLess(noise_floor, 200.0)
        self.assertLess(threshold, 600.0)


class FrameIsSpeechTests(unittest.TestCase):
    def test_hangover_keeps_quiet_trailing_syllables_as_speech(self):
        threshold = 1000.0
        # Between hangover (450) and threshold — still talking.
        self.assertTrue(_frame_is_speech(500.0, threshold, 100.0, speech_detected=True))

    def test_hangover_does_not_apply_before_speech_starts(self):
        threshold = 1000.0
        self.assertFalse(_frame_is_speech(500.0, threshold, 100.0, speech_detected=False))


if __name__ == "__main__":
    unittest.main()
