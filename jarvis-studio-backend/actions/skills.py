"""Jarvis skills — the functional muscle behind the new [ACTION] types.

Every public function returns a ``(ok: bool, message: str)`` tuple, exactly like
``app_launcher`` does, so the dispatcher in ``actions/__init__.py`` can treat them
uniformly and the pipeline can speak the outcome.

Heavy / optional third-party libraries (qrcode, mss, pypdf, fpdf, pycaw,
speedtest) are imported lazily inside each function so a missing package only
breaks the one feature that needs it — with a friendly message telling the user
what to ``pip install`` — instead of crashing the whole backend at import time.

State that the rest of the backend needs to observe (silent timer, sleep mode)
lives here as module-level globals with small query helpers, so ``main.py`` can
consult it without importing TTS/pipeline internals.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import socket
import subprocess
import time
import urllib.request
import webbrowser
from pathlib import Path
from typing import Tuple
from urllib.parse import quote_plus

import app_secrets
import storage

_NO_WINDOW = 0x08000000
_SCHEDULE_PATH = app_secrets.path().with_name("schedule.json")


def _ensure_output_dir() -> Path:
    """Images / QR / screenshots all live in the user's storage 'images' folder."""
    return storage.images_dir()


def _open_file(path: Path) -> None:
    """Best-effort: reveal a freshly-created file to the user."""
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except Exception:
        pass


def open_saved_file(name_or_path) -> Tuple[bool, str]:
    """Open a file JARVIS created with the OS default app.

    The model passes just the file name (e.g. "qr_1700000000.png"); we resolve it
    inside the storage folders so "open the QR you just made" opens the right
    file instead of guessing a directory that was never approved.
    """
    raw = (str(name_or_path) or "").strip().strip('"')
    if not raw:
        return False, "Which file should I open?"
    p = storage.resolve(raw)
    if not p.exists():
        return (False, f"I couldn't find '{raw}' in your storage folders "
                f"({storage.get_root()}).")
    if not storage.is_in_storage(p):
        return False, "I can only open files from your JARVIS storage folder without a separate file action."
    try:
        os.startfile(str(p))  # type: ignore[attr-defined]
        return True, f"Opening {p.name}."
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't open {p.name}: {exc}"


def _http_get(url: str, timeout: float = 8.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ── Silent timer & sleep mode (observed by main.py) ───────────────────────────

_muted_until: float = 0.0          # epoch seconds; TTS stays muted until then
_mute_toggle: bool = False         # persistent mute switch (UI toggle); text still shows
_sleeping: bool = False            # True → pipeline ignores everything but "wake up"


def is_muted() -> bool:
    """True when speech should be suppressed — either the persistent mute toggle
    is on, or a timed 'be quiet for N seconds' window is still active."""
    return _mute_toggle or time.monotonic() < _muted_until


def is_mute_toggled() -> bool:
    """The persistent mute switch alone (for syncing the UI toggle state)."""
    return _mute_toggle


def set_mute(on: bool) -> bool:
    """Turn the persistent mute switch on/off. Returns the new state."""
    global _mute_toggle
    _mute_toggle = bool(on)
    return _mute_toggle


def is_sleeping() -> bool:
    return _sleeping


def wake() -> None:
    global _sleeping
    _sleeping = False


def silence(seconds) -> Tuple[bool, str]:
    """Mute spoken replies for a number of seconds (text answers still appear)."""
    global _muted_until
    try:
        secs = max(1, int(float(seconds)))
    except (TypeError, ValueError):
        return False, "Tell me how many seconds you'd like me to stay quiet."
    _muted_until = time.monotonic() + secs
    mins = secs / 60
    pretty = f"{mins:.0f} minute(s)" if secs >= 60 else f"{secs} seconds"
    return True, f"Going silent for {pretty}. Say my name when you need me."


def sleep_mode(_=None) -> Tuple[bool, str]:
    """Enter standby — ignore all input until the user says 'wake up'."""
    global _sleeping
    _sleeping = True
    return True, "Going to sleep. Say 'wake up' when you need me again."


# ── Time & day ────────────────────────────────────────────────────────────────

def current_time(_=None) -> Tuple[bool, str]:
    now = _dt.datetime.now()
    return True, f"It's {now.strftime('%I:%M %p').lstrip('0')}."


def current_day(_=None) -> Tuple[bool, str]:
    now = _dt.datetime.now()
    return True, f"Today is {now.strftime('%A, %B %d, %Y')}."


# ── Network: IP, location, speed ──────────────────────────────────────────────

def ip_address(_=None) -> Tuple[bool, str]:
    """Report the LAN IP and the public IP."""
    local_ip = "unknown"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass

    public_ip = "unavailable"
    try:
        public_ip = _http_get("https://api.ipify.org").strip() or public_ip
    except Exception:
        pass

    return True, f"Your local IP is {local_ip} and your public IP is {public_ip}."


def location(_=None) -> Tuple[bool, str]:
    """Approximate location from the public IP (ip-api.com, no key needed)."""
    try:
        data = json.loads(_http_get("http://ip-api.com/json/?fields=status,city,regionName,country,lat,lon,isp"))
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't look up your location: {exc}"
    if data.get("status") != "success":
        return False, "I couldn't determine your location right now."
    city = data.get("city", "")
    region = data.get("regionName", "")
    country = data.get("country", "")
    where = ", ".join(p for p in (city, region, country) if p)
    return True, f"You appear to be in {where} (via {data.get('isp', 'your ISP')})."


def internet_speed(_=None) -> Tuple[bool, str]:
    """Measure download/upload speed. Uses the `speedtest` library if present."""
    try:
        import speedtest  # type: ignore
    except ImportError:
        return False, ("Internet-speed testing needs the speedtest module. "
                       "Install it with: pip install speedtest-cli")
    try:
        st = speedtest.Speedtest()
        st.get_best_server()
        down = st.download() / 1_000_000        # bits → Mbps
        up = st.upload() / 1_000_000
        ping = st.results.ping
        return True, (f"Download {down:.1f} Mbps, upload {up:.1f} Mbps, "
                      f"ping {ping:.0f} ms.")
    except Exception as exc:  # noqa: BLE001
        return False, f"The speed test failed: {exc}"


# ── QR codes ──────────────────────────────────────────────────────────────────

def qr_code(text) -> "Tuple[bool, str, str | None]":
    """Generate a QR code for the given text/URL.

    Returns (ok, message, data_url). The data URL (a base64 PNG) is rendered
    inline in the chat so the user actually sees the code, in addition to the
    PNG being saved to disk.
    """
    text = (str(text) or "").strip()
    if not text:
        return False, "Give me a link or some text to turn into a QR code.", None
    try:
        import qrcode  # type: ignore
    except ImportError:
        return False, "QR codes need the qrcode module. Install it with: pip install qrcode", None
    try:
        import base64
        import io
        img = qrcode.make(text)
        out = _ensure_output_dir() / f"qr_{int(time.time())}.png"
        img.save(out)
        # Also encode it inline so the GUI can display it immediately.
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        data_url = "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
        _open_file(out)
        return True, f"Here's the QR code for {text}. Saved to {out}.", data_url
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't make the QR code: {exc}", None


# ── Screenshots ───────────────────────────────────────────────────────────────

def screenshot(_=None) -> Tuple[bool, str]:
    """Capture the full screen to a PNG (real capture, not just opening the tool)."""
    out = _ensure_output_dir() / f"screenshot_{int(time.time())}.png"
    try:
        import mss  # type: ignore
        with mss.mss() as sct:
            sct.shot(mon=-1, output=str(out))
        _open_file(out)
        return True, f"Screenshot saved to {out}."
    except ImportError:
        pass
    except Exception as exc:  # noqa: BLE001
        return False, f"Screenshot failed: {exc}"
    # Fallback to Pillow's ImageGrab.
    try:
        from PIL import ImageGrab  # type: ignore
        ImageGrab.grab(all_screens=True).save(out)
        _open_file(out)
        return True, f"Screenshot saved to {out}."
    except Exception as exc:  # noqa: BLE001
        return False, ("Screenshots need mss or Pillow. Install one with: "
                       f"pip install mss  ({exc})")


# ── Screen capture as base64 (for JARVIS's "eyes" / vision) ───────────────────

def capture_screen_b64(max_side: int = 1280, fmt: str = "PNG") -> "str | None":
    """Capture the full screen as a base64 PNG data URL, in memory (downscaled to
    keep vision-model tokens reasonable). Returns None if capture isn't possible.

    ``fmt="JPEG"`` is for the autopilot's every-step frame: ~5x faster to encode
    and half the upload; screen READING for the user keeps lossless PNG."""
    import base64
    import io
    img = None
    try:
        import mss  # type: ignore
        from PIL import Image  # type: ignore
        with mss.mss() as sct:
            shot = sct.grab(sct.monitors[0])           # all monitors combined
            img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    except Exception:
        try:
            from PIL import ImageGrab  # type: ignore
            img = ImageGrab.grab(all_screens=True)
        except Exception:
            return None
    try:
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        if fmt.upper() == "JPEG":
            img.convert("RGB").save(buf, format="JPEG", quality=80)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        img.save(buf, format="PNG")
        return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return None


# ── Generated-image persistence + thumbnails ──────────────────────────────────

def _split_data_url(s: str) -> "tuple[str, str]":
    """(mime, bare_base64) from a data: URL or raw base64. Defaults to image/png."""
    s = (s or "").strip()
    if s.startswith("data:"):
        head, _, b64 = s.partition(",")
        mime = head[5:].split(";")[0] or "image/png"
        return mime, b64
    return "image/png", s


def save_image_data_url(data_url: str, prefix: str = "imagen") -> "Tuple[bool, str]":
    """Write a generated image (a data: URL or raw base64) into the user's storage
    'images' folder so it persists and can be opened/found later. Mirrors qr_code/
    screenshot. Returns (ok, absolute_path). Best-effort; never raises."""
    import base64
    import binascii
    mime, b64 = _split_data_url(data_url)
    if not b64:
        return False, ""
    ext = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/webp": "webp",
           "image/gif": "gif"}.get(mime.lower(), "png")
    try:
        blob = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return False, ""
    try:
        out = _ensure_output_dir() / f"{prefix}_{int(time.time())}.{ext}"
        out.write_bytes(blob)
        return True, str(out)
    except Exception as exc:  # noqa: BLE001
        print(f"[Image] couldn't save generated image: {exc}", flush=True)
        return False, ""


def thumbnail_data_url(image: str, max_side: int = 512) -> str:
    """Downscale a screenshot (data: URL or raw base64) to a small JPEG data URL for
    the activity feed — keeps the per-step thumbnails light over the websocket.
    Returns a usable data URL even on failure (the original, normalised)."""
    _mime, b64 = _split_data_url(image)
    if not b64:
        return ""
    original = image if (image or "").startswith("data:") else f"data:image/jpeg;base64,{b64}"
    try:
        import base64
        import io
        from PIL import Image  # type: ignore
        img = Image.open(io.BytesIO(base64.b64decode(b64)))
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=70)
        return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:  # noqa: BLE001 — Pillow missing / odd image: send it as-is
        return original


# ── System volume (absolute level) ────────────────────────────────────────────

def set_volume(level) -> Tuple[bool, str]:
    """Set the master volume to an absolute 0-100 percentage via pycaw."""
    try:
        pct = max(0, min(100, int(float(level))))
    except (TypeError, ValueError):
        return False, "Give me a volume level between 0 and 100."
    try:
        from ctypes import cast, POINTER
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
    except ImportError:
        return False, ("Setting an exact volume needs pycaw. Install it with: "
                       "pip install pycaw comtypes")
    try:
        speakers = AudioUtilities.GetSpeakers()
        # pycaw >= 20251023 wraps the device (AudioDevice.EndpointVolume); older
        # releases return the raw COM device that must be Activate()d. The release
        # requirement still allows both, so support both.
        vol = getattr(speakers, "EndpointVolume", None)
        if vol is None:
            iface = speakers.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(iface, POINTER(IAudioEndpointVolume))
        vol.SetMasterVolumeLevelScalar(pct / 100.0, None)
        return True, f"Volume set to {pct}%."
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't change the volume: {exc}"


# ── Power: shutdown / restart / sleep / etc. ──────────────────────────────────

def power(command) -> Tuple[bool, str]:
    cmd = (str(command) or "").strip().lower().replace(" ", "_")
    try:
        if cmd in ("shutdown", "shut_down", "turn_off", "power_off"):
            subprocess.Popen(["shutdown", "/s", "/t", "20"], creationflags=_NO_WINDOW)
            return True, "Shutting down in 20 seconds. Say 'cancel shutdown' to stop it."
        if cmd in ("restart", "reboot"):
            subprocess.Popen(["shutdown", "/r", "/t", "20"], creationflags=_NO_WINDOW)
            return True, "Restarting in 20 seconds. Say 'cancel shutdown' to stop it."
        if cmd in ("cancel", "cancel_shutdown", "abort"):
            subprocess.Popen(["shutdown", "/a"], creationflags=_NO_WINDOW)
            return True, "Shutdown cancelled."
        if cmd in ("logoff", "log_off", "sign_out", "logout"):
            subprocess.Popen(["shutdown", "/l"], creationflags=_NO_WINDOW)
            return True, "Signing you out."
        if cmd in ("lock",):
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"],
                             creationflags=_NO_WINDOW)
            return True, "Locking your screen."
        if cmd in ("sleep", "suspend"):
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                             creationflags=_NO_WINDOW)
            return True, "Putting the computer to sleep."
        if cmd in ("hibernate",):
            subprocess.Popen(["shutdown", "/h"], creationflags=_NO_WINDOW)
            return True, "Hibernating."
        return False, f"I don't know the power command '{command}'."
    except Exception as exc:  # noqa: BLE001
        return False, f"That power command failed: {exc}"


# ── Close an application ───────────────────────────────────────────────────────

# Spoken app name → process image name for taskkill.
_PROC_ALIASES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe",
    "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "brave": "brave.exe",
    "word": "winword.exe", "excel": "excel.exe", "powerpoint": "powerpnt.exe",
    "outlook": "outlook.exe", "notepad": "notepad.exe",
    "calculator": "calculatorapp.exe", "calc": "calculatorapp.exe",
    "paint": "mspaint.exe", "spotify": "spotify.exe", "vlc": "vlc.exe",
    "discord": "discord.exe", "slack": "slack.exe", "steam": "steam.exe",
    "vs code": "code.exe", "vscode": "code.exe", "code": "code.exe",
    "explorer": "explorer.exe", "file explorer": "explorer.exe",
    "task manager": "taskmgr.exe", "terminal": "windowsterminal.exe",
    "cmd": "cmd.exe", "powershell": "powershell.exe",
}


def close_app(name) -> Tuple[bool, str]:
    raw = (str(name) or "").strip()
    if not raw:
        return False, "Which app should I close?"
    proc = _PROC_ALIASES.get(raw.lower(), raw)
    if not proc.lower().endswith(".exe"):
        proc += ".exe"
    try:
        result = subprocess.run(["taskkill", "/F", "/IM", proc],
                                capture_output=True, text=True,
                                creationflags=_NO_WINDOW)
        if result.returncode == 0:
            return True, f"Closed {raw}."
        return False, f"I couldn't find {raw} running."
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't close {raw}: {exc}"


# ── Persistent daily schedule ─────────────────────────────────────────────────

def _load_schedule() -> dict:
    """The schedule, minus dated items whose day has passed. Items carry the date
    they were added for; without it a "dentist tomorrow" filed under 'tuesday'
    recurred every Tuesday forever. Undated (legacy) items are kept as they are."""
    if not _SCHEDULE_PATH.exists():
        return {}
    try:
        data = json.loads(_SCHEDULE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    today = _dt.date.today().isoformat()
    for day, items in list(data.items()):
        if isinstance(items, list):
            data[day] = [i for i in items if not (isinstance(i, dict)
                                                  and i.get("date", today) < today)]
    return data


_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _date_for_day(day: str) -> "str | None":
    """The next date (today included) that falls on weekday ``day``, or None for a
    key that isn't a weekday name."""
    if day not in _WEEKDAYS:
        return None
    today = _dt.date.today()
    ahead = (_WEEKDAYS.index(day) - today.weekday()) % 7
    return (today + _dt.timedelta(days=ahead)).isoformat()


def _time_key(raw: str) -> int:
    """Minutes past midnight for '09:00', '9:30', '9am', '3:15 pm', 'noon'. Raw
    string order put '9:00' after '13:00', so a morning item read as the current
    activity all afternoon. Unparseable or blank times sort last."""
    t = (raw or "").strip().lower().replace(".", "")
    if t in ("noon", "midday"):
        return 12 * 60
    if t == "midnight":
        return 0
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", t)
    if not m or (not m.group(2) and not m.group(3)):
        return 24 * 60
    hour, minute, half = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if half:
        if not 1 <= hour <= 12:
            return 24 * 60
        hour = hour % 12 + (12 if half == "pm" else 0)
    if hour > 23 or minute > 59:
        return 24 * 60
    return hour * 60 + minute


def _sort_items(items: list) -> None:
    items.sort(key=lambda i: _time_key(i.get("time", "")))


def _save_schedule(data: dict) -> None:
    _SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SCHEDULE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _normalise_day(day: str) -> str:
    day = (day or "").strip().lower()
    if not day or day == "today":
        return _dt.datetime.now().strftime("%A").lower()
    if day == "tomorrow":
        return (_dt.datetime.now() + _dt.timedelta(days=1)).strftime("%A").lower()
    return day


def _find_schedule_item(items: list, payload: dict) -> "int | None":
    """Locate the item to edit/remove: by 1-based 'index', else by 'match'/'task'
    substring on the task text (case-insensitive), else by exact 'time'."""
    if not items:
        return None
    idx = payload.get("index")
    if idx is not None:
        try:
            i = int(idx) - 1
            if 0 <= i < len(items):
                return i
        except (TypeError, ValueError):
            pass
    needle = (payload.get("match") or payload.get("old_task") or payload.get("task") or "").strip().lower()
    if needle:
        for i, it in enumerate(items):
            if needle in (it.get("task") or "").lower():
                return i
    when = (payload.get("match_time") or payload.get("old_time") or "").strip()
    if when:
        for i, it in enumerate(items):
            if (it.get("time") or "") == when:
                return i
    # If there's exactly one item, assume that's the one.
    return 0 if len(items) == 1 else None


def schedule(payload) -> Tuple[bool, str]:
    """Get, add, edit or remove items in a simple persistent per-day schedule.

    payload is the whole action dict, e.g.
      {"type":"schedule","do":"add","day":"monday","time":"09:00","task":"Gym"}
      {"type":"schedule","do":"get","day":"today"}
    """
    if not isinstance(payload, dict):
        return False, "I couldn't read that schedule request."
    action = str(payload.get("do") or payload.get("action") or "get").lower()
    day = _normalise_day(payload.get("day"))
    data = _load_schedule()

    if action in ("add", "set", "create"):
        task = (payload.get("task") or "").strip()
        when = (payload.get("time") or "").strip()
        if not task:
            return False, "What should I add to your schedule?"
        item = {"time": when, "task": task}
        date = _date_for_day(day)
        if date:
            item["date"] = date
        data.setdefault(day, []).append(item)
        _sort_items(data[day])
        _save_schedule(data)
        return True, f"Added '{task}'{f' at {when}' if when else ''} to your {day.title()} schedule."

    if action in ("edit", "update", "change", "reschedule", "move"):
        items = data.get(day, [])
        idx = _find_schedule_item(items, payload)
        if idx is None:
            return False, "I couldn't find that item on your schedule to edit."
        old = dict(items[idx])
        # "task" is ALSO the match text (the native tool sends it as both), so it
        # only renames when it differs from what was matched — otherwise moving
        # "Gym session" matched by "gym" renamed it to "gym".
        needle = (payload.get("match") or payload.get("old_task") or "").strip()
        task_arg = (payload.get("task") or "").strip()
        new_task = (payload.get("new_task") or "").strip() or (
            task_arg if task_arg.lower() != needle.lower() else "")
        new_time = (payload.get("new_time") or payload.get("time") or "").strip()
        if new_task:
            items[idx]["task"] = new_task
        if new_time:
            items[idx]["time"] = new_time
        if not new_task and not new_time:
            return False, "Tell me the new time or task for that item."
        _sort_items(items)
        _save_schedule(data)
        when_txt = f" at {new_time}" if new_time else ""
        return True, (f"Updated '{old.get('task', '')}' to "
                      f"'{new_task or old.get('task', '')}'{when_txt} on {day.title()}.")

    if action in ("remove", "delete_item", "remove_item", "cancel"):
        items = data.get(day, [])
        idx = _find_schedule_item(items, payload)
        if idx is None:
            return False, "I couldn't find that item to remove."
        removed = items.pop(idx)
        _save_schedule(data)
        return True, f"Removed '{removed.get('task', '')}' from your {day.title()} schedule."

    if action in ("clear", "clear_all", "wipe"):
        data.pop(day, None)
        _save_schedule(data)
        return True, f"Cleared your {day.title()} schedule."

    # default: get
    items = data.get(day, [])
    if not items:
        return True, f"You have nothing scheduled for {day.title()}."
    lines = ", ".join(f"{i.get('time', '')} {i.get('task', '')}".strip() for i in items)
    return True, f"On {day.title()} you have: {lines}."


def get_today_schedule() -> list:
    """Structured view of today's agenda for the HUD (not spoken).

    Returns a list of {time, task, duration?, done, now} sorted by time, with
    `done` set for past slots and `now` on the next upcoming item.
    """
    day = _normalise_day("today")
    items = _load_schedule().get(day, [])
    out = []
    for it in items:
        out.append({
            "time": (it.get("time") or "").strip(),
            "task": (it.get("task") or "").strip(),
            "duration": (it.get("duration") or it.get("dur") or "").strip() or None,
        })
    out.sort(key=lambda i: _time_key(i["time"]))  # blank/unparseable sort last
    now = _dt.datetime.now()
    now_min = now.hour * 60 + now.minute
    marked_now = False
    for entry in out:
        t = _time_key(entry["time"])
        if t >= 24 * 60:
            continue
        if t < now_min:
            entry["done"] = True
        elif not marked_now:
            entry["now"] = True
            marked_now = True
    return out


# ── PDF: read & create ────────────────────────────────────────────────────────

def read_pdf(path) -> "Tuple[bool, str, str | None]":
    """Extract text from a PDF so the LLM can summarise/answer about it.

    Groq models are text-only, so we extract the text locally and hand it back
    to the dispatcher, which feeds it to the model as a follow-up turn. Returns
    (ok, short_message, extracted_text).
    """
    p = Path((str(path) or "").strip().strip('"')).expanduser()
    if not p.exists():
        # Maybe a bare filename — look in Documents / Downloads / Desktop.
        for base in ("Documents", "Downloads", "Desktop"):
            cand = Path.home() / base / p.name
            if cand.exists():
                p = cand
                break
    if not p.exists() or p.suffix.lower() != ".pdf":
        return False, f"I couldn't find a PDF at {path}.", None
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError:
        return False, "Reading PDFs needs the pypdf module. Install it with: pip install pypdf", None
    try:
        reader = PdfReader(str(p))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
        if not text:
            return False, f"{p.name} has no extractable text (it may be a scan).", None
        excerpt = text[:8000]
        return True, f"Read {len(reader.pages)} page(s) from {p.name}.", excerpt
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't read that PDF: {exc}", None


def text_to_pdf(payload) -> Tuple[bool, str]:
    """Turn text into a PDF saved in Documents.

    payload may be a plain string or a dict {"text":..., "title":...}.
    """
    if isinstance(payload, dict):
        text = (payload.get("text") or "").strip()
        title = (payload.get("title") or "").strip()
    else:
        text, title = (str(payload) or "").strip(), ""
    if not text:
        return False, "Give me the text you'd like turned into a PDF."
    try:
        from fpdf import FPDF  # type: ignore
    except ImportError:
        return False, "Making PDFs needs the fpdf2 module. Install it with: pip install fpdf2"
    try:
        pdf = FPDF()
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()
        if title:
            pdf.set_font("Helvetica", "B", 16)
            pdf.multi_cell(0, 10, title)
            pdf.ln(2)
        pdf.set_font("Helvetica", size=12)
        # latin-1 is all the core fonts support; drop anything exotic.
        safe = text.encode("latin-1", "replace").decode("latin-1")
        for line in safe.split("\n"):
            pdf.multi_cell(0, 8, line if line else " ")
        out_dir = storage.documents_dir()
        name = (title or "document").lower().replace(" ", "_")[:40] or "document"
        out = out_dir / f"{name}_{int(time.time())}.pdf"
        pdf.output(str(out))
        _open_file(out)
        return True, f"PDF saved to {out}."
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't create the PDF: {exc}"


# ── File deletion (always permission-gated by the dispatcher) ─────────────────

# Substrings that mark a path as off-limits even with approval.
_PROTECTED_PATH_BITS = (
    "\\windows", "\\program files", "\\programdata", "system32",
    "\\$recycle", "\\boot", "\\system volume information", "\\appdata\\local\\microsoft",
)


def delete_file(path) -> Tuple[bool, str]:
    """Delete a file/folder — to the Recycle Bin when possible (recoverable).

    Refuses anything under Windows/Program Files/system locations outright, as a
    second safety net behind the GUI approval gate.
    """
    raw = (str(path) or "").strip().strip('"')
    if not raw:
        return False, "Which file should I delete?"
    p = Path(raw).expanduser()
    if not p.exists():
        for base in ("Documents", "Downloads", "Desktop", "Pictures"):
            cand = Path.home() / base / p.name
            if cand.exists():
                p = cand
                break
    if not p.exists():
        return False, f"I couldn't find {raw}."

    low = str(p.resolve()).lower()
    if any(bit in low for bit in _PROTECTED_PATH_BITS):
        return False, "That's a protected system location — I won't delete it."

    try:
        try:
            from send2trash import send2trash  # type: ignore
        except ImportError:
            return False, (
                "Recoverable deletion needs send2trash. Install it first; I won't "
                "permanently delete files as a fallback."
            )
        send2trash(str(p))
        return True, f"Sent {p.name} to the Recycle Bin."
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't delete {p.name}: {exc}"


# ── Browser search (the single search_web entry point) ────────────────────────

def search_web(query) -> Tuple[bool, str]:
    query = (str(query) or "").strip()
    if not query:
        return False, "What would you like me to search for?"
    try:
        webbrowser.open(f"https://www.google.com/search?q={quote_plus(query)}")
        return True, f"Searching the web for {query}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't run the search: {exc}"
