"""Live desktop → phone screen streaming over WebRTC.

This is the media half of the "remote desktop" feature. Signalling (SDP offer/
answer + ICE) rides the EXISTING authenticated JARVIS WebSocket (see the
webrtc_* handlers in main.py) — no third-party broker — while the video itself
flows over a real WebRTC PeerConnection (DTLS-SRTP encrypted, P2P when possible,
relayed when a NAT blocks direct).

Cross-network: aiortc gathers a host candidate for EVERY local interface — LAN,
and crucially the PC's Tailscale address (100.64/10) when Tailscale is up. Since
the phone already reaches this PC over that same Tailscale overlay for the
WebSocket, that host candidate is directly routable end-to-end, so the video
works off-LAN with no TURN server. A public STUN server is added as a floor for
same-NAT reflexive pairing, and any TURN the phone supplies is honoured too.

Design notes:
  * The whole module is import-guarded on `aiortc`. If that dependency isn't
    installed the feature is simply OFF (`is_available()` → False) and the rest
    of the backend is unaffected — nothing here runs at import time.
  * One session at a time (single paired phone). A new offer replaces any prior
    session, so a phone that reconnects gets a clean PeerConnection.
  * The screen grab (mss) is blocking, so it runs in a dedicated single-thread
    executor with a thread-local mss handle; the asyncio loop is never stalled.
  * Frames are downscaled to a bounded max side to cap both bandwidth and the
    VP8 encode cost.
"""

import asyncio
import fractions
import threading
from typing import Awaitable, Callable, Optional

# Everything WebRTC is optional. Import errors here MUST NOT break the backend —
# they only disable this feature.
try:
    from aiortc import (
        RTCConfiguration,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
        VideoStreamTrack,
    )
    from aiortc.sdp import candidate_from_sdp
    import av  # noqa: F401  (pulled in by aiortc; needed for VideoFrame)
    import mss  # already a backend dependency
    import numpy as np
    from aiortc.codecs import vpx as _vpx
    # aiortc's VP8 defaults are tuned for webcams (start 500 kbps, cap 1.5 Mbps):
    # desktop text arrived smeared for the first seconds and never got sharp on a
    # LAN. The encoder reads these module globals per use, and the phone's REMB
    # feedback still pulls the rate down on a slow Tailscale/DERP path.
    _vpx.DEFAULT_BITRATE = 1_200_000
    _vpx.MAX_BITRATE = 4_000_000
    _AIORTC_OK = True
    _IMPORT_ERROR = ""
except Exception as exc:  # noqa: BLE001
    _AIORTC_OK = False
    _IMPORT_ERROR = str(exc)

_VIDEO_CLOCK_RATE = 90000
_DEFAULT_FPS = 12
_DEFAULT_MAX_SIDE = 1280   # fallback only; the phone picks per-network (see useBrain.js)
# ICE "disconnected" is frequently a transient consent-freshness/keepalive hiccup
# (RFC 8445) — a brief Wi-Fi blip or missed STUN check — that self-recovers back to
# "connected" within seconds. Closing on it immediately killed live sessions after
# ~20-30s of totally normal use. Give it this long to recover before tearing down.
_DISCONNECT_GRACE_S = 10

# Emit callback injected by main.py: emit(event: str, data) -> Awaitable.
EmitFn = Callable[[str, object], Awaitable]


def is_available() -> bool:
    """True when aiortc imported cleanly and the feature can run."""
    return _AIORTC_OK


def import_error() -> str:
    """Human-readable reason the feature is off (empty when available)."""
    return _IMPORT_ERROR


if _AIORTC_OK:

    class _ScreenGrabber:
        """Thread-local mss handle so the single grab worker reuses one capture
        session (creating an mss per frame is slow on Windows)."""

        def __init__(self) -> None:
            self._local = threading.local()

        def _sct(self):
            sct = getattr(self._local, "sct", None)
            if sct is None:
                sct = mss.mss()
                self._local.sct = sct
            return sct

        def grab(self, max_side: int):
            sct = self._sct()
            mon = sct.monitors[0]  # all monitors combined (the virtual desktop)
            shot = sct.grab(mon)
            arr = np.frombuffer(shot.bgra, dtype=np.uint8).reshape(shot.height, shot.width, 4)
            scale = min(1.0, max_side / float(max(shot.width, shot.height))) if max_side else 1.0
            # Scale + convert to the encoder's yuv420p in ONE swscale pass. AREA keeps
            # small text legible (nearest-neighbour dropped whole pixel rows) and is
            # ~3x cheaper than the old numpy path, which alone capped 1080p at ~13 fps.
            return av.VideoFrame.from_ndarray(arr, format="bgra").reformat(
                width=max(2, int(shot.width * scale)) & ~1,   # keep even dims
                height=max(2, int(shot.height * scale)) & ~1,
                format="yuv420p", interpolation="AREA")

    class ScreenTrack(VideoStreamTrack):
        """A WebRTC video track whose frames are live desktop grabs, paced to a
        target FPS and downscaled to a bounded size."""

        kind = "video"

        def __init__(self, fps: int = _DEFAULT_FPS, max_side: int = _DEFAULT_MAX_SIDE) -> None:
            super().__init__()
            self._fps = max(1, min(int(fps), 30))
            self._max_side = int(max_side)
            self._grabber = _ScreenGrabber()
            self._executor = None
            self._start = None
            self._next_at = None

        async def recv(self):
            loop = asyncio.get_running_loop()
            if self._executor is None:
                # One dedicated worker → the thread-local mss handle is stable.
                from concurrent.futures import ThreadPoolExecutor
                self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="screengrab")
            if self._start is None:
                self._start = loop.time()
            # Pace to the target FPS as a MINIMUM interval (never grab faster), but do
            # NOT try to catch up a backlog. Stamp pts from the REAL elapsed time, not
            # a frame counter: the old `count / fps` stamp let the media clock drift
            # behind wall-clock whenever software VP8 encode couldn't keep up, so the
            # picture fell further and further behind live the longer/heavier the
            # session — the "gets laggier over time" report. Anchoring pts to the wall
            # clock keeps playback near-live; when encode lags, frames are simply
            # dropped (a small gap) instead of the whole timeline sliding into the past.
            # Frames follow a fixed schedule rather than "last wake-up + period":
            # Windows rounds every sleep up to its ~15.6 ms timer tick, and anchoring
            # to the late wake-up turned a 15 fps target into ~13. A frame that is
            # more than one period late resets the schedule instead of bursting.
            period = 1.0 / self._fps
            now = loop.time()
            if self._next_at is None or now - self._next_at > period:
                self._next_at = now
            elif self._next_at > now:
                await asyncio.sleep(self._next_at - now)
                now = loop.time()
            self._next_at += period

            frame = await loop.run_in_executor(self._executor, self._grabber.grab, self._max_side)
            frame.pts = int((now - self._start) * _VIDEO_CLOCK_RATE)
            frame.time_base = fractions.Fraction(1, _VIDEO_CLOCK_RATE)
            return frame

        def stop(self) -> None:  # noqa: D401
            super().stop()
            if self._executor is not None:
                self._executor.shutdown(wait=False)
                self._executor = None


def _ice_config(ice_servers) -> "RTCConfiguration":
    """Build an RTCConfiguration from a list of {urls, username?, credential?}
    dicts sent by the phone. Always includes a public STUN server as a floor."""
    servers = [RTCIceServer(urls="stun:stun.l.google.com:19302")]
    for s in ice_servers or []:
        try:
            urls = s.get("urls") if isinstance(s, dict) else None
            if not urls:
                continue
            servers.append(
                RTCIceServer(
                    urls=urls,
                    username=s.get("username"),
                    credential=s.get("credential"),
                )
            )
        except Exception:  # noqa: BLE001
            continue
    return RTCConfiguration(iceServers=servers)


class _Session:
    """A single live remote-desktop PeerConnection."""

    def __init__(self, emit: EmitFn, fps: int, max_side: int) -> None:
        self._emit = emit
        self._pc: "Optional[RTCPeerConnection]" = None
        self._track: "Optional[ScreenTrack]" = None
        self._fps = fps
        self._max_side = max_side
        self._disconnect_task: "Optional[asyncio.Task]" = None

    def _cancel_disconnect_watch(self) -> None:
        if self._disconnect_task is not None:
            self._disconnect_task.cancel()
            self._disconnect_task = None

    async def _disconnect_grace(self) -> None:
        try:
            await asyncio.sleep(_DISCONNECT_GRACE_S)
        except asyncio.CancelledError:
            return
        pc = self._pc
        if pc is not None and pc.connectionState == "disconnected":
            print("[WebRTC] disconnected past grace period — closing", flush=True)
            await self.close()

    async def start(self, offer: dict, ice_servers) -> Optional[dict]:
        await self.close()
        pc = RTCPeerConnection(configuration=_ice_config(ice_servers))
        self._pc = pc
        self._track = ScreenTrack(self._fps, self._max_side)
        pc.addTrack(self._track)

        @pc.on("connectionstatechange")
        async def _on_state():  # noqa: ANN202
            state = pc.connectionState
            print(f"[WebRTC] connection state: {state}", flush=True)
            if state == "connected":
                self._cancel_disconnect_watch()
            elif state == "disconnected":
                # Don't tear down a good session on a transient blip — only close
                # if it hasn't recovered after the grace window (see _disconnect_grace).
                self._cancel_disconnect_watch()
                self._disconnect_task = asyncio.ensure_future(self._disconnect_grace())
            elif state in ("failed", "closed"):
                self._cancel_disconnect_watch()
                await self.close()

        await pc.setRemoteDescription(
            RTCSessionDescription(sdp=offer.get("sdp", ""), type=offer.get("type", "offer"))
        )
        answer = await pc.createAnswer()
        # aiortc gathers ICE candidates during setLocalDescription, so the answer
        # SDP we return already carries them (host + any STUN/TURN reflexive).
        await pc.setLocalDescription(answer)
        return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

    async def add_ice(self, candidate: dict) -> None:
        if not self._pc or not candidate:
            return
        cand_str = candidate.get("candidate", "")
        if not cand_str:
            return
        try:
            ice = candidate_from_sdp(cand_str.split(":", 1)[1] if cand_str.startswith("candidate:") else cand_str)
            ice.sdpMid = candidate.get("sdpMid")
            ice.sdpMLineIndex = candidate.get("sdpMLineIndex")
            await self._pc.addIceCandidate(ice)
        except Exception as exc:  # noqa: BLE001
            print(f"[WebRTC] bad ICE candidate ignored: {exc}", flush=True)

    async def close(self) -> None:
        self._cancel_disconnect_watch()
        track, pc = self._track, self._pc
        self._track, self._pc = None, None
        if track is not None:
            try:
                track.stop()
            except Exception:  # noqa: BLE001
                pass
        if pc is not None:
            try:
                await pc.close()
            except Exception:  # noqa: BLE001
                pass


# ── Module-level single session (one paired phone) ───────────────────────────
_session: "Optional[_Session]" = None


async def handle_offer(data: dict, emit: EmitFn) -> None:
    """Phone sent a WebRTC offer → answer it and start streaming the screen."""
    global _session
    if not _AIORTC_OK:
        await emit("warning", f"Screen streaming unavailable on the PC (aiortc missing: {_IMPORT_ERROR}).")
        return
    data = data or {}
    fps = int(data.get("fps") or _DEFAULT_FPS)
    max_side = int(data.get("max_side") or _DEFAULT_MAX_SIDE)
    if _session is not None:
        await _session.close()
    print("[WebRTC] Phone is requesting a live view of this screen…", flush=True)
    _session = _Session(emit, fps, max_side)
    try:
        answer = await _session.start(data.get("offer") or {}, data.get("ice_servers"))
        if answer:
            await emit("webrtc_answer", answer)
            print("[WebRTC] Answered — waiting for the phone to connect…", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[WebRTC] offer handling failed: {exc}", flush=True)
        await emit("warning", f"Couldn't start the screen stream: {exc}")
        await stop()


async def handle_ice(data: dict) -> None:
    """Phone trickled an ICE candidate."""
    if _session is not None:
        await _session.add_ice((data or {}).get("candidate") or {})


async def stop() -> None:
    """Tear the current session down (phone left, or explicit stop_screen)."""
    global _session
    if _session is not None:
        await _session.close()
        _session = None
