"""WhatsApp channel: message JARVIS from anywhere (OpenClaw's WhatsApp channel).

JARVIS links to the user's WhatsApp as a linked device — the way WhatsApp Web does,
by scanning a QR code — through neonize (Python bindings for whatsmeow). It is an
unofficial client: WhatsApp's terms don't cover it, so it's off by default and the
Settings page says so.

Who can command JARVIS (`admit`):
- The account's own "Message yourself" chat. Only the owner can write there, from any
  of their devices, so it needs no list.
- Direct messages from numbers the user listed (for a JARVIS on a second number).
- Nothing else: groups, broadcasts, and everyone else are ignored without a reply, and
  the owner's own messages to other people are never commands.
Even there a message counts only when it starts with JARVIS's name ("Jarvis, what's my
CPU at?") or replies to one of JARVIS's messages: people keep notes in their own chat
and chat normally with a listed number. A photo or document counts by its caption, and
comes with the command.

A message runs as an ordinary chat turn (main._run_whatsapp_turn) with the PC's voice
quiet; the reply comes back here and approvals are asked here. Anything but a lookup
asks first (actions.needs_permission), because nobody may be at the PC to see it. There
is no tool for JARVIS to message anyone: it only ever answers the chat a command came
from.

Guards: replies start with MARK and a self-chat message that does is JARVIS's own;
messages older than STALE_S (queued while the PC was off) are skipped; at most RATE_N
commands a minute.

Seeing what JARVIS is doing from the chat: a browser/desktop task gets one status
message that is edited in place as its steps land (Progress), "Jarvis, status" is
answered at once even mid-task (`on_status`, from activity.py), and a command that has
to wait for another one is told so (main._on_whatsapp_command).
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
MAX_ATTACH = 8 * 1024 * 1024  # the PC chat's upload limit too (main._MAX_UPLOAD_BYTES)
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
    """The command in a message that starts with JARVIS's name, without the name; None
    when it doesn't start with it (not for JARVIS)."""
    m = _NAME.match(text or "")
    return text[m.end():].strip() if m else None


_CARRIERS = ("extendedTextMessage", "imageMessage", "documentMessage", "audioMessage",
             "videoMessage")


def replies_to_jarvis(msg) -> bool:
    """The message quotes one of JARVIS's own replies (they all start with MARK)."""
    from neonize.utils.message import extract_text
    for field in _CARRIERS:
        if msg.HasField(field):
            ctx = getattr(msg, field).contextInfo
            return ctx.HasField("quotedMessage") and \
                (extract_text(ctx.quotedMessage) or "").lstrip().startswith(MARK)
    return False


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


_STATUS = re.compile(
    r"^\W*(?:status|progress|update|what(?:'?s| is) (?:the )?(?:status|progress)|"
    r"what(?: are|'?re) you (?:doing|up to|working on)|are you (?:busy|done|there)|"
    r"how(?:'?s| is) it going|(?:any )?news)\W*$", re.IGNORECASE)


def is_status_request(text: str) -> bool:
    """"Jarvis, status" / "what are you doing?" — the whole message, so "status of my
    Amazon order" stays an ordinary command."""
    return bool(_STATUS.match(text or ""))


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
    """One linked WhatsApp account. `on_command(text, chat, heard, attachment)` runs a
    command (heard=True for a voice note; attachment is (name, mime, bytes) for a photo
    or document, else None); `on_state(state)` reports status to the HUD;
    `transcribe(audio_bytes)` turns a voice note into text; `on_status(chat)`, when
    given, answers "Jarvis, status" without running a turn."""

    def __init__(self, on_command, on_state, transcribe, on_status=None):
        self.on_command, self.on_state, self.transcribe = on_command, on_state, transcribe
        self.on_status = on_status
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
        except ImportError as exc:
            print(f"[WhatsApp] neonize didn't load: {exc!r}", flush=True)
            await self._set(status="error", error="WhatsApp needs the neonize package — "
                                                  "pip install neonize, then restart JARVIS. "
                                                  f"({exc})")
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
        if msg.HasField("documentWithCaptionMessage"):   # a document sent with a caption
            msg = msg.documentWithCaptionMessage.message
        text = extract_text(msg) or ""
        sent_at = float(info.Timestamp or 0)
        if sent_at > 1e12:                 # milliseconds
            sent_at /= 1000
        why = admit(src, text, self._me, settings()["allow"], sent_at, time.time())
        if why not in ("", "stale") or info.ID in self._seen:
            return
        self._seen.append(info.ID)
        own_chat = bool(src.IsFromMe)
        to_jarvis = replies_to_jarvis(msg)
        if why == "stale":
            if to_jarvis or addressed(text) is not None:       # a command, not a note
                print("[WhatsApp] skipped a command sent while JARVIS was offline", flush=True)
                await self.send(src.Chat, "That came in while I was offline, so I didn't run "
                                          "it. Send it again if you still want it.")
            return
        chat = src.Chat
        waiting = self._waiting
        # "Jarvis, status" while a question is open is a status request, not the answer
        # (taken as one it would have been a "no").
        named = addressed(text)
        asks_status = self.on_status is not None and is_status_request(
            named if named is not None else text)
        # The answer to a question asked in this chat. The own chat may come addressed
        # by the account's other id (phone number vs LID), so any own-chat reply counts.
        if waiting and not waiting[1].done() and not asks_status and (
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
        text = text.strip() if to_jarvis else addressed(text)
        if text is None:
            return                         # a note or a chat, not for JARVIS
        if not self._rate.ok(time.time()):
            print("[WhatsApp] rate limit: dropped a command", flush=True)
            return
        if self.on_status is not None and is_status_request(text) and not (
                msg.HasField("imageMessage") or msg.HasField("documentMessage")):
            await self.on_status(chat)
            return
        attachment = None
        if msg.HasField("imageMessage") or msg.HasField("documentMessage"):
            attachment = await self._attachment(client, msg, chat)
            if attachment is None:
                return
        if not text and attachment is None:
            await self.send(chat, "Yes? Send what you'd like after my name, e.g. "
                                  "“Jarvis, what's on my agenda?”")
            return
        await self.on_command(text, chat, heard, attachment)

    async def _attachment(self, client, msg, chat) -> "tuple[str, str, bytes] | None":
        """(name, mime, bytes) of a photo or document, or None after saying why not."""
        if msg.HasField("imageMessage"):
            media, name = msg.imageMessage, "photo.jpg"
        else:
            media = msg.documentMessage
            name = media.fileName or media.title or "document"
        if media.fileLength > MAX_ATTACH:
            await self.send(chat, f"That file is too large; I can take up to "
                                  f"{MAX_ATTACH // (1024 * 1024)} MB.")
            return None
        try:
            data = await client.download_any(msg)
        except Exception as exc:  # noqa: BLE001
            await self.send(chat, f"I couldn't download that file ({exc}).")
            return None
        return name, media.mimetype or "", data

    # ── Sending ───────────────────────────────────────────────────────────────

    async def send(self, chat, text: str) -> bool:
        return await self.send_id(chat, text) is not None

    async def send_id(self, chat, text: str) -> "str | None":
        """Send `text` (in chunks); the id of the last message sent, None on failure
        ('' when the client gave no id)."""
        client = self.client
        if client is None or not (text or "").strip():
            return None
        try:
            mid = ""
            for part in chunks(text):
                resp = await client.send_message(chat, f"{MARK} {part}")
                mid = str(getattr(resp, "ID", "") or "")
            return mid
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] send failed: {exc}", flush=True)
            return None

    async def edit(self, chat, message_id: str, text: str) -> bool:
        """Replace the text of one of JARVIS's own messages (WhatsApp allows it for
        15 minutes after sending)."""
        client = self.client
        if client is None or not message_id or not (text or "").strip():
            return False
        try:
            from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message
            await client.edit_message(chat, message_id,
                                      Message(conversation=f"{MARK} {text[:CHUNK]}"))
            return True
        except Exception as exc:  # noqa: BLE001
            print(f"[WhatsApp] edit failed: {exc}", flush=True)
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


# ── Live progress for a long task (one message, edited in place) ──────────────

EDIT_EVERY_S = 3.0          # at most one edit this often
FALLBACK_EVERY_S = 30.0     # when edits don't take, a fresh message at most this often
_PROGRESS_LINES = 5


def progress_text(goal: str, lines: "list[str]", state: str = "running",
                  summary: str = "") -> str:
    """The status message for a task: its goal, the last few steps, and how it ended."""
    head = {"running": "⏳ Working on it", "done": "✅ Finished",
            "failed": "⚠️ Didn't finish", "stopped": "⏹ Stopped",
            "ended": "⏹ Ended"}.get(state, "⏳ Working on it")
    out = [f"{head}: {' '.join(str(goal or '').split())[:200]}"]
    shown = lines[-_PROGRESS_LINES:]
    if len(lines) > len(shown):
        out.append(f"… {len(lines) - len(shown)} earlier step{'s' if len(lines) - len(shown) > 1 else ''}")
    out += [f"→ {' '.join(str(line).split())[:160]}" for line in shown]
    if state != "running" and summary:
        out.append(summary.strip()[:300])
    elif state == "running":
        out.append("(Send “Jarvis, status” any time.)")
    return "\n".join(out)


class Progress:
    """One task's live status message in a chat. `step`/`finish` are cheap and safe to
    call from an event tap; a single flusher sends the first message, then edits it at
    most every EDIT_EVERY_S. If an edit fails (too old, or the client can't), the
    progress goes out as a fresh message instead, at most every FALLBACK_EVERY_S."""

    def __init__(self, channel: "Channel", chat, goal: str, task_id=None,
                 clock=time.monotonic):
        self.channel, self.chat, self.goal, self.clock = channel, chat, goal, clock
        self.task_id = task_id
        self.lines: "list[str]" = []
        self.state, self.summary = "running", ""
        self.mid: "str | None" = None
        self._shown = ""
        self._last = -1e9
        self._fresh = -1e9
        self._wake = asyncio.Event()
        self._task = asyncio.ensure_future(self._run())

    def step(self, line: str) -> None:
        if line and self.state == "running":
            self.lines.append(str(line))
            self._wake.set()

    def finish(self, ok: bool, summary: str = "", stopped: bool = False) -> None:
        self.state = "stopped" if stopped else ("done" if ok else "failed")
        self.summary = summary
        self._wake.set()

    def text(self) -> str:
        return progress_text(self.goal, self.lines, self.state, self.summary)

    async def _run(self) -> None:
        try:
            while True:
                await self._wake.wait()
                self._wake.clear()
                wait = EDIT_EVERY_S - (self.clock() - self._last)
                if wait > 0 and self.state == "running":
                    await asyncio.sleep(wait)
                    self._wake.clear()
                await self._show(self.text())
                if self.state != "running":
                    return
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001 — progress is a courtesy, never a failure
            print(f"[WhatsApp] progress failed: {exc}", flush=True)

    async def _show(self, text: str) -> None:
        if text == self._shown:
            return
        now = self.clock()
        self._last = now
        if self.mid and await self.channel.edit(self.chat, self.mid, text):
            self._shown = text
            return
        if self.mid is None or now - self._fresh >= FALLBACK_EVERY_S or self.state != "running":
            mid = await self.channel.send_id(self.chat, text)
            if mid is not None:
                self.mid, self._shown, self._fresh = mid or None, text, now

    def start(self) -> None:
        """Send the first message now (the goal, before any step)."""
        self._wake.set()

    async def close(self, timeout: float = 10.0) -> None:
        """Wait for the last update to go out (or give up after `timeout`). A task that
        never reported how it ended is shown as ended, never as done."""
        if self.state == "running":
            self.state = "ended"
            self._wake.set()
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()


def _key(jid) -> str:
    return f"{getattr(jid, 'User', '')}@{getattr(jid, 'Server', '')}"
