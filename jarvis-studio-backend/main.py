import argparse
import asyncio
import base64
import binascii
import contextlib
import datetime as _dt
import os
import re
import sys
import time

# LLM responses contain Unicode the Windows console can't encode by default
# (em-dashes, non-breaking hyphens "‑", emoji), which makes print() raise
# UnicodeEncodeError under the default cp1252 codec and aborts the pipeline.
# Force UTF-8 output (start.py decodes our stdout as UTF-8); replace anything odd.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from server.websocket_server import (
    emit, send_to, start as start_websocket, register_handler, on_connect,
    on_disconnect, send_to_current, current_client_id,
    configure_device_identity, set_remote_allowed,
    emit_task_event, subscribe_task, subscribe_local_clients,
    is_current_sender_remote, current_authenticated_device,
    connected_device_ids,
    disconnect_authenticated_device,
)
from server import webrtc_screen
from llm.groq_bridge import (
    initialize, send_prompt, stream_prompt, get_sysinfo, set_model, set_tts,
    reset_history, set_history, translate_text, get_api_key, get_gemini_key, set_live_context,
    startup_greeting, vision_query, web_answer, set_allowed_dirs, get_allowed_dirs,
    set_api_keys, set_storage_dir, generate_image, note_outcome, remember_turn_outcome,
    has_llm_credentials,
    model_leaks_reasoning, reply_is_quote_wrapped, summarize_error, get_model,
    routing_note, get_history, set_skill_context, get_screen, set_screen,
    get_manual_location, set_manual_location,
    get_always_on, set_always_on, set_conversation_mode,
    get_system_alerts, set_system_alerts, get_overlay, set_overlay,
    quick_completion, CAPABILITY_INDEX,
)
from llm import gemini_bridge, live_audio, live_tools, model_discovery, vertex_auth
from actions import execute_actions, run_specs, run_action, needs_permission, _BROWSER_READONLY
from actions import skills, fs_access, recorder, computer, browser, app_launcher
import autopilot
import storage
import memory_store
import routines
import reminders
import playbooks
import places
# First-run asset provisioning is OPTIONAL. If the module is missing from a frozen
# build, or fails to import for any reason, the backend MUST still start — the
# assets then fall back to lazy download-on-first-use. Never let it brick startup.
try:
    import provisioning
except Exception:  # noqa: BLE001
    provisioning = None

# Running snapshot of first-run/repair asset setup, so a HUD that connects LATE (or
# reloads) still renders the setup screen. emit() is a no-op when no client is
# connected, and the WebView typically connects a beat after the backend starts
# provisioning — without this replay the user would see no progress at all.
_setup_snapshot = None        # {"total", "order":[k], "items":{k:{label,status,error}}, "complete", "had_error"}
_setup_in_progress = False    # guard so a repair can't run twice concurrently


def _reduce_setup_snapshot(prev, data):
    """Fold one `setup_progress` event into the cumulative snapshot (mirrors the
    GUI's reduceSetupProgress so a replay reconstructs the exact same screen)."""
    if not isinstance(data, dict):
        return prev
    stage = data.get("stage")
    if stage == "start":
        return {"total": data.get("total") or 0, "order": [], "items": {},
                "complete": False, "had_error": False}
    snap = (dict(prev) if (prev and not prev.get("complete")) else
            {"total": data.get("total") or 0, "order": [], "items": {},
             "complete": False, "had_error": False})
    snap["order"] = list(snap.get("order", []))
    snap["items"] = dict(snap.get("items", {}))
    if stage == "complete":
        snap["complete"] = True
        snap["had_error"] = bool(data.get("had_error"))
        return snap
    key = data.get("asset")
    if key and stage in ("downloading", "done", "error"):
        if key not in snap["items"]:
            snap["order"].append(key)
        snap["items"][key] = {"label": data.get("label") or key, "status": stage,
                              "error": data.get("error") or ""}
        if stage == "error":
            snap["had_error"] = True
    return snap


async def _setup_emit(event, data):
    """Emit wrapper passed to provisioning: records the cumulative snapshot, then
    broadcasts. (`emit`/`_track_task` are defined later in this module but resolve at
    call time, so referencing them here is fine.)"""
    global _setup_snapshot
    if event == "setup_progress":
        _setup_snapshot = _reduce_setup_snapshot(_setup_snapshot, data)
    await emit(event, data)


def _setup_snapshot_for_replay():
    """The snapshot to push to a newly-connected client, or None. A fully-successful
    completed setup is NOT replayed (it would re-pop the 'all set' card on every
    reconnect); an in-progress or errored one IS, so it stays visible and retryable."""
    snap = _setup_snapshot
    if not snap:
        return None
    if snap.get("complete") and not snap.get("had_error"):
        return None
    return {"stage": "snapshot", **snap}
from wake_word.base import WakeWordDetector
from transcription.whisper_transcriber import transcribe, preload as preload_whisper
from tts.tts_engine import TTSEngine
import telemetry
import weather as weather_mod

# Phrases that pull Jarvis out of standby / sleep mode.
_WAKE_PHRASES = ("wake up", "wake", "jarvis wake", "are you there", "you awake")

_tts: "TTSEngine | None" = None
_trigger_listen: "asyncio.Event | None" = None
_pipeline_lock: "asyncio.Lock | None" = None
# Set while always-on continuous voice mode is enabled (no wake word needed).
_always_on_event: "asyncio.Event | None" = None
# Set by the Stop button: aborts the in-flight reply (stops generation, drops any
# queued speech, and forces the HUD back to idle so the next request can go).
_interrupt: "asyncio.Event | None" = None


def _interrupted() -> bool:
    return _interrupt is not None and _interrupt.is_set()


# Computer-control overlay state: the user can PAUSE the running autopilot loop (it
# holds between steps without acting) and type live CORRECTIONS that get injected
# into the operator's next decision. Both are read by autopilot.run_task via the
# `paused` / `corrections` callbacks below.
_control_paused: bool = False
_pending_corrections: "list[str]" = []


def _control_is_paused() -> bool:
    return _control_paused


def _pop_corrections() -> str:
    """Drain + join any corrections the user typed since the last poll (then clear)."""
    global _pending_corrections
    if not _pending_corrections:
        return ""
    out = " ".join(_pending_corrections)
    _pending_corrections = []
    return out


async def _emit_control_state() -> None:
    """Broadcast the desktop-control state plus the overlay's pause flag."""
    await emit("control_state", {**computer.state(), "paused": _control_paused})


def _clean_desc(desc: "str | None") -> str:
    """Strip a trailing parenthetical from a permission description. The Approve
    dialog's text carries a disclosure ('(during a task I may glance at a
    screenshot…)') meant for the ASK; left in a refusal it reads oddly ('I won't
    open and control the browser (during a task…)')."""
    return re.sub(r"\s*\([^)]*\)\s*$", "", (desc or "").strip()) or "do that"


def _spoken_refusal(desc: "str | None") -> str:
    """A clean 'Understood, sir — I won't …' line for a denied permission."""
    return f"Understood, sir — I won't {_clean_desc(desc)}."


# Appended to screen-vision questions to curb fabrication: the model must report
# only what's visible and transcribe text verbatim, rather than inventing a
# plausible answer (the "You're invited to Anaay's party!" guess when the screen
# actually read "SWEET SIXTEEN / Anaay / …").
_SCREEN_READ_GUIDANCE = (
    " Report ONLY what is actually visible in this image. If I asked you to read "
    "text, transcribe it EXACTLY as shown — every word and number — and do not "
    "paraphrase, autocomplete, translate, or invent anything; if a part is unclear, "
    "cut off, or unreadable, say so plainly instead of guessing."
)

# Latest HUD data, cached so a freshly-connected client gets values immediately
# instead of waiting for the next poll tick.
_last_telemetry: "dict | None" = None
_last_netinfo: "dict | None" = None
_last_weather: "dict | None" = None
_last_schedule: "list | None" = None
# The app the user is currently focused on (for the floating overlay pill).
_last_active_app: "dict | None" = None

# Self-awareness: precise GPS override, greeting debounce, proactive-alert cooldowns.
_user_coords: "tuple[float, float] | None" = None       # precise browser GPS (auto)
_manual_coords: "tuple | None" = None                   # (lat, lon, label) user-pinned override
_last_greet: float = 0.0
_alert_cooldowns: "dict[str, float]" = {}
# True from the moment a wake/voice/text turn begins until its reply is done.
# Proactive alerts (e.g. "CPU at 99%") stay silent while this is set, so JARVIS
# never blurts out a status line just before answering what the user actually asked.
_user_busy: bool = False

# Pending Approve/Deny requests, keyed by request id → Future resolved by the GUI.
_pending_perms: "dict[str, asyncio.Future]" = {}


async def request_permission(kind: str, description: str, timeout: float = 120.0) -> bool:
    """Ask the GUI for Approve/Deny on a dangerous action. Blocks until the user
    responds (or the request times out → treated as denied)."""
    loop = asyncio.get_running_loop()
    req_id = f"{time.time()}"
    fut: "asyncio.Future" = loop.create_future()
    _pending_perms[req_id] = fut
    await emit("permission_request", {"id": req_id, "kind": kind, "description": description})
    print(f"[Permission] Awaiting approval: {description}", flush=True)
    try:
        return bool(await asyncio.wait_for(fut, timeout))
    except asyncio.TimeoutError:
        print(f"[Permission] Timed out (denied): {description}", flush=True)
        return False
    finally:
        _pending_perms.pop(req_id, None)
        # The request is broadcast to every window (HUD + floating overlay);
        # answering in one must dismiss it everywhere, including on timeout.
        await emit("permission_request", None)


async def handle_permission_response(data) -> None:
    """GUI → backend: the user clicked Approve or Deny."""
    if not isinstance(data, dict):
        return
    fut = _pending_perms.get(data.get("id"))
    if fut is not None and not fut.done():
        fut.set_result(bool(data.get("approved")))


# Pending mid-task clarify questions, keyed by request id → Future resolved by GUI.
_pending_clarify: "dict[str, asyncio.Future]" = {}


async def request_clarification(question: str, timeout: float = 90.0) -> str:
    """Ask the user ONE short question mid-task and block until they answer (or it
    times out → ""). The autopilot uses this when it hits genuine ambiguity it
    can't resolve from the page itself. Returns the typed answer ("" = skipped)."""
    question = (question or "").strip()
    if not question:
        return ""
    loop = asyncio.get_running_loop()
    req_id = f"{time.time()}"
    fut: "asyncio.Future" = loop.create_future()
    _pending_clarify[req_id] = fut
    await emit("clarify_request", {"id": req_id, "question": question})
    print(f"[Clarify] Awaiting answer: {question}", flush=True)
    try:
        return str(await asyncio.wait_for(fut, timeout) or "").strip()
    except asyncio.TimeoutError:
        print(f"[Clarify] Timed out (skipped): {question}", flush=True)
        return ""
    finally:
        _pending_clarify.pop(req_id, None)
        await emit("clarify_request", None)        # dismiss in every window


async def handle_clarify_response(data) -> None:
    """GUI → backend: the user answered (or skipped) a mid-task question."""
    if not isinstance(data, dict):
        return
    fut = _pending_clarify.get(data.get("id"))
    if fut is not None and not fut.done():
        fut.set_result(str(data.get("answer") or ""))


# ── Remote mobile control (Tailscale + QR pairing) ────────────────────────────
# A paired phone over Tailscale can submit tasks the PC runs. The transport
# (server/websocket_server.py) does the Tailscale gate + per-message crypto; here
# we own: minting the QR pairing challenge, the S1 human-approval of a new phone,
# device list/revoke, and the task shim that streams progress back to just that
# phone (never the broadcast bus — see emit_task_event). All device-management
# handlers are LOCAL-HUD-ONLY; the default-deny remote allowlist (set in main())
# lets a phone invoke only the task vocabulary.
_device_registry = None            # DeviceIdentityRegistry | None (None → remote off)
_remote_task_busy = False          # single-flight guard: one remote actuation at a time
# Who is driving the autopilot right now: "" (nobody), "local" or "remote". The
# interrupt flag, the consent windows, the browser target and the mouse itself are
# all process-global, so a local and a phone task running together would clear
# each other's Stop, disarm each other's consent and fight over the cursor.
_autopilot_owner = ""
_remote_task_id = ""               # id of the in-flight remote task (cancel/subscribe ownership)
_remote_task_owner = ""            # device_id that submitted it (only it may cancel/resubscribe)
_remote_task_kind = ""             # "browser"/"computer" of the in-flight task — scopes task.cancel's browser force-kill
REMOTE_PROTOCOL_VERSION = 2        # matches the phone's v2 wire (pc.ts PROTOCOL_VERSION)

# ── Live remote-desktop state (WebRTC screen view + direct input) ─────────────
# One paired phone at a time. The SCREEN lease binds a WebRTC session to the
# connection+device that opened it; the CONTROL lease additionally gates direct
# mouse/keyboard, is armed by that same phone (its fingerprint gate IS the
# consent, like pc_task), and carries a lease_id + monotonic seq so a stale or
# replayed input envelope can't drive. Locks are created in main() (need a loop).
_remote_screen_lock: "asyncio.Lock | None" = None
_remote_input_lock: "asyncio.Lock | None" = None
_remote_screen_lease: dict = {}    # {client_id, device_id, session_id, answered}
_remote_control_lease: dict = {}   # {client_id, device_id, screen_session_id, lease_id, expires_at, last_seq}

# Per-task coordination for the ONE in-flight remote task (single-flight), all
# reset at task start. The emit lock serializes outbound events; the journal keeps
# recent (seq,event,data) so a reconnect can replay what it missed with ORIGINAL
# seqs; the pending question is the outstanding mid-task clarify awaiting an answer.
_remote_emit_lock: "asyncio.Lock | None" = None
_remote_task_journal: "list[tuple[int, str, dict]]" = []
_remote_pending_question: "tuple[str, object] | None" = None
_REMOTE_JOURNAL_MAX = 600          # a task's step budget keeps it well under this
_REMOTE_CLARIFY_TIMEOUT_S = 240.0  # wait for a phone answer, under its 5-min window
# Journals of recently FINISHED remote tasks: task_id -> (owner device_id, journal).
# A phone whose socket died mid-task (screen locked, network switch) usually comes
# back after the task ended; without this its resubscribe found nothing and it
# reported a false "stopped making progress" 210s later for a task that succeeded.
_remote_finished: "dict[str, tuple[str, list]]" = {}
_REMOTE_FINISHED_MAX = 8


def _tailscale_ip() -> str:
    """This host's Tailscale IPv4 (the address the QR advertises), or "" if
    Tailscale isn't up. Used for DISPLAY only; the socket binds 0.0.0.0 and the
    crypto+whois gate is the real access control (plan G3)."""
    import subprocess
    kwargs = {"capture_output": True, "text": True, "timeout": 2.0, "check": False}
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], **kwargs).stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        ip = line.strip()
        if ip:
            return ip
    return ""


async def handle_device_pairing_create(data) -> None:
    """Local HUD → mint a short-lived QR pairing challenge. Public material only
    (see device_identity): host + fingerprint + one-use challenge id, never a
    secret. Advertises the Tailscale IP as the host."""
    if is_current_sender_remote() or _device_registry is None:
        return
    name = str((data or {}).get("device_name") or "Aura phone")[:80]
    try:
        challenge = _device_registry.create_pairing_challenge(
            device_name=name, scopes=("tasks",), ttl_seconds=120,
        )
    except Exception as exc:  # noqa: BLE001
        await emit("device.pairing", {"ok": False, "reason": str(exc)})
        return
    pin = challenge.pop("pin", "")              # shown on screen, NEVER in the QR
    ts_ip = _tailscale_ip()
    payload = {**challenge, "host": ts_ip, "port": 8765}
    await emit("device.pairing", {
        "ok": True,
        "payload": payload,                     # JSON the phone scans
        "pin": pin,                             # typed into the phone by hand
        "host": ts_ip,
        "tailscale_ready": bool(ts_ip),
        "host_fingerprint": challenge.get("host_fingerprint", ""),
    })


def _device_roster() -> list:
    """Paired-device list annotated with which ones hold a live socket NOW —
    so the HUD can mark the connected phone and the user never again revokes
    the live device thinking it was a stale duplicate."""
    live = connected_device_ids()
    return [
        {**d, "connected": d["device_id"] in live}
        for d in _device_registry.list_devices()
    ]


async def handle_device_list(data) -> None:
    """Local HUD → the paired-device roster (incl. pending-approval devices)."""
    if is_current_sender_remote() or _device_registry is None:
        return
    await emit("device.list", _device_roster())


async def handle_device_revoke(data) -> None:
    """Local HUD → revoke a device, kill its live sockets, then remove the row
    so the phone can PAIR AGAIN later (fresh QR + PIN). Revoke-without-remove
    permanently bricked a phone: claim_pairing refuses revoked rows and the HUD
    had no separate remove button."""
    if is_current_sender_remote() or _device_registry is None:
        return
    device_id = str((data or {}).get("device_id") or "")
    if device_id:
        _device_registry.revoke(device_id)
        with contextlib.suppress(Exception):
            await disconnect_authenticated_device(device_id)
        _device_registry.remove(device_id)
    await emit("device.list", _device_roster())


# How often a running remote task pings the phone so its idle watchdog stays fed
# during long silent operations. Must be comfortably under the phone's idle timeout
# (pc.ts taskIdleTimeoutMs, 210s) so a couple of dropped pings don't trip it.
_REMOTE_HEARTBEAT_S = 60


async def _run_remote_task(task_id: str, kind: str, goal: str, device_name: str) -> None:
    """Run one phone-submitted autopilot task, streaming progress to THAT phone
    only (emit_task_event is per-subscriber, never the broadcast bus → G1/G2).

    Approval lives ON THE PHONE (a fingerprint/Face-ID confirm before the task is
    ever sent), so the desktop trusts an authenticated task and runs it — no PC
    prompt (that would defeat controlling the PC while you're away from it). The
    device is already PIN-paired + crypto-signed + replay-protected.
    """
    device_id = current_authenticated_device()
    subscribe_task(task_id)            # the requesting phone (submitting connection only)
    subscribe_local_clients(task_id)   # let the desktop watch too
    _audit = _device_registry.record_event if _device_registry is not None else (lambda *_a, **_k: None)
    _audit({"event": "task.received", "device_id": device_id, "task_id": task_id,
            "kind": kind, "goal": goal[:200]})
    # Wire shape matches the phone's durable v2 client (aura-android remote/pc.ts):
    # an explicit task.accepted ack, per-step task.event, and a terminal task.status
    # carrying state + (on success) a proof the phone requires before it will call a
    # task done. The phone correlates every event by the task_id IT submitted, which
    # is why handle_task_submit honours the client id instead of minting its own.
    # THIS task's events must leave in host-counter order. The heartbeat below emits
    # CONCURRENTLY with the step stream, and the phone drops the link on out-of-order
    # counters — so funnel every outbound event for this task through one lock.
    global _remote_emit_lock, _remote_task_journal, _remote_pending_question
    if _remote_emit_lock is None:
        _remote_emit_lock = asyncio.Lock()
    _remote_task_journal = []        # (seq, event, data) of THIS task, for reconnect replay
    _remote_pending_question = None  # (prompt_id, Future) while a clarify is outstanding

    async def _emit(event: str, data: dict, *, seq: "int | None" = None) -> None:
        async with _remote_emit_lock:
            used = (await emit_task_event(task_id, event, data)
                    if seq is None else
                    await emit_task_event(task_id, event, data, seq=seq))
            # Journal durable events (seq>0) so a reconnecting phone can replay what
            # it missed WITH original seqs. seq=0 heartbeats stay out-of-band.
            if isinstance(used, int) and used > 0:
                _remote_task_journal.append((used, event, data))
                if len(_remote_task_journal) > _REMOTE_JOURNAL_MAX:
                    del _remote_task_journal[0]

    await _emit("task.accepted",
                {"task_id": task_id, "state": "accepted", "goal": goal,
                 "v": REMOTE_PROTOCOL_VERSION})

    async def on_step(line: str) -> None:
        await _emit("task.event",
                    {"task_id": task_id, "kind": "step", "message": line,
                     "ok": not line.lstrip().startswith("✗")})

    async def ask(question: str) -> str:
        # Mid-task clarify round-trip: emit the question to the phone, then WAIT for
        # its task.answer (resolved by handle_task_answer) up to the clarify window.
        # The phone pauses its idle watchdog while awaiting the user, so this can sit.
        # On timeout we return "" and the operator proceeds with its best guess.
        global _remote_pending_question
        prompt_id = "q-" + os.urandom(6).hex()
        fut = asyncio.get_running_loop().create_future()
        _remote_pending_question = (prompt_id, fut)
        await _emit("task.event",
                    {"task_id": task_id, "kind": "clarification.challenge",
                     "payload": {"question": question, "prompt_id": prompt_id}})
        try:
            answer = await asyncio.wait_for(fut, timeout=_REMOTE_CLARIFY_TIMEOUT_S)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            answer = ""
        finally:
            _remote_pending_question = None
            await _emit("task.event",
                        {"task_id": task_id, "kind": "clarification.resolved",
                         "payload": {"prompt_id": prompt_id}})
        return (answer or "").strip()

    # The phone's fingerprint gate IS this task's consent (see docstring), but
    # autopilot.run_task re-checks the PC-side consent windows every step —
    # unarmed, a remote task dies at step 0 with "control expired mid-task".
    # Arm the right window for the task, and restore the PC's prior posture after
    # so a remote task never leaves a consent window open the user didn't grant.
    was_consented = browser.is_approved() if kind == "browser" else computer.is_armed()
    if kind == "browser":
        browser.approve(15)
    else:
        armed_ok, arm_msg = computer.arm(15)
        if not armed_ok:
            _audit({"event": "task.result", "device_id": device_id, "task_id": task_id,
                    "ok": False, "stopped": False})
            await _emit("task.status",
                        {"task_id": task_id, "state": "failed",
                         "summary": arm_msg, "v": REMOTE_PROTOCOL_VERSION})
            return

    # Keep the phone's idle watchdog fed while the PC is legitimately busy but
    # SILENT between step events — above all the cold browser launch (up to ~150s
    # with no step), which used to trip the phone's timeout and surface as a false
    # "couldn't control the PC's browser" apology. seq=0 → out-of-band: it bumps the
    # watchdog without entering the durable journal, so it's never replayed. This is
    # what makes the phone's patience DYNAMIC — a task may run as long as it likes as
    # long as the PC keeps signalling; only genuine silence (a dead PC) times out.
    async def _heartbeat() -> None:
        try:
            while True:
                await asyncio.sleep(_REMOTE_HEARTBEAT_S)
                await _emit("task.heartbeat", {"task_id": task_id}, seq=0)
        except asyncio.CancelledError:
            pass

    # Make this remote task STOPPABLE: the autopilot loop polls `interrupted`
    # between steps, so a phone task.cancel / stop_control (both trip `_interrupt`)
    # breaks it — exactly like the local desktop task path. Clear any stale interrupt
    # first so a STOP from the PREVIOUS task can't abort this fresh one at step 0.
    if _interrupt is not None:
        _interrupt.clear()
    # Route a web task the user explicitly aimed at THEIR browser (their real Chrome,
    # over CDP) instead of JARVIS's dedicated one. Deterministic keyword match — the
    # model dropping a flag is exactly why "do it in my browser" kept using the wrong
    # one. Falls back to the own browser (with a heads-up) if Chrome can't be reached.
    personal_target = False
    if kind == "browser" and browser.wants_personal_browser(goal):
        ok_p, msg_p = await asyncio.get_running_loop().run_in_executor(
            None, browser.ensure_personal_available)
        if ok_p:
            browser.set_target("personal")
            personal_target = True
        elif msg_p:
            await emit("warning", msg_p)
    hb = asyncio.create_task(_heartbeat())
    try:
        res = await autopilot.run_task(
            kind, goal, on_step=on_step, ask=ask,
            hint=playbooks.autopilot_hint(goal), interrupted=_interrupted,
        )
        ok = bool(res.get("ok"))
        stopped = bool(res.get("stopped"))
        summary = (res.get("summary") or "").strip()
        findings = [str(f)[:200] for f in (res.get("findings") or [])[:12] if str(f).strip()]
        state = "cancelled" if stopped else ("succeeded" if ok else "failed")
        _audit({"event": "task.result", "device_id": device_id, "task_id": task_id,
                "ok": ok, "stopped": stopped})
        terminal = {"task_id": task_id, "state": state, "summary": summary,
                    "findings": findings, "v": REMOTE_PROTOCOL_VERSION}
        if ok:
            # The phone gates completion on proof.passed + a non-empty evidence list.
            # passed mirrors autopilot's OWN verdict (res.ok already runs the
            # _summary_looks_incomplete gate) — not a fabricated pass; evidence ids
            # are the real findings, or the task id (audit-log correlator) if none.
            terminal["proof"] = {"passed": True,
                                 "evidence_ids": findings or [task_id]}
        await _emit("task.status", terminal)
        print(f"[Remote] task {task_id} ({kind}) {'✓' if ok else '✗'} {goal!r}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _audit({"event": "task.error", "device_id": device_id, "task_id": task_id, "error": str(exc)[:200]})
        await _emit("task.status",
                    {"task_id": task_id, "state": "failed",
                     "summary": f"error: {exc}", "v": REMOTE_PROTOCOL_VERSION})
        print(f"[Remote] task {task_id} failed: {exc}", flush=True)
    finally:
        hb.cancel()
        if personal_target:
            browser.set_target("own")   # next task defaults back to JARVIS's own browser
        # A cancelled task raises CancelledError on await — and it's a BaseException,
        # so suppress(Exception) alone lets it escape (leaking out of the whole
        # handler when a task finishes fast enough that the heartbeat never entered
        # its own try/except). Suppress both.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await hb
        if not was_consented:
            if kind == "browser":
                browser.revoke()
            else:
                computer.disarm()


async def _classify_task_kind(goal: str) -> str:
    """Decide whether a kind-less PC command belongs in the BROWSER or a native
    desktop app. Used for the live-view "command mode" path, which forwards the
    user's typed/spoken command straight to the PC with no phone-side model to
    choose a kind — so the DESKTOP (which knows what's installed and open) routes
    it. Falls back to 'browser' if the quick model call fails."""
    try:
        raw = await quick_completion(
            "You route a command for a Windows PC assistant. Answer with ONLY one "
            "word — 'browser' if it belongs on the web or inside a web browser (a "
            "website, YouTube, Gmail, a Google search, an online tool), or 'computer' "
            "if it's a native desktop app, a file, or Windows itself (Notepad, File "
            "Explorer, Settings, an installed program, the taskbar).",
            f"Command: {goal}\n\nbrowser or computer:",
            max_tokens=4,
        )
    except Exception:  # noqa: BLE001
        return "browser"
    return "computer" if "computer" in (raw or "").lower() else "browser"


async def _reject_remote_task(task_id: str) -> None:
    """Tell the submitting phone, as a terminal state it understands, that the PC is
    busy. The submitter isn't subscribed yet (only _run_remote_task does that), and
    the phone has no "rejected" state — so this used to reach nobody and the phone
    sat on its 210s idle watchdog before blaming the PC for "no progress"."""
    subscribe_task(task_id)
    await emit_task_event(task_id, "task.status", {
        "task_id": task_id, "state": "failed",
        "summary": "Your PC is busy with another task right now, sir — try again when it finishes.",
        "v": REMOTE_PROTOCOL_VERSION,
    })


async def handle_task_submit(data) -> None:
    """Remote phone → run a task on the PC. Remote-only; local uses text_input."""
    global _remote_task_busy, _remote_task_id, _remote_task_owner, _remote_task_kind
    global _autopilot_owner
    if not is_current_sender_remote() or not isinstance(data, dict):
        return
    goal = str(data.get("goal") or data.get("text") or "").strip()
    if not goal:
        return
    device_id = current_authenticated_device()
    # Honour the phone-supplied task_id when it's well-formed. The phone (pc.ts)
    # correlates every incoming event by the id IT generated (a 122-bit crypto
    # UUID via newId("task")), so the host must run under that same id or the phone
    # never matches a single event and idle-times-out. A UUID isn't the "guessable
    # phone-supplied id" the round-4 note warned about, and this build has no
    # task.subscribe verb + single-flight, so there's no cross-device observe vector
    # to exploit. Fall back to a server id only for a malformed/absent client id.
    import re
    import secrets
    submitted = str(data.get("task_id") or "").strip()
    task_id = (submitted if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", submitted)
               else f"{device_id or 'dev'}.{secrets.token_urlsafe(9)}")
    # Idempotent resubmit: the phone re-sends task.submit with the SAME id when the
    # link dropped before task.accepted reached it. Re-attach it to that task
    # (running or just finished) instead of rejecting it as "busy" or re-running it.
    if task_id == _remote_task_id or task_id in _remote_finished:
        await handle_task_subscribe({"task_id": task_id, "resume_after_seq": 0})
        return
    # Single-flight: autopilot.run_task drives the one shared desktop/HUD, so two
    # concurrent remote tasks would fight over the mouse. Reject the second.
    if _remote_task_busy or _autopilot_owner:
        await _reject_remote_task(task_id)
        return
    # Route by the phone-chosen kind. 'auto' (the live-view command mode, which has
    # no phone-side model to pick a kind) is classified HERE on the desktop — which
    # actually knows what's installed/open — into browser vs a native desktop app.
    raw_kind = str(data.get("kind") or "").strip().lower()
    if raw_kind.startswith(("computer", "desktop")):
        kind = "computer"
    elif raw_kind == "auto":
        kind = await _classify_task_kind(goal)
    else:
        kind = "browser"
    device_name = "Aura phone"
    if _device_registry is not None:
        for dev in _device_registry.list_devices():
            if dev.get("device_id") == device_id:
                device_name = dev.get("name") or device_name
                break
    # Re-check: _classify_task_kind above awaited, and a local task may have started.
    if _remote_task_busy or _autopilot_owner:
        await _reject_remote_task(task_id)
        return
    _remote_task_busy = True
    _autopilot_owner = "remote"
    _remote_task_id = task_id
    _remote_task_owner = device_id
    _remote_task_kind = kind
    try:
        await _run_remote_task(task_id, kind, goal, device_name)
    finally:
        _remote_finished[task_id] = (device_id, list(_remote_task_journal))
        while len(_remote_finished) > _REMOTE_FINISHED_MAX:
            del _remote_finished[next(iter(_remote_finished))]
        _autopilot_owner = ""
        _remote_task_busy = False
        _remote_task_id = ""
        _remote_task_owner = ""
        _remote_task_kind = ""


async def handle_task_cancel(data) -> None:
    """Remote phone → STOP the in-flight PC task. Trips the same interrupt the
    autopilot loop polls between steps (see _run_remote_task's interrupted=), so the
    task breaks at its next step. Single-flight means there's exactly one task to
    stop; still, only the device that submitted it may cancel it."""
    if not is_current_sender_remote() or not isinstance(data, dict):
        return
    task_id = str(data.get("task_id") or "")
    if not _remote_task_id or (task_id and task_id != _remote_task_id):
        return
    if current_authenticated_device() != _remote_task_owner:
        return
    if _interrupt is not None:
        _interrupt.set()
    if _remote_task_kind == "browser":
        # A browser autopilot step can block inside Playwright (page load / wait);
        # the interrupt is only polled BETWEEN steps, so force-kill the browser now
        # to unblock it — otherwise cancel appears to do nothing until the step ends.
        try:
            browser.abort()
        except Exception:  # noqa: BLE001
            pass
    print(f"[Remote] task {_remote_task_id} cancel requested "
          f"({str(data.get('reason') or 'user_cancelled')})", flush=True)


async def handle_task_answer(data) -> None:
    """Remote phone answering a mid-task clarify question (task.answer) — resolves
    the Future that _run_remote_task's ask() is awaiting. Ownership-checked, and the
    prompt_id (when supplied) must match the outstanding question."""
    global _remote_pending_question
    if not is_current_sender_remote() or not isinstance(data, dict):
        return
    if str(data.get("task_id") or "") != _remote_task_id:
        return
    if current_authenticated_device() != _remote_task_owner:
        return
    pending = _remote_pending_question
    if not pending:
        return
    prompt_id, fut = pending
    supplied = str(data.get("prompt_id") or "")
    if supplied and supplied != prompt_id:
        return
    if not fut.done():
        fut.set_result(str(data.get("answer") or ""))


async def handle_task_subscribe(data) -> None:
    """Remote phone re-attaching to its in-flight task after a reconnect — the old
    socket's subscription died with it, so future step events would otherwise never
    reach the new socket. Ownership-checked to the submitting device, then it
    replays the events missed while disconnected so the phone closes its seq gap
    instead of buffering (and eventually false-timing-out). A task that already
    FINISHED replays from _remote_finished, terminal status included."""
    if not is_current_sender_remote() or not isinstance(data, dict):
        return
    task_id = str(data.get("task_id") or "")
    if not task_id:
        return
    running = task_id == _remote_task_id
    if running:
        owner, journal = _remote_task_owner, _remote_task_journal
    elif task_id in _remote_finished:
        owner, journal = _remote_finished[task_id]
    else:
        return
    if current_authenticated_device() != owner:
        return
    subscribe_task(task_id)              # attach THIS (reconnected) socket
    try:
        resume_after = int(data.get("resume_after_seq") or 0)
    except (TypeError, ValueError):
        resume_after = 0
    # Replay missed events WITH their original seqs so the phone advances its cursor
    # contiguously. Held under the emit lock so a live step can't interleave the
    # replay out of order. (Loopback HUDs subscribed to the same task see a few
    # duplicate rows on a reconnect — harmless; they key by seq.)
    if _remote_emit_lock is not None:
        async with _remote_emit_lock:
            for seq, event, payload in list(journal):
                if seq > resume_after:
                    await emit_task_event(task_id, event, payload, seq=seq)
    if not running:
        return
    # seq=0 → out-of-band keepalive: resets the phone's idle watchdog immediately.
    await emit_task_event(task_id, "task.status",
                          {"task_id": task_id, "state": "running",
                           "v": REMOTE_PROTOCOL_VERSION}, seq=0)


# ── Live remote-desktop: WebRTC screen view + direct mouse/keyboard ───────────
# Signalling (offer/answer/ICE) and input ride the SAME signed phone socket; the
# video flows over a real WebRTC PeerConnection (see server/webrtc_screen.py).

def _screen_owner() -> bool:
    """True when the current sender owns the active screen session."""
    return bool(
        _remote_screen_lease
        and _remote_screen_lease.get("client_id") == current_client_id()
        and _remote_screen_lease.get("device_id") == current_authenticated_device()
    )


async def _send_screen_error(message: str) -> None:
    await send_to_current("webrtc_error", {"message": message})


def _release_remote_control() -> bool:
    """Drop the control lease + disarm the mouse/keyboard, releasing any held
    button. Returns whether anything was actually armed (so the caller knows to
    refresh the control_state banner)."""
    had_lease = bool(_remote_control_lease)
    _remote_control_lease.clear()
    was_armed = computer.is_armed()
    try:
        computer.release_inputs()
    except Exception:  # noqa: BLE001
        pass
    if was_armed:
        computer.disarm()
    return had_lease or was_armed


async def handle_webrtc_offer(data) -> None:
    """Phone opened the live-view: mint a screen session bound to this connection
    + device, answer its WebRTC offer, and start streaming the screen."""
    data = data if isinstance(data, dict) else {}
    client_id = current_client_id()
    device_id = current_authenticated_device()
    if not is_current_sender_remote() or not client_id or not device_id:
        await _send_screen_error("Screen streaming requires an authenticated phone.")
        return
    if not webrtc_screen.is_available():
        await _send_screen_error(
            f"Screen streaming is unavailable on this PC ({webrtc_screen.import_error()}).")
        return
    if _remote_screen_lock is None:
        await _send_screen_error("Screen streaming is not ready.")
        return
    async with _remote_screen_lock:
        existing = _remote_screen_lease
        # Same DEVICE on a new socket = the phone reconnecting (network switch,
        # app resume) while the server still holds its dead socket for up to the
        # ~40s ping timeout — let it take over rather than refusing its own screen.
        if existing and existing.get("device_id") != device_id:
            await _send_screen_error("Another device owns the active screen session.")
            return
        if existing:
            await webrtc_screen.stop()      # same phone renewing — clean restart
            if _release_remote_control():
                await _emit_control_state()
        session_id = "screen-" + os.urandom(18).hex()
        _remote_screen_lease.clear()
        _remote_screen_lease.update({
            "client_id": client_id, "device_id": device_id,
            "session_id": session_id, "answered": False,
        })

        async def _send_session_event(event, payload):
            # The phone learns the session id ONLY from the answer, then must echo
            # it back on every ice/stop/arm/input message or the host rejects it.
            if event == "webrtc_answer" and isinstance(payload, dict):
                if _remote_screen_lease.get("session_id") != session_id:
                    return
                _remote_screen_lease["answered"] = True
                payload = {**payload, "session_id": session_id,
                           "screen_session_id": session_id}
            await send_to_current(event, payload)

        safe_offer = {k: data[k] for k in ("offer", "fps", "max_side", "ice_servers")
                      if k in data}
        await webrtc_screen.handle_offer(safe_offer, _send_session_event)
        if (not _remote_screen_lease.get("answered")
                and _remote_screen_lease.get("session_id") == session_id):
            _clear_remote_screen_lease()


async def handle_webrtc_ice(data) -> None:
    """Phone trickled an ICE candidate for its owned, answered session."""
    data = data if isinstance(data, dict) else {}
    if _remote_screen_lock is None:
        return
    async with _remote_screen_lock:
        if (not _screen_owner()
                or str(data.get("screen_session_id") or "") != _remote_screen_lease.get("session_id")
                or not _remote_screen_lease.get("answered")):
            return
        await webrtc_screen.handle_ice({"candidate": data.get("candidate")})


async def handle_stop_screen(data) -> None:
    """Phone closed the live view — free the capturer and any control lease."""
    data = data if isinstance(data, dict) else {}
    if _remote_screen_lock is None:
        return
    async with _remote_screen_lock:
        if not _screen_owner():
            return
        await webrtc_screen.stop()
        released = _release_remote_control()
        _clear_remote_screen_lease()
    if released:
        await _emit_control_state()


def _clear_remote_screen_lease() -> None:
    _remote_screen_lease.clear()


async def handle_arm_control(data) -> None:
    """Phone armed direct mouse/keyboard over its live view. The phone already
    gated this behind a device fingerprint (useBrain.js requireBiometric) and the
    socket is crypto-authenticated as the owner — THAT is the consent, exactly
    like pc_task. No PC-side prompt (the user is away from the PC, which is the
    whole point of remote control); the owner just gets an out-of-band heads-up."""
    data = data if isinstance(data, dict) else {}
    client_id = current_client_id()
    device_id = current_authenticated_device()

    async def _deny(message: str) -> None:
        await send_to_current("remote_input_ack",
                              {"action": "arm", "ok": False, "message": message})

    if not is_current_sender_remote() or not client_id or not device_id:
        await _deny("Remote control requires an authenticated phone.")
        return
    if _remote_screen_lock is None:
        await _deny("Remote control is not ready.")
        return
    async with _remote_screen_lock:
        session_id = str(data.get("screen_session_id") or "")
        if (not _screen_owner() or not _remote_screen_lease.get("answered")
                or session_id != _remote_screen_lease.get("session_id")):
            await _deny("Start viewing the PC's screen before arming control, sir.")
            return
        try:
            minutes = min(10.0, max(1.0, float(data.get("minutes") or 5.0)))
        except (TypeError, ValueError):
            minutes = 5.0
        ok, msg = computer.arm(minutes)
        if ok:
            _remote_control_lease.clear()
            _remote_control_lease.update({
                "client_id": client_id, "device_id": device_id,
                "screen_session_id": session_id, "lease_id": os.urandom(18).hex(),
                "expires_at": time.monotonic() + minutes * 60.0, "last_seq": 0,
            })
        lease_id = str(_remote_control_lease.get("lease_id") or "")
    await _emit_control_state()
    await send_to_current("control_state",
                          {**computer.state(), "paused": _control_paused, "lease_id": lease_id})
    await send_to_current("remote_input_ack", {
        "action": "arm", "ok": bool(ok), "message": msg,
        **({"lease_id": lease_id, "screen_session_id": session_id, "next_seq": 1} if ok else {}),
    })
    if ok:
        # Arming grants live physical control without ever passing through a PC
        # prompt — so alert the owner out of band on their own HUD.
        with contextlib.suppress(Exception):
            await emit("warning",
                       f"Your PC is now under live remote control from paired device {device_id!r}. "
                       "Press STOP CONTROL if this wasn't you.")


async def handle_disarm_control(data) -> None:
    """Phone released direct control. Disarming is always safe, so this is lenient
    — it clears the lease and disarms regardless of an exact envelope match."""
    data = data if isinstance(data, dict) else {}
    if _remote_input_lock is None:
        return
    binding = {"lease_id": str(data.get("lease_id") or ""),
               "screen_session_id": str(data.get("screen_session_id") or "")}
    if "seq" in data:
        binding["seq"] = data.get("seq")
    async with _remote_input_lock:
        _release_remote_control()
        ok, msg = computer.disarm()
    await _emit_control_state()
    await send_to_current("control_state",
                          {**computer.state(), "paused": _control_paused, "lease_id": ""})
    await send_to_current("remote_input_ack",
                          {**binding, "action": "disarm", "ok": bool(ok), "message": msg})


def _remote_control_error(data: dict) -> str:
    """Validate one input envelope against the live control lease: same owner,
    same still-open screen session, matching lease_id, unexpired, and a seq that
    advances (dedup/replay guard). Empty string when the input may proceed."""
    lease = _remote_control_lease
    if not lease:
        return "Remote input isn't armed — arm control first, sir."
    if (lease.get("client_id") != current_client_id()
            or lease.get("device_id") != current_authenticated_device()):
        return "That control lease belongs to a different device."
    screen = _remote_screen_lease
    if not screen or screen.get("session_id") != lease.get("screen_session_id"):
        _remote_control_lease.clear()
        return "The bound screen session ended."
    if str(data.get("screen_session_id") or "") != screen.get("session_id"):
        return "Remote input has a stale screen-session id."
    if str(data.get("lease_id") or "") != lease.get("lease_id"):
        return "The control lease id is missing or stale."
    if time.monotonic() >= float(lease.get("expires_at") or 0):
        _remote_control_lease.clear()
        return "The remote-control lease expired — re-arm to continue."
    try:
        seq = int(data.get("seq"))
    except (TypeError, ValueError):
        return "Remote input needs a sequence number."
    if seq <= int(lease.get("last_seq") or 0):
        return "Duplicate or out-of-order input dropped."
    lease["last_seq"] = seq
    return ""


async def handle_remote_input(data) -> None:
    """One direct input event from the phone's live-view canvas, serialized and
    lease-checked, then routed to the guarded computer.* primitives."""
    if not isinstance(data, dict):
        return
    if _remote_input_lock is None:
        return
    async with _remote_input_lock:
        error = _remote_control_error(data)
        if error:
            await send_to_current("remote_input_ack", {
                "lease_id": str(data.get("lease_id") or ""), "seq": data.get("seq"),
                "action": str(data.get("action") or ""), "ok": False, "message": error,
            })
            return
        action = str(data.get("action") or "").lower()
        loop = asyncio.get_running_loop()

        def _do():
            if action in ("click_xy", "double"):
                return computer.click_xy(data.get("x"), data.get("y"), double=(action == "double"))
            if action == "right_click":
                return computer.right_click_xy(data.get("x"), data.get("y"))
            if action == "move":
                return computer.move_xy(data.get("x"), data.get("y"))
            if action == "down":
                return computer.mouse_down_xy(data.get("x"), data.get("y"))
            if action == "up":
                return computer.mouse_up_xy(data.get("x"), data.get("y"))
            if action == "type":
                return computer.type_text(str(data.get("text") or ""))
            if action == "key":
                return computer.press_keys(str(data.get("key") or ""))
            if action == "key_down":
                return computer.key_down(str(data.get("key") or ""))
            if action == "key_up":
                return computer.key_up(str(data.get("key") or ""))
            if action == "scroll":
                return computer.scroll_amount(data.get("amount"))
            return False, f"Unknown remote input action: {action!r}"

        try:
            ok, msg = await loop.run_in_executor(None, _do)
        except Exception as exc:  # noqa: BLE001
            ok, msg = False, f"Remote input failed: {exc}"
        await send_to_current("remote_input_ack", {
            "lease_id": str(data.get("lease_id") or ""), "seq": data.get("seq"),
            "action": action, "ok": bool(ok), "message": msg,
        })


async def handle_remote_disconnect(_client) -> None:
    """A phone's socket dropped — tear down any screen/control lease it owned so a
    stale session can't linger (and the mouse button is never left held)."""
    if not is_current_sender_remote() or _remote_screen_lock is None:
        return
    client_id = current_client_id()
    device_id = current_authenticated_device()
    released = False
    async with _remote_screen_lock:
        owns_screen = (_remote_screen_lease.get("client_id") == client_id
                       and _remote_screen_lease.get("device_id") == device_id)
        owns_control = (_remote_control_lease.get("client_id") == client_id
                        and _remote_control_lease.get("device_id") == device_id)
        if owns_screen:
            await webrtc_screen.stop()
            _clear_remote_screen_lease()
        if owns_control or owns_screen:
            released = _release_remote_control()
    if released:
        await _emit_control_state()


# ── Browser control (JARVIS's hands, scoped to a browser it owns) ─────────────
# Captured at startup so the browser worker thread (which owns Playwright) can
# safely marshal a HUD state update back onto the event loop.
_main_loop: "asyncio.AbstractEventLoop | None" = None


def _on_browser_state(st: dict) -> None:
    """Push browser open/url/title state to the HUD. Thread-safe — called from
    the browser worker thread, not the event loop."""
    loop = _main_loop
    if loop is None or not loop.is_running():
        return
    loop.call_soon_threadsafe(lambda: asyncio.create_task(emit("browser_state", st)))


def _on_control_state(st: dict) -> None:
    """Push desktop (mouse/keyboard) control armed/disarmed state to the HUD so it
    can show a 'CONTROL ACTIVE' banner with a STOP button. Thread-safe — arm()/
    disarm() may be called from an executor thread, not the event loop."""
    loop = _main_loop
    if loop is None or not loop.is_running():
        return
    payload = {**(st or {}), "paused": _control_paused}
    loop.call_soon_threadsafe(lambda: asyncio.create_task(emit("control_state", payload)))


async def _report_task_failure(label: str, exc: Exception) -> None:
    await emit("warning", f"{label} failed: {exc}")
    await emit("status", "idle")


def _track_task(coro, label: str) -> asyncio.Task:
    """Start a background task and surface failures to the HUD."""
    task = asyncio.create_task(coro)

    def _done(t: asyncio.Task) -> None:
        try:
            t.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:  # noqa: BLE001
            print(f"[Task] {label} failed: {exc}", flush=True)
            loop = _main_loop
            if loop is not None and loop.is_running():
                loop.create_task(_report_task_failure(label, exc))

    task.add_done_callback(_done)
    return task


_PIPELINE_FAIL_MSG = "Sorry sir — something went wrong on my end. Please try again."
_EMPTY_REPLY_MSG = ("I'm afraid I didn't get a useful answer that time, sir. "
                    "Could you try again?")


async def _finish_tts(consumer: "asyncio.Task | None",
                      tts_queue: "asyncio.Queue | None") -> None:
    """Drain a per-turn TTS sentence queue (safe to call when already drained)."""
    if consumer is None or tts_queue is None:
        return
    try:
        tts_queue.put_nowait(None)
        await consumer
    except Exception as exc:  # noqa: BLE001
        print(f"[Pipeline] TTS drain failed: {exc}", flush=True)


async def _report_pipeline_error(exc: Exception, context: str = "") -> None:
    """Log a pipeline failure and surface one calm spoken line to the user."""
    print(f"[Pipeline] {exc}", flush=True)
    try:
        brief = await summarize_error(str(exc), context)
    except Exception:  # noqa: BLE001
        brief = ""
    await emit("response", brief or _PIPELINE_FAIL_MSG)


async def handle_browser_control(data) -> None:
    """HUD browser toggle: open JARVIS's browser (and grant control consent), or
    close it (and revoke). Toggling on from the HUD IS the user's approval, so no
    separate dialog is needed. Blocking Playwright runs in an executor."""
    if not isinstance(data, dict):
        return
    loop = asyncio.get_running_loop()
    if data.get("open"):
        browser.approve()                       # the toggle is explicit consent
        _, msg = await loop.run_in_executor(None, browser.open_blank)
    else:
        browser.revoke()
        _, msg = await loop.run_in_executor(None, browser.close)
    await emit("response", msg)


async def handle_browser_panel(data) -> None:
    """Companion side panel opened/closed — tile the Playwright browser beside it."""
    if not isinstance(data, dict):
        return
    if not data.get("open"):
        return
    width = data.get("width", 400)
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, browser.dock_browser_window, width)


# ── Home-screen customization + voice modes ───────────────────────────────────

async def _emit_screen() -> None:
    await emit("screen", get_screen())


async def _apply_screen_results(results: "list[dict]") -> bool:
    """Apply any screen-customization action results (persist + broadcast). Returns
    True if at least one was applied, so callers can refresh the HUD."""
    applied = False
    for r in results:
        if r.get("type") == "screen" and isinstance(r.get("screen_patch"), dict):
            set_screen(r["screen_patch"])
            applied = True
    if applied:
        await _emit_screen()
    return applied


async def handle_set_screen(data) -> None:
    """Apply a screen patch from the HUD Customize panel (user-driven), persist
    and broadcast it to every client."""
    if isinstance(data, dict):
        set_screen(data)
        await _emit_screen()


async def handle_set_overlay(data) -> None:
    """Apply the floating-overlay config (enabled / hide-list) from Settings,
    persist and broadcast it."""
    if isinstance(data, dict):
        o = set_overlay(data)
        await emit("overlay", o)


async def handle_set_always_on(data) -> None:
    """Toggle always-on continuous voice mode (no wake word)."""
    on = bool(data.get("on")) if isinstance(data, dict) else bool(data)
    set_always_on(on)
    _always_on_event.set() if on else _always_on_event.clear()
    await emit("always_on", on)
    await emit("response", "Always-on listening is on, sir — I'm all ears."
               if on else "Always-on listening off, sir. Say “Hey JARVIS” to wake me.")


async def handle_set_conversation_mode(data) -> None:
    on = bool(data.get("on")) if isinstance(data, dict) else bool(data)
    set_conversation_mode(on)
    await emit("conversation_mode", on)


def _is_wake_phrase(text: str) -> bool:
    low = (text or "").strip().lower()
    return any(p in low for p in _WAKE_PHRASES)


# ── Streaming helpers: hide tag-blocks from the live display + speak by sentence ──
_FULL_TAG_RE = re.compile(
    r"\[(CHART|SCHEDULE|FLOWCHART|TABLE|ACTION)\b.*?\[/\1\]", re.DOTALL | re.IGNORECASE
)
_TAG_OPEN_RE = re.compile(r"\[(CHART|SCHEDULE|FLOWCHART|TABLE|ACTION)\b", re.IGNORECASE)

# Defensive scrub of the injected "LIVE STATE" senses block. It's a SYSTEM
# message (groq_bridge._system_messages) telling the model its current readings —
# the model must never repeat it, yet some models (seen on gemini-2.5-flash) echo
# it verbatim at the top of a reply. _display_text feeds both the chat AND the
# per-sentence TTS, so scrubbing here keeps the block off the screen and out of
# the voice for EVERY model and path (the existing _strip_reasoning only covers
# gemma/llama-8b). We drop the "LIVE STATE" header and the contiguous run of
# telemetry-shaped readout lines that follows; a lone such line (a genuine
# "Weather: sunny") is preserved.
_LIVE_HEADER_RE = re.compile(r"(?im)^\s*LIVE\s*STATE\b.*$")
# The unmistakable first line of build_live_context() ("Time: 14:14, Monday …")
# also marks the block start when the header itself wasn't echoed.
_LIVE_START_RE = re.compile(r"(?im)^\s*Time:\s*\d{1,2}:\d{2},\s")
_LIVE_LINE_RE = re.compile(
    r"(?im)^\s*(Time|System|Battery|Network|Active window|Weather|Agenda)\b")


def _strip_live_state(text: str) -> str:
    if not text or ("LIVE STATE" not in text.upper()
                    and not _LIVE_LINE_RE.search(text)):
        return text
    out: "list[str]" = []
    run: "list[str]" = []
    in_block = False
    for ln in text.split("\n"):
        if _LIVE_HEADER_RE.match(ln):
            in_block, run = True, []          # everything readout-shaped after → block
            continue
        if _LIVE_LINE_RE.match(ln):
            if _LIVE_START_RE.match(ln):
                in_block = True               # the readout's known first line
            run.append(ln)
            continue
        if not (in_block or len(run) >= 2):   # a lone readout-shaped line → keep
            out.extend(run)
        run, in_block = [], False
        out.append(ln)
    if not (in_block or len(run) >= 2):
        out.extend(run)
    return "\n".join(out).strip()


def _display_text(raw: str) -> str:
    """Prose only: drop completed [TAG]…[/TAG] blocks, any half-streamed tag, and
    any echoed LIVE STATE senses block so the chat shows clean text while
    charts/tables render at the end."""
    t = _FULL_TAG_RE.sub("", raw)
    m = _TAG_OPEN_RE.search(t)          # an opening tag with no closer yet → cut it off
    if m:
        t = t[: m.start()]
    t = _strip_live_state(t)
    return re.sub(r"[ \t]+", " ", t).strip()


# The internal note note_outcome() staples onto an assistant turn so JARVIS can
# recall real file paths — strip it before showing a restored bubble to the user.
_OUTCOME_NOTE_RE = re.compile(r"\s*\[Action results you should remember:.*$", re.DOTALL)


def _history_for_display() -> list:
    """Turn the restored LLM history into clean HUD chat bubbles so a reopened
    app shows the prior conversation instead of a blank log."""
    out = []
    for m in get_history():
        role = m.get("role")
        content = m.get("content") or ""
        if role == "user":
            text = content.strip()
            if text:
                out.append({"role": "user", "text": text})
        elif role == "assistant":
            text = _display_text(_OUTCOME_NOTE_RE.sub("", content))
            if text:
                out.append({"role": "jarvis", "text": text})
    return out


# Markers of a leaked chain-of-thought (seen on Gemma + the fast Llama-8B):
# bullet/header lines like "* Persona:", "Constraint:", "Action block:", and
# narration of intent like "The user wants…", "I should…", "Let me…". Specific
# enough that a normal reply (even one with markdown bullets) won't trip it.
_LEAK_MARKER_RE = re.compile(
    r"(?im)^\s*[\*\-]?\s*(persona|constraints?|action required|action block|"
    r"role|output format|reasoning|step\s*\d|user wants|the user (?:wants|is "
    r"asking|didn't|hasn't|has not|did not)|i should|i need to|i have to|i'll "
    r"need|i will (?:ask|need)|let me|first,?\s|since (?:no|the user)|"
    r"they (?:want|didn't|did not|haven't))\b"
)

# Content inside quotes — Gemma tends to wrap its actual spoken reply in quotes
# and surround it with reasoning. Ordered most- to least-specific.
_QUOTE_PATTERNS = (
    r"''(.+?)''",          # doubled single quotes  ''like this''
    r"“([^”]{3,})”",       # smart double quotes
    r"‘([^’]{3,})’",       # smart single quotes
    r'"([^"]{3,})"',       # straight double quotes
)


def _extract_quoted(text: str) -> str:
    """Return the longest quoted span found in `text` (any common quote style),
    or '' if there are none. Used for Gemma, whose real reply is the quoted bit."""
    cands: list[str] = []
    for pat in _QUOTE_PATTERNS:
        cands += [m.strip() for m in re.findall(pat, text, re.DOTALL) if m.strip()]
    # Whole reply wrapped in a single pair of straight single quotes.
    t = text.strip()
    if len(t) >= 2 and t[0] == "'" and t[-1] == "'" and t.count("'") == 2:
        cands.append(t[1:-1].strip())
    return max(cands, key=len) if cands else ""


def _strip_reasoning(text: str, quote_only: bool = False) -> str:
    """Recover the real spoken reply from a leaked chain-of-thought.

    For Gemma (`quote_only=True`) the user only wants what's inside the quotes, so
    we return the longest quoted span and nothing else whenever one exists. For
    other leak-prone models (the fast Llama-8B) we only intervene when reasoning
    markers are present: prefer a quoted/back-ticked sentence, else drop the
    bullet/header/intent-narration lines. A no-op on clean replies.
    """
    if not text:
        return text
    if quote_only:
        quoted = _extract_quoted(text)
        if quoted:
            return quoted
        # No quotes at all — fall through to the generic cleanup below.
    if not _LEAK_MARKER_RE.search(text):
        return text
    cands = [q.strip() for q in re.findall(r'"([^"]{4,})"', text) if " " in q]
    cands += [q.strip() for q in re.findall(r"`{1,3}([^`]{4,})`{1,3}", text) if " " in q]
    if cands:
        return max(cands, key=len)
    keep = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s or s[0] in "*-#" or _LEAK_MARKER_RE.match(s):
            continue
        keep.append(s)
    return " ".join(keep).strip() or text


def _complete_sentences(text: str, start: int):
    """Return (new complete sentences after `start`, new cursor). A sentence is
    complete once it ends in . ! ? (or a newline) — so we can speak it early."""
    seg = text[start:]
    out, last = [], 0
    for m in re.finditer(r"[.!?]+(?=\s)|\n+", seg):
        s = seg[last:m.end()].strip()
        if len(s) > 1:
            out.append(s)
        last = m.end()
    return out, start + last


async def _drain_tts(queue: "asyncio.Queue") -> None:
    """Consume queued sentences and speak them in order until a None sentinel.
    Honours the mute switch live, so toggling mute silences in-flight speech too.
    Stops pulling the moment the user hits Stop, so queued sentences are dropped
    (not just the one currently playing)."""
    while True:
        s = await queue.get()
        if s is None:
            return
        if _interrupted():
            continue          # drained: skip remaining sentences until the sentinel
        if _tts and not skills.is_muted():
            await _tts.speak(s)


# How many observe→act cycles the text pipeline may run AFTER the first turn.
# Each cycle feeds the previous actions' results back and lets the model take the
# NEXT action (e.g. browser list → click → list → …). Bounded so it always ends.
_MAX_AGENTIC_STEPS = 6

def _did_browser_mutation(results: "list[dict]") -> bool:
    """True if a step successfully performed a page-CHANGING browser action (i.e.
    any browser verb that isn't read-only), so we should re-observe the page before
    letting the model decide its next move — a click/type returns no page text to
    react to. Reuses actions' canonical read-only verb set so the two never drift."""
    for r in results or []:
        if r.get("ok") and r.get("type") in ("browser", "web", "webbrowser") \
                and str(r.get("do") or "").lower() not in _BROWSER_READONLY:
            return True
    return False


# Only these domains benefit from agentic self-correction: the observe→act loop
# re-lists a changed page/window and retries. A failed ONE-SHOT action (a HUD
# restyle, a clarifying question like "Which panel, sir?", a weather miss) is
# terminal — it should be reported, NOT fed back as something to "fix" by taking
# another action. Feeding those in made the model invent unrelated recovery
# actions (e.g. re-applying the red theme after a panel clarification).
_RECOVERABLE_FAIL_TYPES = frozenset((
    "browser", "web", "webbrowser", "computer", "desktop",
))


def _failure_feeds(results: "list[dict]") -> "list[dict]":
    """Turn RECOVERABLE action failures (browser/computer) into a feed for the
    agentic loop, so the model SEES what went wrong and can self-correct (re-list a
    changed page and click a fresh number, fix malformed JSON, try a different
    approach). One-shot/user-facing failures are deliberately excluded — they are
    reported to the user and end the turn, never retried."""
    lines = []
    for r in results or []:
        if (r.get("ok") is False and r.get("message")
                and str(r.get("type") or "").lower() in _RECOVERABLE_FAIL_TYPES):
            do = f"/{r.get('do')}" if r.get("do") else ""
            lines.append(f"{r.get('type')}{do} FAILED: {r['message']}")
    if not lines:
        return []
    return [{"name": "failed actions", "content": "\n".join(lines)}]


# Result types whose `message` IS the answer the user asked for (time, weather,
# directions…) — always spoken in full. Everything else is a CONFIRMATION
# ("Opening Notepad", "Hiding the power panel") whose detail already shows in the
# ✓ card, so 2+ of them collapse into ONE spoken line instead of being read out
# one by one (the repetitive readout the user disliked).
_INFO_RESULT_TYPES = frozenset((
    "time", "current_time", "clock", "day", "date", "current_day",
    "ip", "ip_address", "location", "internet_speed", "speedtest",
    "weather", "forecast", "temperature", "places", "nearby",
    "directions", "route", "distance_to", "news", "headlines",
))


def _strip_politeness(msg: str) -> str:
    """Trim a trailing ', sir.' / 'sir.' and full stop for splicing into a list."""
    return re.sub(r",?\s*sir\.?\s*$", "", (msg or "").strip().rstrip("."),
                  flags=re.IGNORECASE).strip()


def _summarize_confirmations(confirms: "list[dict]") -> str:
    """Collapse 2+ confirmation results into ONE short spoken line — e.g. 'Done,
    sir — hid 4 panels and reset the display' instead of reading five near-
    identical lines aloud. Panel show/hide are counted; other actions contribute
    one brief clause each (capped). No model call — pure, instant, deterministic."""
    from collections import Counter
    panel_counts: "Counter" = Counter()
    others: "list[dict]" = []
    for r in confirms:
        t = str(r.get("type") or "").lower()
        do = str(r.get("do") or "").lower()
        if t in ("screen", "theme", "hud") and do in ("hide_panel", "show_panel",
                                                       "hide_all", "show_all"):
            verb = "hid" if "hide" in do else "showed"
            n = len((r.get("screen_patch") or {}).get("panels", {})) or 1
            panel_counts[verb] += n
        else:
            others.append(r)
    clauses = [f"{verb} {n} panel{'s' if n != 1 else ''}"
               for verb, n in panel_counts.items()]
    for r in others[:2]:
        c = _strip_politeness(r.get("message") or "")
        if c:
            clauses.append(c[0].lower() + c[1:])
    extra = len(others) - 2
    if extra > 0:
        clauses.append(f"{extra} more")
    if not clauses:
        return "Done, sir."
    body = clauses[0] if len(clauses) == 1 else \
        ", ".join(clauses[:-1]) + " and " + clauses[-1]
    return f"Done, sir — {body}."


def _spoken_summary(results: "list[dict]") -> str:
    """The spoken confirmation for a batch of successful action results: real
    ANSWERS (weather/time/news…) spoken in full, plus ONE collapsed line for 2+
    plain confirmations. A `speak` of "" keeps a result silent (e.g. listings)."""
    ok = [r for r in results or [] if r.get("ok") and not r.get("needs_autopilot")]
    info, confirm = [], []
    for r in ok:
        (info if str(r.get("type") or "").lower() in _INFO_RESULT_TYPES
         else confirm).append(r)
    parts = [r.get("speak", r.get("message")) or "" for r in info]
    # Confirmations that aren't explicitly silenced (speak="").
    loud = [r for r in confirm if r.get("speak", r.get("message"))]
    if len(loud) == 1:
        parts.append(loud[0].get("speak", loud[0].get("message")) or "")
    elif len(loud) >= 2:
        parts.append(_summarize_confirmations(loud))
    return " ".join(p for p in parts if p).strip()


def _browser_failed(results: "list[dict]") -> bool:
    return any(r.get("ok") is False and r.get("type") in ("browser", "web", "webbrowser")
               for r in results or [])


# Conjunctions/separators that join tasks in a compound request.
_CLAUSE_SPLIT_RE = re.compile(
    r"\b(?:and(?:\s+then)?|then|also|plus|after\s+that|afterwards)\b|[;,]",
    re.IGNORECASE)


def _suspected_missed_tasks(user_text: str, results: "list[dict]") -> bool:
    """Cheap heuristic for 'the model probably dropped a task': the request has
    two or more action-shaped clauses but fewer actions were emitted. Used to
    trigger ONE silent completion-check turn — the last layer of defence after
    prompt rules + low temperature + big-model routing."""
    from llm.groq_bridge import _ACTIONY_RE
    clauses = [c for c in _CLAUSE_SPLIT_RE.split(user_text or "") if c and c.strip()]
    if len(clauses) < 2:
        return False
    asked = sum(1 for c in clauses if _ACTIONY_RE.search(c))
    return asked >= 2 and len(results or []) < asked


# A reply that asserts the deed is already done — the tell-tale phrasing of the
# "said it did it but didn't" failure when it is paired with ZERO executed
# actions. Kept tight (completion-claim verbs, not mere mentions) so genuine Q&A
# answers that happen to contain a command word don't trip it.
_COMPLETION_CLAIM_RE = re.compile(
    r"(?:\bi['’]?ve\b|\bi have\b|\bdone\b|\ball set\b|\bconsider it done\b|"
    r"\bright away\b|\bas you wish\b|\bhere you go\b|\bthere you go\b|\bon it\b|"
    r"\b(?:is|are|it['’]?s)\s+now\b|"
    r"\b(?:set|changed|changing|switched|switching|turned|turning|toggled|toggling|"
    r"opened|opening|closed|closing|started|starting|stopped|stopping|"
    r"launched|launching|played|playing|paused|pausing|muted|muting|unmuted|"
    r"unmuting|created|creating|made|making|generated|generating|enabled|enabling|"
    r"disabled|disabling|adjusted|adjusting|updated|updating|applied|applying|"
    r"recolou?red|recolou?ring|moved|moving|hidden|hiding|shown|showing)\b)",
    re.IGNORECASE,
)


def _claimed_done_without_acting(user_text: str, reply_text: str,
                                 results: "list[dict]") -> bool:
    """Detect the single-command 'said it did it but didn't' failure: the request
    was action-shaped, the model emitted NO action at all, yet its spoken reply
    claims the task is done. This is the counterpart to _suspected_missed_tasks,
    which only guards compound (2+-clause) requests — a lone command like "set the
    HUD to blue" otherwise has no safety net, so a model that drops the [ACTION]
    block reports false success. When True, the caller runs ONE silent
    completion-check turn so the action actually runs (or the model says it had
    nothing to do)."""
    from llm.groq_bridge import _ACTIONY_RE
    if results:                                 # something ran — not this failure
        return False
    if not _ACTIONY_RE.search(user_text or ""):  # not a command turn — leave Q&A alone
        return False
    return bool(_COMPLETION_CLAIM_RE.search(reply_text or ""))


async def _completion_check(user_text: str, observed_results: "list[dict]",
                            task_tier: str = "") -> "list[dict]":
    """Run a silent slim turn asking the model to emit ONLY the [ACTION]s still
    missing from a compound request (or 'OK' if none). No chat bubble and no
    speech for the check itself — only recovered actions surface. Returns the
    feeds from any recovered actions so the agentic loop can continue."""
    done = "; ".join(filter(None, (r.get("message") for r in observed_results))) \
        or "nothing has been done yet"
    check = (f"My request was: \"{user_text}\". So far you have done: {done}. "
             "If any part of my request has NOT been handled yet, do the missing "
             "part(s) now — call the matching function (or emit the missing [ACTION] "
             "block) with no prose. If everything is already handled, reply with "
             "exactly: OK")
    raw = ""
    tool_sink: "list" = []
    async for _d, raw in stream_prompt(check, record_history=False, slim=True,
                                       tool_sink=tool_sink, task_tier=task_tier):
        if _interrupted():
            return []
    loop = asyncio.get_running_loop()
    if tool_sink:
        specs = [live_tools.to_spec(c.get("name", ""), c.get("args")) for c in tool_sink]
        results = await loop.run_in_executor(None, run_specs, specs)
    else:
        _clean, results = await loop.run_in_executor(None, execute_actions, raw)
    if not results:
        return []
    print(f"[Check] recovered {len(results)} missed task(s)", flush=True)
    feeds = await _surface_results(results, user_text)
    # Speak the recovered actions' outcomes (the check turn itself stays silent),
    # collapsed the same way as the first turn so recovery never reads a long list.
    ok_speech = _spoken_summary(results)
    if ok_speech and _tts and not skills.is_muted():
        await emit("status", "speaking")
        await _tts.speak(ok_speech)
    return feeds


_COMPUTER_MUTATING = {"click", "click_element", "press_element", "tap",
                      "double_click", "type", "type_text", "write", "enter_text",
                      "fill", "press", "key", "keys", "hotkey", "shortcut",
                      "send_keys", "scroll"}


async def _observe_browser_if_needed(feeds: "list[dict]",
                                     observed_results: "list[dict]") -> None:
    """Append a fresh element/control listing to `feeds` when the model will need
    eyes for its next step: after a successful page/UI-changing action, or after a
    FAILED browser/computer action (stale numbers / wrong element) while control
    is still live. Skipped when this step already produced a listing."""
    loop = asyncio.get_running_loop()
    if not any(f.get("name") == "browser page elements" for f in feeds):
        if _did_browser_mutation(observed_results) \
                or (_browser_failed(observed_results) and browser.is_open()):
            ok, _msg, listing = await loop.run_in_executor(None, browser.list_elements)
            if ok and listing:
                feeds.append({"name": "browser page elements", "content": listing})
    # Same re-observe for desktop control: after a click/type/keystroke the UI
    # likely changed (or the click failed because it had) — re-list the window.
    if not any(f.get("name") == "desktop ui controls" for f in feeds):
        relevant = [r for r in observed_results or [] if r.get("type") == "computer"]
        acted = any(r.get("ok") and str(r.get("do")) in _COMPUTER_MUTATING for r in relevant)
        failed = any(r.get("ok") is False for r in relevant)
        if (acted or failed) and computer.is_armed():
            ok, _msg, listing = await loop.run_in_executor(None, computer.list_ui)
            if ok and listing:
                feeds.append({"name": "desktop ui controls", "content": listing})


async def _apply_action_side_effects(results: "list[dict]") -> None:
    """Mirror an executed action batch's side-effects to the HUD + memory: outcome
    recall, command log, recordings/schedule refresh, in-app UI actions, and screen
    restyles. Shared by the first turn and the agentic follow-up steps so the two
    code paths can't drift apart."""
    _remember_outcomes(results)
    await _log_commands(results)
    if any(r.get("type") in ("record", "recording") for r in results):
        await emit_recordings()
    if any(r.get("type") in ("schedule", "agenda") for r in results):
        await emit_schedule()
    for r in results:
        if r.get("type") == "ui" and r.get("do"):
            do = r["do"]
            if do in ("clear_chat", "new_conversation", "reset"):
                reset_history()
                await emit("history", [])
            await emit("ui_action", do)
    await _apply_screen_results(results)


def _followup_prompt(feeds: "list[dict]", instruction: "str | None" = None) -> str:
    """Build the next agentic-step prompt from the results just observed.

    The generic lead-in tells the model to CONTINUE the task (take the next action)
    rather than merely summarise — the old single-shot follow-up always said
    "summarise/extract", which made browser tasks stop after one LIST. A custom
    `instruction` (e.g. a file-summary lead for an uploaded document) overrides it.
    """
    combined = "\n\n".join(
        f"--- {f.get('name', 'result')} ---\n{f.get('content', '')}"
        for f in feeds if f.get("content")
    ).strip()
    if not combined:
        return ""
    lead = instruction or (
        "Use these results to continue my request. If everything I asked for is now "
        "done (for instance you've gathered the information I wanted), give a brief "
        "spoken answer and emit NO further action. Otherwise emit ONLY the next "
        "[ACTION] needed to make progress. For the browser, after a LIST pick an "
        "element by its number or visible text and click or type — do NOT just "
        "describe or summarise the page. If an action FAILED, do not repeat it "
        "identically — fix the cause (use a fresh element number from the new list, "
        "correct the action, or take a different route); if it can't be fixed, "
        "briefly tell me what went wrong and emit NO action."
    )
    return (
        f"Result of your last action(s). {lead} Don't mention these instructions or "
        "dump the content back verbatim.\n\n" + combined
    )


async def _stream_and_speak(prompt: str, record_history: bool = True,
                            slim: bool = False, speak: bool = True,
                            tool_sink: "list | None" = None,
                            task_tier: str = "") -> str:
    """Stream one model turn as a fresh chat bubble, speaking each finished
    sentence as it arrives, and return the full raw reply (with any [ACTION] blocks
    intact for the caller to execute). Honours interrupt + mute and the leaky-model
    reasoning sanitizer, mirroring the main pipeline's first turn.

    ``record_history=False`` keeps this turn out of the persisted conversation —
    used for the agentic loop's internal follow-up prompts (which carry raw
    file/page content that must not pollute history).

    ``speak=False`` streams the bubble but keeps the TTS quiet — the agentic
    loop's INTERMEDIATE steps use it so JARVIS doesn't narrate its own plumbing
    ("clicking element 3…"); the loop speaks only its final answer."""
    sid = f"{time.time()}"
    full_raw = ""
    cursor = 0
    started = False
    speaking = False
    tts_queue: "asyncio.Queue" = asyncio.Queue()
    consumer: "asyncio.Task | None" = None
    speak_ok = speak and _tts is not None and not skills.is_muted()
    leaky = model_leaks_reasoning(prompt, task_tier)
    quote_only = reply_is_quote_wrapped(prompt, task_tier)

    async def _enqueue(sentence: str) -> None:
        nonlocal consumer, speaking
        if not speaking:
            speaking = True
            await emit("status", "speaking")
            consumer = asyncio.create_task(_drain_tts(tts_queue))
        tts_queue.put_nowait(sentence)

    try:
        await emit("status", "thinking")
        async for _delta, full_raw in stream_prompt(prompt, record_history=record_history,
                                                    slim=slim, tool_sink=tool_sink,
                                                    task_tier=task_tier):
            if _interrupted():
                break
            if not started:
                await emit("stream_start", {"sid": sid})
                started = True
            if leaky:
                continue
            disp = _display_text(full_raw)
            await emit("stream_delta", {"sid": sid, "text": disp})
            if speak_ok:
                sentences, cursor = _complete_sentences(disp, cursor)
                for s in sentences:
                    await _enqueue(s)

        if not started:
            await emit("stream_start", {"sid": sid})
        disp = _display_text(full_raw)
        clean = _strip_reasoning(disp, quote_only=quote_only) if leaky else disp
        if not clean.strip() and not tool_sink:
            clean = _EMPTY_REPLY_MSG
        if leaky:
            await emit("stream_delta", {"sid": sid, "text": clean})
        if speak_ok and not _interrupted():
            tail = (clean if leaky else disp[cursor:]).strip()
            if tail:
                await _enqueue(tail)
        await emit("response_end", {"sid": sid, "text": clean, "actions": []})
    except Exception:
        if started:
            await emit("response_end", {"sid": sid, "text": _PIPELINE_FAIL_MSG, "actions": []})
        raise
    finally:
        await _finish_tts(consumer, tts_queue)
    return full_raw


async def _handle_vision(result: dict) -> None:
    """Capture the screen, ask the vision model the result's question, and speak
    the answer. Shared by the first turn, the agentic loop AND the permission-
    approval path — an approved see_screen returns a deferred {needs_vision} result
    that, before this, the approval loops silently dropped (only feed_to_model was
    handled), so "what's on my screen?" did nothing after the user approved."""
    loop = asyncio.get_running_loop()
    await emit("status", "thinking")
    img = await loop.run_in_executor(None, skills.capture_screen_b64)
    if not img:
        answer = "I couldn't capture your screen — the screenshot libraries may be missing."
    else:
        q = (result.get("question") or "Describe what is on the screen and anything notable.").strip()
        answer = (await vision_query(img, q + _SCREEN_READ_GUIDANCE)
                  or "I had a look but couldn't quite make out the details, sir.")
    print(f"[Vision] {answer[:120]}", flush=True)
    await emit("response", answer)
    if _tts and not skills.is_muted():
        await emit("status", "speaking")
        await _tts.speak(answer)


async def _handle_web_search(result: dict, *, speak: bool = True) -> str:
    """Answer a grounded Google-Search query (Gemini) and speak it — JARVIS's own
    'look it up for my knowledge' path, no browser opened. The spoken text carries
    no URLs; the cited sources go to the HUD as a compact card. Shared by the first
    turn, the agentic loop and the permission-approval path. Returns the answer."""
    await emit("status", "thinking")
    query = str(result.get("query") or "").strip()
    ok, answer, sources = await web_answer(query)
    print(f"[WebSearch] {'✓' if ok else '✗'} {answer[:120]}", flush=True)
    await emit("response", answer)
    # Surface citations as a card the HUD can render as links (the spoken answer
    # deliberately carries none). Harmless if the frontend doesn't render it.
    if ok and sources:
        await emit("action", [{
            "type": "web_search", "ok": True, "message": "Sources",
            "query": query[:80],
            "sources": [{"title": s.get("title", ""), "uri": s.get("uri", "")}
                        for s in sources[:5]],
        }])
    if answer and speak and _tts and not skills.is_muted():
        await emit("status", "speaking")
        await _tts.speak(answer)
    return answer


async def _handle_location_set(result: dict, *, speak: bool = True) -> str:
    """Pin or clear the user's location from a voice command ("set my location to
    Powai"). Geocodes + applies it everywhere, shows a card, and (text path) speaks
    the confirmation. Returns the spoken message."""
    do = str(result.get("do") or "set").lower().strip()
    if do in ("clear", "reset", "auto", "automatic", "remove", "off", "unset"):
        ok, msg = await _clear_manual_location()
    else:
        ok, msg = await _set_manual_location(place=str(result.get("place") or "").strip())
    await emit("action", [{"type": "set_location", "ok": ok, "message": msg}])
    await emit("sysinfo", get_sysinfo())
    if speak and msg and _tts and not skills.is_muted():
        await emit("status", "speaking")
        await _tts.speak(msg)
    return msg


async def _persist_generated_image(card: dict, data_url: str, caption: str) -> None:
    """Save a generated image to the storage 'images' folder and annotate its card
    with the filename (`target`) + saved path in the message. This is what makes
    the image reliably KEEPABLE: the GUI's Open/Folder buttons use `target` (the
    in-webview download can silently fail under WebView2), 'open the image you just
    made' works via outcome memory, and the file survives the chat. Best-effort."""
    if not data_url:
        return
    loop = asyncio.get_running_loop()
    saved_ok, path = await loop.run_in_executor(None, skills.save_image_data_url, data_url)
    if saved_ok:
        card["target"] = os.path.basename(path)
        card["message"] = f"{caption} Saved to {path}."


async def _handle_image_gen(result: dict) -> None:
    """Generate the image described by the result, render it as a card, log it,
    and speak the caption. Shared across all execution paths (incl. approval)."""
    await emit("status", "thinking")
    ok, msg, data_url = await generate_image(result.get("prompt", ""))
    card = {"type": "generate_image", "ok": ok, "message": msg,
            **({"image": data_url} if data_url else {})}
    if ok:
        await _persist_generated_image(card, data_url, msg)
    print(f"[Image] {'✓' if ok else '✗'} {card['message'][:100]}", flush=True)
    await emit("action", [card])
    await _log_commands([{**card, "target": result.get("prompt", "")[:40]}])
    if msg and _tts and not skills.is_muted():
        await emit("status", "speaking")
        await _tts.speak(msg)


async def _run_autopilot_task(result: dict, *, speak: bool = True) -> str:
    """Run one deferred autopilot goal (browser_task / computer_task) and report
    ONCE at the end — this is the cure for "JARVIS reads out the button numbers":
    every internal step (observe → decide → click/type) is silent; the HUD shows a
    single live-updating progress bubble, and the user hears exactly one sentence.

    Returns the final outcome line (the voice path hands it to the Live model as
    the tool response instead of speaking it itself — pass ``speak=False`` there).
    """
    kind = result.get("kind") or \
        ("computer" if str(result.get("type", "")).startswith(("computer", "desktop"))
         else "browser")
    goal = str(result.get("goal") or "").strip()
    global _autopilot_owner
    # One autopilot at a time (see _autopilot_owner). Checked and claimed with no
    # await in between, so two starts can't both pass.
    if _autopilot_owner:
        busy = ("I'm in the middle of a task from your phone, sir — stop it first or "
                "give me a moment." if _autopilot_owner == "remote" else
                "I'm already working on another task, sir — stop it first or give me "
                "a moment.")
        await emit("response", busy)
        return busy
    _autopilot_owner = "local"
    try:
        return await _run_autopilot_task_owned(result, kind, goal, speak=speak)
    finally:
        _autopilot_owner = ""


async def _run_autopilot_task_owned(result: dict, kind: str, goal: str, *,
                                    speak: bool) -> str:
    """The body of _run_autopilot_task, run while this process owns the autopilot."""
    global _control_paused, _pending_corrections
    # Start each task un-paused with an empty correction queue, so a stale pause or
    # leftover correction from a prior task can't affect this one.
    _control_paused = False
    _pending_corrections = []
    icon = "🖱" if kind == "computer" else "🌐"
    sid = f"{time.time()}"
    await emit("status", "working")
    await emit("stream_start", {"sid": sid})
    # Agent Activity feed: a dedicated panel mirrors every step AND the screenshots
    # the operator saw (the browser page for browser tasks; the screen for desktop
    # tasks, when autopilot vision is on). These rows are read-only auditability;
    # they never bypass the consent gates.
    await emit("agent_task", {"id": sid, "kind": kind, "goal": goal})
    lines = [f"{icon} On it, sir — {goal}"]
    await emit("stream_delta", {"sid": sid, "text": "\n".join(lines)})

    async def on_step(line: str) -> None:
        # Live progress in the chat bubble (display only — never spoken). The
        # final response_end replaces the step log with the outcome sentence.
        lines.append(f"  → {line}")
        await emit("stream_delta", {"sid": sid, "text": "\n".join(lines)})
        # Mirror to the activity panel as a discrete, persistent row (a leading
        # "✗ " from the operator's progress line marks a failed step).
        await emit("agent_step", {"id": sid, "line": line,
                                  "ok": not line.lstrip().startswith("✗")})

    _shot_sig = {"v": ""}

    async def on_shot(b64: str) -> None:
        # A page screenshot the operator saw this step → downscale to a light
        # thumbnail (off the event loop) and stream it to the activity panel.
        # Skip an unchanged frame so re-observing the same page isn't re-sent.
        if not b64:
            return
        loop = asyncio.get_running_loop()
        thumb = await loop.run_in_executor(None, skills.thumbnail_data_url, b64)
        if not thumb or thumb == _shot_sig["v"]:
            return
        _shot_sig["v"] = thumb
        await emit("agent_shot", {"id": sid, "image": thumb})

    async def ask(question: str) -> str:
        await on_step(f"❓ {question}")
        await emit("status", "idle")
        answer = await request_clarification(question)
        await emit("status", "working")
        await on_step(f"↳ {answer or '(skipped)'}")
        return answer

    async def reconsent(consent_kind: str, task_goal: str) -> bool:
        """The bounded control window expired mid-task — ask for it back rather
        than binning the work. A decline still stops the task."""
        what = "browser control" if consent_kind == "browser" else "computer control"
        await on_step(f"⏳ {what} expired — asking to continue")
        await emit("status", "idle")
        granted = await request_permission(
            consent_kind, f"keep {what} to finish: {task_goal[:60]}")
        await emit("status", "working")
        if granted:
            if consent_kind == "browser":
                browser.approve()
            else:
                computer.arm()
        await on_step("▶ continuing" if granted else "✗ not re-approved")
        return granted

    final = "I couldn't finish that, sir."
    try:
        res = await autopilot.run_task(kind, goal, interrupted=_interrupted,
                                       on_step=on_step, on_shot=on_shot, ask=ask,
                                       paused=_control_is_paused,
                                       corrections=_pop_corrections,
                                       reconsent=reconsent,
                                       hint=playbooks.autopilot_hint(goal))
        summary = (res.get("summary") or "").strip()
        if res.get("stopped"):
            final = "Stopped, sir."
            summary = ""
        else:
            final = summary or ("Done, sir." if res.get("ok")
                                else "I couldn't finish that, sir.")
        await emit("response_end", {"sid": sid, "text": f"{icon} {final}", "actions": []})
        await emit("agent_task_end", {"id": sid, "ok": bool(res.get("ok")),
                                      "summary": final, "stopped": bool(res.get("stopped"))})
        card = {"type": f"{kind}_task", "ok": bool(res.get("ok")), "message": goal[:80],
                "target": goal[:60]}
        findings = res.get("findings") or []
        if findings:
            card["findings"] = [str(f)[:200] for f in findings[:12]]
        await emit("action", [card])
        await _log_commands([card])
        steps = res.get("steps") or []
        print(f"[Autopilot:{kind}] {'✓' if res.get('ok') else '✗'} {goal!r} "
              f"({len(steps)} steps) → {final}", flush=True)
        if steps and not res.get("ok"):
            did = " Steps attempted: " + "; ".join(str(s)[:90] for s in steps[-10:]) + "."
        elif res.get("trace"):
            did = " I did: " + "; ".join(str(t) for t in (res.get("trace") or [])[:12]) + "."
        elif findings:
            did = " I noted: " + "; ".join(str(f)[:120] for f in findings[:8]) + "."
        else:
            did = ""
        note_outcome(f"({kind} autopilot task) goal: {goal!r} → "
                     f"{'completed' if res.get('ok') else 'stopped before finishing'}: "
                     f"{final}{did}")
        if res.get("ok") and not res.get("stopped"):
            try:
                playbooks.capture_auto(goal, res.get("trace") or [])
            except Exception:  # noqa: BLE001
                pass
        if speak and summary and _tts and not skills.is_muted() and not _interrupted():
            await emit("status", "speaking")
            await _tts.speak(summary)
    except Exception as exc:  # noqa: BLE001
        print(f"[Autopilot:{kind}] failed: {exc}", flush=True)
        await emit("response_end", {"sid": sid, "text": f"{icon} {final}", "actions": []})
        await emit("agent_task_end", {"id": sid, "ok": False, "summary": final, "stopped": False})
    finally:
        await emit("status", "idle")
    return final


async def _surface_results(results: "list[dict]", user_text: str) -> "list[dict]":
    """Emit cards / command-log / memory / side-effects for a follow-up step's
    actions, handle gated (permission), vision and image-gen results, and return
    the feed_to_model payloads (plus a fresh browser observation after a page
    change) so the agentic loop can take its next step.

    Unlike the old follow-up — which ran a step's actions but DISCARDED the results
    (no cards, gated actions silently dropped, no further feedback) — this surfaces
    everything the first turn does and keeps the loop fed."""
    results = _autopilot_fallbacks(results, user_text)
    immediate = [r for r in results if not r.get("needs_permission")
                 and not r.get("needs_vision") and not r.get("needs_image_gen")
                 and not r.get("needs_autopilot") and not r.get("needs_web_search")]
    pending = [r for r in results if r.get("needs_permission")]
    vision = [r for r in results if r.get("needs_vision")]
    images = [r for r in results if r.get("needs_image_gen")]
    websearch = [r for r in results if r.get("needs_web_search")]
    autop = [r for r in results if r.get("needs_autopilot")]
    feeds = [r["feed_to_model"] for r in immediate if r.get("feed_to_model")]
    observed_results = list(immediate)
    loop = asyncio.get_running_loop()

    if immediate:
        for r in immediate:
            tag = "✓" if r.get("ok") else "✗"
            print(f"[Action] {tag} (step) {r.get('type')} → {r.get('message')}", flush=True)
        await emit("action", immediate)
        await _apply_action_side_effects(immediate)
        # Failures are fed back to the model below so it can self-correct; the
        # corrective follow-up turn does the talking then (no double-speak).
        err_brief = "" if _failure_feeds(immediate) \
            else await _brief_errors(immediate, user_text)
        if err_brief and _tts and not skills.is_muted():
            await emit("status", "speaking")
            await _tts.speak(err_brief)

    # Vision: look at the screen and speak the answer.
    for _v in vision:
        await _handle_vision(_v)

    # Image generation.
    for im in images:
        await _handle_image_gen(im)

    # Live web search: grounded Gemini answer, spoken with sources (no browser).
    for ws in websearch:
        await _handle_web_search(ws)

    # Autopilot goals: run the silent operator loop; it reports once at the end.
    for a in autop:
        await _run_autopilot_task(a)

    # Gated actions — ask before doing, so a follow-up read/delete isn't silently
    # dropped (the old follow-up never even surfaced these).
    for p in pending:
        kind = p.get("kind", "action")
        # If the user already consented to the browser / computer control earlier
        # in this batch, the consent window is open — don't re-prompt for
        # follow-up actions. One approval covers the whole session.
        if kind == "browser" and browser.is_approved():
            approved = True
        elif kind == "computer" and computer.is_armed():
            approved = True
        else:
            await emit("status", "idle")
            approved = await request_permission(kind, p.get("description", "that action"))
        if approved:
            if kind == "browser":
                browser.approve()
            elif kind == "computer":
                computer.arm()
            res = await loop.run_in_executor(None, run_action, p["spec"])
            # An approved see_screen / generate_image / autopilot goal returns a
            # DEFERRED result that still has to be run — route it through the
            # shared handlers instead of dropping it.
            if res.get("needs_vision"):
                await _handle_vision(res)
                continue
            if res.get("needs_image_gen"):
                await _handle_image_gen(res)
                continue
            if res.get("needs_web_search"):
                await _handle_web_search(res)
                continue
            if res.get("needs_autopilot"):
                await _run_autopilot_task(res)
                continue
            await emit("action", [res])
            _remember_outcomes([res])
            await _log_commands([res])
            observed_results.append(res)
            if res.get("feed_to_model"):
                feeds.append(res["feed_to_model"])
        else:
            msg = _spoken_refusal(p.get("description"))
            await emit("action", [{"type": p.get("type"), "ok": False, "message": msg}])

    # Feed failures back so the model can self-correct, and re-observe the page
    # after a page-changing (or failed) browser action so it has eyes for the
    # next step.
    feeds += _failure_feeds(observed_results)
    await _observe_browser_if_needed(feeds, observed_results)
    return feeds


async def _agentic_loop(feeds: "list[dict]", user_text: str = "",
                        instruction: "str | None" = None,
                        max_steps: int = _MAX_AGENTIC_STEPS,
                        task_tier: str = "") -> None:
    """Observe→act loop for the text pipeline: feed the previous actions' results
    back, let the model take the NEXT action, and repeat until it stops emitting
    actions (or the step cap). This is what lets JARVIS actually COMPLETE multi-step
    browser tasks (open → list → click → list → …) and chained requests, instead of
    doing only the first action and halting.

    Text-only models also "read" files this way: the first iteration carries the
    extracted file/dir/page text (optionally with a custom `instruction`, e.g. a
    file-summary lead), the model answers, emits no action, and the loop ends."""
    loop = asyncio.get_running_loop()
    for step in range(max_steps):
        if _interrupted() or not feeds:
            return
        prompt = _followup_prompt(feeds, instruction if step == 0 else None)
        if not prompt:
            return
        # Internal scaffolding turn — keep it out of the persisted conversation,
        # send the SLIM system prompt (these mechanical steps don't need the
        # full ~4k-token prompt, which is what was burning Groq's token budget),
        # and DON'T speak it: intermediate prose is plumbing narration ("let me
        # click the first result…"), exactly what the user doesn't want to hear.
        tool_sink: "list" = []
        full_raw = await _stream_and_speak(prompt, record_history=False, slim=True,
                                           speak=False, tool_sink=tool_sink,
                                           task_tier=task_tier)
        if _interrupted():
            return
        if tool_sink:
            specs = [live_tools.to_spec(c.get("name", ""), c.get("args")) for c in tool_sink]
            results = await loop.run_in_executor(None, run_specs, specs)
        else:
            _clean, results = await loop.run_in_executor(None, execute_actions, full_raw)
        if not results:            # model answered with no further action → done
            # This final turn IS the answer (a file summary, the gathered info) —
            # speak it once, now that we know no more actions follow.
            disp = _display_text(full_raw)
            if model_leaks_reasoning(prompt, task_tier):
                disp = _strip_reasoning(disp,
                                        quote_only=reply_is_quote_wrapped(prompt, task_tier))
            disp = disp.strip()
            if disp and _tts and not skills.is_muted() and not _interrupted():
                await emit("status", "speaking")
                await _tts.speak(disp)
            return
        feeds = await _surface_results(results, user_text)


def _voice_system_instruction() -> str:
    """A compact persona prompt for native-audio (Live API) sessions.

    The Live API speaks/listens directly, so we don't use the big tag-emitting
    SYSTEM_PROMPT here (those tags would just be read aloud). Instead: persona,
    brevity, the no-volunteer-status rule, the tool-use contract, and the current
    LIVE STATE as senses. Actions are performed via real function calls (see
    live_tools) — not spoken tags."""
    ctx = build_live_context()
    return (
        "You are JARVIS (Tony Stark's): composed, quietly witty. Speaking aloud — "
        "1–2 sentences; occasional 'sir'. LIVE STATE is private senses — mention "
        "only if asked. CALL the matching function; confirm from its real result; "
        "never read element numbers or page dumps aloud.\n\n"
        + CAPABILITY_INDEX + "\n\n"
        "LIVE STATE (mention only if asked):\n" + ctx
    )


async def _execute_voice_tool(call: dict) -> dict:
    """Run ONE Live-API function call through the shared action framework and
    return a Gemini functionResponse dict.

    This is what makes native-audio JARVIS actually act — and confirm truthfully.
    It reuses the exact same `actions.run_action` + permission gate the text
    pipeline uses, and mirrors the pipeline's side effects (action cards, command
    log, outcome memory, ui_action forwarding) so the HUD stays in sync. The
    model speaks its confirmation only AFTER seeing the result we hand back."""
    name = call.get("name", "")
    args = call.get("args") or {}
    cid = call.get("id")
    loop = asyncio.get_running_loop()

    def respond(payload: dict) -> dict:
        return {"id": cid, "name": name, "response": payload}

    spec = live_tools.to_spec(name, args)
    print(f"[Voice-Tool] {name}({args}) → {spec}", flush=True)

    # Dangerous actions (shutdown/restart/delete, file reads) need approval — the
    # same Approve/Deny dialog the text pipeline uses.
    gated, kind, desc = needs_permission(spec)
    if gated:
        await emit("status", "idle")
        approved = await request_permission(kind, desc)
        if not approved:
            msg = f"The user declined to let you {_clean_desc(desc)}."
            await emit("action", [{"type": spec.get("type"), "ok": False, "message": msg}])
            return respond({"result": msg})
        if kind == "browser":
            browser.approve()                      # consent granted for follow-up steps
        elif kind == "computer":
            computer.arm()                         # same: approval opens the armed window

    atype = spec.get("type")

    # see_screen → capture the screen + ask the vision model; hand the answer
    # back so JARVIS relays it in its own voice.
    if atype == "see_screen":
        await emit("status", "thinking")
        img = await loop.run_in_executor(None, skills.capture_screen_b64)
        if not img:
            answer = "I couldn't capture the screen — the screenshot libraries may be missing."
        else:
            q = (spec.get("question") or "Describe what is on the screen.").strip()
            answer = (await vision_query(img, q + _SCREEN_READ_GUIDANCE)
                      or "I had a look but couldn't quite make out the details.")
        await emit("action", [{"type": "see_screen", "ok": bool(img), "message": answer}])
        return respond({"result": answer})

    # generate_image → produce a picture and render it as an action card.
    if atype == "generate_image":
        await emit("status", "thinking")
        ok, msg, data_url = await generate_image(spec.get("prompt", ""))
        card = {"type": "generate_image", "ok": ok, "message": msg,
                **({"image": data_url} if data_url else {})}
        if ok:
            await _persist_generated_image(card, data_url, msg)
        await emit("action", [card])
        await _log_commands([{**card, "target": spec.get("prompt", "")[:40]}])
        return respond({"result": msg})

    # Everything else runs synchronously via run_action (blocking → executor).
    res = await loop.run_in_executor(None, run_action, spec)

    # web_search → a grounded Gemini answer pulled live from Google (no browser).
    # Hand the ANSWER back as the tool result so the voice model relays it in its
    # own voice; surface the citations as a HUD card.
    if res.get("needs_web_search"):
        await emit("status", "thinking")
        ok_ws, answer, sources = await web_answer(res.get("query", ""))
        card = {"type": "web_search", "ok": ok_ws,
                "message": "Sources" if sources else answer}
        if sources:
            card["sources"] = [{"title": s.get("title", ""), "uri": s.get("uri", "")}
                               for s in sources[:5]]
        await emit("action", [card])
        return respond({"result": answer})

    # browser_task / computer_task → the silent autopilot loop (it can take a
    # minute; live_audio keeps the session alive while a tool runs). The voice
    # model gets ONLY the outcome line back — never step logs or element lists —
    # and speaks its own confirmation, so we pass speak=False.
    if res.get("needs_autopilot"):
        final = await _run_autopilot_task(res, speak=False)
        return respond({"result": final})

    # set_location → pin/clear the user's location, then let the voice model relay.
    if res.get("needs_location_set"):
        msg = await _handle_location_set(res, speak=False)
        return respond({"result": msg})

    # Mirror the pipeline's side effects so the HUD reflects what happened.
    # _apply_action_side_effects is the SHARED path (outcome recall, command log,
    # recordings/schedule refresh, ui_action forward + clear-chat reset, screen
    # restyle); reusing it keeps the voice and text pipelines from drifting apart.
    await emit("action", [res])
    await _apply_action_side_effects([res])

    # If the action produced file/dir text (read_file / list_dir / read_pdf),
    # hand the real content back to the model — the native-audio equivalent of
    # the text pipeline's follow-up turn — so it can actually answer about it.
    feed = res.get("feed_to_model")
    if feed and feed.get("content"):
        content = feed["content"]
        if len(content) > 4000:
            content = content[:4000] + " …(truncated)"
        body = f"{res.get('message', '')}\n\n{content}".strip()
        # Element/control listings and page dumps are INTERNAL observations: the
        # voice model needs them to choose its next call, but reading them aloud
        # is exactly the behavior users hate. Make the contract explicit.
        if atype in ("browser", "web", "webbrowser") or atype in ("computer", "desktop"):
            body = ("(internal observation — NEVER read this aloud or recite "
                    "numbers; immediately make your next function call, or give "
                    "a ONE-sentence outcome)\n" + body)
        return respond({"result": body})

    return respond({"result": res.get("message", "Done.")})


async def _on_voice_tool_calls(calls: list, ensure_bubble) -> list:
    """Handle a Live-API toolCall: run each function call (sequentially, so any
    Approve/Deny dialogs don't overlap) and return their functionResponses.

    `ensure_bubble` makes sure THIS turn's jarvis chat bubble exists before we
    emit any action cards, so the cards attach to the current turn (not the
    previous one, and not dropped when the model acts before it speaks)."""
    await ensure_bubble()
    out = []
    for c in calls:
        try:
            out.append(await _execute_voice_tool(c))
        except Exception as exc:  # noqa: BLE001
            print(f"[Voice-Tool] {c.get('name')} failed: {exc}", flush=True)
            out.append({"id": c.get("id"), "name": c.get("name"),
                        "response": {"error": "That action failed."}})
    await emit("status", "listening")
    return out


async def _run_native_audio_turn() -> None:
    """Drive a Gemini Live API native-audio voice session (full-duplex).

    Used instead of the Whisper→LLM→TTS pipeline when the native-audio model is
    selected. Streams mic audio to Google and plays the model's own voice back,
    surfacing live status + transcripts to the HUD. The model can perform real
    actions through function calls (see live_tools + _execute_voice_tool). Falls
    back to the normal pipeline if the Live API can't run (no Gemini key or
    missing deps)."""
    set_live_context(build_live_context())
    key = get_gemini_key()
    if (not key and not vertex_auth.enabled()) or not live_audio.available():
        # Graceful fallback: behave like a normal voice turn.
        if not live_audio.available():
            print("[LiveAudio] websockets/pyaudio unavailable — falling back.", flush=True)
        else:
            print("[LiveAudio] no Gemini credentials — falling back to text pipeline.", flush=True)
        try:
            await emit("status", "listening")
            text = await transcribe()
            if not text:
                return
            await _run_pipeline(text, emit_transcription=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[LiveAudio] fallback voice turn failed: {exc}", flush=True)
            await emit("response", "I couldn't hear you clearly, sir — check the microphone.")
            await emit("status", "idle")
        return

    # One id per turn for each side so the exchange renders as exactly two
    # live-updating bubbles (user + jarvis) instead of one-per-transcript-chunk.
    state = {"sid": None, "uid": None}

    async def ensure_bubble() -> None:
        """Create this turn's jarvis chat bubble if it doesn't exist yet."""
        if state["sid"] is None:
            state["sid"] = f"{time.time()}"
            await emit("stream_start", {"sid": state["sid"]})

    async def on_tool_call(calls: list) -> list:
        return await _on_voice_tool_calls(calls, ensure_bubble)

    async def on_event(kind: str, payload) -> None:
        if kind == "status":
            await emit("status", payload)
        elif kind == "user_partial":
            # Upsert a SINGLE user bubble (keyed by uid) as the transcript builds,
            # rather than emitting a new bubble for every partial chunk.
            if state["uid"] is None:
                state["uid"] = f"u{time.time()}"
            await emit("transcription", {"text": payload, "uid": state["uid"]})
        elif kind == "model_partial":
            await ensure_bubble()
            await emit("stream_delta", {"sid": state["sid"], "text": payload})
        elif kind == "turn_complete":
            user = payload.get("user", "") if isinstance(payload, dict) else ""
            model = payload.get("model", "") if isinstance(payload, dict) else ""
            if user and state["uid"] is not None:
                # Lock in the final, clean user transcript for this turn.
                await emit("transcription", {"text": user, "uid": state["uid"]})
            if state["sid"] is not None:
                await emit("response_end", {"sid": state["sid"], "text": model, "actions": []})
            state["sid"] = None
            state["uid"] = None
            if user or model:
                print(f"[Jarvis] (voice) {model}", flush=True)
                remember_turn_outcome(user, model or "(voice reply)")
        elif kind == "error":
            await emit("response", payload)

    async with _pipeline_lock:
        if _interrupt is not None:
            _interrupt.clear()
        try:
            print("[Jarvis] Native-audio session…", flush=True)
            user_text, model_text = await live_audio.run_session(
                key, get_model(), _voice_system_instruction(),
                on_event=on_event, interrupted=_interrupted, muted=skills.is_muted,
                tools=live_tools.function_declarations(), on_tool_call=on_tool_call,
                history=get_history(),
            )
            if state["sid"] is not None:
                await emit("response_end", {"sid": state["sid"], "text": model_text, "actions": []})
                if user_text or model_text:
                    print(f"[Jarvis] (voice) {model_text}", flush=True)
                    remember_turn_outcome(user_text, model_text or "(voice reply)")
        except Exception as exc:  # noqa: BLE001
            await _report_pipeline_error(exc)
        finally:
            await emit("status", "idle")


async def _run_pipeline(text: str, emit_transcription: bool = True,
                        task_tier: str = "") -> None:
    """Run the full LLM + TTS pipeline. Serialised by _pipeline_lock."""
    async with _pipeline_lock:
        if _interrupt is not None:
            _interrupt.clear()          # fresh turn — forget any earlier Stop
        # ── Standby mode: ignore everything until the user says "wake up" ──────
        if skills.is_sleeping():
            if _is_wake_phrase(text):
                skills.wake()
                if emit_transcription:
                    await emit("transcription", text)
                await emit("response", "I'm awake, sir. How can I help?")
                if _tts and not skills.is_muted():
                    await emit("status", "speaking")
                    await _tts.speak("I'm awake, sir. How can I help?")
                await emit("status", "idle")
            else:
                await emit("status", "idle")
            return

        if emit_transcription:
            await emit("transcription", text)

        sid = f"{time.time()}"          # stream id so the UI can finalize idempotently
        full_raw = ""
        cursor = 0
        started = False
        speaking = False
        tts_queue: "asyncio.Queue" = asyncio.Queue()
        consumer: "asyncio.Task | None" = None

        async def _enqueue(sentence: str) -> None:
            nonlocal consumer, speaking
            if not speaking:
                speaking = True
                await emit("status", "speaking")
                consumer = asyncio.create_task(_drain_tts(tts_queue))
            tts_queue.put_nowait(sentence)

        try:
            await emit("status", "thinking")
            print(f"[Jarvis] Thinking… [Route] {routing_note(text, task_tier)}", flush=True)

            # Inject any playbook(s) relevant to THIS request so JARVIS follows the
            # learned recipe (cleared when nothing matches, so it never lingers).
            set_skill_context(playbooks.context_block(text))

            speak_ok = _tts is not None and not skills.is_muted()
            leaky = model_leaks_reasoning(text, task_tier)
            quote_only = reply_is_quote_wrapped(text, task_tier)
            # Native function/tool calls land here (empty unless the model called
            # tools); the bridge keeps yielding (delta, full) text exactly as before.
            tool_sink: "list" = []
            async for _delta, full_raw in stream_prompt(text, tool_sink=tool_sink,
                                                        task_tier=task_tier):
                if _interrupted():          # user hit Stop — abort generation now
                    break
                if not started:
                    await emit("stream_start", {"sid": sid})
                    started = True
                if leaky:
                    continue                # accumulate only; clean + reveal at the end
                disp = _display_text(full_raw)
                await emit("stream_delta", {"sid": sid, "text": disp})
                if speak_ok:
                    sentences, cursor = _complete_sentences(disp, cursor)
                    for s in sentences:
                        await _enqueue(s)

            # If the user interrupted, finalize the partial text and bail out — no
            # actions, no further speech (finally restores idle + drains TTS).
            if _interrupted():
                disp = _display_text(full_raw)
                if leaky:
                    disp = _strip_reasoning(disp, quote_only=quote_only)
                if not started:
                    await emit("stream_start", {"sid": sid})
                await emit("response_end", {"sid": sid, "text": disp, "actions": []})
                return

            # A tool-only turn (the model called a function with no spoken preamble)
            # is NOT an empty reply — its confirmation comes from the action result.
            if not (full_raw or "").strip() and not tool_sink:
                full_raw = _EMPTY_REPLY_MSG
                if not started:
                    await emit("stream_start", {"sid": sid})
                    started = True
                await emit("stream_delta", {"sid": sid, "text": full_raw})
                if speak_ok:
                    await _enqueue(full_raw)

            # Stream done — run the turn's actions. Gated (dangerous) ones are
            # deferred until the user approves; everything else runs immediately.
            # NATIVE path: the model's function calls (tool_sink) become specs and
            # run through the SAME dispatcher (run_specs) as the legacy [ACTION]
            # tags, so all downstream handling below is identical. Run off the event
            # loop: actions can block on the network or the browser worker, which
            # would otherwise freeze telemetry, the websocket and the Stop button.
            loop = asyncio.get_running_loop()
            if tool_sink:
                for _c in tool_sink:
                    print(f"[Tool] {_c.get('name')}({_c.get('args')})", flush=True)
                specs = [live_tools.to_spec(_c.get("name", ""), _c.get("args"))
                         for _c in tool_sink]
                clean = _display_text(full_raw)
                if leaky:
                    clean = _strip_reasoning(clean, quote_only=quote_only)
                    await emit("stream_delta", {"sid": sid, "text": clean})
                results = await loop.run_in_executor(None, run_specs, specs)
            else:
                clean, results = await loop.run_in_executor(None, execute_actions, full_raw)
                if leaky:
                    clean = _strip_reasoning(clean, quote_only=quote_only)   # recover reply
                    await emit("stream_delta", {"sid": sid, "text": clean})
            results = _autopilot_fallbacks(results, text)
            immediate = [r for r in results if not r.get("needs_permission")
                         and not r.get("needs_vision") and not r.get("needs_image_gen")
                         and not r.get("needs_autopilot") and not r.get("needs_web_search")
                         and not r.get("needs_location_set")]
            pending = [r for r in results if r.get("needs_permission")]
            vision = [r for r in results if r.get("needs_vision")]
            images = [r for r in results if r.get("needs_image_gen")]
            websearch = [r for r in results if r.get("needs_web_search")]
            locsets = [r for r in results if r.get("needs_location_set")]
            autop = [r for r in results if r.get("needs_autopilot")]
            feeds = [r["feed_to_model"] for r in immediate if r.get("feed_to_model")]
            observed_results = list(immediate)
            for r in immediate:
                tag = "✓" if r.get("ok") else "✗"
                print(f"[Action] {tag} {r.get('type')} → {r.get('message')}", flush=True)
            print(f"\n[Jarvis] {clean}\n")

            if speak_ok:
                tail = (clean if leaky else _display_text(full_raw)[cursor:]).strip()
                if tail:
                    await _enqueue(tail)
                err_brief = "" if _failure_feeds(immediate) \
                    else await _brief_errors(immediate, text)
                if not speaking:
                    ok_speech = _spoken_summary(immediate)
                    if ok_speech:
                        await _enqueue(ok_speech)
                    if err_brief:
                        await _enqueue(err_brief)
                elif err_brief:
                    await _enqueue(err_brief)

            if not started:
                await emit("stream_start", {"sid": sid})

            await emit("response_end", {"sid": sid, "text": clean, "actions": immediate})
            await _apply_action_side_effects(immediate)

            for _v in vision:
                await _handle_vision(_v)

            for im in images:
                await _handle_image_gen(im)

            web_answers = []
            for ws in websearch:
                _ans = await _handle_web_search(ws)
                if _ans:
                    web_answers.append(_ans)

            for ls in locsets:
                await _handle_location_set(ls)

            for a in autop:
                observed_results.append(_autopilot_record(a, await _run_autopilot_task(a)))

            for p in pending:
                spoken_msg = ""
                kind = p.get("kind", "action")
                if kind == "browser" and browser.is_approved():
                    approved = True
                elif kind == "computer" and computer.is_armed():
                    approved = True
                else:
                    await emit("status", "idle")
                    approved = await request_permission(kind, p.get("description", "that action"))
                if approved:
                    if kind == "browser":
                        browser.approve()
                    elif kind == "computer":
                        computer.arm()
                    res = await loop.run_in_executor(None, run_action, p["spec"])
                    if res.get("needs_vision"):
                        await _handle_vision(res)
                        continue
                    if res.get("needs_image_gen"):
                        await _handle_image_gen(res)
                        continue
                    if res.get("needs_web_search"):
                        await _handle_web_search(res)
                        continue
                    if res.get("needs_autopilot"):
                        observed_results.append(
                            _autopilot_record(res, await _run_autopilot_task(res)))
                        continue
                    tag = "✓" if res.get("ok") else "✗"
                    print(f"[Action] {tag} (approved) {res.get('type')} → {res.get('message')}", flush=True)
                    await emit("action", [res])
                    _remember_outcomes([res])
                    await _log_commands([res])
                    observed_results.append(res)
                    spoken_msg = res.get("speak", res.get("message", ""))
                    if res.get("feed_to_model"):
                        feeds.append(res["feed_to_model"])
                else:
                    spoken_msg = _spoken_refusal(p.get("description"))
                    await emit("action", [{"type": p.get("type"), "ok": False, "message": spoken_msg}])
                if spoken_msg and _tts and not skills.is_muted():
                    await emit("status", "speaking")
                    await _tts.speak(spoken_msg)

            # Persist transient lookup ANSWERS (web search, nearby places, weather,
            # directions, news…) into history so follow-up turns keep context — a
            # native tool-only turn otherwise logs NOTHING (which is why "find one
            # close to me" forgot the topic was croissants). Durable HUD state lives
            # in LIVE STATE and is intentionally NOT recorded here.
            if not _interrupted():
                _info_msgs = [r.get("message", "") for r in immediate
                              if r.get("ok") and r.get("message")
                              and str(r.get("type") or "").lower() in _INFO_RESULT_TYPES]
                _outcome = " ".join(x for x in (web_answers + _info_msgs) if x).strip()
                if _outcome:
                    remember_turn_outcome(text, _outcome)

            if not _interrupted():
                if _suspected_missed_tasks(text, results):
                    feeds += await _completion_check(text, observed_results, task_tier)
                elif _claimed_done_without_acting(text, clean, results):
                    # The model claimed it acted but emitted no action — recover the
                    # dropped [ACTION] so JARVIS does what it said (or admits it can't).
                    print("[Check] reply claimed success with no action — recovering",
                          flush=True)
                    feeds += await _completion_check(text, observed_results, task_tier)

            feeds += _failure_feeds(observed_results)
            await _observe_browser_if_needed(feeds, observed_results)
            if feeds:
                await _agentic_loop(feeds, text, task_tier=task_tier)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            await _report_pipeline_error(exc, text)
        finally:
            await _finish_tts(consumer, tts_queue)
            await emit("status", "idle")


def _autopilot_record(task: dict, outcome: str) -> dict:
    """An autopilot run as a result the missed-task check can read. One goal covers
    a whole compound ask ("open Notepad and write a list"); unrecorded, the check
    read "nothing has been done yet" and ran the goal a second time."""
    return {"type": task.get("type"), "ok": True, "message": f"{task.get('goal')} → {outcome}"}


def _autopilot_fallbacks(results: "list[dict]", user_text: str) -> "list[dict]":
    """A read-only listing refused for one of the user's own folders becomes an
    autopilot goal in the user's OWN words, gated like any computer_task. Re-asked,
    the model passed on "list the files in Downloads" and dropped the question
    ("how many PDFs?")."""
    if not user_text or not any(r.get("autopilot_fallback") for r in results):
        return results
    return [r for r in results if not r.get("autopilot_fallback")] \
        + run_specs([{"type": "computer_task", "goal": user_text}])


async def _brief_errors(results: "list[dict]", user_text: str = "") -> str:
    """Collect failure messages from action results and return ONE brief line about
    what went wrong. A failure must ALWAYS be voiced — so this is guaranteed to
    return a non-empty line whenever something failed, and never hangs: the
    optional AI rewrite is bounded and falls back to the (already user-facing)
    action message if it's slow or unavailable. Returns '' only when nothing
    failed."""
    errors = [r["message"] for r in (results or [])
              if r.get("ok") is False and r.get("message")]
    if not errors:
        return ""
    raw = " ".join(errors)
    # Deterministic line that's ALWAYS available — action messages are already
    # phrased for the user ("I couldn't reach the maps service…").
    deterministic = errors[0] if len(errors) == 1 else \
        f"A few things went wrong, sir: {raw}"
    if len(deterministic) > 220:
        deterministic = deterministic[:217].rstrip() + "…"
    ctx = f"the user asked: {user_text}" if user_text else ""
    try:
        nicer = await asyncio.wait_for(summarize_error(raw, ctx), timeout=5.0)
    except Exception:  # noqa: BLE001 — timeout or any model/transport failure
        nicer = ""
    return (nicer or "").strip() or deterministic


_FILE_PRODUCING = ("record", "recording", "qr_code", "screenshot", "text_to_pdf")


def _remember_outcomes(results: "list[dict]") -> None:
    """Feed real action results (esp. saved file paths) into JARVIS's memory so a
    later 'open the file you just made' references the actual filename, not a guess."""
    msgs = []
    root = str(storage.get_root()).lower()
    for r in results or []:
        if not r or not r.get("ok") or not r.get("message"):
            continue
        m = r["message"]
        if r.get("type") in _FILE_PRODUCING or "saved" in m.lower() or root in m.lower():
            msgs.append(m)
    if msgs:
        note_outcome(" ".join(msgs))


async def _log_commands(results: "list[dict]") -> None:
    """Push executed actions to the GUI's mini-terminal (command log)."""
    entries = []
    for r in results or []:
        if not r or r.get("type") in ("ui",):
            continue
        entries.append({
            "type": r.get("type", "action"),
            "target": r.get("target", ""),
            "message": r.get("message", ""),
            "ok": r.get("ok"),
            "ts": time.time(),
        })
    if entries:
        await emit("command_log", entries)


async def emit_recordings() -> None:
    """Tell the Skills panel which recorders are running."""
    await emit("recordings", recorder.active_recordings())


async def handle_run_action(data) -> None:
    """Direct, deterministic action execution from GUI buttons (Skills/Power),
    bypassing the LLM so there's no guessed durations or misphrasing. Still routes
    destructive actions through the Approve/Deny gate."""
    spec = data.get("spec") if isinstance(data, dict) else None
    if not isinstance(spec, dict):
        return
    _track_task(_run_direct_action(spec), "direct action")


async def _run_direct_action(spec: dict) -> None:
    async with _pipeline_lock:
        try:
            gated, kind, desc = needs_permission(spec)
            if gated:
                await emit("status", "idle")
                approved = await request_permission(kind, desc)
                if not approved:
                    msg = _spoken_refusal(desc)
                    await emit("response", msg)
                    await emit("action", [{"type": spec.get("type"), "ok": False, "message": msg}])
                    return
            loop = asyncio.get_running_loop()
            res = await loop.run_in_executor(None, run_action, spec)
            if res.get("needs_web_search"):
                await _handle_web_search(res)
                return
            if res.get("needs_image_gen"):
                await emit("status", "thinking")
                ok, msg, data_url = await generate_image(res.get("prompt", ""))
                res = {"type": "generate_image", "ok": ok, "message": msg,
                       **({"image": data_url} if data_url else {})}
                if ok:
                    await _persist_generated_image(res, data_url, msg)
            tag = "✓" if res.get("ok") else "✗"
            print(f"[Action] {tag} (direct) {res.get('type')} → {res.get('message')}", flush=True)
            spoken = res.get("message", "Done.")
            if res.get("ok") is False and res.get("message"):
                spoken = await summarize_error(res["message"]) or spoken
            await emit("response", spoken)
            await emit("action", [res])
            _remember_outcomes([res])
            await _log_commands([res])
            await emit_recordings()
            if res.get("type") in ("schedule", "agenda"):
                await emit_schedule()
            if res.get("type") == "ui" and res.get("do"):
                do = res["do"]
                if do in ("clear_chat", "new_conversation", "reset"):
                    reset_history()
                    await emit("history", [])
                await emit("ui_action", do)
            await _apply_screen_results([res])
            if spoken and _tts and not skills.is_muted():
                await emit("status", "speaking")
                await _tts.speak(spoken)
            if res.get("feed_to_model"):
                await _agentic_loop([res["feed_to_model"]])
        except Exception as exc:  # noqa: BLE001
            await _report_pipeline_error(exc)
        finally:
            await emit("status", "idle")


async def handle_text_input(data) -> None:
    # New chat clients send {text, task_tier}; keeping the string form preserves
    # voice, remote-device, and older desktop clients.
    if isinstance(data, dict):
        text = str(data.get("text") or "").strip()
        task_tier = str(data.get("task_tier") or "")
    else:
        text = str(data or "").strip()
        task_tier = ""
    if not text:
        return
    # Runs as a background task; frontend already added the user bubble locally
    _track_task(_run_pipeline(text, emit_transcription=False, task_tier=task_tier),
                "text pipeline")


async def handle_trigger_listen(data) -> None:
    # Push-to-talk: when the trigger carries ptt=true (Ctrl+Space *held*), record
    # until the key is released (finish_listen) instead of stopping on silence.
    try:
        from transcription import whisper_transcriber as _wt
        _wt.set_ptt(bool((data or {}).get("ptt")))
    except Exception:  # noqa: BLE001
        pass
    if _trigger_listen is not None:
        _trigger_listen.set()


async def handle_finish_listen(_data) -> None:
    """End the current listening turn NOW and transcribe what was captured.

    Fired by the overlay ✓ button and by releasing the push-to-talk key. Unlike
    stop_speech (which cancels/drops the turn), this commits the audio so far.
    """
    try:
        from transcription import whisper_transcriber as _wt
        _wt.request_finish()
    except Exception:  # noqa: BLE001
        pass


async def handle_stop_speech(_data) -> None:
    """Hard stop: abort the whole in-flight reply, not just the current sentence.

    Sets the interrupt flag (so streaming generation breaks out and queued
    sentences are dropped), aborts any in-progress microphone recording, kills
    active audio, and immediately frees the HUD back to idle so the user can issue
    their next request right away."""
    if _interrupt is not None:
        _interrupt.set()
    # Cut off the microphone if we're mid-listen (the overlay "stop" button works
    # while JARVIS is listening, not just while it's speaking).
    try:
        from transcription.whisper_transcriber import request_cancel
        request_cancel()
    except Exception:       # noqa: BLE001 — best-effort
        pass
    if _tts:
        _tts.stop()
    # A hard stop is also a panic stop: immediately revoke any armed mouse/keyboard
    # control window so JARVIS can't take another desktop action after the user
    # said stop. Cheap and safe — a genuine next task simply re-approves.
    try:
        computer.disarm()
    except Exception:       # noqa: BLE001
        pass
    await emit("status", "idle")


async def handle_stop_control(_data) -> None:
    """The explicit 'STOP CONTROL' button: halt JARVIS's hands NOW. Disarms the
    mouse/keyboard control window, revokes browser-autopilot consent, and trips
    the interrupt flag so any running autopilot loop breaks out at its next step.
    The control-state / browser-state broadcasts clear the HUD banners."""
    global _control_paused, _pending_corrections
    if _interrupt is not None:
        _interrupt.set()
    try:
        # Also drop any live remote-control lease + release a held button, so a
        # STOP from the phone's hard-stop button truly lets go of the mouse.
        _release_remote_control()
    except Exception:       # noqa: BLE001
        pass
    try:
        # Hard STOP: force-kill JARVIS's browser so a step blocked inside Playwright
        # returns at once (revoke alone only flips consent and can't unblock it).
        browser.abort()
        await emit("browser_state", browser.state())
    except Exception:       # noqa: BLE001
        pass
    if _tts:
        _tts.stop()
    _control_paused = False           # a fresh task starts un-paused
    _pending_corrections = []
    await _emit_control_state()
    await emit("status", "idle")


async def handle_pause_control(data) -> None:
    """Pause / resume the running computer autopilot loop (the control overlay's
    Pause button). Pausing holds the loop BETWEEN steps — JARVIS stops acting but
    the task and the armed consent window stay alive — until resumed or stopped."""
    global _control_paused
    _control_paused = bool(data.get("paused")) if isinstance(data, dict) else bool(data)
    await _emit_control_state()


async def handle_agent_correction(data) -> None:
    """A live correction typed into the control overlay ('you're doing X wrong, do
    Y'). Queued and injected into the autopilot operator's NEXT step decision."""
    if not isinstance(data, dict):
        return
    text = str(data.get("text") or "").strip()
    if text:
        _pending_corrections.append(text[:500])
        await emit("response", "Got it, sir — I'll take that into account on the next step.")


async def handle_set_mute(data) -> None:
    """Persistent mute toggle from the GUI. data = {muted: bool}.
    Cuts off any in-flight speech immediately when muting."""
    muted = bool(data.get("muted")) if isinstance(data, dict) else bool(data)
    skills.set_mute(muted)
    if muted and _tts:
        _tts.stop()
    await emit("mute", skills.is_mute_toggled())


async def _refresh_models(force: bool = False) -> None:
    """Re-query each provider for its model list (Phase 3 discovery) and broadcast
    the refreshed sysinfo so the GUI dropdown shows only models the active keys can
    serve. Best-effort and off the request path — discovery never raises."""
    try:
        await model_discovery.refresh(force=force)
    except Exception as exc:  # noqa: BLE001
        print(f"[Models] discovery failed: {exc}", flush=True)
        return
    await _broadcast_sysinfo()
    # Smart routing: recompute the ranking for whatever the keys reach now (a key
    # added or removed, a catalog change). Benchmark-scored, so it's instant; only
    # models the catalog doesn't know trigger a (cached) web lookup.
    await _run_ranking()


async def _broadcast_sysinfo() -> None:
    info = get_sysinfo()
    await emit("config", info)
    await emit("sysinfo", info)


async def _run_ranking(force: bool = False) -> None:
    """(Re-)rank the models, broadcasting before (Settings shows "ranking…") and
    after (the new order + how it went). Failures fall back to the legacy tiers."""
    from llm import groq_bridge
    task = asyncio.ensure_future(groq_bridge.rank_models(force=force,
                                                         on_update=_broadcast_sysinfo))
    await asyncio.sleep(0)                # let it flag in_progress before we broadcast
    await _broadcast_sysinfo()
    try:
        await task
    except Exception as exc:  # noqa: BLE001
        print(f"[Ranker] ranking failed: {exc}", flush=True)
    await _broadcast_sysinfo()


async def handle_rerank_models(_data) -> None:
    """Settings ▸ Smart routing ▸ Re-rank: re-discover the models (new releases
    appear) and rank them again even though no key was added."""
    from llm import model_ranker
    if model_ranker.ranking_in_progress or model_ranker.rerank_requested:
        return
    # Show "Ranking…" at once — discovery takes a few seconds before ranking starts.
    model_ranker.rerank_requested = True
    await _broadcast_sysinfo()

    async def _go() -> None:
        try:
            await model_discovery.refresh(force=True)
        except Exception as exc:  # noqa: BLE001 — rank whatever the cache has
            print(f"[Models] discovery failed: {exc}", flush=True)
        try:
            await _run_ranking(force=True)
        finally:
            model_ranker.rerank_requested = False
            await _broadcast_sysinfo()
    _track_task(_go(), "model re-rank")


async def handle_refresh_models(_data) -> None:
    """GUI Settings panel opened → refresh the discovered model list (cheap; the
    credentials-hash cache skips the network when nothing changed)."""
    await _refresh_models(force=False)


async def handle_rerun_setup(_data) -> None:
    """Manual "Repair components" / "Retry" from the GUI: re-show the full setup
    screen and re-fetch any missing/corrupt runtime asset (Chromium / Whisper / Piper).
    Runs in the background so the websocket handler returns immediately; guarded so a
    second click while one is running is ignored."""
    global _setup_in_progress
    if provisioning is None:
        await emit("warning", "Setup repair is unavailable in this build.")
        return
    if _setup_in_progress:
        return
    async def _run() -> None:
        global _setup_in_progress
        _setup_in_progress = True
        try:
            await provisioning.provision(_setup_emit, force=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[Setup] repair error: {exc}", flush=True)
        finally:
            _setup_in_progress = False
    _track_task(_run(), "setup repair")


async def handle_set_config(data) -> None:
    """Apply runtime settings from the GUI Settings panel / onboarding."""
    if not isinstance(data, dict):
        return
    if "model_override" in data:
        await set_model(data["model_override"])
    if "tts" in data and _tts is not None:
        set_tts(data["tts"])
        _tts.set_backend(data["tts"])
    if "stt" in data:
        from llm.groq_bridge import set_stt as set_groq_stt
        from transcription.whisper_transcriber import set_stt as set_whisper_stt
        set_groq_stt(data["stt"])
        set_whisper_stt(data["stt"])
    # API keys (onboarding + Settings). Stored in the git-ignored secret store,
    # never echoed back. Blank values are ignored by set_api_keys.
    from llm.groq_bridge import PREFIXED_PROVIDERS
    llm_keys = {"groq_api_key", "gemini_api_key"} | {f"{p}_api_key" for p in PREFIXED_PROVIDERS}
    key_updates = {k: data[k] for k in (*llm_keys,
                                        "picovoice_key", "spotify_client_id",
                                        "spotify_client_secret", "spotify_redirect_uri",
                                        "elevenlabs_api_key", "google_maps_key")
                   if k in data}
    if key_updates:
        set_api_keys(key_updates)
    # Settings ▸ API Keys ▸ Remove. Only the key names above can be removed.
    removable = llm_keys | {"elevenlabs_api_key", "google_maps_key"}
    remove = [k for k in (data.get("remove_keys") or []) if k in removable]
    if remove:
        from llm.groq_bridge import remove_api_keys
        still = remove_api_keys(remove)
        if still:
            await emit("warning", "Couldn't remove " + ", ".join(still) + " — it comes "
                       "from an environment variable. Unset it and restart JARVIS.")
    if data.get("clear_quota_benches"):
        # Operator override: an escalated bench can park a route for a day off one
        # bad window, and set_api_keys only clears them as a side effect of saving
        # a key. This is the explicit way back.
        from llm import quota
        quota.clear()
    if data.get("storage_dir"):
        set_storage_dir(data["storage_dir"])
    if "always_on" in data:
        set_always_on(bool(data["always_on"]))
        if _always_on_event is not None:
            _always_on_event.set() if data["always_on"] else _always_on_event.clear()
        await emit("always_on", get_always_on())
    if "conversation_mode" in data:
        set_conversation_mode(bool(data["conversation_mode"]))
        await emit("conversation_mode", bool(data["conversation_mode"]))
    if "system_alerts" in data:
        set_system_alerts(bool(data["system_alerts"]))
    if "autopilot_model" in data:
        from llm.groq_bridge import set_autopilot_model
        set_autopilot_model(data["autopilot_model"])
    if "autopilot_thinking" in data:
        from llm.groq_bridge import set_autopilot_thinking
        set_autopilot_thinking(data["autopilot_thinking"])
    if "autopilot_vision" in data:
        from llm.groq_bridge import set_autopilot_vision
        set_autopilot_vision(data["autopilot_vision"])
    if "native_tools" in data:
        from llm.groq_bridge import set_native_tools
        set_native_tools(data["native_tools"])
    # Provider EDITION (Phase 4): vertex / gemini / offline. Changing it flips
    # vertex_auth.enabled() and the resolve_model routing, so re-discover models.
    if "provider_mode" in data:
        from llm.groq_bridge import set_provider_mode
        set_provider_mode(data["provider_mode"])
    if "offline_model" in data:
        from llm.groq_bridge import set_offline_model
        set_offline_model(data["offline_model"])
    if "ollama_url" in data:
        from llm.groq_bridge import set_ollama_url
        set_ollama_url(data["ollama_url"])
    if isinstance(data.get("screen"), dict):
        set_screen(data["screen"])
        await _emit_screen()
    if isinstance(data.get("overlay"), dict):
        await emit("overlay", set_overlay(data["overlay"]))
    if "allowed_dirs" in data:
        cleaned = set_allowed_dirs(data["allowed_dirs"])
    else:
        cleaned = get_allowed_dirs()
    # Always keep the storage folders readable so JARVIS can open files it made.
    fs_access.set_allowed(list(cleaned) + storage.all_dirs())
    # Echo the new (key-free) state back so every client stays in sync.
    info = get_sysinfo()
    await emit("config", info)
    await emit("sysinfo", info)
    # If a model-relevant credential changed, re-discover the available models in
    # the background and re-broadcast (the newly-added key's models then appear in
    # the dropdown). Off the save round-trip so Settings still closes instantly.
    if ((llm_keys | {"provider_mode"}) & set(data)) or remove \
            or "ollama_url" in data:
        _track_task(_refresh_models(force=True), "model discovery refresh")


async def handle_reset(_data) -> None:
    """Clear conversation memory."""
    reset_history()
    await emit("history", [])


# ── Conversation history / "Recents" ──────────────────────────────────────────
def _conversation_title(messages: list) -> str:
    """A short title for a conversation: its first user message."""
    for m in messages:
        if m.get("role") == "user":
            text = (m.get("content") or "").strip().replace("\n", " ")
            if text:
                return text[:60]
    return "New conversation"


async def _emit_conversations() -> None:
    await emit("conversations", memory_store.list_conversations())


async def handle_new_conversation(_data) -> None:
    """Save the current chat into Recents and start a fresh one."""
    current = get_history()
    if current:
        memory_store.archive_conversation(current, _conversation_title(current))
    reset_history()
    await emit("history", [])
    await _emit_conversations()


async def handle_list_conversations(_data) -> None:
    await _emit_conversations()


async def handle_open_conversation(data) -> None:
    """Reopen an archived conversation: park the current one in Recents, make the
    selected one active again, and display it."""
    cid = (data or {}).get("id")
    if not cid:
        return
    current = get_history()
    if current:
        memory_store.archive_conversation(current, _conversation_title(current))
    set_history(memory_store.pop_conversation(cid))
    await emit("conversation_loaded", _history_for_display())
    await _emit_conversations()


async def handle_delete_conversation(data) -> None:
    cid = (data or {}).get("id")
    if cid:
        memory_store.delete_conversation(cid)
    await _emit_conversations()


async def handle_clear_conversations(_data) -> None:
    memory_store.clear_conversations()
    await _emit_conversations()


# ── Memory view: everything JARVIS has stored, with per-item forget ───────────
def _id_ts(pid: str) -> "float | None":
    """Playbook ids embed their creation time in ms ("pb1781…", "auto1781…")."""
    digits = re.sub(r"\D", "", pid or "")
    return int(digits) / 1000 if len(digits) >= 12 else None


async def _emit_memory() -> None:
    def pb(p: dict) -> dict:
        pid = p.get("id") or ""
        return {"id": pid, "name": p.get("name") or "", "triggers": p.get("triggers") or [],
                "steps": p.get("steps") or "", "builtin": pid.startswith("b_"), "ts": _id_ts(pid)}
    await emit("memory", {
        "location": str(storage.get_root() / "memory"),
        "facts": memory_store.all_facts(),
        "playbooks": [pb(p) for p in playbooks.all_playbooks()],
        "learned": [pb(p) for p in playbooks.learned()],
        "conversations": memory_store.list_conversations(),
        "current": len(get_history()),
        "now": time.time(),            # timeline right edge (render stays pure)
    })


async def handle_memory_list(_data) -> None:
    await _emit_memory()


async def handle_memory_remember(data) -> None:
    memory_store.add_fact((data or {}).get("text") or "")
    await _emit_memory()


async def handle_memory_forget(data) -> None:
    kind, iid = (data or {}).get("kind"), (data or {}).get("id") or ""
    if kind == "fact":
        memory_store.delete_fact(iid)
    elif kind in ("playbook", "learned") and iid:
        playbooks.remove_playbook(iid)      # exact-id match; a built-in gets hidden
    elif kind == "conversation" and iid:
        memory_store.delete_conversation(iid)
        await _emit_conversations()
    await _emit_memory()



_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
_MAX_UPLOAD_BYTES = 8 * 1024 * 1024
_MAX_UPLOAD_TEXT = 8_000
_MAX_UPLOAD_PDF_PAGES = 30
_MAX_IMAGE_PIXELS = 36_000_000


def _decode_upload_b64(b64: str) -> "tuple[bytes | None, str]":
    compact = re.sub(r"\s+", "", b64 or "")
    if not compact:
        return None, "That upload was empty."
    if len(compact) > ((_MAX_UPLOAD_BYTES + 2) // 3) * 4 + 8:
        return None, f"That file is too large. The upload limit is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
    try:
        blob = base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError):
        return None, "That upload was not valid base64."
    if len(blob) > _MAX_UPLOAD_BYTES:
        return None, f"That file is too large. The upload limit is {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB."
    return blob, ""


def _validate_image_blob(blob: bytes) -> str:
    try:
        import io
        from PIL import Image
    except ImportError:
        return ""
    try:
        with Image.open(io.BytesIO(blob)) as img:
            w, h = img.size
    except Exception:
        return "I couldn't read that image file, sir."
    if w * h > _MAX_IMAGE_PIXELS:
        return "That image is too large for me to process safely."
    return ""


def _extract_upload_text(name: str, mime: str, blob: bytes) -> str:
    """Pull text out of an uploaded PDF or text file (blocking -> run in executor)."""
    low = (name or "").lower()
    if low.endswith(".pdf") or "pdf" in (mime or ""):
        try:
            import io
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(blob))
            txt = "\n".join((p.extract_text() or "") for p in reader.pages[:_MAX_UPLOAD_PDF_PAGES])
            return txt.strip()[:_MAX_UPLOAD_TEXT]
        except Exception as exc:  # noqa: BLE001
            print(f"[Upload] PDF extract failed: {exc}", flush=True)
            return ""
    try:
        return blob.decode("utf-8", errors="replace").strip()[:_MAX_UPLOAD_TEXT]
    except Exception:  # noqa: BLE001
        return ""


async def handle_chat_upload(data) -> None:
    """A file the user attached in the chat. Images → the vision model; PDFs/text
    → extract locally then a follow-up turn (the same path JARVIS uses to 'read'
    files). The frontend already showed the user's '📎 filename' bubble."""
    if not isinstance(data, dict):
        return
    name = data.get("name") or "file"
    mime = (data.get("mime") or "").lower()
    raw = data.get("data") or ""
    prompt = (data.get("prompt") or "").strip()
    task_tier = str(data.get("task_tier") or "")
    if raw.startswith("data:"):
        b64 = raw.split(",", 1)[1] if "," in raw else ""
    else:
        b64 = raw
    blob, upload_error = _decode_upload_b64(b64)
    if upload_error or blob is None:
        await emit("response", upload_error or "I couldn't read that upload, sir.")
        await emit("status", "idle")
        return
    b64 = base64.b64encode(blob).decode("ascii")
    data_url = f"data:{mime or 'application/octet-stream'};base64,{b64}"
    is_image = mime.startswith("image/") or name.lower().endswith(_IMAGE_EXTS)
    loop = asyncio.get_running_loop()

    async with _pipeline_lock:
        try:
            await emit("status", "thinking")
            if is_image:
                image_error = await loop.run_in_executor(None, _validate_image_blob, blob)
                if image_error:
                    await emit("response", image_error)
                    return
                answer = (await vision_query(data_url, prompt or "Describe this image in "
                          "detail and note anything notable.", task_tier)
                          or "I couldn't quite interpret that image, sir.")
                await emit("response", answer)
                print(f"[Upload] image {name} → {answer[:80]}", flush=True)
                if _tts and not skills.is_muted():
                    await emit("status", "speaking")
                    await _tts.speak(answer)
                return

            text = await loop.run_in_executor(None, _extract_upload_text, name, mime, blob)
            if not text:
                msg = f"I couldn't read any text from {name}, sir."
                await emit("response", msg)
                return
            instruction = prompt or (f"This is a file called {name} the user uploaded. "
                                     "Summarise it and give the key points.")
            await _agentic_loop([{"name": name, "content": text}], instruction=instruction,
                                task_tier=task_tier)
        except Exception as exc:  # noqa: BLE001
            await _report_pipeline_error(exc, prompt or name)
        finally:
            await emit("status", "idle")


def _best_coords() -> "tuple":
    """(lat, lon, label) from the highest-priority source: a user-PINNED manual
    location > precise browser GPS > coarse IP location."""
    if _manual_coords:
        return _manual_coords[0], _manual_coords[1], _manual_coords[2]
    if _user_coords:
        return _user_coords[0], _user_coords[1], (_last_netinfo or {}).get("location", "")
    nf = _last_netinfo or {}
    return nf.get("lat"), nf.get("lon"), nf.get("location", "")


async def _use_location(lat, lon, label: str = "") -> None:
    """Push a chosen (lat, lon[, label]) to EVERY location consumer: the places/
    directions/weather actions, the autopilot browser's geolocation, and the HUD
    netinfo + weather cards. Reverse-geocodes a label when none is given."""
    global _last_netinfo, _last_weather
    loop = asyncio.get_running_loop()
    if not label:
        label = await loop.run_in_executor(None, telemetry.reverse_geocode, lat, lon) or ""
    _last_netinfo = {**(_last_netinfo or {}), "lat": lat, "lon": lon,
                     **({"location": label} if label else {})}
    # places.set_user_coords feeds find_places/directions/weather AND the autopilot
    # browser (its _apply_geolocation re-reads this on its next op).
    places.set_user_coords(lat, lon, label or _last_netinfo.get("location", ""))
    await emit("netinfo", _last_netinfo)
    wx = await loop.run_in_executor(None, weather_mod.get_weather, lat, lon,
                                    label or _last_netinfo.get("location", ""))
    if wx:
        _last_weather = wx
        await emit("weather", wx)


async def handle_set_location(data) -> None:
    """Precise browser-GPS coords from the GUI → refresh weather for them, UNLESS a
    manual location is pinned (which always wins; we still remember the GPS fix so
    clearing the pin reverts to it)."""
    global _user_coords
    if not isinstance(data, dict):
        return
    lat, lon = data.get("lat"), data.get("lon")
    if lat is None or lon is None:
        return
    acc = data.get("accuracy")
    print(f"[Location] browser fix: {lat}, {lon}"
          + (f" (±{round(acc)} m)" if isinstance(acc, (int, float)) else ""), flush=True)
    _user_coords = (lat, lon)
    if _manual_coords:                       # a pinned location overrides browser GPS
        return
    await _use_location(lat, lon)


async def _set_manual_location(place: str = "", lat=None, lon=None) -> "tuple[bool, str]":
    """Pin the user's location (by place name → geocoded, or by explicit coords),
    persist it, and apply it everywhere. Returns (ok, spoken message)."""
    global _manual_coords
    loop = asyncio.get_running_loop()
    label = ""
    if (lat is None or lon is None) and place:
        cur = _best_coords()                 # bias the geocode to the user's area
        g = await loop.run_in_executor(None, places.geocode, place, cur[0], cur[1], 5)
        if not g:
            return False, f"I couldn't find “{place}” on the map, sir."
        lat, lon = g["lat"], g["lon"]
        label = places.concise_label(g)      # 'Kandivali East, Mumbai', NOT the 15-part address
    if lat is None or lon is None:
        return False, "Tell me a place or address to set as your location, sir."
    if not label:
        label = await loop.run_in_executor(None, telemetry.reverse_geocode, lat, lon) \
            or (place or "").strip()[:60]
    _manual_coords = (float(lat), float(lon), label)
    set_manual_location({"lat": float(lat), "lon": float(lon), "label": label})
    await _use_location(float(lat), float(lon), label)
    print(f"[Location] pinned manual: {label} ({lat:.5f}, {lon:.5f})", flush=True)
    short = label.split(",")[0].strip() if label else "there"
    return True, f"Location pinned to {short}, sir — I'll use that until you clear it."


async def _clear_manual_location() -> "tuple[bool, str]":
    """Drop the pinned location and revert to automatic (browser GPS / IP)."""
    global _manual_coords
    _manual_coords = None
    set_manual_location(None)
    lat, lon, label = _best_coords()
    if lat is not None and lon is not None:
        await _use_location(lat, lon, label)
    print("[Location] manual pin cleared — back to automatic.", flush=True)
    return True, "Back to your automatic location, sir."


async def handle_set_manual_location(data) -> None:
    """GUI Settings 'My location' field / Clear button → pin or clear the override."""
    if not isinstance(data, dict):
        return
    if data.get("clear"):
        await _clear_manual_location()
    else:
        await _set_manual_location(place=(data.get("place") or data.get("query") or "").strip(),
                                   lat=data.get("lat"), lon=data.get("lon"))
    info = get_sysinfo()
    await emit("sysinfo", info)
    await emit("config", info)


async def push_sysinfo(client=None) -> None:
    """Send current model + system info + cached HUD data to a new client."""
    async def push(event: str, data) -> None:
        if client is None:
            await emit(event, data)
        else:
            await send_to(client, event, data)

    info = get_sysinfo()   # builds once; reused below (it probes the OS for RAM)
    await push("sysinfo", info)
    await push("config", info)
    # Replay the first-run/repair setup screen to a late-connecting or reloading HUD
    # (only while it's still in progress or showing an error — see the helper).
    _snap = _setup_snapshot_for_replay()
    if _snap is not None:
        await push("setup_progress", _snap)
    await push("mute", skills.is_mute_toggled())
    await push("recordings", recorder.active_recordings())
    await push("browser_state", browser.state())
    await push("control_state", computer.state())
    await push("screen", get_screen())
    await push("overlay", get_overlay())
    await push("always_on", get_always_on())
    await push("conversation_mode", bool(info.get("conversation_mode")))
    if _last_active_app is not None:
        await push("active_app", _last_active_app)
    if _last_telemetry is not None:
        await push("telemetry", _last_telemetry)
    if _last_netinfo is not None:
        await push("netinfo", _last_netinfo)
    if _last_weather is not None:
        await push("weather", _last_weather)
    if _last_schedule is not None:
        await push("schedule", _last_schedule)

    # Restore the visible conversation log from last session (the frontend only
    # applies this when its chat is still empty, so reconnects don't clobber a
    # live session).
    disp = _history_for_display()
    if disp:
        await push("history", disp)

    # Send the saved-conversations ("Recents") list so the chat history panel fills.
    # Non-fatal: a hiccup loading Recents must never block the greeting that follows.
    with contextlib.suppress(Exception):
        await push("conversations", memory_store.list_conversations())

    # Greet on (re)connect, debounced so reconnects don't re-fire a costly LLM
    # call. HMR/StrictMode double-mounts, tab refreshes and brief network blips all
    # reconnect within minutes; a 10-min window suppresses those while still
    # greeting a genuine "reopened the app later". Skip while onboarding is pending.
    global _last_greet
    now = time.monotonic()
    if has_llm_credentials() and not info.get("needs_setup") \
            and now - _last_greet > 600:
        _last_greet = now
        _track_task(_do_greeting(), "startup greeting")


# ── Self-awareness: live context, startup greeting, proactive alerts ──────────

def build_live_context() -> str:
    """Compact snapshot of everything on the HUD, fed to the model as its senses."""
    def n(x):
        return f"{x:.0f}" if isinstance(x, (int, float)) else x

    parts = [f"Time: {_dt.datetime.now().strftime('%H:%M, %A %d %B %Y')}."]
    t = _last_telemetry or {}
    if t:
        line = f"System: CPU {n(t.get('cpu', 0))}%, memory {n(t.get('ram', 0))}% of {round(t.get('ramTotalGb') or 0)}GB"
        if t.get("gpu") is not None:
            line += f", GPU {n(t['gpu'])}%"
        line += f", disk {n(t.get('disk', 0))}% full, disk activity {n(t.get('diskActivity', 0))}%."
        parts.append(line)
        bp = t.get("batteryPct")
        if bp is not None:
            state = "charging" if t.get("charging") else "on battery"
            rem = f", {t['remaining']} left" if t.get("remaining") and not t.get("charging") else ""
            parts.append(f"Battery: {n(bp)}% ({state}{rem}).")
    nf = _last_netinfo or {}
    if t or nf:
        parts.append(f"Network: {n(t.get('down', 0))} Mbps down / {n(t.get('up', 0))} up, "
                     f"ping {t.get('ping') or '—'} ms, location {nf.get('location', '—')}.")
    # What the user is currently looking at (so "summarise this" / "what's this"
    # has context). Reuse the title the active_app_loop already polls off-thread
    # (cached in _last_active_app) instead of a second blocking Win32 call here;
    # on_jarvis suppresses our own HUD window. Clipboard is NOT injected here —
    # it's read on demand only.
    la = _last_active_app or {}
    win = "" if la.get("on_jarvis") else (la.get("title") or "").strip()
    if win:
        parts.append(f"Active window (what the user is looking at): {win}.")
    w = _last_weather
    if w and w.get("temp") is not None:
        parts.append(f"Weather: {round(w['temp'])}°C, {w.get('condition', '')}, "
                     f"humidity {w.get('humidity')}%.")
    sch = _last_schedule or []
    pending = [i for i in sch if not i.get("done")]
    if pending:
        parts.append("Agenda (remaining today): "
                     + "; ".join(f"{i.get('time', '')} {i.get('task', '')}".strip() for i in pending))
    else:
        parts.append("Agenda: nothing pending today." if sch else "Agenda: nothing scheduled today.")
    return "\n".join(parts)


async def _do_greeting() -> None:
    """Speak a one-off, context-aware JARVIS greeting shortly after connect."""
    await asyncio.sleep(2.5)                       # let first telemetry/weather land
    set_live_context(build_live_context())
    text = await startup_greeting()
    if not text:
        return
    async with _pipeline_lock:
        try:
            await emit("response", text)
            print(f"[Jarvis] (greeting) {text}", flush=True)
            if _tts and not skills.is_muted():
                await emit("status", "speaking")
                await _tts.speak(text)
        except Exception as exc:  # noqa: BLE001
            print(f"[Greeting] playback failed: {exc}", flush=True)
        finally:
            await emit("status", "idle")


async def _run_routine(routine: dict) -> None:
    """Fire a scheduled routine: run its saved prompt through the normal pipeline
    so it can answer with live context, take actions, and speak the result."""
    prompt = (routine.get("prompt") or "").strip()
    if not prompt:
        return
    print(f"[Routine] {routine.get('time')} → {prompt}", flush=True)
    set_live_context(build_live_context())
    # No user bubble — this is JARVIS acting proactively, not the user asking.
    await _run_pipeline(prompt, emit_transcription=False)


async def _speak_proactive(msg: str) -> None:
    async with _pipeline_lock:
        try:
            await emit("response", msg)
            print(f"[Jarvis] (alert) {msg}", flush=True)
            if _tts and not skills.is_muted():
                await emit("status", "speaking")
                await _tts.speak(msg)
        except Exception as exc:  # noqa: BLE001
            print(f"[Alert] playback failed: {exc}", flush=True)
        finally:
            await emit("status", "idle")


async def maybe_alert(stats: dict) -> None:
    """Rule-based proactive alerts (no API calls). Debounced; one at a time.
    Stays silent while the user is mid-interaction so a status line never gets
    spoken right before (or instead of) the answer to their actual request."""
    if _user_busy or _pipeline_lock.locked() or skills.is_sleeping():
        return
    now = time.monotonic()

    # Agenda reminders — fire once when an item's time matches the clock.
    cur = _dt.datetime.now().strftime("%H:%M")
    for it in (_last_schedule or []):
        if it.get("done") or it.get("time") != cur:
            continue
        key = f"agenda:{it.get('time')}:{it.get('task')}"
        if now - _alert_cooldowns.get(key, 0) > 3600:
            _alert_cooldowns[key] = now
            await _speak_proactive(f"Reminder, sir: it's {it.get('time')} — {it.get('task')}.")
            return

    # System alerts (CPU/RAM/battery) are opt-in — many find them noisy. Agenda
    # reminders above always fire. Toggle in Settings (system_alerts).
    if not get_system_alerts():
        return

    cpu = stats.get("cpu") or 0
    ram = stats.get("ram") or 0
    bp = stats.get("batteryPct")
    candidates = []
    if ram >= 90:
        candidates.append(("ram", f"Heads up, sir — memory usage is at {ram:.0f}%. You may want to close a few apps."))
    if cpu >= 95:
        candidates.append(("cpu", f"The CPU's running flat out at {cpu:.0f}%, sir."))
    if bp is not None and bp <= 15 and not stats.get("charging"):
        candidates.append(("battery", f"Battery's down to {bp:.0f}%, sir — might be wise to plug in."))
    for key, msg in candidates:
        if now - _alert_cooldowns.get(key, 0) < 600:      # 10-min cooldown
            continue
        _alert_cooldowns[key] = now
        await _speak_proactive(msg)
        return


# ── HUD data: telemetry + connectivity + weather + schedule ───────────────────

async def emit_schedule() -> None:
    global _last_schedule
    _last_schedule = skills.get_today_schedule()
    await emit("schedule", _last_schedule)


async def telemetry_loop() -> None:
    """Emit fast-changing system stats ~every 1.5 s, refresh the model's live
    context, and fire any rule-based proactive alerts."""
    global _last_telemetry
    tick = 0
    while True:
        try:
            _last_telemetry = telemetry.get_fast_stats()
            await emit("telemetry", _last_telemetry)
            if tick % 3 == 0:                     # refresh model context ~every 4.5s
                set_live_context(build_live_context())
            await maybe_alert(_last_telemetry)
            # Fire any scheduled routines whose time has come (skipped while the
            # user is mid-interaction or JARVIS is asleep). due() self-dedupes.
            if not _user_busy and not skills.is_sleeping():
                for r in routines.due():
                    _track_task(_run_routine(r), "routine")
            # One-shot reminders/timers fire regardless of sleep (you still want
            # your timer), but not mid-interaction.
            if not _user_busy:
                for rem in reminders.due():
                    _track_task(_speak_proactive(reminders.spoken_for(rem)), "reminder")
        except Exception as exc:  # noqa: BLE001
            print(f"[Telemetry] {exc}", flush=True)
        tick += 1
        await asyncio.sleep(1.5)


async def active_app_loop() -> None:
    """Poll the foreground app ~every 1.5 s and emit `active_app` whenever it
    changes, so the floating overlay pill can show itself only when the user is in
    another app (and filter by which app). 1.5 s is plenty for an overlay trigger
    and halves the Win32+psutil wakeups vs. the old 0.7 s poll."""
    global _last_active_app
    loop = asyncio.get_running_loop()
    while True:
        try:
            info = await loop.run_in_executor(None, computer.foreground_app)
            if info != _last_active_app:
                _last_active_app = info
                await emit("active_app", info)
        except Exception as exc:  # noqa: BLE001
            print(f"[ActiveApp] {exc}", flush=True)
        await asyncio.sleep(1.5)


async def ping_loop() -> None:
    """Refresh TCP-latency ping ~every 4 s (blocking → executor)."""
    loop = asyncio.get_running_loop()
    while True:
        try:
            await loop.run_in_executor(None, telemetry.measure_ping)
        except Exception:
            pass
        await asyncio.sleep(4)


async def slow_info_loop() -> None:
    """Emit connectivity + weather + schedule on start, then every ~10 min."""
    global _last_netinfo, _last_weather
    loop = asyncio.get_running_loop()
    while True:
        try:
            _last_netinfo = await loop.run_in_executor(None, telemetry.get_net_info)

            # Priority: a PINNED manual location > precise browser GPS > coarse IP.
            if _manual_coords:
                lat, lon, place = _manual_coords
                _last_netinfo = {**_last_netinfo, "lat": lat, "lon": lon,
                                 **({"location": place} if place else {})}
            elif _user_coords:
                lat, lon = _user_coords
                place = await loop.run_in_executor(None, telemetry.reverse_geocode, lat, lon)
                _last_netinfo = {**_last_netinfo, "lat": lat, "lon": lon,
                                 **({"location": place} if place else {})}
            else:
                lat = _last_netinfo.get("lat")
                lon = _last_netinfo.get("lon")
                place = _last_netinfo.get("location", "")
            if lat is not None and lon is not None:
                places.set_user_coords(lat, lon, place or "")
            await emit("netinfo", _last_netinfo)

            wx = await loop.run_in_executor(None, weather_mod.get_weather, lat, lon, place)
            if wx:
                _last_weather = wx
                await emit("weather", wx)

            await emit_schedule()
        except Exception as exc:  # noqa: BLE001
            print(f"[HUD-info] {exc}", flush=True)
        await asyncio.sleep(600)


async def text_loop() -> None:
    await emit("status", "idle")
    print("\n[Jarvis] Ready. Type a message and press Enter. (Ctrl+C to quit)\n")

    loop = asyncio.get_running_loop()
    while True:
        try:
            text = await loop.run_in_executor(None, lambda: input("> "))
            text = text.strip()
            if not text:
                continue
            await _run_pipeline(text, emit_transcription=True)
        except (KeyboardInterrupt, EOFError):
            print("\n[Jarvis] Shutting down.")
            break
        except Exception as exc:
            print(f"\n[Error] {exc}")
            await emit("status", "idle")


async def wake_word_loop(detector: WakeWordDetector) -> None:
    global _user_busy
    loop = asyncio.get_running_loop()
    detection_event = asyncio.Event()

    def on_wake() -> None:
        loop.call_soon_threadsafe(detection_event.set)

    detector.on_detected(on_wake)
    # Track whether the loop believes the detector is running so we never
    # double-open the microphone or stop one that isn't running.
    running = False

    def detector_start() -> None:
        nonlocal running
        if not running:
            detector.start()
            running = True

    def detector_stop() -> None:
        nonlocal running
        if running:
            detector.stop()
            running = False

    detector_start()

    await emit("status", "idle")
    print("[Jarvis] Ready. Say 'Hey Jarvis' to activate.\n", flush=True)

    try:
        while True:
            always_on = _always_on_event is not None and _always_on_event.is_set()

            # ── 1. In always-on mode, skip the wake word and listen continuously.
            #    Otherwise wait for the wake word OR a manual mic trigger. ──────
            if always_on:
                await loop.run_in_executor(None, detector_stop)   # we drive the mic ourselves
                detection_event.clear()
                _trigger_listen.clear()
                await emit("status", "listening")
            else:
                detector_start()                 # ensure it's live (no double-open)
                wake_task = asyncio.create_task(detection_event.wait())
                trig_task = asyncio.create_task(_trigger_listen.wait())
                done, pending = await asyncio.wait(
                    {wake_task, trig_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await t
                detection_event.clear()
                _trigger_listen.clear()
                # ── Pop the overlay up the INSTANT we detect — BEFORE the mic
                #    handoff. The detector's PyAudio teardown can take a few hundred
                #    ms, so doing it first (and on the event loop) was what made the
                #    popup feel laggy. Emit now, then release the mic off-loop. ────
                await emit("wake_triggered", True)
                await emit("status", "listening")
                await loop.run_in_executor(None, detector_stop)   # release mic off-loop

            # From here until the reply is done we're mid-interaction — hold off
            # any proactive status alerts so JARVIS doesn't blurt out "CPU at 99%"
            # right before answering.
            _user_busy = True
            try:
                if gemini_bridge.is_native_audio_model(get_model()):
                    await _run_native_audio_turn()
                    continue

                print("[Jarvis] Listening…", flush=True)

                # Let the wake-word mic release fully before opening the recorder
                # (PortAudio on Windows often needs >200 ms).
                if not always_on:
                    await asyncio.sleep(0.45)

                text = await transcribe()

                if not text:
                    print("[Jarvis] No speech detected.", flush=True)
                    await emit("status", "idle")
                    continue

                print(f"[Jarvis] Heard: {text}", flush=True)
                await _run_pipeline(text, emit_transcription=True)
            except Exception as exc:  # noqa: BLE001
                print(f"[Jarvis] Voice turn failed: {exc}", flush=True)
                msg = (str(exc).lower())
                if "pyaudio" in msg or "portaudio" in msg or "input" in msg or "mic" in msg:
                    spoken = ("I couldn't reach the microphone, sir — make sure no other "
                              "app is using it and try again.")
                else:
                    spoken = _PIPELINE_FAIL_MSG
                await emit("response", spoken)
                await emit("status", "idle")
            finally:
                _user_busy = False

    except asyncio.CancelledError:
        pass
    finally:
        detector_stop()


async def main(use_text: bool) -> None:
    global _tts, _trigger_listen, _pipeline_lock, _interrupt, _main_loop, _always_on_event
    global _manual_coords, _remote_screen_lock, _remote_input_lock, _remote_emit_lock

    _trigger_listen = asyncio.Event()
    _pipeline_lock = asyncio.Lock()
    _interrupt = asyncio.Event()
    # Serialize live remote-desktop signalling and input on this loop.
    _remote_screen_lock = asyncio.Lock()
    _remote_input_lock = asyncio.Lock()
    _remote_emit_lock = asyncio.Lock()

    # So the browser worker thread (which owns Playwright) can marshal a HUD
    # state update back onto this loop.
    _main_loop = asyncio.get_running_loop()
    browser.set_state_callback(_on_browser_state)
    computer.set_state_callback(_on_control_state)
    # Warm the Playwright driver in the background now (no window opens), so the
    # first time the user opens the browser it only pays the Chromium launch.
    try:
        browser.prewarm()
    except Exception:  # noqa: BLE001
        pass
    # Likewise the installed-app index ("open BlueJ" needs a Start-Menu /
    # StartApps / program-dir scan that takes seconds cold).
    try:
        app_launcher.prewarm_index()
    except Exception:  # noqa: BLE001
        pass

    register_handler("text_input", handle_text_input)
    register_handler("trigger_listen", handle_trigger_listen)
    register_handler("finish_listen", handle_finish_listen)
    register_handler("stop_speech", handle_stop_speech)
    register_handler("stop_control", handle_stop_control)
    register_handler("pause_control", handle_pause_control)
    register_handler("agent_correction", handle_agent_correction)
    register_handler("set_mute", handle_set_mute)
    register_handler("run_action", handle_run_action)
    register_handler("set_config", handle_set_config)
    register_handler("reset", handle_reset)
    register_handler("set_location", handle_set_location)
    register_handler("set_manual_location", handle_set_manual_location)
    register_handler("permission_response", handle_permission_response)
    register_handler("clarify_response", handle_clarify_response)
    register_handler("browser_control", handle_browser_control)
    register_handler("browser_panel", handle_browser_panel)
    register_handler("chat_upload", handle_chat_upload)
    register_handler("set_screen", handle_set_screen)
    register_handler("set_overlay", handle_set_overlay)
    register_handler("set_always_on", handle_set_always_on)
    register_handler("set_conversation_mode", handle_set_conversation_mode)
    register_handler("refresh_models", handle_refresh_models)
    register_handler("rerank_models", handle_rerank_models)
    register_handler("rerun_setup", handle_rerun_setup)
    register_handler("new_conversation", handle_new_conversation)
    register_handler("list_conversations", handle_list_conversations)
    register_handler("open_conversation", handle_open_conversation)
    register_handler("delete_conversation", handle_delete_conversation)
    register_handler("clear_conversations", handle_clear_conversations)
    register_handler("memory_list", handle_memory_list)
    register_handler("memory_remember", handle_memory_remember)
    register_handler("memory_forget", handle_memory_forget)
    # ── Remote mobile control ────────────────────────────────────────────────
    # Bring up the crypto identity registry. If it fails (e.g. cryptography wheel
    # missing in a frozen build), remote stays OFF and the socket keeps its
    # localhost-only default — the local app is never affected.
    global _device_registry
    try:
        from autonomy.device_identity import DeviceIdentityRegistry
        _device_registry = DeviceIdentityRegistry()
        configure_device_identity(_device_registry)
        # A cryptographically paired + PIN-verified phone is trusted to drive the
        # whole TASK/CONTROL protocol — task forwarding, its lifecycle (cancel/
        # reconnect/answer/approve), and STOP. Authentication proves it's the owner;
        # the LLM's own judgement gates whether any given task runs. What stays
        # local-GUI-only is the desktop app configuring ITSELF — set_config (keys/
        # provider/allowed_dirs sandbox), run_action (a RAW action with no model in
        # the loop), reset/rerun_setup, conversation management — none of which a
        # phone sends or should be able to invoke on the host. Ownership of each
        # task_id is still enforced per-handler (a phone can only touch its own).
        set_remote_allowed({
            "task.submit", "task.cancel", "task.subscribe", "task.unsubscribe",
            "task.status", "task.list", "task.answer", "approval.response",
            "protocol.hello", "stop_control",
            # Live remote-desktop: WebRTC screen signalling + direct input.
            "webrtc_offer", "webrtc_ice", "stop_screen",
            "arm_control", "disarm_control", "remote_input",
        })
        register_handler("device.pairing_create", handle_device_pairing_create)
        register_handler("device.list", handle_device_list)
        register_handler("device.revoke", handle_device_revoke)
        register_handler("task.submit", handle_task_submit)
        register_handler("task.cancel", handle_task_cancel)
        register_handler("task.subscribe", handle_task_subscribe)
        register_handler("task.answer", handle_task_answer)
        register_handler("webrtc_offer", handle_webrtc_offer)
        register_handler("webrtc_ice", handle_webrtc_ice)
        register_handler("stop_screen", handle_stop_screen)
        register_handler("arm_control", handle_arm_control)
        register_handler("disarm_control", handle_disarm_control)
        register_handler("remote_input", handle_remote_input)
        on_disconnect(handle_remote_disconnect)
        # Bind all interfaces so a Tailscale peer can reach us; the crypto+whois
        # transport gate is the real access control (plan G3/S3). Respect an
        # explicit launcher override if one was set.
        os.environ.setdefault("JARVIS_WS_BIND", "0.0.0.0")
        print(f"[Remote] Identity ready: {_device_registry.host_fingerprint}", flush=True)
    except Exception as exc:  # noqa: BLE001
        _device_registry = None
        print(f"[Remote] Disabled (identity init failed: {exc}); local-only.", flush=True)
    on_connect(push_sysinfo)
    # Make stored coords available to the places/weather actions immediately.
    if _user_coords:
        places.set_user_coords(_user_coords[0], _user_coords[1])

    await initialize()
    # Discover the per-key model list in the background (Phase 3), then re-broadcast
    # sysinfo so the GUI dropdown reflects the real available models (the first
    # on_connect push carries only the curated fallback until this lands).
    _track_task(_refresh_models(), "model discovery")
    # Restore a pinned manual location (config is loaded now) so it wins over GPS/IP.
    _loc = get_manual_location()
    if isinstance(_loc, dict) and _loc.get("lat") is not None and _loc.get("lon") is not None:
        _lbl = str(_loc.get("label") or "")
        # Heal a previously-saved VERBOSE label (the full 15-part Nominatim address)
        # down to a concise 'City, Region' so the HUD LOCATION field isn't a wall.
        if len(_lbl) > 48 or _lbl.count(",") > 3:
            _short = await asyncio.get_running_loop().run_in_executor(
                None, telemetry.reverse_geocode, _loc["lat"], _loc["lon"])
            if _short and _short != _lbl:
                _lbl = _short
                set_manual_location({**_loc, "label": _lbl})
        _manual_coords = (_loc["lat"], _loc["lon"], _lbl)
        places.set_user_coords(_manual_coords[0], _manual_coords[1], _manual_coords[2])
        print(f"[Location] restored pinned location: {_lbl or _manual_coords[:2]}", flush=True)
    _always_on_event = asyncio.Event()
    if get_always_on():
        _always_on_event.set()
    ws_server = await start_websocket()
    storage.ensure_dirs()       # create the user's storage folder tree
    # Read-only terminal whitelist + JARVIS's own storage folders (so it can
    # list/read/open the files it created without the user approving them).
    fs_access.set_allowed(get_allowed_dirs() + storage.all_dirs())
    _tts = TTSEngine()          # also pre-inits pygame mixer

    # Background HUD data feeds (telemetry, ping, connectivity/weather/schedule).
    # Started BEFORE first-run provisioning so the HUD — clock, telemetry, and the
    # setup-progress screen — is alive and animating while the assets download.
    hud_tasks = [
        asyncio.create_task(telemetry_loop()),
        asyncio.create_task(ping_loop()),
        asyncio.create_task(slow_info_loop()),
        asyncio.create_task(active_app_loop()),
    ]

    # ── First-run asset provisioning ────────────────────────────────────────────
    # The installer ships lean, so the heavy runtime assets (Chromium, the Whisper
    # base model, the Piper voice) download ONCE here, on first launch, with progress
    # shown on the HUD (setup_progress events). Instant and silent on every later run
    # (all present). Awaited so the warm-ups below load from a complete cache and the
    # wake-word loop doesn't start mid-download. Best-effort — never blocks on error.
    global _setup_in_progress
    try:
        if provisioning is not None:
            _setup_in_progress = True
            await provisioning.provision(_setup_emit)
    except Exception as exc:  # noqa: BLE001 — provisioning must never crash startup
        print(f"[Setup] asset provisioning error: {exc}", flush=True)
    finally:
        _setup_in_progress = False

    # Warm the local Whisper model + Piper voice in the BACKGROUND (load the ONNX
    # models into RAM; the DOWNLOADS are already done by provisioning above). Not
    # awaited, so the app is responsive immediately and the wake-word loop starts at
    # once: Whisper is the offline STT fallback (~40s cold load) and Piper is the
    # daily-driver voice (~7s load) whose load would otherwise stall the first reply.
    _track_task(preload_whisper(), "whisper preload")
    _track_task(_tts.preload(), "tts preload")

    try:
        if use_text:
            await text_loop()
        else:
            from wake_word import create_detector
            detector = create_detector()
            await wake_word_loop(detector)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[Jarvis] Shutting down.")
    finally:
        for t in hud_tasks:
            t.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*hud_tasks, return_exceptions=True)
        with contextlib.suppress(Exception):
            from llm.groq_bridge import aclose_http
            await aclose_http()
        with contextlib.suppress(Exception):
            await gemini_bridge.aclose()
        with contextlib.suppress(Exception):
            from tts.tts_engine import aclose_tts_http
            await aclose_tts_http()
        with contextlib.suppress(Exception):
            import memory_store
            memory_store.flush_history()    # don't lose the last turn on exit
        ws_server.close()
        await ws_server.wait_closed()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Jarvis backend")
    parser.add_argument("--text", action="store_true", help="Use typed input instead of wake word")
    args = parser.parse_args()

    asyncio.run(main(use_text=args.text))
