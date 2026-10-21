"""Commands from the PC to the paired phone: "set an alarm on my phone", said at the PC
or sent on WhatsApp from any number allowed to command JARVIS (WhatsApp → this PC →
the phone).

The phone is a client of this PC's WebSocket (server/websocket_server.py), so a command
rides its live, authenticated socket as a host-signed ``phone.command`` {id, text}. The
phone runs it as an ordinary turn of its own brain, limited to a short list of phone
tools (alarms, timers, reminders, flashlight / ringer / Do Not Disturb, volume, media),
and answers ``phone.result`` {id, ok, reply} on the same link. ``ok`` comes from the
phone's own tool results, never from its words — the rule both apps share — and only
the device the command went to can answer it.

Nothing is queued: a phone whose JARVIS is closed, or paused by Android after a while
in the background, can't answer, and the reply says so instead of promising it will
happen later.
"""

from __future__ import annotations

import asyncio
import secrets

TIMEOUT_S = 45.0
_REPLY_MAX = 600

NOT_CONNECTED = ("Your phone isn't connected to me right now, sir. Open JARVIS on the "
                 "phone (it connects over Tailscale) and ask again.")
NO_ANSWER = ("Your phone didn't answer within {s} seconds. JARVIS on it may be paused in "
             "the background — open it on the phone and ask again.")

_loop: "asyncio.AbstractEventLoop | None" = None
_send = None          # async (device_id, event, data) -> sockets sent to
_connected = None     # () -> set of device ids with a live socket
_paired = None        # () -> bool: a phone has been paired at all
_pending: "dict[str, tuple[str, asyncio.Future]]" = {}


def configure(loop, send, connected, paired) -> None:
    global _loop, _send, _connected, _paired
    _loop, _send, _connected, _paired = loop, send, connected, paired


def paired() -> bool:
    try:
        return bool(_paired and _paired())
    except Exception:  # noqa: BLE001
        return False


def _result(ok: bool, message: str, reply: str = "") -> dict:
    out = {"type": "phone_command", "ok": ok, "message": message}
    if reply:
        out["speak"] = ""        # the model says it in its own words
        out["feed_to_model"] = {
            "name": "phone",
            "content": ("The phone did it" if ok else "The phone did NOT do it")
                       + f" (its own report, from its tool results): {reply}"}
    return out


async def command(text: str, timeout: float = TIMEOUT_S) -> dict:
    text = " ".join(str(text or "").split())[:500]
    if not text:
        return _result(False, "What should your phone do?")
    devices = sorted(_connected() if _connected else ())
    if not devices or _send is None:
        return _result(False, NOT_CONNECTED)
    device = devices[0]
    cid = secrets.token_hex(8)
    fut = asyncio.get_running_loop().create_future()
    _pending[cid] = (device, fut)
    try:
        if not await _send(device, "phone.command", {"id": cid, "text": text}):
            return _result(False, NOT_CONNECTED)
        print(f"[Phone] sent {cid}: {text!r}", flush=True)
        answer = await asyncio.wait_for(fut, timeout)
    except asyncio.TimeoutError:
        return _result(False, NO_ANSWER.format(s=int(timeout)))
    finally:
        _pending.pop(cid, None)
    ok = answer.get("ok") is True
    reply = " ".join(str(answer.get("reply") or "").split())[:_REPLY_MAX] or \
        ("Done." if ok else "It couldn't do that.")
    print(f"[Phone] {cid} {'✓' if ok else '✗'} {reply!r}", flush=True)
    return _result(ok, f"Phone: {reply}", reply)


def on_result(device_id: str, data) -> bool:
    """A phone's `phone.result`. Only the device the command went to may answer it."""
    if not isinstance(data, dict):
        return False
    entry = _pending.get(str(data.get("id") or ""))
    if entry is None or entry[0] != device_id or entry[1].done():
        return False
    entry[1].set_result(data)
    return True


def run(spec: dict) -> dict:
    """actions._run_one entry for {"type": "phone_command", "request": …}. Runs on an
    executor thread; the round trip itself on the main loop."""
    if _loop is None or not _loop.is_running():
        return _result(False, NOT_CONNECTED)
    fut = asyncio.run_coroutine_threadsafe(
        command(spec.get("request") or spec.get("text") or ""), _loop)
    try:
        return fut.result(timeout=TIMEOUT_S + 5)
    except Exception as exc:  # noqa: BLE001
        fut.cancel()
        return _result(False, f"Couldn't reach your phone ({exc}).")
