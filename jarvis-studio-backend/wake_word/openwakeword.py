import threading
import time
from pathlib import Path
from typing import Callable, Optional
import collections
import os

import numpy as np
import pyaudio

# --- CONFIGURATION ---
# Optional custom-trained "Hey Jarvis" model. If this file is present we use it;
# otherwise we transparently fall back to openWakeWord's bundled "hey_jarvis"
# model so wake-word detection always works out of the box.
_DEFAULT_CUSTOM_MODEL_PATH = Path(__file__).resolve().parent / "models" / "hey_jarvis.onnx"
_CUSTOM_MODEL_PATH = Path(
    os.environ.get("JARVIS_WAKE_MODEL_PATH")
    or os.environ.get("AURA_WAKE_MODEL_PATH")
    or _DEFAULT_CUSTOM_MODEL_PATH
).expanduser()
_BUILTIN_MODEL = "hey_jarvis"
# Trigger confidence (0–1). Lowered to 0.3 (from 0.5) for snappier, more reliable
# "Hey Jarvis" pickup with the built-in model — the 3s cooldown keeps repeats in check.
_THRESHOLD = 0.4
_SAMPLE_RATE = 16_000
_CHUNK = 1280        # 80 ms
_COOLDOWN = 3.0


# --- CUSTOM INFERENCE WRAPPER (raw .onnx + torchaudio mel features) ---
class CustomInference:
    """Recognizer for a custom-trained single-output wake-word .onnx model."""

    def __init__(self, model_path):
        import onnxruntime as ort
        import torchaudio.transforms as T

        self.key = Path(model_path).stem
        self.session = ort.InferenceSession(model_path)
        self.featurizer = T.MelSpectrogram(
            sample_rate=_SAMPLE_RATE, n_fft=400, hop_length=1001, n_mels=64
        )
        self.audio_buffer = collections.deque(maxlen=16000)  # 1 second of audio

    def predict(self, audio_data) -> float:
        import torch

        # 1. Update buffer with new incoming audio
        self.audio_buffer.extend(audio_data.astype(np.float32) / 32768.0)

        # We need a full 1 second (16000 samples) to run inference
        if len(self.audio_buffer) < 16000:
            return 0.0

        # 2. Extract Features
        with torch.no_grad():
            audio_tensor = torch.tensor(np.array(self.audio_buffer), dtype=torch.float32)
            x = self.featurizer(audio_tensor)
            x = torch.log(x + 1e-9).unsqueeze(0).transpose(1, 2)
            # Ensure shape [1, 16, 64]
            if x.shape[1] > 16:
                x = x[:, :16, :]
            elif x.shape[1] < 16:
                x = torch.nn.functional.pad(x, (0, 0, 0, 16 - x.shape[1]))

        # 3. Inference
        input_name = self.session.get_inputs()[0].name
        logit = self.session.run(None, {input_name: x.numpy()})[0]
        prob = 1 / (1 + np.exp(-logit))  # Sigmoid
        return float(prob[0][0])


# --- BUILT-IN openWakeWord WRAPPER (pretrained "hey_jarvis") ---
class BuiltinInference:
    """Recognizer backed by an openWakeWord pretrained model (e.g. hey_jarvis)."""

    def __init__(self, name: str = _BUILTIN_MODEL):
        import openwakeword
        from openwakeword.model import Model

        # Feature (melspectrogram/embedding) models are fetched once, then cached.
        try:
            openwakeword.utils.download_models()
        except Exception:
            pass

        self.key = name
        self._model = Model(wakeword_models=[name], inference_framework="onnx")

    def predict(self, audio_data) -> float:
        # openWakeWord handles featurization internally; feed raw int16 frames.
        scores = self._model.predict(audio_data)
        return float(scores.get(self.key, 0.0))


def _build_recognizer():
    """Pick the custom model if its file exists, else the built-in fallback."""
    if _CUSTOM_MODEL_PATH.exists():
        try:
            rec = CustomInference(str(_CUSTOM_MODEL_PATH))
            print(f"[Jarvis] Wake word: custom model '{rec.key}' "
                  f"({_CUSTOM_MODEL_PATH}).", flush=True)
            return rec
        except Exception as exc:
            print(f"[Jarvis] Custom wake-word model failed to load ({exc}); "
                  f"falling back to built-in '{_BUILTIN_MODEL}'.", flush=True)
    else:
        print(f"[Jarvis] Custom wake-word model not found at {_CUSTOM_MODEL_PATH}; "
              f"using built-in '{_BUILTIN_MODEL}'. Say 'Hey Jarvis' to activate.",
              flush=True)
    return BuiltinInference(_BUILTIN_MODEL)


# --- DETECTOR CLASS ---
from wake_word.base import WakeWordDetector


class OpenWakeWordDetector(WakeWordDetector):  # Keeps name for compatibility
    def __init__(self) -> None:
        self._callback: Optional[Callable[[], None]] = None
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def on_detected(self, callback: Callable[[], None]) -> None:
        self._callback = callback

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        try:
            model = _build_recognizer()
        except Exception as exc:
            # Never take the whole backend down over wake-word setup: the GUI mic
            # button (manual trigger) and typed input still work without it.
            print(f"[Jarvis] Wake-word detection disabled — could not load any model "
                  f"({exc}). Use the mic button to talk to Jarvis.", flush=True)
            self._running = False
            return

        # Opening the mic can fail (no device, or it's already in use). Never let
        # that take the whole backend down — just disable wake-word listening; the
        # mic button + typed input still work.
        pa = None
        stream = None
        try:
            pa = pyaudio.PyAudio()
            stream = pa.open(
                rate=_SAMPLE_RATE,
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                frames_per_buffer=_CHUNK,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"[Jarvis] Wake-word microphone unavailable ({exc}); use the mic "
                  f"button to talk. ", flush=True)
            self._running = False
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:  # noqa: BLE001
                    pass
            return
        print(f"[Jarvis] Listening with wake word: {model.key}...", flush=True)

        last_trigger = 0.0
        try:
            while self._running:
                raw = stream.read(_CHUNK, exception_on_overflow=False)
                audio = np.frombuffer(raw, dtype=np.int16)

                score = model.predict(audio)

                now = time.time()
                if score >= _THRESHOLD and now - last_trigger > _COOLDOWN and self._callback:
                    last_trigger = now
                    print(f"[Jarvis] Detected! (Score: {score:.2f})")
                    self._callback()
        finally:
            self._running = False
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:  # noqa: BLE001
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:  # noqa: BLE001
                    pass
