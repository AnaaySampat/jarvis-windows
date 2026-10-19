"""The command after "Hey Jarvis", recorded from the wake word's own mic stream.

The old flow stopped the detector after a wake, waited 0.45 s for Windows to free the
mic, and opened a fresh recording, so "Hey Jarvis, open Spotify" said in one breath
lost its first words. Now the detector keeps the last 3 s it heard and, on a wake,
keeps reading the SAME stream until the command ends. One clip — the 3 s before the
wake, the wake itself and the command — is transcribed once, and that transcript is
also the wake check: the small wake model also fires on conversation that only
mentions Jarvis, so a command counts only when "Hey Jarvis" is in the words.

Port of the Android app's WakePreRoll.kt / wakeword.ts (tuned live on a phone,
2026-10-11). Pure numpy, no audio device, so it's unit-tested.
"""

from __future__ import annotations

import collections
import os
import re

import numpy as np

SAMPLE_RATE = 16_000
PRE_ROLL_S = 3.0


def _env_s(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


class PreRoll:
    """The last few seconds of int16 audio, oldest first."""

    def __init__(self, seconds: float = PRE_ROLL_S, rate: int = SAMPLE_RATE):
        self._chunks: "collections.deque[np.ndarray]" = collections.deque()
        self._size = 0
        self._cap = int(seconds * rate)

    def write(self, chunk: np.ndarray) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        while self._chunks and self._size - len(self._chunks[0]) >= self._cap:
            self._size -= len(self._chunks.popleft())

    def snapshot(self) -> np.ndarray:
        if not self._chunks:
            return np.zeros(0, dtype=np.int16)
        return np.concatenate(list(self._chunks))[-self._cap:]


class NoiseFloor:
    """Energy VAD against the room: the 30th-percentile chunk of the last ~4 s before
    the wake, held fixed while the command records so the command can't move it.
    Loud = `ratio`× that level. A tracked floor that drops at once falls to one quiet
    chunk, and the room then counts as speech: every capture ran to its ceiling."""

    def __init__(self, min_rms: float = 0.005, ratio: float = 2.0, window: int = 50):
        self.min_rms, self.ratio = min_rms, ratio
        self._recent: "collections.deque[float]" = collections.deque(maxlen=window)
        self._held = -1.0

    def level(self) -> float:
        if not self._recent:
            return 0.0
        s = sorted(self._recent)
        return s[(len(s) - 1) * 3 // 10]

    @property
    def floor(self) -> float:
        return self._held if self._held >= 0 else self.level()

    def hold(self) -> None:
        self._held = self.level()

    def release(self) -> None:
        self._held = -1.0

    def loud(self, chunk: np.ndarray) -> bool:
        x = chunk.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(x * x))) if len(x) else 0.0
        if self._held < 0 and rms > 0:        # exact zeros are the mic opening, not the room
            self._recent.append(rms)
        return rms > max(self.min_rms, self.floor * self.ratio)


class CommandCapture:
    """Seeded with the pre-roll; ends on a pause after speech, no speech at all, or a
    ceiling. The desktop allows a longer pause and command than the phone (1 s / 10 s):
    people dictate longer things to a PC. Both are environment-tunable."""

    # The model fires before "Jarvis" has ended; loud audio this soon is its tail. Longer
    # than the phone's 0.3 s so a tail can't count as a command and end the capture
    # during the pause people leave after "Hey Jarvis".
    GRACE_S = 0.5
    MIN_RUN = 2            # a lone loud 80 ms chunk is a room blip, not speech
    SILENCE_S = _env_s("JARVIS_WAKE_SILENCE_SECS", 1.5)
    NO_SPEECH_S = 4.0
    MAX_S = _env_s("JARVIS_WAKE_MAX_SECS", 15.0)

    def __init__(self, seed: np.ndarray, rate: int = SAMPLE_RATE):
        self._parts = [seed]
        self._rate = rate
        self._after = 0          # samples since the wake
        self._run = 0
        self._last_loud = 0.0
        self.speech = False      # did anything past the wake sound like speech?

    def add(self, chunk: np.ndarray, loud: bool) -> bool:
        """Add the next chunk; True once the command is over."""
        self._parts.append(chunk)
        self._after += len(chunk)
        t = self._after / self._rate
        self._run = self._run + 1 if loud else 0
        if self._run >= self.MIN_RUN and t > self.GRACE_S:
            self._last_loud = t
            self.speech = True
        if t >= self.MAX_S:
            return True
        return t - self._last_loud >= self.SILENCE_S if self.speech else t >= self.NO_SPEECH_S

    def audio(self) -> np.ndarray:
        return np.concatenate(self._parts).astype(np.int16)


# "Hey Jarvis" as Whisper writes it: a greeting, then the name ("Kay"/"A." for "Hey"
# and Jervis/Travis-style spellings were seen live). A bare "Jarvis" doesn't count —
# that's how conversation about Jarvis sounds.
_WAKE_PHRASE = re.compile(
    r"(?:\b(?:hey|hi|hay|hei|ay|eh|kay|ok(?:ay)?|hello|yo)\b[\s,.!-]*|\ba[.,!]\s*)"
    r"(?:j|ch|tr)[ae]r?v[aeiu]s\b", re.IGNORECASE)

# Whisper's stock output for near-silence: never a command.
_NOISE = {
    "", "you", "thank you", "thanks", "thank you for watching", "thanks for watching",
    "please subscribe", "bye", "bye bye", "goodbye", "so", "okay", "ok", "the",
    "i'm sorry", "silence", "music", "applause", "you're welcome", "see you next time",
}


def heard_wake_phrase(text: str) -> bool:
    return bool(_WAKE_PHRASE.search(text or ""))


def command_after_wake(text: str) -> "str | None":
    """What follows the LAST "Hey Jarvis" in a clip's transcript (the pre-roll can hold
    talk from before it); None when no wake phrase was heard — a false wake."""
    end = -1
    for m in _WAKE_PHRASE.finditer(text or ""):
        end = m.end()
    if end < 0:
        return None
    return re.sub(r"^[\s,.:;!?-]+", "", text[end:]).strip()


def is_noise(text: str) -> bool:
    norm = re.sub(r"\s+", " ", re.sub(r"[.!?,;:\"'”“\[\]]", "", (text or "").lower())).strip()
    return norm in _NOISE
