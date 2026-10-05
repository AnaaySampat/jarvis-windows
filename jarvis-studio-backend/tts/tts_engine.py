import asyncio
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Optional

_TAG_RE = re.compile(
    r"\[(CHART|SCHEDULE|FLOWCHART|TABLE|ACTION)\].*?\[/\1\]",
    re.DOTALL | re.IGNORECASE,
)

import jarvis_paths

# Backends that need a network connection (Piper + pyttsx3 are fully offline).
_ONLINE_BACKENDS = {"edge-tts", "elevenlabs", "chirp"}

# Voice defaults — chosen for a refined British "JARVIS" timbre.
_EDGE_VOICE = "en-GB-RyanNeural"             # Azure neural British male (free)
_PIPER_VOICE = "en_GB-alan-medium"           # offline neural British male
_ELEVEN_VOICE = "JBFqnCBsd6RMkjVDRZzb"       # ElevenLabs "George" — mature British male
_ELEVEN_MODEL = "eleven_flash_v2_5"          # lowest-latency streaming model
_CHIRP_VOICE = "en-GB-Chirp3-HD-Charon"      # Google Cloud TTS Chirp 3 HD British male


def _bundled_piper_dir():
    preload = os.environ.get("JARVIS_PRELOAD_ASSETS_DIR")
    candidates = []
    if preload:
        candidates.append(Path(preload) / "piper")
    candidates.append(jarvis_paths.bundle_root() / "preload-assets" / "piper")
    for path in candidates:
        if path.is_dir():
            return path
    return None


def piper_voice_present(name: str = _PIPER_VOICE) -> bool:
    """True if the default Piper voice is already on disk (bundled or downloaded)
    — so first-run provisioning can skip the ~60MB fetch. Never raises."""
    try:
        bundled = _bundled_piper_dir()
        if bundled is not None and any(bundled.glob(f"{name}.onnx*")):
            return True
        import storage
        return (storage.get_root() / "tts" / "piper" / f"{name}.onnx").exists()
    except Exception:  # noqa: BLE001
        return False


def ensure_piper_voice(name: str = _PIPER_VOICE) -> "tuple[bool, str]":
    """Download the default Piper voice if missing (first-run provisioning). Copies
    a bundled voice in when present, else fetches it. Returns (ok, error); does NOT
    load the model into memory (that's TTSEngine.preload's job). Never raises."""
    try:
        import storage
        vdir = storage.get_root() / "tts" / "piper"
        vdir.mkdir(parents=True, exist_ok=True)
        model = vdir / f"{name}.onnx"
        bundled = _bundled_piper_dir()
        if bundled is not None:
            for src in bundled.glob(f"{name}.onnx*"):
                dst = vdir / src.name
                if not dst.exists():
                    shutil.copy2(src, dst)
        if not model.exists():
            from piper.download_voices import download_voice
            download_voice(name, vdir)
        return (True, "") if model.exists() else (False, "voice file missing after download")
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _strip_tags(text: str) -> str:
    text = re.sub(_TAG_RE, "", text)
    return re.sub(r"\s+", " ", text).strip()


# Cache the connectivity probe briefly. speak() runs once PER SENTENCE, so a
# multi-sentence reply previously paid this blocking socket connect every
# sentence; within a single reply the answer never changes. A short TTL keeps it
# responsive to the network actually dropping mid-session.
_NET_TTL_S = 20.0
_net_ok: "bool | None" = None
_net_ts: float = 0.0


def _has_internet() -> bool:
    global _net_ok, _net_ts
    now = time.monotonic()
    if _net_ok is not None and (now - _net_ts) < _NET_TTL_S:
        return _net_ok
    try:
        socket.create_connection(("8.8.8.8", 53), timeout=3).close()
        _net_ok = True
    except OSError:
        _net_ok = False
    _net_ts = now
    return _net_ok


# One shared async HTTP client for the cloud TTS backends (Chirp / ElevenLabs).
# speak() is called once per sentence, so opening a fresh AsyncClient each time
# meant a new DNS + TLS handshake to the TTS endpoint for EVERY sentence. Reusing
# one keep-alive connection removes that per-sentence handshake. Created lazily on
# the running loop; closed via aclose_tts_http() on shutdown.
_http_client = None  # type: ignore[var-annotated]


def _http() -> "object":
    global _http_client
    import httpx
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=30)
    return _http_client


async def aclose_tts_http() -> None:
    """Close the shared TTS HTTP client (call on backend shutdown)."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def _load_cfg() -> dict:
    return jarvis_paths.read_config()


class TTSEngine:
    """Text-to-speech with pluggable backends.

    Backends (set via the 'tts' preference in config.json, switchable at runtime):
        elevenlabs  — premium cloud voice (best quality; needs an API key + net)
        chirp       — Google Cloud TTS Chirp 3 HD (premium British; on GCP credits via ADC)
        piper       — offline neural voice (free, unlimited, private) ← daily driver
        edge-tts    — free Azure neural voice (British male; needs net)
        pyttsx3     — fully offline OS voice (robotic; last-resort fallback)
        off         — speech disabled (text still shows)

    speak() tries the selected backend and degrades gracefully: a cloud backend
    that fails or is offline falls back to Piper (if available) then pyttsx3, so
    JARVIS always says *something*. Strips [CHART]/[ACTION]/etc. blocks first.
    """

    def __init__(self) -> None:
        cfg = _load_cfg()
        pref = cfg.get("tts", "piper")
        self._enabled = pref != "off"
        self._backend = pref if pref != "off" else "off"
        self._stopped = False
        # Circuit breaker: set once ElevenLabs returns a quota/credits/auth error,
        # so later sentences skip it entirely instead of re-hitting a doomed 401
        # (with its network round-trip + a log line) on EVERY sentence.
        self._eleven_blocked = False
        # Voice/model overrides (optional, from config.json).
        self._piper_name = cfg.get("piper_voice") or _PIPER_VOICE
        self._eleven_voice = cfg.get("elevenlabs_voice") or _ELEVEN_VOICE
        self._eleven_model = cfg.get("elevenlabs_model") or _ELEVEN_MODEL
        self._chirp_voice = cfg.get("chirp_voice") or _CHIRP_VOICE
        self._piper_voice = None        # lazily-loaded PiperVoice (cached)
        # Guards the one-time Piper model load so a preload() racing the first
        # speak() (e.g. the startup greeting) can't load the ONNX model twice.
        self._piper_lock = threading.Lock()

        self._pyttsx3 = None
        try:
            import pyttsx3
            self._pyttsx3 = pyttsx3.init()
        except Exception as exc:  # noqa: BLE001
            print(f"[TTS] pyttsx3 unavailable: {exc}", flush=True)

        self._init_mixer()
        print(f"[TTS] Backend: {self._backend}", flush=True)

    # ── setup helpers ─────────────────────────────────────────────────────────
    def _init_mixer(self) -> None:
        """pygame mixer plays the audio for edge-tts / Piper / ElevenLabs."""
        try:
            import pygame
            if not pygame.mixer.get_init():
                pygame.mixer.init()
        except Exception as exc:  # noqa: BLE001
            print(f"[TTS] pygame init failed: {exc}", flush=True)

    def set_backend(self, pref: str) -> None:
        """Switch TTS backend at runtime."""
        self._enabled = pref != "off"
        self._backend = pref if pref != "off" else "off"
        # Pick up any voice override the user changed alongside the backend.
        cfg = _load_cfg()
        self._piper_name = cfg.get("piper_voice") or _PIPER_VOICE
        self._eleven_voice = cfg.get("elevenlabs_voice") or _ELEVEN_VOICE
        self._chirp_voice = cfg.get("chirp_voice") or _CHIRP_VOICE
        if self._piper_name != getattr(self, "_loaded_piper_name", None):
            self._piper_voice = None     # force reload if the voice changed
        if self._backend in ("edge-tts", "elevenlabs", "piper", "chirp"):
            self._init_mixer()
        print(f"[TTS] Backend switched to: {pref}", flush=True)

    def stop(self) -> None:
        """Interrupt any active playback immediately."""
        self._stopped = True
        try:
            import pygame
            if pygame.mixer.get_init():
                pygame.mixer.music.stop()
        except Exception:  # noqa: BLE001
            pass
        if self._pyttsx3 is not None:
            try:
                self._pyttsx3.stop()
            except Exception:  # noqa: BLE001
                pass

    # ── main entry point ──────────────────────────────────────────────────────
    async def speak(self, text: str) -> None:
        self._stopped = False
        if not getattr(self, "_enabled", True):
            return
        clean = _strip_tags(text)
        if not clean:
            return

        backend = self._backend
        # Online backends need a connection; if offline, skip straight to Piper.
        # Probe off the event loop — the socket connect blocks up to 3s.
        if backend in _ONLINE_BACKENDS:
            loop = asyncio.get_running_loop()
            if not await loop.run_in_executor(None, _has_internet):
                print(f"[TTS] offline — {backend} unavailable, using Piper.", flush=True)
                backend = "piper"
        # Tripped circuit breaker: don't even try ElevenLabs again this session —
        # go straight to the offline neural voice (which the fallback chain below
        # would land on anyway, just without the wasted 401 round-trip per line).
        if backend == "elevenlabs" and self._eleven_blocked:
            backend = "piper"

        try:
            if backend == "elevenlabs":
                await self._speak_eleven(clean)
                return
            if backend == "chirp":
                await self._speak_chirp(clean)
                return
            if backend == "piper":
                await self._speak_piper(clean)
                return
            if backend == "edge-tts":
                await self._speak_edge(clean)
                return
        except Exception as exc:  # noqa: BLE001
            print(f"[TTS] {backend} failed ({exc}); falling back.", flush=True)
            # Cloud/edge failure → try the offline neural voice before the robot.
            if backend != "piper":
                try:
                    await self._speak_piper(clean)
                    return
                except Exception as exc2:  # noqa: BLE001
                    print(f"[TTS] Piper fallback failed ({exc2}).", flush=True)

        if self._pyttsx3 is not None:
            await self._speak_pyttsx3(clean)
        else:
            print("[TTS] No TTS backend available — skipping speech.", flush=True)

    # ── shared playback ───────────────────────────────────────────────────────
    async def _play_file(self, path: str) -> None:
        """Play an audio file (mp3/wav) via pygame, then delete it."""
        import pygame
        try:
            loop = asyncio.get_running_loop()

            def _play() -> None:
                if self._stopped:
                    return
                if not pygame.mixer.get_init():
                    pygame.mixer.init()
                pygame.mixer.music.load(path)
                pygame.mixer.music.play()
                while pygame.mixer.music.get_busy():
                    pygame.time.wait(50)
                pygame.mixer.music.unload()      # release the lock before deleting

            await loop.run_in_executor(None, _play)
        finally:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except Exception:  # noqa: BLE001
                    pass

    # ── ElevenLabs (premium cloud) ────────────────────────────────────────────
    async def _speak_eleven(self, text: str) -> None:
        import app_secrets
        key = app_secrets.get("elevenlabs_api_key")
        if not key:
            raise RuntimeError("no ElevenLabs API key configured")
        url = (f"https://api.elevenlabs.io/v1/text-to-speech/{self._eleven_voice}"
               f"/stream?output_format=mp3_44100_128")
        payload = {
            "text": text,
            "model_id": self._eleven_model,
            "voice_settings": {"stability": 0.4, "similarity_boost": 0.75, "style": 0.0},
        }
        audio = bytearray()
        async with _http().stream("POST", url, json=payload,
                                  headers={"xi-api-key": key,
                                           "Content-Type": "application/json"}) as resp:
            if resp.status_code != 200:
                body = (await resp.aread())[:200]
                # A quota/credits/auth failure won't clear within the session —
                # trip the breaker so we stop retrying ElevenLabs on every line.
                low = bytes(body).lower()
                if (resp.status_code in (401, 403, 429)
                        or b"quota" in low or b"credit" in low):
                    if not self._eleven_blocked:
                        print("[TTS] ElevenLabs unavailable (quota/auth) — switching "
                              "to the offline voice for the rest of the session.",
                              flush=True)
                    self._eleven_blocked = True
                raise RuntimeError(f"ElevenLabs HTTP {resp.status_code}: {body!r}")
            async for chunk in resp.aiter_bytes():
                if self._stopped:
                    return
                audio.extend(chunk)
        if self._stopped or not audio:
            return
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(bytes(audio))
        await self._play_file(path)

    # ── Google Cloud TTS — Chirp 3 HD (premium, on GCP credits via ADC) ────────
    async def _speak_chirp(self, text: str) -> None:
        import base64
        from llm import vertex_auth
        if not vertex_auth.enabled():
            raise RuntimeError("Chirp TTS needs Vertex/ADC (gcloud auth application-default login)")
        token = await vertex_auth.get_access_token_async()
        project = vertex_auth.project()
        voice = self._chirp_voice
        # "en-GB-Chirp3-HD-Charon" → languageCode "en-GB".
        lang = "-".join(voice.split("-")[:2])
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        if project:
            headers["x-goog-user-project"] = project     # route quota/billing to the project
        body = {
            "input": {"text": text},
            "voice": {"languageCode": lang, "name": voice},
            "audioConfig": {"audioEncoding": "MP3"},
        }
        resp = await _http().post(
            "https://texttospeech.googleapis.com/v1/text:synthesize",
            headers=headers, json=body)
        if resp.status_code != 200:
            raise RuntimeError(f"Cloud TTS HTTP {resp.status_code}: {resp.text[:200]}")
        audio_b64 = resp.json().get("audioContent")
        if not audio_b64:
            raise RuntimeError("Cloud TTS returned no audio")
        audio = base64.b64decode(audio_b64)
        if self._stopped or not audio:
            return
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(audio)
        await self._play_file(path)

    # ── Piper (offline neural) ────────────────────────────────────────────────
    def _ensure_piper(self):
        """Load (downloading on first use) the Piper voice. Blocking — call in an
        executor. Cached across calls; the lock makes a preload()/first-speak race
        load the model exactly once."""
        if self._piper_voice is not None:
            return self._piper_voice
        with self._piper_lock:
            if self._piper_voice is not None:    # another thread won the race
                return self._piper_voice
            import storage
            from piper import PiperVoice
            from piper.download_voices import download_voice
            name = self._piper_name
            vdir = storage.get_root() / "tts" / "piper"
            vdir.mkdir(parents=True, exist_ok=True)
            model = vdir / f"{name}.onnx"
            bundled = _bundled_piper_dir()
            if bundled is not None:
                for src in bundled.glob(f"{name}.onnx*"):
                    dst = vdir / src.name
                    if not dst.exists():
                        try:
                            shutil.copy2(src, dst)
                        except Exception as exc:  # noqa: BLE001
                            print(f"[TTS] Couldn't copy bundled Piper asset {src.name}: {exc}", flush=True)
            if not model.exists():
                print(f"[TTS] Downloading Piper voice '{name}' (one-time)…", flush=True)
                download_voice(name, vdir)
            voice = PiperVoice.load(str(model))
            self._loaded_piper_name = name
            self._piper_voice = voice            # publish only once fully loaded
        return self._piper_voice

    async def preload(self) -> None:
        """Warm up the selected backend so the FIRST spoken reply has no cold start.

        Piper (the default) loads a neural ONNX model + onnxruntime session on first
        use — ~1-2s — which previously stalled the startup greeting / first wake-word
        reply. Loading it here (off the event loop, at boot) makes that first reply
        speak immediately. A no-op for backends that don't need a local model."""
        if self._backend != "piper":
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._ensure_piper)
            print("[TTS] Piper voice preloaded.", flush=True)
        except Exception as exc:  # noqa: BLE001 — fall back to lazy load on first speak
            print(f"[TTS] Piper preload failed (will load on first use): {exc}", flush=True)

    async def _speak_piper(self, text: str) -> None:
        import wave
        loop = asyncio.get_running_loop()

        def _synth() -> str:
            voice = self._ensure_piper()
            fd, path = tempfile.mkstemp(suffix=".wav")
            os.close(fd)
            with wave.open(path, "wb") as w:
                voice.synthesize_wav(text, w)
            return path

        path = await loop.run_in_executor(None, _synth)
        if self._stopped:
            if os.path.exists(path):
                os.unlink(path)
            return
        await self._play_file(path)

    # ── edge-tts (free cloud, British male) ───────────────────────────────────
    async def _speak_edge(self, text: str) -> None:
        import edge_tts
        communicate = edge_tts.Communicate(text, voice=_EDGE_VOICE)
        audio = bytearray()
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio.extend(chunk["data"])
        if self._stopped or not audio:
            return
        fd, path = tempfile.mkstemp(suffix=".mp3")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(bytes(audio))
        await self._play_file(path)

    # ── pyttsx3 (offline OS voice, last resort) ───────────────────────────────
    async def _speak_pyttsx3(self, text: str) -> None:
        if self._pyttsx3 is None:
            return
        loop = asyncio.get_running_loop()

        def _run() -> None:
            self._pyttsx3.say(text)
            self._pyttsx3.runAndWait()

        await loop.run_in_executor(None, _run)
