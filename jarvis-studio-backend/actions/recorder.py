"""Voice & video recording for Jarvis.

A tiny background-thread recorder you can start and stop by voice:

  • voice  → captures the default microphone to a .wav  (uses pyaudio, already a dep)
  • video  → captures the default webcam to an .avi via OpenCV

Files land in the user's storage 'recordings' folder (see storage.py).
Everything is best-effort and returns the usual ``(ok, message)`` tuple. Only
one recording of each kind runs at a time.
"""

from __future__ import annotations

import threading
import time
import wave
from pathlib import Path
from typing import Optional, Tuple

import storage


def _out_dir() -> Path:
    """Recordings live in the user's storage 'recordings' folder."""
    return storage.recordings_dir()


class _Recorder:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._ready = threading.Event()
        self.path: Optional[Path] = None
        self._error: Optional[str] = None
        self._frames = 0

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _worker(self) -> None:
        try:
            self._run()
        except Exception as exc:  # noqa: BLE001 — device/encoder failures belong in the reply
            self._error = str(exc)
        finally:
            self._ready.set()  # failed startup must wake start() too

    def _begin(self, message: str) -> Tuple[bool, str]:
        self._error = None
        self._frames = 0
        self._stop.clear()
        self._ready.clear()
        self._thread = threading.Thread(target=self._worker, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=8):
            self._stop.set()
            return False, "The recording could not start in time; the capture device may be busy."
        if self._error:
            return False, f"The recording failed, sir — {self._error}."
        if not self.active or not self._frames:
            return False, "The recording stopped before any frames were captured."
        return True, message

    def _finish(self, media: str, saved: str, timeout: float) -> Tuple[bool, str]:
        if self._thread is None:
            return False, f"I'm not recording any {media} right now."
        self._stop.set()
        self._thread.join(timeout=timeout)
        if self.active:
            return False, "The recording is still stopping; wait a moment and ask me to stop again."
        self._thread = None
        if self._error:
            return False, f"The recording failed, sir — {self._error}."
        try:
            valid_file = self.path is not None and self.path.is_file() and self.path.stat().st_size > 0
        except OSError:
            valid_file = False
        if not self._frames or not valid_file:
            return False, "The recording didn't save any captured frames, sir."
        return True, f"Saved your {saved} to {self.path}."


class _AudioRecorder(_Recorder):
    def start(self) -> Tuple[bool, str]:
        if self.active:
            return False, "I'm already recording audio."
        try:
            import pyaudio  # noqa: F401
        except ImportError:
            return False, "Voice recording needs pyaudio (it ships with Jarvis already)."
        self.path = _out_dir() / f"voice_{int(time.time())}.wav"
        return self._begin("Recording audio. Say 'stop recording' when you're done.")

    def stop(self) -> Tuple[bool, str]:
        return self._finish("audio", "recording", 5)

    def _run(self) -> None:
        import pyaudio
        rate, chunk = 44_100, 1024
        pa = pyaudio.PyAudio()
        stream = None
        frames = []
        # Keep pa.open() INSIDE the try so a failed device-open (no mic, device
        # busy) can't leak the PyAudio instance or kill the thread silently —
        # the error is recorded and surfaced by stop().
        try:
            stream = pa.open(format=pyaudio.paInt16, channels=1, rate=rate,
                             input=True, frames_per_buffer=chunk)
            while not self._stop.is_set():
                frames.append(stream.read(chunk, exception_on_overflow=False))
                self._frames += 1
                self._ready.set()
        except Exception as exc:  # noqa: BLE001
            self._error = str(exc)
        finally:
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                if frames:
                    with wave.open(str(self.path), "wb") as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(pa.get_sample_size(pyaudio.paInt16))
                        wf.setframerate(rate)
                        wf.writeframes(b"".join(frames))
                elif self._error is None:
                    self._error = "no audio was captured"
            except Exception as exc:  # noqa: BLE001
                self._error = str(exc)
            finally:
                try:
                    pa.terminate()
                except Exception:  # noqa: BLE001
                    pass


class _VideoRecorder(_Recorder):
    def start(self) -> Tuple[bool, str]:
        if self.active:
            return False, "I'm already recording video."
        try:
            import cv2  # noqa: F401
        except ImportError:
            return False, ("Video recording needs OpenCV. Install it with: "
                           "pip install opencv-python")
        self.path = _out_dir() / f"video_{int(time.time())}.avi"
        return self._begin("Recording from your webcam. Say 'stop recording' when you're done.")

    def stop(self) -> Tuple[bool, str]:
        return self._finish("video", "video", 6)

    def _run(self) -> None:
        import cv2
        cap, writer = None, None
        try:
            cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                raise RuntimeError("the webcam is unavailable or in use")
            w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
            h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
            fourcc = cv2.VideoWriter_fourcc(*"XVID")
            writer = cv2.VideoWriter(str(self.path), fourcc, 20.0, (w, h))
            if not writer.isOpened():
                raise RuntimeError("the video writer could not open the output file")
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    if self._stop.is_set():
                        break
                    raise RuntimeError("the webcam stopped producing frames")
                writer.write(frame)
                self._frames += 1
                self._ready.set()
        finally:
            if cap is not None:
                cap.release()
            if writer is not None:
                writer.release()


class _ScreenRecorder(_Recorder):
    """Capture the whole screen to a video file (mss grab → OpenCV writer)."""

    def start(self) -> Tuple[bool, str]:
        if self.active:
            return False, "I'm already recording your screen."
        try:
            import cv2  # noqa: F401
            import mss  # noqa: F401
        except ImportError:
            return False, ("Screen recording needs mss and OpenCV. Install them with: "
                           "pip install mss opencv-python")
        self.path = _out_dir() / f"screen_{int(time.time())}.mp4"
        return self._begin("Recording your screen. Say 'stop recording' when you're done.")

    def stop(self) -> Tuple[bool, str]:
        return self._finish("screen", "screen recording", 6)

    def _run(self) -> None:
        import cv2
        import mss
        import numpy as np
        fps = 12.0
        with mss.mss() as sct:
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            w, h = mon["width"], mon["height"]
            fourcc = cv2.VideoWriter_fourcc(*"mp4v")
            writer = cv2.VideoWriter(str(self.path), fourcc, fps, (w, h))
            frame_interval = 1.0 / fps
            try:
                if not writer.isOpened():
                    raise RuntimeError("the screen video writer could not open the output file")
                while not self._stop.is_set():
                    t0 = time.time()
                    shot = sct.grab(mon)
                    frame = np.array(shot)[:, :, :3]      # BGRA → BGR
                    writer.write(frame)
                    self._frames += 1
                    self._ready.set()
                    elapsed = time.time() - t0
                    if elapsed < frame_interval:
                        time.sleep(frame_interval - elapsed)
            finally:
                writer.release()


_audio = _AudioRecorder()
_video = _VideoRecorder()
_screen = _ScreenRecorder()


def record(payload) -> Tuple[bool, str]:
    """Dispatch start/stop for audio / video / screen recording.

    payload: {"media":"audio|video|screen","do":"start|stop"} (also accepts "action").
    Recording always runs until explicitly stopped — never on a timer.
    """
    if isinstance(payload, dict):
        media = str(payload.get("media") or payload.get("target") or "audio").lower()
        do = str(payload.get("do") or payload.get("action") or "start").lower()
    else:
        media, do = "audio", "start"

    if media in ("screen", "desktop", "display"):
        rec = _screen
    elif media in ("video", "webcam", "camera", "cam"):
        rec = _video
    else:
        rec = _audio

    if do in ("stop", "end", "finish"):
        return rec.stop()
    return rec.start()


def stop_all() -> None:
    for rec in (_audio, _video, _screen):
        if rec.active:
            rec.stop()


def active_recordings() -> dict:
    """Which recorders are currently running — for the GUI Skills panel."""
    return {"audio": _audio.active, "video": _video.active, "screen": _screen.active}
