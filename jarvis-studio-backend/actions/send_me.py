"""send_to_me: something for the user themself — their own WhatsApp "Message yourself"
chat, or their own mailbox. It takes no recipient, so it can't reach anyone else; that
is what keeps "JARVIS messages nobody" true with it in the palette.

WhatsApp goes through the linked device (whatsapp_channel.Channel, on the main loop) and
every message starts with its MARK, so the self chat never reads it back as a command.
Email goes through the connected mailbox's SMTP server, from and to its own address,
with the same app password the IMAP connector uses (email_box).

`attach` is a file in the folders JARVIS may read (fs_access), or "screenshot" for a
fresh one: the model calls its tools all at once, so it can't hand over the path of a
screenshot it hasn't taken yet.
"""

from __future__ import annotations

import asyncio
import mimetypes
import os
import re
import smtplib
from email.message import EmailMessage

_loop: "asyncio.AbstractEventLoop | None" = None
_whatsapp = lambda: None        # noqa: E731 — main sets it: the live Channel, or None
TIMEOUT_S = 90
MAX_BYTES = 20 * 1024 * 1024    # under Gmail's 25 MB; WhatsApp documents take more
CAPTION_MAX = 1000

# provider → (host, port); 465 is SSL, 587 STARTTLS. Tests swap SMTP / SMTP_TLS.
_SMTP = {"gmail": ("smtp.gmail.com", 465), "yahoo": ("smtp.mail.yahoo.com", 465),
         "icloud": ("smtp.mail.me.com", 587)}
SMTP_SSL = smtplib.SMTP_SSL
SMTP_TLS = smtplib.SMTP

NO_CHANNEL = ("I have nowhere to send it, sir — link WhatsApp or connect your email in "
              "Settings ▸ Connectors.")


def attach_main(loop: "asyncio.AbstractEventLoop", whatsapp) -> None:
    """main, at startup: its loop, and a getter for the WhatsApp channel (relinking
    replaces it)."""
    global _loop, _whatsapp
    _loop, _whatsapp = loop, whatsapp


def _result(ok: bool, msg: str) -> dict:
    return {"type": "send_to_me", "ok": ok, "message": msg}


def _screenshot() -> "tuple[str, str]":
    from actions import skills
    ok, msg = skills.screenshot()
    m = re.search(r"saved to (.+?)\.?$", msg or "") if ok else None
    return (m.group(1), "") if m else ("", msg or "I couldn't take the screenshot, sir.")


def _file(attach: str) -> "tuple[str, str]":
    """(path, "") for a file it may send, or ("", why not)."""
    if attach.lower() in ("screenshot", "screen", "the screen", "a screenshot"):
        return _screenshot()
    from actions import fs_access
    path = os.path.abspath(os.path.expanduser(attach.strip().strip('"')))
    if not fs_access.is_allowed(path):
        return "", (f"{attach} isn't in a folder I'm allowed to read, sir — add its folder "
                    "in Settings ▸ Files.")
    if not os.path.isfile(path):
        return "", f"I can't find {attach}, sir."
    if os.path.getsize(path) > MAX_BYTES:
        return "", f"{os.path.basename(path)} is over {MAX_BYTES // 2**20} MB, too big to send."
    return path, ""


def smtp_server(provider: str, imap_host: str) -> "tuple[str, int] | None":
    if provider in _SMTP:
        return _SMTP[provider]
    host = re.sub(r"^imap\.", "smtp.", imap_host or "")
    return (host, 465) if host and host != imap_host else None


def compose_to_self(address: str, text: str, path: str = "",
                    data: bytes = b"") -> EmailMessage:
    """An email from the user's own address to itself."""
    name = os.path.basename(path) if path else ""
    first = (text.strip().splitlines() or [""])[0]
    msg = EmailMessage()
    msg["From"] = msg["To"] = address
    msg["Subject"] = (first[:70] + ("…" if len(first) > 70 else "")) if first \
        else f"{name} — from JARVIS"
    msg.set_content(text.strip() or f"{name}, sent by JARVIS.")
    if path:
        mime = (mimetypes.guess_type(path)[0] or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=mime[0], subtype=mime[1], filename=name)
    return msg


def _send_email(text: str, path: str, data: bytes) -> "tuple[bool, str]":
    from actions import email_box
    import app_secrets
    s = email_box.settings()
    password = (app_secrets.get(email_box.SECRET) or "").replace(" ", "")
    server = smtp_server(s.get("provider", ""), s.get("host", ""))
    if not (s.get("address") and password and server):
        return False, NO_CHANNEL
    host, port = server
    try:
        conn = SMTP_SSL(host, port, timeout=30) if port == 465 else SMTP_TLS(host, port, timeout=30)
        with conn:
            if port != 465:
                conn.starttls()
            conn.login(s["address"], password)
            conn.send_message(compose_to_self(s["address"], text, path, data))
    except (smtplib.SMTPException, OSError) as exc:
        return False, f"Your mail server didn't take it, sir ({exc})."
    return True, f"Emailed it to you at {s['address']}."


async def _wa_send(ch, text: str, path: str, data: bytes) -> bool:
    chat = ch.own_chat()
    if chat is None:
        return False
    if not path:
        return await ch.send(chat, text)
    caption = text if len(text) <= CAPTION_MAX else ""
    mime = mimetypes.guess_type(path)[0] or "application/octet-stream"
    if mime.startswith("image/"):
        ok = await ch.send_image(chat, data, caption)
    else:
        ok = await ch.send_file(chat, data, os.path.basename(path), mime, caption)
    if ok and text and not caption:
        ok = await ch.send(chat, text)
    return ok


def _send_whatsapp(ch, text: str, path: str, data: bytes) -> "tuple[bool, str]":
    if _loop is None or not _loop.is_running():
        return False, NO_CHANNEL
    fut = asyncio.run_coroutine_threadsafe(_wa_send(ch, text, path, data), _loop)
    try:
        ok = fut.result(timeout=TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001
        fut.cancel()
        return False, f"WhatsApp didn't take it, sir ({exc})."
    return (True, "Sent it to your WhatsApp.") if ok else \
        (False, "WhatsApp didn't take it, sir — is it still linked?")


def run(spec: dict) -> dict:
    """actions._run_one entry for {"type": "send_to_me", "text", "attach", "via"}. Runs
    on an executor thread."""
    from actions import email_box
    text = str(spec.get("text") or spec.get("message") or "").strip()
    attach = str(spec.get("attach") or spec.get("file") or "").strip()
    via = str(spec.get("via") or "").strip().lower()
    path, data = "", b""
    if attach:
        path, err = _file(attach)
        if err:
            return _result(False, err)
        with open(path, "rb") as fh:
            data = fh.read()
    if not text and not path:
        return _result(False, "There's nothing to send, sir — give me the text or a file.")
    ch = _whatsapp()
    wa = ch if ch is not None and ch.own_chat() is not None else None
    mail = email_box.configured()
    if via in ("email", "mail", "gmail", "e-mail"):
        use = "email" if mail else ""
    elif via == "whatsapp":
        use = "whatsapp" if wa else ""
    else:
        use = "whatsapp" if wa else ("email" if mail else "")
    if not use:
        return _result(False, NO_CHANNEL if not via else
                       f"Your {via} isn't connected, sir — Settings ▸ Connectors.")
    ok, msg = (_send_whatsapp(wa, text, path, data) if use == "whatsapp"
               else _send_email(text, path, data))
    return _result(ok, msg)
