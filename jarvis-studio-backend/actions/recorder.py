"""Voice & video recording for Jarvis.

A tiny background-thread recorder you can start and stop by voice:

  • voice  → captures the default microphone to a .wav  (uses pyaudio, already a dep)
  • video  → captures the default webcam (+ mic best-effort) to an .avi/.mp4 via OpenCV

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


class _AudioRecorder:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.path: Optional[Path] = None
        self._error: Optional[str] = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> Tuple[bool, str]:
        if self.active:
            return False, "I'm already recording audio."
        try:
            import pyaudio  # noqa: F401
        except ImportError:
            return False, "Voice recording needs pyaudio (it ships with Jarvis already)."
        self.path = _out_dir() / f"voice_{int(time.time())}.wav"
        self._error = None
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True, "Recording audio. Say 'stop recording' when you're done."

    def stop(self) -> Tuple[bool, str]:
        if not self.active:
            return False, "I'm not recording any audio right now."
        self._stop.set()
        self._thread.join(timeout=5)
        if self._error:
            return False, f"The recording failed, sir — {self._error}."
        if self.path is None or not self.path.exists():
            return False, "The recording didn't save, sir."
        return True, f"Saved your recording to {self.path}."

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
                pa.terminate()


class _VideoRecorder:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.path: Optional[Path] = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> Tuple[bool, str]:
        if self.active:
            return False, "I'm already recording video."
        try:
            import cv2  # noqa: F401
        except ImportError:
            return False, ("Video recording needs OpenCV. Install it with: "
                           "pip install opencv-python")
        self.path = _out_dir() / f"video_{int(time.time())}.avi"
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True, "Recording from your webcam. Say 'stop recording' when you're done."

    def stop(self) -> Tuple[bool, str]:
        if not self.active:
            return False, "I'm not recording any video right now."
        self._stop.set()
        self._thread.join(timeout=6)
        if self.path is None or not self.path.exists():
            return False, ("The video didn't save, sir — the webcam may be "
                           "unavailable or in use.")
        return True, f"Saved your video to {self.path}."

    def _run(self) -> None:
        import cv2
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            self._stop.set()
            return
        w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)) or 640
        h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 480
        fourcc = cv2.VideoWriter_fourcc(*"XVID")
        writer = cv2.VideoWriter(str(self.path), fourcc, 20.0, (w, h))
        try:
            while not self._stop.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                writer.write(frame)
        finally:
            cap.release()
            writer.release()


class _ScreenRecorder:
    """Capture the whole screen to a video file (mss grab → OpenCV writer)."""

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.path: Optional[Path] = None

    @property
    def active(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

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
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True, "Recording your screen. Say 'stop recording' when you're done."

    def stop(self) -> Tuple[bool, str]:
        if not self.active:
            return False, "I'm not recording the screen right now."
        self._stop.set()
        self._thread.join(timeout=6)
        if self.path is None or not self.path.exists():
            return False, "The screen recording didn't save, sir."
        return True, f"Saved your screen recording to {self.path}."

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
                while not self._stop.is_set():
                    t0 = time.time()
                    shot = sct.grab(mon)
                    frame = np.array(shot)[:, :, :3]      # BGRA → BGR
                    writer.write(frame)
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
