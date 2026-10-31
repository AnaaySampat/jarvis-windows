"""Deterministic gesture actions. No arbitrary key strings, shell commands,
model prompts, approval responses or file paths are accepted here."""
from __future__ import annotations
import asyncio
import re
from collections import OrderedDict

_results: OrderedDict = OrderedDict()

MEDIA_KEYS = {
    "media.toggle": 0xB3, "media.next": 0xB0, "media.previous": 0xB1,
    "media.mute": 0xAD, "media.up": 0xAF, "media.down": 0xAE,
    "pc.toggle": 0xB3, "pc.mute": 0xAD, "pc.up": 0xAF, "pc.down": 0xAE,
}
SLIDE_KEYS = {"pc.next": "pagedown", "pc.previous": "pageup"}
TV_ACTIONS = {
    "tv.toggle": ("key", "play_pause"), "tv.next": ("key", "next"),
    "tv.previous": ("key", "previous"), "tv.mute": ("mute", ""),
    "tv.up": ("volume_up", "5"), "tv.down": ("volume_down", "5"),
}
REMOTE_ACTIONS = frozenset(MEDIA_KEYS) | frozenset(SLIDE_KEYS) | {"mic.mute", "mic.unmute"}
LOCAL_ACTIONS = REMOTE_ACTIONS | frozenset(TV_ACTIONS) | {"timer.start", "read_aloud"}


def microphone_mute(mute: bool) -> tuple[bool, str]:
    """Change and read back the default Windows capture endpoint, not a guessed
    Teams/Zoom shortcut. A meeting using another input may have different state."""
    try:
        import comtypes
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        comtypes.CoInitialize()
        try:
            device = AudioUtilities.GetMicrophone()
            endpoint = getattr(device, "EndpointVolume", None)
            if endpoint is None:
                endpoint = cast(device.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
                                POINTER(IAudioEndpointVolume))
            endpoint.SetMute(int(mute), None)
            if bool(endpoint.GetMute()) != mute:
                return False, "Windows did not confirm the microphone mute state."
            return True, f"Default microphone {'muted' if mute else 'unmuted'}."
        finally:
            comtypes.CoUninitialize()
    except Exception:
        return False, "Microphone control is unavailable. It needs Windows, pycaw and a default input device."


def execute(action: str, data: dict) -> tuple[bool, str]:
    if action not in LOCAL_ACTIONS:
        return False, "Unknown or disallowed gesture action."
    if action in MEDIA_KEYS:
        from actions.app_launcher import _media_key
        return _media_key(MEDIA_KEYS[action], "Sent the media command to Windows.")
    if action in SLIDE_KEYS:
        from actions import computer
        # Existing armed-control guard, expiry and own-window exclusion remain.
        return computer.press_keys(SLIDE_KEYS[action])
    if action in ("mic.mute", "mic.unmute"):
        return microphone_mute(action == "mic.mute")
    if action in TV_ACTIONS:
        from actions import tv
        verb, target = TV_ACTIONS[action]
        result = tv.run({"type": "tv", "do": verb, "target": target})
        return bool(result.get("ok")), str(result.get("message") or "TV command failed.")
    if action == "timer.start":
        import reminders
        try:
            seconds = int(data.get("seconds"))
        except (TypeError, ValueError, OverflowError):
            return False, "Choose a timer duration between 5 seconds and 24 hours."
        if not 5 <= seconds <= 86400:
            return False, "Choose a timer duration between 5 seconds and 24 hours."
        return reminders.add_reminder(f"in {seconds} seconds", "Gesture timer", is_timer=True)
    return False, "This gesture action needs the speech handler."


async def submit(data: dict, runtime) -> None:
    """The runtime supplies the same authenticated sender and control-lease
    checks as remote_input. The camera never grants control or approval."""
    if not isinstance(data, dict):
        return
    request_id, action = str(data.get("request_id") or ""), str(data.get("action") or "")
    if not re.fullmatch(r"gesture_[a-z0-9_]{1,80}", request_id):
        return
    key = (runtime.current_client_id(), request_id)
    async def reply(ok, summary):
        await runtime.send_to_current("gesture_result", {
            "request_id": request_id, "ok": bool(ok), "summary": str(summary)})
    if key in _results:
        if _results[key] is not None:
            await runtime.send_to_current("gesture_result", _results[key])
        return
    if action not in LOCAL_ACTIONS:
        await reply(False, "Unknown or disallowed gesture action.")
        return
    if runtime.is_current_sender_remote() and action not in REMOTE_ACTIONS:
        await reply(False, "That gesture action is local to the PC.")
        return
    if sum(value is None for value in _results.values()) >= 16:
        await reply(False, "The PC is busy with gesture requests. Try again shortly.")
        return
    _results[key] = None

    async def run():
        ok, summary = False, "Gesture command failed."
        try:
            if runtime.is_current_sender_remote():
                if runtime._remote_input_lock is None:
                    raise ValueError("PC control is not ready.")
                async with runtime._remote_input_lock:
                    error = runtime._remote_control_error(data)
                    if error:
                        raise ValueError(error)
                    ok, summary = await asyncio.get_running_loop().run_in_executor(None, execute, action, data)
            elif action == "read_aloud":
                text = str(data.get("text") or "").strip()
                if not text or len(text) > 4000:
                    raise ValueError("Choose between 1 and 4000 characters to read aloud.")
                if not runtime._tts or runtime.skills.is_muted():
                    raise ValueError("Speech is unavailable or muted.")
                if runtime._pipeline_lock is None or runtime._pipeline_lock.locked():
                    raise ValueError("JARVIS is busy. Stop its current reply before reading.")
                async with runtime._pipeline_lock:
                    await runtime.emit("status", "speaking")
                    try:
                        await runtime._tts.speak(text)
                        ok, summary = True, "Reading finished."
                    finally:
                        await runtime.emit("status", "idle")
            else:
                ok, summary = await asyncio.get_running_loop().run_in_executor(None, execute, action, data)
        except asyncio.CancelledError:
            _results.pop(key, None)
            raise
        except Exception as exc:  # noqa: BLE001 — return the real result
            summary = str(exc)
        result = {"request_id": request_id, "ok": bool(ok), "summary": str(summary)}
        _results[key] = result
        while len(_results) > 256:
            completed = next((k for k, value in _results.items() if value is not None), None)
            if completed is None:
                break
            _results.pop(completed, None)
        await runtime.send_to_current("gesture_result", result)

    # Keep the socket receptive to stop_speech while read-aloud is speaking.
    runtime._track_task(run(), "gesture action")
