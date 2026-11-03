"""Groq mode never loads, downloads or silently falls back to local Whisper."""

import asyncio
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import wave

import numpy as np

import provisioning
from llm import groq_bridge as bridge
from transcription import whisper_transcriber as wt


class GroqTranscriptionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_mode, self.original_model = wt._stt_mode, wt._model
        self.addCleanup(setattr, wt, "_stt_mode", self.original_mode)
        self.addCleanup(setattr, wt, "_model", self.original_model)
        wt.set_stt("groq")
        self.audio = np.array([1000, -1000] * 800, dtype=np.int16)
        self.cloud = mock.AsyncMock(return_value="Jarvis cloud check 42")
        self.local = mock.Mock(side_effect=AssertionError("Local Whisper must not run in Groq mode"))
        for patch in (
            mock.patch.object(bridge, "get_api_key", return_value="configured-test-key"),
            mock.patch.object(bridge, "transcribe_audio", self.cloud),
            mock.patch.object(wt, "_get_model", self.local),
            mock.patch.object(wt, "_do_transcribe", self.local),
            mock.patch.object(wt, "_record_until_silence", return_value=self.audio),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def _all_inputs(self):
        return [await wt.transcribe(), await wt.transcribe_clip(self.audio),
                await wt.transcribe_voice_note(b"synthetic-ogg")]

    async def test_microphone_clip_and_voice_note_use_groq_only(self):
        self.assertEqual(await self._all_inputs(), ["Jarvis cloud check 42"] * 3)
        self.assertEqual(self.cloud.await_count, 3)
        wav_data = self.cloud.await_args_list[0].args[0]
        with wave.open(io.BytesIO(wav_data), "rb") as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()), (16000, 1, 2))
            self.assertEqual(audio.readframes(audio.getnframes()), self.audio.tobytes())
        self.assertEqual(self.cloud.await_args_list[1].kwargs["prompt"], "Jarvis")
        self.assertEqual(self.cloud.await_args_list[2].kwargs["filename"], "voice.ogg")
        self.local.assert_not_called()

    async def test_failed_groq_request_never_uses_local_model_for_any_input(self):
        self.cloud.return_value = None
        for call in (wt.transcribe, lambda: wt.transcribe_clip(self.audio),
                     lambda: wt.transcribe_voice_note(b"synthetic-ogg")):
            with self.subTest(input=call):
                with self.assertRaisesRegex(wt.TranscriptionError, "Groq.*Local transcription was not used"):
                    await call()
        self.local.assert_not_called()

    async def test_missing_key_is_actionable_without_cloud_or_local_call(self):
        with mock.patch.object(bridge, "get_api_key", return_value=""):
            for call in (wt.transcribe, lambda: wt.transcribe_clip(self.audio),
                         lambda: wt.transcribe_voice_note(b"synthetic-ogg")):
                with self.subTest(input=call):
                    with self.assertRaisesRegex(wt.TranscriptionError, "Settings.*API Keys"):
                        await call()
        self.cloud.assert_not_awaited()
        self.local.assert_not_called()

    async def test_empty_success_stays_empty_without_local_retry(self):
        self.cloud.return_value = ""
        self.assertEqual(await self._all_inputs(), [""] * 3)
        self.local.assert_not_called()

    async def test_transport_exception_is_safe_and_never_falls_back(self):
        self.cloud.side_effect = OSError("untrusted transport details")
        with self.assertRaises(wt.TranscriptionError) as error:
            await wt.transcribe_clip(self.audio)
        self.assertIn("internet connection", str(error.exception))
        self.assertNotIn("untrusted transport details", str(error.exception))
        self.local.assert_not_called()

    async def test_preload_is_a_noop_in_groq_mode(self):
        await wt.preload()
        self.local.assert_not_called()

    async def test_silence_does_not_call_a_provider(self):
        with mock.patch.object(wt, "_record_until_silence", return_value=np.array([], dtype=np.int16)):
            self.assertEqual(await wt.transcribe(), "")
        self.assertEqual(await wt.transcribe_clip(np.array([], dtype=np.int16)), "")
        self.assertEqual(await wt.transcribe_voice_note(b""), "")
        self.cloud.assert_not_awaited()
        self.local.assert_not_called()


class GroqOnlyLegacyPreferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_mode, self.original_model = wt._stt_mode, wt._model
        self.addCleanup(setattr, wt, "_stt_mode", self.original_mode)
        self.addCleanup(setattr, wt, "_model", self.original_model)

    async def test_legacy_local_choice_uses_groq_for_every_audio_input(self):
        wt.set_stt("local")
        self.assertFalse(wt.local_transcription_enabled())
        self.assertEqual(wt._stt_mode, "groq")
        audio = np.array([1000, -1000], dtype=np.int16)
        with mock.patch.object(wt, "_get_model", side_effect=AssertionError("local inference")), \
             mock.patch.object(wt, "_record_until_silence", return_value=audio), \
             mock.patch.object(bridge, "get_api_key", return_value="test-key"), \
             mock.patch.object(bridge, "transcribe_audio", mock.AsyncMock(return_value="Online check")) as cloud:
            self.assertEqual(await wt.transcribe(), "Online check")
            self.assertEqual(await wt.transcribe_clip(audio), "Online check")
            self.assertEqual(await wt.transcribe_voice_note(b"voice"), "Online check")
            await wt.preload()
        self.assertEqual(cloud.await_count, 3)

    async def test_legacy_preferences_cannot_allocate_or_retain_a_local_model(self):
        wt._model = object()
        wt.set_stt("local")
        self.assertIsNone(wt._model)
        with self.assertRaisesRegex(wt.TranscriptionError, "Local Whisper is disabled"):
            wt._get_model()
        with mock.patch.object(wt, "_get_model") as model:
            await wt.preload()
            model.assert_not_called()

    def test_import_does_not_require_faster_whisper(self):
        original_import = __import__
        attempts = []

        def import_without_local(name, *args, **kwargs):
            if name.startswith("faster_whisper"):
                attempts.append(name)
                raise ImportError("not installed")
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location("cloud_only_transcription_import", Path(wt.__file__))
        module = importlib.util.module_from_spec(spec)
        with mock.patch("builtins.__import__", import_without_local):
            spec.loader.exec_module(module)
        self.assertEqual(attempts, [])
        self.assertFalse(module.local_transcription_enabled())


class ProvisioningChoiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.original_mode, self.original_model = wt._stt_mode, wt._model
        self.addCleanup(setattr, wt, "_stt_mode", self.original_mode)
        self.addCleanup(setattr, wt, "_model", self.original_model)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.speech_present, self.speech_download = mock.Mock(return_value=False), mock.Mock(return_value=(True, ""))
        self.assets = (
            ("browser", "browser", lambda: True, lambda: (True, "")),
            ("speech", "speech", self.speech_present, self.speech_download),
            ("voice", "voice", lambda: True, lambda: (True, "")),
        )
        for patch in (
            mock.patch.object(provisioning, "_ASSETS", self.assets),
            mock.patch.object(provisioning, "_marker_path", return_value=Path(self.directory.name) / "setup-complete"),
            mock.patch.object(provisioning, "_log_setup"),
            mock.patch.object(provisioning.asyncio, "sleep", mock.AsyncMock()),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def test_groq_first_run_and_repair_do_not_probe_or_download_whisper(self):
        wt.set_stt("groq")
        emit = mock.AsyncMock()
        self.assertEqual(provisioning.missing_assets(), [])
        await provisioning.provision(emit)
        await provisioning.provision(emit, force=True)
        self.speech_present.assert_not_called()
        self.speech_download.assert_not_called()
        starts = [call.args[1] for call in emit.await_args_list if call.args[1].get("stage") == "start"]
        self.assertTrue(starts)
        self.assertTrue(all("speech" not in event["assets"] for event in starts))

    async def test_legacy_local_choice_does_not_restore_offline_model_repair(self):
        wt.set_stt("local")
        self.assertEqual(provisioning.missing_assets(), [])
        await provisioning.provision(mock.AsyncMock())
        self.speech_present.assert_not_called()
        self.speech_download.assert_not_called()


if __name__ == "__main__":
    unittest.main()
