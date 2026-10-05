"""Gemini Live API — native-audio voice sessions.

This is the full backend for the "Gemini 2.5 Native Audio" model. Unlike the
normal pipeline (Whisper → LLM → TTS), the Live API is **full-duplex**: we stream
microphone PCM straight to Google over a WebSocket and play back the model's own
synthesised voice as it arrives, with built-in voice-activity detection and
barge-in (the model stops talking the moment you start). We also ask for input +
output transcripts so the HUD's conversation log still fills in.

Audio formats (fixed by the Live API):
  • microphone → 16 kHz, mono, signed 16-bit little-endian PCM
  • model      → 24 kHz, mono, signed 16-bit little-endian PCM

Everything here is best-effort and dependency-guarded: if ``websockets`` or
``pyaudio`` aren't importable, :func:`available` returns False and the caller
falls back to the text model + normal TTS.

Tool use (the reason native-audio JARVIS can actually *act*): we advertise
``functionDeclarations`` in the setup frame. When the model wants to do something
the server sends a ``toolCall``; we run it through the shared :mod:`actions`
framework and reply with a ``toolResponse``. The model's spoken confirmation is
then grounded in the real result instead of being made up.

Protocol (BidiGenerateContent, v1beta):
  client → {"setup": {..., "tools": [{"functionDeclarations": [...]}]}}
  server → {"setupComplete": {}}
  client → {"realtimeInput": {"mediaChunks": [{mimeType, data}]}}   (mic frames)
  server → {"serverContent": {"modelTurn": {"parts": [{inlineData}]},
                              "inputTranscription": {...},
                              "outputTranscription": {...},
                              "turnComplete": bool, "interrupted": bool}}
  server → {"toolCall": {"functionCalls": [{id, name, args}]}}      (wants to act)
  client → {"toolResponse": {"functionResponses": [{id, name, response}]}}
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from typing import Awaitable, Callable, Optional

from . import vertex_auth

try:                       # both are already app dependencies, but guard anyway
    import websockets
    import pyaudio
    _DEPS_OK = True
except Exception:          # noqa: BLE001
    websockets = None      # type: ignore
    pyaudio = None         # type: ignore
    _DEPS_OK = False

# WebSocket endpoint for the bidirectional Live API (Developer API, ?key= auth).
_WS_URL = ("wss://generativelanguage.googleapis.com/ws/"
           "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent")

# Vertex AI Live API endpoint template (Bearer/ADC auth). The model in the setup
# frame must be the full project/location-scoped publisher path (see _connection).
# {region} is filled in per session; the service version (v1 vs v1beta1) is chosen
# by _VERTEX_WS_VERSION so it can be flipped if Google moves the service.
_VERTEX_WS_VERSION = "v1beta1"
_VERTEX_WS_URL = ("wss://{region}-aiplatform.googleapis.com/ws/"
                  "google.cloud.aiplatform." + _VERTEX_WS_VERSION +
                  ".LlmBidiService/BidiGenerateContent")

# Audio constants dictated by the Live API.
_MIC_RATE = 16_000
_OUT_RATE = 24_000
_MIC_CHUNK = 1_600          # 100 ms of 16 kHz mono int16
_OUT_CHUNK = 2_400          # 100 ms of 24 kHz mono int16

# Session lifecycle guards (so a session can't hang forever).
_INACTIVITY_TIMEOUT = 12.0  # close after this many seconds with no model audio + no speech
_MAX_SESSION = 180.0        # hard cap on a single voice session
# Half-duplex echo guard: keep the mic muted to the model for this long after the
# last audio we played, so the tail of JARVIS's own voice (sitting in the speaker
# buffer) isn't captured and fed back. See _send_loop.
_ECHO_GUARD_TAIL_S = 0.7
# Local RMS gate — mark the session active while the user is audibly speaking so
# the idle timer doesn't end the session before Google sends inputTranscription.
_MIC_SPEECH_RMS = 250

# Default prebuilt voice. Any valid Live API voice name works.
_DEFAULT_VOICE = "Charon"

EventCb = Callable[[str, object], Awaitable[None]]
# Given a list of function calls [{id, name, args}], returns a matching list of
# function responses [{id, name, response}].
ToolCb = Callable[[list], Awaitable[list]]


def available() -> bool:
    """True when the native-audio session can actually run on this machine."""
    return _DEPS_OK


_SENTENCE_RE = re.compile(r"[^.!?]*[.!?]+|\S[^.!?]*$")


def _dedupe(text: str) -> str:
    """Tidy an output transcript: insert a missing space after sentence-ending
    punctuation that got glued to the next word ("open.and" → "open. and"), and
    drop a sentence that exactly repeats the one before it. The Live API
    occasionally re-sends the trailing transcription segment, which otherwise
    shows up as a doubled tail in the chat."""
    if not text:
        return text
    fixed = re.sub(r"([.!?])([A-Za-z])", r"\1 \2", text)
    out: "list[str]" = []
    prev = ""
    for m in _SENTENCE_RE.finditer(fixed):
        s = m.group(0).strip()
        if not s:
            continue
        norm = s.lower()
        if norm == prev:                       # exact repeat of the last sentence
            continue
        out.append(s)
        prev = norm
    return " ".join(out).strip()


def _auth_url(api_key: str) -> str:
    return f"{_WS_URL}?key={api_key.strip()}"


def _setup_message(model_field: str, system_text: str, voice: str = _DEFAULT_VOICE,
                   tools: "Optional[list]" = None) -> dict:
    """The initial setup frame. Asks for AUDIO out plus both transcripts, and
    advertises our function tools so the model can actually perform actions.

    ``model_field`` is the already-formatted model identifier for the active
    endpoint: ``models/<id>`` for the Developer API, or the full
    ``projects/.../locations/.../publishers/google/models/<id>`` path for Vertex
    (built in :meth:`LiveAudioSession._connection`)."""
    setup = {
        "model": model_field,
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}
            },
            "temperature": 0.7,
        },
        "systemInstruction": {"parts": [{"text": system_text}]},
        "inputAudioTranscription": {},
        "outputAudioTranscription": {},
    }
    if tools:
        setup["tools"] = [{"functionDeclarations": tools}]
    # Default server VAD ends turns on brief pauses; bias toward waiting longer so
    # mid-sentence gaps don't cut the user off.
    setup["realtimeInputConfig"] = {
        "automaticActivityDetection": {
            "endOfSpeechSensitivity": "END_SENSITIVITY_LOW",
            "silenceDurationMs": 2500,
        }
    }
    return {"setup": setup}


class LiveAudioSession:
    """One full-duplex voice session against the Gemini Live API.

    Use :meth:`run` to drive a complete conversation turn (or a short back-and-
    forth) and return. The session ends on: inactivity after the model finishes
    speaking, the hard time cap, the caller's interrupt, or a socket error.
    """

    def __init__(self, api_key: str, model: str, system_text: str,
                 on_event: Optional[EventCb] = None,
                 interrupted: Optional[Callable[[], bool]] = None,
                 muted: Optional[Callable[[], bool]] = None,
                 voice: str = _DEFAULT_VOICE,
                 tools: "Optional[list]" = None,
                 on_tool_call: "Optional[ToolCb]" = None,
                 history: "Optional[list]" = None) -> None:
        self._api_key = api_key
        self._model = model
        self._system_text = system_text
        self._on_event = on_event
        self._interrupted = interrupted or (lambda: False)
        self._muted = muted or (lambda: False)
        self._voice = voice
        self._tools = tools
        self._on_tool_call = on_tool_call
        self._history = history or []

        self._pa = None
        self._mic = None
        self._spk = None
        # Per-turn transcript accumulators (reset at each turnComplete so every
        # turn renders as its own chat bubble instead of one ever-growing blob).
        self.user_text = ""
        self.model_text = ""
        # The most recently completed turn — returned to the caller.
        self._last_user = ""
        self._last_model = ""
        # In-flight tool-call tasks, so we can cancel them on teardown / cancellation.
        self._tool_tasks: "set[asyncio.Task]" = set()
        # Coordination between the receive loop and the rest of the session.
        self._last_activity = time.monotonic()
        self._model_speaking = False
        # monotonic time of the most recent model-audio frame we PLAYED — drives
        # the half-duplex echo guard's tail after the model stops talking.
        self._spoke_at = 0.0
        self._stop = asyncio.Event()

    # ── event helper ──────────────────────────────────────────────────────────
    async def _emit(self, kind: str, payload: object) -> None:
        if self._on_event is not None:
            try:
                await self._on_event(kind, payload)
            except Exception:  # noqa: BLE001
                pass

    # ── audio device setup / teardown ─────────────────────────────────────────
    def _open_audio(self) -> None:
        self._pa = pyaudio.PyAudio()
        self._mic = self._pa.open(format=pyaudio.paInt16, channels=1,
                                  rate=_MIC_RATE, input=True,
                                  frames_per_buffer=_MIC_CHUNK)
        self._spk = self._pa.open(format=pyaudio.paInt16, channels=1,
                                  rate=_OUT_RATE, output=True,
                                  frames_per_buffer=_OUT_CHUNK)

    def _close_audio(self) -> None:
        for dev in (self._mic, self._spk):
            try:
                if dev is not None:
                    dev.stop_stream()
                    dev.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._pa is not None:
                self._pa.terminate()
        except Exception:  # noqa: BLE001
            pass
        self._mic = self._spk = self._pa = None

    # ── Guarded device I/O (run via executor; may race with _close_audio) ──────
    # The mic read / speaker write are dispatched to executor threads; teardown
    # can null/close the streams while one is in flight. Snapshotting the handle
    # and catching everything turns a potential PortAudio use-after-close into a
    # benign no-op instead of a native crash.
    def _mic_read(self) -> bytes:
        m = self._mic
        if m is None:
            return b""
        try:
            return m.read(_MIC_CHUNK, exception_on_overflow=False)
        except Exception:  # noqa: BLE001
            return b""

    @staticmethod
    def _pcm_rms(data: bytes) -> float:
        if len(data) < 2:
            return 0.0
        n = len(data) // 2
        total = 0
        for i in range(0, n * 2, 2):
            sample = int.from_bytes(data[i:i + 2], "little", signed=True)
            total += sample * sample
        return (total / n) ** 0.5

    def _spk_write(self, pcm: bytes) -> None:
        s = self._spk
        if s is None:
            return
        try:
            s.write(pcm)
        except Exception:  # noqa: BLE001
            pass

    def _flush_speaker(self) -> None:
        """Drop buffered output audio so the model stops talking immediately on
        barge-in, instead of finishing its already-queued sentence."""
        s = self._spk
        if s is None:
            return
        try:
            s.stop_stream()
            s.start_stream()
        except Exception:  # noqa: BLE001
            pass

    # ── endpoint / auth selection ─────────────────────────────────────────────
    async def _connection(self) -> "tuple[str, dict, str]":
        """(ws_url, headers, model_field) for the active credential.

        Vertex AI when ADC is configured — Bearer auth on a region-scoped
        WebSocket, with the model as a full publisher-resource path so the session
        bills to the user's Google Cloud project/credits. Otherwise the Developer
        API (``?key=`` in the URL, ``models/<id>``)."""
        if vertex_auth.enabled():
            token = await vertex_auth.get_access_token_async()
            region = vertex_auth.region()
            project = vertex_auth.project()
            url = _VERTEX_WS_URL.format(region=region)
            headers = {"Authorization": f"Bearer {token}"}
            model_field = (f"projects/{project}/locations/{region}"
                           f"/publishers/google/models/{self._model}")
            return url, headers, model_field
        model = self._model if self._model.startswith("models/") else f"models/{self._model}"
        return _auth_url(self._api_key), {}, model

    # ── public entry point ────────────────────────────────────────────────────
    async def run(self) -> "tuple[str, str]":
        """Run the session to completion. Returns (user_text, model_text)."""
        if not available():
            await self._emit("error", "Native audio needs the websockets + pyaudio packages.")
            return "", ""
        if not self._api_key and not vertex_auth.enabled():
            await self._emit("error", "Native audio needs a Gemini API key or Vertex (gcloud ADC).")
            return "", ""
        try:
            url, headers, model_field = await self._connection()
            async with websockets.connect(
                url, additional_headers=headers, max_size=None, ping_interval=20,
            ) as ws:
                await ws.send(json.dumps(_setup_message(
                    model_field, self._system_text, self._voice, self._tools)))
                await self._await_setup(ws)
                await self._seed_history(ws)
                await self._emit("status", "listening")
                self._open_audio()
                send_task = asyncio.create_task(self._send_loop(ws))
                recv_task = asyncio.create_task(self._recv_loop(ws))
                idle_task = asyncio.create_task(self._idle_watch())
                done, pending = await asyncio.wait(
                    {send_task, recv_task, idle_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    with _suppress():
                        await t
        except Exception as exc:  # noqa: BLE001
            print(f"[LiveAudio] session error: {exc}", flush=True)
            await self._emit("error", "I lost the live-audio connection. Try again, sir.")
        finally:
            self._cancel_tool_tasks()
            self._close_audio()
        # Return the last *completed* turn; if the session ended mid-turn, fall
        # back to whatever has accumulated so the caller can still finalize it.
        return (self._last_user or self.user_text.strip(),
                self._last_model or _dedupe(self.model_text))

    async def _await_setup(self, ws) -> None:
        """Wait for setupComplete (with a short timeout)."""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=15)
            msg = json.loads(raw) if isinstance(raw, (str, bytes)) else {}
            if "setupComplete" not in msg:
                print(f"[LiveAudio] unexpected setup reply: {str(msg)[:200]}", flush=True)
        except asyncio.TimeoutError:
            raise RuntimeError("setup timed out")

    async def _seed_history(self, ws) -> None:
        """Replay this chat into the Live session so a mid-chat model switch
        (text → native audio) still knows what already happened.

        ``turnComplete`` stays false so the model does not answer the recap."""
        turns = []
        for m in self._history[-40:]:
            role = m.get("role")
            if role not in ("user", "assistant"):
                continue
            text = (m.get("content") or "").strip()
            if not text:
                continue
            if len(text) > 800:
                text = text[:800].rstrip() + " …"
            g_role = "model" if role == "assistant" else "user"
            if turns and turns[-1]["role"] == g_role:
                turns[-1]["parts"][0]["text"] += "\n" + text
            else:
                turns.append({"role": g_role, "parts": [{"text": text}]})
        if turns and turns[0]["role"] == "model":
            turns.insert(0, {"role": "user", "parts": [{"text": "(earlier context)"}]})
        if not turns:
            return
        try:
            await ws.send(json.dumps({
                "clientContent": {"turns": turns, "turnComplete": False},
            }))
        except Exception as exc:  # noqa: BLE001
            print(f"[LiveAudio] history seed failed: {exc}", flush=True)

    # ── microphone → server ───────────────────────────────────────────────────
    async def _send_loop(self, ws) -> None:
        """Read mic frames and stream them to the model as PCM chunks."""
        loop = asyncio.get_running_loop()
        while not self._should_stop():
            data = await loop.run_in_executor(None, self._mic_read)
            if not data:                  # teardown / read error → end the loop
                break
            # ── Half-duplex echo guard ──────────────────────────────────────
            # We have no acoustic echo cancellation, so while JARVIS is speaking
            # (and for a short tail after) its own voice leaks from the speakers
            # into the mic. Streamed on, the Live API transcribes that as the user
            # and answers it — an endless "you said X" → "X" → … loop where the
            # model repeats its own last line back. So we simply stop sending mic
            # audio while we're the one talking. We keep READING the mic above so
            # PortAudio's buffer can't overflow; we just don't forward it. (Muted =
            # nothing played = nothing to echo, so keep listening; the Stop button
            # still interrupts a reply.)
            if not self._muted() and (
                    self._model_speaking
                    or (time.monotonic() - self._spoke_at) < _ECHO_GUARD_TAIL_S):
                continue
            if self._pcm_rms(data) >= _MIC_SPEECH_RMS:
                self._mark_active()
            b64 = base64.b64encode(data).decode("ascii")
            frame = {"realtimeInput": {"mediaChunks": [
                {"mimeType": f"audio/pcm;rate={_MIC_RATE}", "data": b64}]}}
            try:
                await ws.send(json.dumps(frame))
            except Exception:  # noqa: BLE001
                break
        self._stop.set()

    # ── server → speaker + transcripts + tool calls ───────────────────────────
    async def _recv_loop(self, ws) -> None:
        loop = asyncio.get_running_loop()
        async for raw in ws:
            if self._should_stop():
                break
            try:
                msg = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue

            # ── Tool call: the model wants to perform a real action. We run it
            #    through the actions framework and reply with a toolResponse so
            #    its spoken confirmation is grounded in the actual result. ──────
            tc = msg.get("toolCall") or msg.get("tool_call")
            if tc:
                self._mark_active()
                self._dispatch_tool_call(ws, tc)
                continue
            if msg.get("toolCallCancellation") or msg.get("tool_call_cancellation"):
                self._cancel_tool_tasks()
                continue

            sc = msg.get("serverContent") or msg.get("server_content") or {}

            # Barge-in: the user started talking over the model. Flush the speaker
            # so already-buffered audio stops immediately rather than playing on.
            if sc.get("interrupted"):
                self._model_speaking = False
                await loop.run_in_executor(None, self._flush_speaker)
                await self._emit("status", "listening")

            # Audio out + assistant transcript.
            model_turn = sc.get("modelTurn") or sc.get("model_turn") or {}
            for part in (model_turn.get("parts") or []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    self._mark_active()
                    if not self._model_speaking:
                        self._model_speaking = True
                        await self._emit("status", "speaking")
                    if not self._muted():
                        pcm = base64.b64decode(inline["data"])
                        # Stamp BEFORE the (blocking) write so the echo guard's
                        # tail is measured from real playback, covering buffered
                        # audio that keeps sounding after the write returns.
                        self._spoke_at = time.monotonic()
                        await loop.run_in_executor(None, self._spk_write, pcm)

            out_tx = (sc.get("outputTranscription") or sc.get("output_transcription") or {}).get("text")
            if out_tx:
                self.model_text += out_tx
                await self._emit("model_partial", _dedupe(self.model_text))
            in_tx = (sc.get("inputTranscription") or sc.get("input_transcription") or {}).get("text")
            if in_tx:
                self.user_text += in_tx
                self._mark_active()
                await self._emit("user_partial", self.user_text.strip())

            if sc.get("turnComplete") or sc.get("turn_complete"):
                self._model_speaking = False
                self._mark_active()
                await self._finish_turn()
                await self._emit("status", "listening")
        self._stop.set()

    async def _finish_turn(self) -> None:
        """Finalize the current turn: emit it as a completed exchange and reset
        the per-turn accumulators so the next turn starts a fresh bubble."""
        user = self.user_text.strip()
        model = _dedupe(self.model_text)
        if user or model:
            self._last_user, self._last_model = user, model
            await self._emit("turn_complete", {"user": user, "model": model})
        self.user_text = ""
        self.model_text = ""

    # ── tool calls ─────────────────────────────────────────────────────────────
    def _dispatch_tool_call(self, ws, tc: dict) -> None:
        """Run a toolCall off the recv loop so audio keeps flowing while the
        action executes (and a permission dialog can block without stalling us)."""
        calls = tc.get("functionCalls") or tc.get("function_calls") or []
        if not calls:
            return
        task = asyncio.create_task(self._run_tool_calls(ws, calls))
        self._tool_tasks.add(task)
        task.add_done_callback(self._tool_tasks.discard)

    async def _run_tool_calls(self, ws, calls: list) -> None:
        if self._on_tool_call is not None:
            try:
                responses = await self._on_tool_call(calls)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                print(f"[LiveAudio] tool handler error: {exc}", flush=True)
                responses = [{"id": c.get("id"), "name": c.get("name"),
                              "response": {"error": "I couldn't complete that action."}}
                             for c in calls]
        else:
            responses = [{"id": c.get("id"), "name": c.get("name"),
                          "response": {"error": "No tool handler configured."}}
                         for c in calls]
        # Always answer, or the model hangs waiting for the tool result.
        self._mark_active()
        try:
            await ws.send(json.dumps({"toolResponse": {"functionResponses": responses}}))
        except Exception:  # noqa: BLE001
            pass

    def _cancel_tool_tasks(self) -> None:
        for t in list(self._tool_tasks):
            t.cancel()
        self._tool_tasks.clear()

    # ── lifecycle helpers ─────────────────────────────────────────────────────
    def _mark_active(self) -> None:
        self._last_activity = time.monotonic()

    def _should_stop(self) -> bool:
        return self._stop.is_set() or self._interrupted()

    async def _idle_watch(self) -> None:
        """End the session after a stretch of inactivity or the hard cap."""
        start = time.monotonic()
        while not self._should_stop():
            await asyncio.sleep(0.5)
            now = time.monotonic()
            # Don't time out while a tool call is running (it may be awaiting the
            # user's Approve/Deny, or a long autopilot task). Checked BEFORE the
            # hard cap so the cap can't cancel a tool mid-flight and silently
            # drop its result — the cap resumes once the tool finishes.
            if self._tool_tasks:
                self._mark_active()
                start = now            # extend the hard cap past the tool run
                continue
            if now - start > _MAX_SESSION:
                break
            # Only count idleness once the model isn't actively speaking.
            if not self._model_speaking and now - self._last_activity > _INACTIVITY_TIMEOUT:
                break
        self._stop.set()


class _suppress:
    """Tiny async-friendly suppressor for CancelledError on task await."""
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return exc_type is not None and issubclass(exc_type, asyncio.CancelledError)


async def run_session(api_key: str, model: str, system_text: str,
                      on_event: Optional[EventCb] = None,
                      interrupted: Optional[Callable[[], bool]] = None,
                      muted: Optional[Callable[[], bool]] = None,
                      voice: str = _DEFAULT_VOICE,
                      tools: "Optional[list]" = None,
                      on_tool_call: "Optional[ToolCb]" = None,
                      history: "Optional[list]" = None) -> "tuple[str, str]":
    """Convenience wrapper: build a session, run it, return (user_text, model_text)."""
    session = LiveAudioSession(api_key, model, system_text, on_event=on_event,
                               interrupted=interrupted, muted=muted, voice=voice,
                               tools=tools, on_tool_call=on_tool_call,
                               history=history)
    return await session.run()
