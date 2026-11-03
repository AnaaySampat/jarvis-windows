"""Recording replies reflect opened devices, usable encoders and finalized output."""
from pathlib import Path
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from actions import recorder


class RecorderFailureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.enterContext(mock.patch.object(recorder, "_out_dir", return_value=self.output))

    def test_video_start_reports_unavailable_webcam_and_releases_it(self):
        capture = mock.Mock()
        capture.isOpened.return_value = False
        fake_cv = types.SimpleNamespace(VideoCapture=lambda *_: capture)
        with mock.patch.dict(sys.modules, {"cv2": fake_cv}):
            instance = recorder._VideoRecorder()
            ok, message = instance.start()
            self.assertFalse(ok, message)
            self.assertIn("webcam", message.lower())
            instance._thread.join(timeout=2)
            capture.release.assert_called_once()

    def test_video_start_rejects_failed_writer_even_when_file_exists(self):
        capture = mock.Mock()
        capture.isOpened.return_value = True
        capture.get.return_value = 16
        capture.read.return_value = (False, None)
        writer = mock.Mock()
        writer.isOpened.return_value = False
        def make_writer(path, *_args):
            Path(path).write_bytes(b"an invalid encoder header")
            return writer
        fake_cv = types.SimpleNamespace(VideoCapture=lambda *_: capture,
            CAP_PROP_FRAME_WIDTH=3, CAP_PROP_FRAME_HEIGHT=4,
            VideoWriter_fourcc=lambda *_: 0, VideoWriter=make_writer)
        with mock.patch.dict(sys.modules, {"cv2": fake_cv}):
            instance = recorder._VideoRecorder()
            ok, message = instance.start()
            self.assertFalse(ok, message)
            self.assertIn("writer", message.lower())
            instance._thread.join(timeout=2)
            self.assertFalse(instance.stop()[0])
        capture.release.assert_called_once()
        writer.release.assert_called_once()

    def test_audio_device_initialization_error_is_reported_by_start(self):
        def unavailable():
            raise OSError("no input device")
        with mock.patch.dict(sys.modules, {"pyaudio": types.SimpleNamespace(PyAudio=unavailable)}):
            instance = recorder._AudioRecorder()
            ok, message = instance.start()
            self.assertFalse(ok, message)
            self.assertIn("no input device", message)
            instance._thread.join(timeout=2)

    def test_screen_capture_startup_exception_is_reported(self):
        def unavailable():
            raise OSError("capture device unavailable")
        with mock.patch.dict(sys.modules, {"cv2": types.SimpleNamespace(),
                                           "mss": types.SimpleNamespace(mss=unavailable)}):
            instance = recorder._ScreenRecorder()
            ok, message = instance.start()
            self.assertFalse(ok, message)
            self.assertIn("capture device unavailable", message)
            instance._thread.join(timeout=2)

    def test_stop_retains_error_from_a_worker_that_already_exited(self):
        instance = recorder._AudioRecorder()
        instance._thread = threading.Thread(target=lambda: None)
        instance._thread.start()
        instance._thread.join()
        instance._error = "microphone disconnected"
        ok, message = instance.stop()
        self.assertFalse(ok)
        self.assertIn("microphone disconnected", message)

    def test_stop_never_calls_an_unfinished_file_saved(self):
        for cls in (recorder._AudioRecorder, recorder._VideoRecorder, recorder._ScreenRecorder):
            with self.subTest(recorder=cls.__name__):
                instance = cls()
                instance.path = self.output / (cls.__name__ + ".partial")
                instance.path.write_bytes(b"still being written")
                instance._thread = types.SimpleNamespace(is_alive=lambda: True, join=lambda **_: None)
                ok, message = instance.stop()
                self.assertFalse(ok, message)
                self.assertIn("stopping", message.lower())

    def test_audio_success_captures_frames_and_finalizes_a_readable_wave(self):
        import wave
        stream = mock.Mock()
        def read(*_args, **_kwargs):
            time.sleep(0.005)
            return b"\0\0" * 1024
        stream.read.side_effect = read
        audio = mock.Mock()
        audio.open.return_value = stream
        audio.get_sample_size.return_value = 2
        fake_audio = types.SimpleNamespace(PyAudio=lambda: audio, paInt16=8)
        with mock.patch.dict(sys.modules, {"pyaudio": fake_audio}):
            instance = recorder._AudioRecorder()
            ok, message = instance.start()
            self.assertTrue(ok, message)
            self.assertTrue(instance.active)
            ok, message = instance.stop()
            self.assertTrue(ok, message)
            self.assertFalse(instance.active)
            self.assertFalse(instance.stop()[0])
            with wave.open(str(instance.path), "rb") as output:
                self.assertGreater(output.getnframes(), 0)
                self.assertEqual(output.getsampwidth(), 2)
        stream.close.assert_called_once()
        audio.terminate.assert_called_once()

    def test_screen_writer_failure_is_caught_and_released(self):
        capture = mock.MagicMock()
        capture.__enter__.return_value = capture
        capture.monitors = [{"width": 16, "height": 16}]
        writer = mock.Mock()
        writer.isOpened.return_value = False
        fake_cv = types.SimpleNamespace(VideoWriter_fourcc=lambda *_: 0,
                                        VideoWriter=lambda *_: writer)
        with mock.patch.dict(sys.modules, {"cv2": fake_cv,
                                           "mss": types.SimpleNamespace(mss=lambda: capture)}):
            instance = recorder._ScreenRecorder()
            ok, message = instance.start()
            self.assertFalse(ok, message)
            self.assertIn("writer", message)
            instance._thread.join(timeout=2)
        writer.release.assert_called_once()
        capture.__exit__.assert_called_once()


if __name__ == "__main__":
    unittest.main()
