import asyncio
import io
import os
import sys
import threading
import wave
from pathlib import Path

import numpy as np
import pyaudio
from faster_whisper import WhisperModel

# Set by the GUI's "stop" button (via main.handle_stop_speech) to abort an
# in-progress recording so the user can cut JARVIS off mid-listen. Checked every
# audio frame in _record_until_silence; cleared at the start of each recording.
_cancel_listen = threading.Event()

# Set by handle_finish_listen (overlay ✓ button / push-to-talk key release) to end
# the recording NOW and KEEP what was captured (unlike _cancel_listen, which drops
# it). Checked every audio frame; cleared at the start of each recording.
_finish_listen = threading.Event()

# Push-to-talk mode: while set, the recorder ignores silence-based stopping and the
# pre-speech timeout, so it records continuously until the key is released
# (_finish_listen) or the hard MAX_RECORD_SECS cap. Set per-turn by set_ptt().
_ptt_mode = threading.Event()


def request_cancel() -> None:
    """Ask the active microphone recording (if any) to stop immediately."""
    _cancel_listen.set()


def request_finish() -> None:
    """End the active recording now but KEEP the captured audio for transcription."""
    _finish_listen.set()


def set_ptt(on: bool) -> None:
    """Enable/disable push-to-talk recording for the next turn."""
    if on:
        _ptt_mode.set()
    else:
        _ptt_mode.clear()

# Which speech-to-text engine to use: "groq" (Groq Whisper Turbo, cloud) with a
# local fallback, or "local" (offline faster-whisper). Set by main from config.
_stt_mode = "groq"


def set_stt(mode: str) -> None:
    global _stt_mode
    _stt_mode = "local" if str(mode).lower() == "local" else "groq"

SAMPLE_RATE = 16_000
CHUNK_SIZE = 1280           # 80 ms frames — same as wake word detector
SILENCE_THRESHOLD = int(os.environ.get("JARVIS_SILENCE_RMS", "180"))
# Quiet microphones can put speech below a fixed RMS gate. Treat frames that are
# clearly above the calibrated room floor as speech even if they miss the hard
# threshold.
SOFT_SPEECH_MIN_RMS = int(os.environ.get("JARVIS_SOFT_SPEECH_RMS", "120"))
SOFT_SPEECH_RATIO = 0.65
# Once speech starts, keep the gate open down to this fraction of threshold so
# quiet syllables / trailing words aren't misread as end-of-turn.
SPEECH_HANGOVER_RATIO = float(os.environ.get("JARVIS_SPEECH_HANGOVER", "0.45"))
SILENCE_DURATION = float(os.environ.get("JARVIS_SILENCE_SECS", "2.5"))
PRE_SPEECH_TIMEOUT = 5.0   # seconds to wait for speech before aborting
MAX_RECORD_SECS = 30
# Ambient-noise calibration: sample the room for a moment, then treat anything
# under (noise floor × margin) as silence. This is what stops Jarvis from
# "staying on" after you finish — a fixed 500 RMS floor never trips in a room
# with any background hum, so it would record until the 30 s cap.
#   • MARGIN is kept modest so the threshold sits *between* ambient and speech.
#   • CEILING caps the adaptive threshold so a high-gain mic (or a calibration
#     window that accidentally catches the start of speech) can never push the
#     cutoff *above* normal speech — which would make every word read as silence
#     and capture nothing. Normal speech is well above 2500 RMS at typical gain.
NOISE_CALIBRATION_SECS = 0.35
NOISE_MARGIN = 1.5
SILENCE_CEILING = 2500

_model: "WhisperModel | None" = None


def _bundled_model_path() -> "str | None":
    candidates = []
    explicit = os.environ.get("JARVIS_BUNDLED_WHISPER_MODEL")
    if explicit:
        candidates.append(Path(explicit))
    preload = os.environ.get("JARVIS_PRELOAD_ASSETS_DIR")
    if preload:
        candidates.append(Path(preload) / "whisper-base")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "preload-assets" / "whisper-base")
    for path in candidates:
        try:
            if path.is_dir() and any(path.iterdir()):
                return str(path)
        except OSError:
            continue
    return None


def model_present() -> bool:
    """True if the faster-whisper base model is already available — bundled, or in
    the Hugging Face cache from a previous download — so first-run provisioning can
    skip the ~150MB fetch. Never raises."""
    if _bundled_model_path():
        return True
    try:
        from huggingface_hub import try_to_load_from_cache
        hit = try_to_load_from_cache("Systran/faster-whisper-base", "model.bin")
        return isinstance(hit, str) and os.path.isfile(hit)
    except Exception:  # noqa: BLE001
        return False


def _calibrate_threshold(calib_rms: list[float]) -> tuple[float, float]:
    """Derive (noise_floor, silence_threshold) from the opening calibration window.

    Users often start speaking immediately after the wake word, so the calib window
    can contain speech. Only quiet frames inform the floor; if every frame looks
    like speech, fall back to the fixed minimum rather than a speech-inflated cutoff.
    """
    ambient_cap = float(SILENCE_THRESHOLD) * 1.8
    ambient = [r for r in calib_rms if r <= ambient_cap]
    if ambient:
        noise_floor = float(np.percentile(ambient, 25))
    else:
        noise_floor = float(SILENCE_THRESHOLD) / NOISE_MARGIN
    threshold = max(float(SILENCE_THRESHOLD), noise_floor * NOISE_MARGIN)
    return noise_floor, min(threshold, float(SILENCE_CEILING))


def _frame_is_speech(rms: float, threshold: float, noise_floor: float,
                     speech_detected: bool) -> bool:
    """True when this mic frame should count as the user still talking."""
    soft_speech = (
        rms >= SOFT_SPEECH_MIN_RMS
        and rms >= threshold * SOFT_SPEECH_RATIO
        and rms >= max(noise_floor * 1.25, SOFT_SPEECH_MIN_RMS)
    )
    if rms >= threshold or soft_speech:
        return True
    return speech_detected and rms >= threshold * SPEECH_HANGOVER_RATIO


def _get_model() -> WhisperModel:
    global _model
    if _model is None:
        model_ref = _bundled_model_path() or "base"
        print(f"[Whisper] Loading model from {model_ref}...", flush=True)
        _model = WhisperModel(model_ref, device="cpu", compute_type="int8")
        print("[Whisper] Model ready.", flush=True)
    return _model


def _record_until_silence() -> np.ndarray:
    """Open the default microphone and record until sustained silence (or timeout).

    Returns a float32 numpy array normalized to [-1, 1], or an empty array if
    no speech was detected within PRE_SPEECH_TIMEOUT seconds.
    Audio is processed entirely in RAM — never written to disk.
    """
    pa = pyaudio.PyAudio()
    # Open the stream INSIDE a guard so a failed device-open (no mic / device
    # busy) can't leak the PyAudio instance — otherwise pa.terminate() in the
    # finally below is never reached and a PortAudio handle is leaked per attempt.
    try:
        stream = pa.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=SAMPLE_RATE,
            input=True,
            frames_per_buffer=CHUNK_SIZE,
        )
    except Exception:
        pa.terminate()
        raise

    frames: list[np.ndarray] = []
    silent_chunks = 0
    speech_detected = False
    chunks_per_sec = SAMPLE_RATE / CHUNK_SIZE
    silence_limit = int(SILENCE_DURATION * chunks_per_sec)
    pre_speech_limit = int(PRE_SPEECH_TIMEOUT * chunks_per_sec)
    max_chunks = int(MAX_RECORD_SECS * chunks_per_sec)
    calib_chunks = max(2, int(NOISE_CALIBRATION_SECS * chunks_per_sec))

    def _rms(chunk: np.ndarray) -> float:
        return float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))

    # ── Calibrate the ambient noise floor from the first moments of audio, then
    #    set a threshold above it. Median is used so a stray word at the very
    #    start doesn't inflate the floor and break silence detection. ──────────
    threshold = float(SILENCE_THRESHOLD)
    noise_floor = 0.0
    calib_rms: list[float] = []
    max_rms = 0.0           # loudest frame seen — for diagnosing capture vs threshold
    cancelled = False

    _cancel_listen.clear()  # fresh recording — forget any earlier stop request
    _finish_listen.clear()  # fresh recording — forget any earlier finish request
    ptt = _ptt_mode.is_set()
    try:
        for i in range(max_chunks):
            if _cancel_listen.is_set():     # GUI "stop" pressed → drop this turn
                cancelled = True
                frames = []
                break
            if _finish_listen.is_set():     # ✓ / push-to-talk release → commit now
                break
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            chunk = np.frombuffer(data, dtype=np.int16)
            frames.append(chunk)

            rms = _rms(chunk)
            max_rms = max(max_rms, rms)

            if i < calib_chunks:
                calib_rms.append(rms)
                if i == calib_chunks - 1:
                    noise_floor, threshold = _calibrate_threshold(calib_rms)
                continue  # don't judge silence while still calibrating

            if _frame_is_speech(rms, threshold, noise_floor, speech_detected):
                speech_detected = True
                silent_chunks = 0
            elif ptt:
                # Push-to-talk: record continuously until the key is released; never
                # auto-stop on silence or the pre-speech timeout.
                pass
            elif speech_detected:
                silent_chunks += 1
                if silent_chunks >= silence_limit:
                    break
            elif i >= pre_speech_limit:
                # No speech detected within the timeout — abort
                frames = []
                break
    finally:
        _ptt_mode.clear()    # push-to-talk is per-turn; the next trigger re-sets it
        stream.stop_stream()
        stream.close()
        pa.terminate()

    if cancelled:
        print("[Whisper] Listening cancelled by user.", flush=True)
        return np.array([], dtype=np.int16)
    print(f"[Whisper] mic diag — loudest frame RMS={max_rms:.0f}, "
          f"silence threshold={threshold:.0f}, speech_detected={speech_detected}", flush=True)
    if not frames:
        return np.array([], dtype=np.int16)
    return np.concatenate(frames).astype(np.int16)


def _to_wav_bytes(audio_i16: np.ndarray) -> bytes:
    """Wrap raw int16 PCM as an in-memory WAV (for the Groq STT upload)."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)            # 16-bit
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio_i16.tobytes())
    return buf.getvalue()


def _do_transcribe(audio_i16: np.ndarray) -> str:
    """Local (offline) transcription with faster-whisper."""
    if audio_i16.size == 0:
        return ""
    model = _get_model()
    audio = audio_i16.astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio, language="en", beam_size=1)
    return " ".join(s.text.strip() for s in segments).strip()


async def preload() -> None:
    """Warm the local Whisper model so the offline fallback (used when Groq STT
    fails or when STT is set to 'local') is instant instead of stalling ~40s on
    first use. Runs in an executor; main() schedules this as a background task so
    it never blocks startup — including the wake-word loop."""
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _get_model)


async def transcribe() -> str:
    """Record from the microphone and return transcribed text.

    Blocks the executor (not the event loop) while recording and transcribing.
    Returns an empty string if no speech was detected.
    """
    loop = asyncio.get_running_loop()
    audio = await loop.run_in_executor(None, _record_until_silence)
    secs = audio.size / SAMPLE_RATE
    if secs == 0:
        print("[Whisper] No audio captured (mic returned silence or speech timed out).",
              flush=True)
        return ""
    else:
        print(f"[Whisper] Captured {secs:.1f}s of audio; transcribing…", flush=True)

    from llm.groq_bridge import get_stt, transcribe_audio
    if _stt_mode == "groq" and get_stt() == "groq":
        wav_bytes = _to_wav_bytes(audio)
        text = await transcribe_audio(wav_bytes)
        if text:
            return text
        if text is None:
            print("[Whisper] Groq transcription failed; falling back to local model.", flush=True)
        else:
            print("[Whisper] Groq transcription returned empty; falling back to local model.", flush=True)

    return await loop.run_in_executor(None, _do_transcribe, audio)
