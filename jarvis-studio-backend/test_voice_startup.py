"""The saved Groq preference never schedules local transcription inference."""

import unittest
from unittest import mock
import main
from llm import groq_bridge as gb


class VoiceStartupTests(unittest.TestCase):
    def test_groq_preference_does_not_silently_become_local_without_a_key(self):
        with mock.patch.object(gb, "_config", {"stt": "groq"}), \
             mock.patch.object(gb, "_api_key", ""):
            self.assertEqual(gb.get_stt(), "groq")

    def _schedule(self, mode):
        scheduled = []
        local = mock.Mock(return_value="local-job")
        speech = mock.Mock(return_value="tts-job")
        set_mode = mock.Mock()
        with mock.patch.object(main, "get_stt", return_value=mode), \
             mock.patch.object(main, "set_transcriber_stt", set_mode), \
             mock.patch.object(main, "preload_whisper", local), \
             mock.patch.object(main, "_tts", mock.Mock(preload=speech)), \
             mock.patch.object(main, "_track_task", side_effect=lambda job, label: scheduled.append(label)):
            main._schedule_voice_preloads()
        return local, speech, set_mode, scheduled

    def test_groq_startup_preloads_voice_but_never_local_whisper(self):
        local, speech, mode, scheduled = self._schedule("groq")
        local.assert_not_called()
        speech.assert_called_once()
        mode.assert_called_once_with("groq")
        self.assertEqual(scheduled, ["tts preload"])

    def test_legacy_local_preference_cannot_schedule_local_whisper(self):
        local, _, mode, scheduled = self._schedule("local")
        local.assert_not_called()
        mode.assert_called_once_with("groq")
        self.assertEqual(scheduled, ["tts preload"])


if __name__ == "__main__":
    unittest.main()
