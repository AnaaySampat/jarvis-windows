"""WhatsApp channel: message JARVIS from anywhere (OpenClaw's WhatsApp channel).

JARVIS links to the user's WhatsApp as a linked device — the way WhatsApp Web does,
by scanning a QR code — through neonize (Python bindings for whatsmeow). It is an
unofficial client: WhatsApp's terms don't cover it, so it's off by default and the
Settings page says so.

Who can command JARVIS (`admit`):
- The account's own "Message yourself" chat. Only the owner can write there, from any
  of their devices, so it needs no list. People keep notes there, so a message counts
  only when it starts with JARVIS's name ("Jarvis, what's my CPU at?").
- Direct messages from numbers the user listed (for a JARVIS on a second number).
- Nothing else: groups, broadcasts, and everyone else are ignored without a reply, and
  the owner's own messages to other people are never commands.

A message runs as an ordinary chat turn (main._run_whatsapp_turn) with the PC's voice
quiet; the reply comes back here, approvals are asked here, and everything still goes
through the same tools and approval prompts. There is no tool for JARVIS to message
anyone: it only ever answers the chat a command came from.

Guards: replies start with MARK and a self-chat message that does is JARVIS's own;
messages older than STALE_S (queued while the PC was off) are skipped; at most RATE_N
commands a minute.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
from collections import deque

import jarvis_paths

MARK = "🤖"                 # every reply starts with it
STALE_S = 180               # a command older than this was sent while JARVIS was offline
RATE_N, RATE_WINDOW_S = 6, 60
CHUNK = 3500                # WhatsApp takes ~65k, but a wall of text is unreadable
_SETTINGS = "whatsapp.json"
_DB = "whatsapp.db"         # the linked-device session: as sensitive as WhatsApp Web's
_DEFAULTS = {"enabled": False, "allow": []}


# ── Settings ──────────────────────────────────────────────────────────────────

def _settings_path():
    return jarvis_paths.app_data_dir() / _SETTINGS


def db_path():
    return jarvis_paths.app_data_dir() / _DB


def norm_number(raw) -> str:
    """'+91 98765-43210' → '919876543210'; '' unless it's 7–15 digits (E.164)."""
    digits = re.sub(r"\D", "", str(raw or ""))
    return digits if 7 <= len(digits) <= 15 else ""


def settings() -> dict:
    try:
        raw = json.loads(_settings_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or corrupt: defaults
        raw = {}
    return {"enabled": bool(raw.get("enabled", False)),
            "allow": [n for n in map(norm_number, raw.get("allow") or []) if n]}


def save_settings(patch: dict) -> dict:
    cur = settings()
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    if "allow" in patch:
        raw = patch["allow"]
        items = raw if isinstance(raw, list) else re.split(r"[,;\n]+", str(raw or ""))
        cur["allow"] = list(dict.fromkeys(n for n in map(norm_number, items) if n))[:20]
    try:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur), encoding="utf-8")
    except OSError as exc:
        print(f"[WhatsApp] couldn't save settings: {exc}", flush=True)
    return cur


# ── Who may command JARVIS (pure; unit-tested) ────────────────────────────────

_NOT_A_PERSON = ("g.us", "broadcast", "newsletter")


def _phone(jid) -> str:
    return jid.User if jid is not None and getattr(jid, "Server", "") == "s.whatsapp.net" else ""


def is_self_chat(src, me: "set[str]") -> bool:
    """The owner's "Message yourself" chat: sent by the owner into their own chat. The
    chat and sender can be the phone-number and LID forms of the same account, so
    the sender's alternate id counts too."""
    chat = src.Chat.User
    own = {u for u in (getattr(src.Sender, "User", ""),
                       getattr(getattr(src, "SenderAlt", None), "User", "")) if u}
    return bool(src.IsFromMe) and (chat in me or chat in own)


def admit(src, text: str, me: "set[str]", allow: "list[str]", sent_at: float,
          now: float) -> str:
    """'' when the message may be a command, else why not. `src` is the message's
    MessageSource (Chat, Sender, IsFromMe, IsGroup, SenderAlt); `me` the linked
    account's own user ids (phone number and LID)."""
    chat = src.Chat
    if src.IsGroup or getattr(chat, "Server", "") in _NOT_A_PERSON:
        return "group or broadcast"
    if src.IsFromMe:
        if not is_self_chat(src, me):
            return "outgoing"            # the owner writing to someone else
        if (text or "").lstrip().startswith(MARK):
            return "own reply"
        return "stale" if sent_at and now - sent_at > STALE_S else ""
    sender = _phone(src.Sender) or _phone(getattr(src, "SenderAlt", None))
    if not (sender and sender in allow):
        return "not allowed"
    return "stale" if sent_at and now - sent_at > STALE_S else ""


_NAME = re.compile(r"^\W*(?:(?:hey|hi|ok(?:ay)?|yo)\W+)?jarvis\b\W*", re.IGNORECASE)


def addressed(text: str) -> "str | None":
    """The command in a self-chat message that starts with JARVIS's name, without the
    name; None when it doesn't start with it (a note to self)."""
    m = _NAME.match(text or "")
    return text[m.end():].strip() if m else None


class RateLimit:
    def __init__(self, n: int = RATE_N, window: float = RATE_WINDOW_S):
        self.n, self.window = n, window
        self._at: "deque[float]" = deque()

    def ok(self, now: float) -> bool:
        while self._at and now - self._at[0] > self.window:
            self._at.popleft()
        if len(self._at) >= self.n:
            return False
        self._at.append(now)
        return True


_YES = re.compile(r"^\s*(?:(?:y|yes|yeah|yep|ok(?:ay)?|sure|approve[d]?|allow(?:ed)?|"
                  r"go(?: ahead)?|do it|confirm(?:ed)?)\b|👍)", re.IGNORECASE)


def is_yes(answer: str) -> bool:
    """An approval reply. Anything that isn't plainly yes is a no."""
    return bool(_YES.match(answer or "")) and not re.search(r"\b(?:no|don'?t|not)\b",
                                                           answer or "", re.IGNORECASE)


# ── Replies (pure; unit-tested) ───────────────────────────────────────────────

_DIVIDER = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")


def for_whatsapp(text: str) -> str:
    """Markdown → WhatsApp: **bold** is *bold* there, and a pipe table becomes one line
    per row."""
    out = []
    for line in (text or "").split("\n"):
        s = line.strip()
        if "|" in s and _DIVIDER.match(s):
            continue
        if s.startswith("|") and s.endswith("|") and s.count("|") >= 2:
            line = " · ".join(c.strip() for c in s.strip("|").split("|") if c.strip())
        out.append(line)
    text = "\n".join(out)
    text = re.sub(r"\*\*\*(\S(?:.*?\S)?)\*\*\*", r"*_\1_*", text)
    text = re.sub(r"\*\*(\S(?:.*?\S)?)\*\*", r"*\1*", text)
    text = re.sub(r"^#{1,6}\s+(.+)$", r"*\1*", text, flags=re.MULTILINE)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunks(text: str, size: int = CHUNK) -> "list[str]":
    """Split on paragraph, then line, then word boundaries."""
    text = text.strip()
    parts: "list[str]" = []
    while len(text) > size:
        cut = max(text.rfind("\n\n", 0, size), text.rfind("\n", 0, size), text.rfind(" ", 0, size))
        cut = cut if cut > size // 2 else size
        parts.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    return parts + ([text] if text else [])


def _data_url_bytes(url: str) -> "bytes | None":
    m = re.match(r"^data:image/[\w.+-]+;base64,(.+)$", url or "", re.DOTALL)
    if not m:
        return None
    try:
        return base64.b64decode(m.group(1))
    except ValueError:
        return None


def compose(events: list) -> "tuple[str, list[tuple[bytes, str]]]":
    """The reply to send back, from the HUD events a turn emitted: what JARVIS said
    (response_end / response text), or — for a turn that only acted — each action's own
    line; plus any picture it produced (a generated image, a QR code, a screenshot)."""
    said: "list[str]" = []
    acted: "list[str]" = []
    images: "list[tuple[bytes, str]]" = []
    seen_images: "set[int]" = set()

    def take(results) -> None:
        for r in results or []:
            if not isinstance(r, dict) or r.get("internal"):
                continue
            msg = str(r.get("message") or "").strip()
            if msg and r.get("ok") is not None and msg != "Sources":
                acted.append(("✓ " if r.get("ok") else "✗ ") + msg)
            blob = _data_url_bytes(str(r.get("image") or ""))
            if blob is None and r.get("file"):
                try:
                    with open(r["file"], "rb") as fh:
                        blob = fh.read()
                except OSError:
                    blob = None
            if blob and hash(blob) not in seen_images:
                seen_images.add(hash(blob))
                images.append((blob, msg[:200]))

    for event, data in events:
        if event == "response_end" and isinstance(data, dict):
            text = str(data.get("text") or "").strip()
            if text:
                said.append(text)
            take(data.get("actions"))
        elif event == "response" and isinstance(data, str) and data.strip():
            said.append(data.strip())
        elif event == "action" and isinstance(data, list):
            take(data)
    lines: "list[str]" = []
    for line in said or acted:
        if not lines or lines[-1] != line:     # a progress bubble then its final line
            lines.append(line)
    return for_whatsapp("\n\n".join(lines)), images


# ── The linked device (neonize) ───────────────────────────────────────────────

class Channel:
    """One linked WhatsApp account. `on_command(text, chat, heard)` runs a command
    (heard=True for a voice note); `on_state(state)` reports status to the HUD;
    `transcribe(audio_bytes)` turns a voice note into text."""

    def __init__(self, on_command, on_state, transcribe):
        self.on_command, self.on_state, self.transcribe = on_command, on_state, transcribe
        self.client = None
        self.state = {"status": "off", "qr": "", "number": "", "error": ""}
        self._me: "set[str]" = set()
        self._seen: "deque[str]" = deque(maxlen=200)
        self._rate = RateLimit()
        self._waiting: "tuple[str, asyncio.Future] | None" = None
        self._task: "asyncio.Task | None" = None
        self._stopping = False
        self._failures = 0

    async def _set(self, **kw) -> None:
        self.state.update(kw)
        await self.on_state(dict(self.state))

    async def start(self) -> None:
        if self.client is not None:
            return
        self._stopping = False
        try:
            from neonize.aioze.client import NewAClient
            from neonize.aioze.events import (
                ConnectedEv, ConnectFailureEv, DisconnectedEv, LoggedOutEv, MessageEv,
                PairStatusEv, TemporaryBanEv,
            )
        except ImportError:
            await self._set(status="error", error="WhatsApp needs the neonize package — "
                                                  "pip install neonize, then restart JARVIS.")
            return
        db = db_path()
        db.parent.mkdir(parents=True, exist_ok=True)
        client = NewAClient(str(db))
        self.client = client

        async def on_qr(_c, data: bytes) -> None:
            await self._set(status="linking", qr=data.decode("utf-8", "replace"), error="")

        async def on_pair(_c, ev) -> None:
            if ev.Error:
                await self._set(status="error", qr="", error=f"Linking failed: {ev.Error}")
            else:
                await self._set(status="connecting", qr="")

        async def on_connected(c, _ev) -> None:
            try:
                me = await c.get_me()
                self._me = {u for u in (me.JID.User, me.LID.User) if u}
                number = me.JID.User
            except Exception:  # noqa: BLE001
                number = ""
            self._failures = 0
            print(f"[WhatsApp] connected as +{number}", flush=True)
            await self._set(status="connected", qr="", number=number, error="")

        async def on_logged_out(_c, _ev) -> None:
            await self._set(status="logged_out", qr="", number="",
                            error="WhatsApp unlinked JARVIS. Turn it off and on to link again.")

        async def on_ban(_c, ev) -> None:
            detail = " ".join(str(ev).split())[:120]
            await self._set(status="error", error=f"WhatsApp blocked this link for now ({detail}).")

        async def on_disconnected(_c, _ev) -> None:
            if self.state["status"] == "connected":
                await self._set(status="connecting")

        async def on_failure(_c, ev) -> None:
            await self._set(status="error", error=f"Couldn't connect to WhatsApp ({ev.Reason}).")

        async def on_message(c, ev) -> None:
            try:
                await self._on_message(c, ev)
            except Exception as exc:  # noqa: BLE001 — one bad message never stops the channel
                print(f"[WhatsApp] message handling failed: {exc}", flush=True)

        client.event.qr(on_qr)
        client.event(PairStatusEv)(on_pair)
        client.event(ConnectedEv)(on_connected)
        client.event(LoggedOutEv)(on_logged_out)
        client.event(TemporaryBanEv)(on_ban)
        client.event(DisconnectedEv)(on_disconnected)
        client.event(ConnectFailureEv)(on_failure)
        client.event(MessageEv)(on_message)
        await self._set(status="connecting", error="")
        try:
            self._task = await client.connect()
        except Exception as exc:  # noqa: BLE001
            self.client = None
            await self._set(status="error", error=f"Couldn't start WhatsApp: {exc}")
            return
        # The connection runs as this task for the client's whole life; it only ends
        # on stop() or a failure, e.g. no network at startup.
        self._task.add_done_callback(self._connection_ended)

    def _connection_ended(self, task: "asyncio.Task") -> None:
        if self._stopping or task.cancelled() or task.exception() is None:
            return
        self.client = None
        delay = min(300, 30 * 2 ** self._failures)
        self._failures += 1
        print(f"[WhatsApp] connection failed: {task.exception()}", flush=True)
        asyncio.ensure_future(self._retry(str(task.exception()), delay))

    async def _retry(self, error: str, delay: float) -> None:
        await self._set(status="error", qr="",
                        error=f"Couldn't reach WhatsApp ({error[:160]}). Trying again in "
                              f"{int(delay)} s.")
        await asyncio.sleep(delay)
        if not self._stopping and self.client is None and settings()["enabled"]:
            await self.start()

    async def stop(self) -> None:
        self._stopping = True
        client, self.client = self.client, None
        if client is not None:
            try:
                await client.disconnect()
                await client.stop()
            except Exception:  # noqa: BLE001
                pass
        await self._set(status="off", qr="", error="")

    async def unlink(self) -> None:
        """Log this device out of WhatsApp and forget the session."""
        client = self.client
        if client is not None:
            try:
                await client.logout()
            except Exception:  # noqa: BLE001 — already unlinked on the phone
                pass
        await self.stop()
        for suffix in ("", "-wal", "-shm"):
            try:
                db_path().with_name(db_path().name + suffix).unlink(missing_ok=True)
            except OSError:
                pass
        await self._set(number="", error="")

    async def _on_message(self, client, ev) -> None:
        from neonize.utils.message import extract_text
        info = ev.Info
        src = info.MessageSource
        msg = ev.Message
        text = extract_text(msg) or ""
        sent_at = float(info.Timestamp or 0)
        if sent_at > 1e12:                 # milliseconds
            sent_at /= 1000
        why = admit(src, text, self._me, settings()["allow"], sent_at, time.time())
        if why not in ("", "stale") or info.ID in self._seen:
            return
        self._seen.append(info.ID)
        own_chat = bool(src.IsFromMe)
        if why == "stale":
            if not (own_chat and addressed(text) is None):      # a command, not a note
                print("[WhatsApp] skipped a command sent while JARVIS was offline", flush=True)
                await self.send(src.Chat, "That came in while I was offline, so I didn't run "
                                          "it. Send it again if you still want it.")
            return
        chat = src.Chat
        waiting = self._waiting
        # The answer to a question asked in this chat. The own chat may come addressed
        # by the account's other id (phone number vs LID), so any own-chat reply counts.
        if waiting and not waiting[1].done() and (
                waiting[0] == _key(chat) or (own_chat and waiting[0].split("@")[0] in self._me)):
            waiting[1].set_result(text)
            return
        heard = False
        if not text.strip() and msg.HasField("audioMessage"):
            try:
                audio = await client.download_any(msg)
                text = (await self.transcribe(audio) or "").strip()
                heard = True
            except Exception as exc:  # noqa: BLE001
                await self.send(chat, f"I couldn't hear that voice note ({exc}).")
                return
        if own_chat:
            text = addressed(text)
            if text is None:
                return                     # a note to self, not for JARVIS
        if not self._rate.ok(time.time()):
            print("[WhatsApp] rate limit: dropped a command", flush=True)
            return
        if not text.strip():
            await self.send(chat, "Yes? Send what you'd like after my name, e.g. "
                                  "“Jarvis, what's on my agenda?”" if own_chat else
                            "I can read text and voice notes here. For a photo, use the "
                            "chat on the PC.")
            return
        await self.on_command(text.strip(), chat, heard)

    # ── Sending ───────────────────────────────────────────────────────────────

    async def send(self, chat, text: str) -> bool:
        client = self.client
        if client is None or not (text or "").strip():
            return False
        try:
            for part in chunks(text):
                await client.send_message(chat, f"{MARK} {part}")
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] send failed: {exc}", flush=True)
            return False

    async def send_image(self, chat, data: bytes, caption: str = "") -> bool:
        client = self.client
        if client is None:
            return False
        try:
            await client.send_image(chat, data, caption=f"{MARK} {caption}".strip())
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] image send failed: {exc}", flush=True)
            return False

    async def typing(self, chat, on: bool) -> None:
        client = self.client
        if client is None:
            return
        try:
            from neonize.utils.enum import ChatPresence, ChatPresenceMedia
            await client.send_chat_presence(
                chat, ChatPresence.CHAT_PRESENCE_COMPOSING if on else ChatPresence.CHAT_PRESENCE_PAUSED,
                ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
        except Exception:  # noqa: BLE001 — a typing dot is never worth an error
            pass

    async def ask(self, chat, question: str, timeout: float,
                  on_timeout: str = "No answer, so I've taken that as a no.") -> "str | None":
        """Send a question to `chat` and return the next message from it, or None on
        timeout (saying `on_timeout`). That message is the answer, not a command."""
        fut = asyncio.get_running_loop().create_future()
        self._waiting = (_key(chat), fut)
        try:
            if not await self.send(chat, question):
                return None
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            await self.send(chat, on_timeout)
            return None
        finally:
            if self._waiting and self._waiting[1] is fut:
                self._waiting = None


def _key(jid) -> str:
    return f"{getattr(jid, 'User', '')}@{getattr(jid, 'Server', '')}"
