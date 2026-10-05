"""One-shot reminders & timers — "remind me in 10 minutes to…", "set a timer for
5 minutes".

Unlike :mod:`routines` (recurring, day-based), a reminder fires exactly **once**
at an absolute moment, then deletes itself. Persisted via :mod:`memory_store` to
``<storage-root>/memory/reminders.json`` so a restart doesn't lose them.

``due()`` returns the reminders whose moment has arrived and removes them, so the
telemetry tick can poll it freely. Best-effort; never raises into the caller.
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
import time

import memory_store

_FILE = "reminders.json"

# due() is polled from the telemetry loop (event-loop thread) while add/remove run
# on executor threads from action dispatch. Without serialising the read→modify→
# write, two callers can both load the same list and the later _save resurrects a
# fired reminder (double-fire) or drops a just-added one. This lock makes each
# read-modify-write atomic.
_lock = threading.Lock()


def _all() -> list:
    data = memory_store.read_json(_FILE, [])
    return data if isinstance(data, list) else []


def _save(items: list) -> bool:
    return memory_store.write_json(_FILE, items)


def _relative_seconds(s: str) -> int:
    """Total seconds from every '<n> <unit>' pair in the string (0 = none)."""
    total = 0
    for num, unit in re.findall(
            r"(\d+)\s*(hour|hours|hr|hrs|h|minute|minutes|min|mins|m|second|seconds|sec|secs|s)\b", s):
        n = int(num)
        if unit.startswith(("hour", "hr", "h")):
            total += n * 3600
        elif unit.startswith(("min", "m")):
            total += n * 60
        else:
            total += n
    return total


def _parse_when(when) -> "tuple[float, str]":
    """Parse a time expression → (epoch_seconds, human_label). epoch 0 = invalid.

    Handles 'in 10 minutes', 'in 1 hour 30 minutes', '30 seconds', 'at 3pm',
    'at 15:30', bare '15:30' / 'at 8', and bare durations like '5 min'.
    """
    s = str(when or "").strip().lower()
    if not s:
        return 0.0, ""
    now = _dt.datetime.now()

    # An explicit "in <n> …" is unambiguously RELATIVE — resolve it first so a
    # stray clock token elsewhere ("remind me in 5 minutes … the 6pm meeting")
    # can't hijack it into an absolute time.
    if re.search(r"\bin\s+\d", s):
        total = _relative_seconds(s)
        if total > 0:
            target = now + _dt.timedelta(seconds=total)
            return target.timestamp(), _fmt_duration(total)

    # Absolute clock time: 12-hour ("3pm", "3:45 pm"), then 24-hour with or
    # without "at" ("at 15:30", "15:30"), then hour-only ("at 8").
    h = mi = None
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", s)
    if m:
        h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
        if ap == "pm" and h < 12:
            h += 12
        if ap == "am" and h == 12:
            h = 0
    else:
        m = re.search(r"\b(?:at\s+)?(\d{1,2}):(\d{2})\b", s)
        if m:
            h, mi = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"\bat\s+(\d{1,2})\b", s)   # "at 8" → 08:00 (next occurrence)
            if m:
                h, mi = int(m.group(1)), 0
    if h is not None and 0 <= h <= 23 and 0 <= mi <= 59:
        target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
        if target <= now:                           # already passed → tomorrow
            target += _dt.timedelta(days=1)
        return target.timestamp(), target.strftime("%H:%M")

    # Fallback: any bare relative duration anywhere ("5 min", "30 seconds").
    total = _relative_seconds(s)
    if total > 0:
        target = now + _dt.timedelta(seconds=total)
        return target.timestamp(), _fmt_duration(total)
    return 0.0, ""


def _fmt_duration(secs: int) -> str:
    h, rem = divmod(secs, 3600)
    mi, s = divmod(rem, 60)
    bits = []
    if h:
        bits.append(f"{h} hour{'s' if h != 1 else ''}")
    if mi:
        bits.append(f"{mi} minute{'s' if mi != 1 else ''}")
    if s and not h:
        bits.append(f"{s} second{'s' if s != 1 else ''}")
    return " ".join(bits) or "a moment"


def add_reminder(when, text="", is_timer=False) -> "tuple[bool, str]":
    epoch, label = _parse_when(when)
    if not epoch:
        return False, ("I need a time, sir — like “in 10 minutes” or “at 3pm”.")
    text = (text or "").strip()
    with _lock:
        items = _all()
        items.append({"id": f"rem{int(time.time() * 1000)}", "at": epoch,
                      "text": text, "timer": bool(is_timer)})
        if not _save(items):
            return False, "I couldn't save that, sir — the write failed."
    when_s = _dt.datetime.fromtimestamp(epoch).strftime("%H:%M")
    if is_timer and not text:
        return True, f"Timer set, sir — I'll let you know in {label}."
    what = f" to {text}" if text else ""
    return True, f"I'll remind you{what} at {when_s}, sir."


def due(now_epoch: "float | None" = None) -> list:
    """Reminders whose time has come. Removes them (they fire once)."""
    nowt = now_epoch if now_epoch is not None else time.time()
    with _lock:
        items = _all()
        fired = [r for r in items if (r.get("at") or 0) <= nowt]
        if fired:
            _save([r for r in items if (r.get("at") or 0) > nowt])
    return fired


def spoken_for(reminder: dict) -> str:
    text = (reminder.get("text") or "").strip()
    if reminder.get("timer") and not text:
        return "Your timer's up, sir."
    if text:
        return f"Reminder, sir: {text}."
    return "Here's your reminder, sir."


def list_text() -> str:
    items = sorted(_all(), key=lambda r: r.get("at", 0))
    if not items:
        return "You have no reminders set, sir."
    lines = []
    for r in items:
        when_s = _dt.datetime.fromtimestamp(r.get("at", 0)).strftime("%H:%M")
        what = r.get("text") or ("timer" if r.get("timer") else "reminder")
        lines.append(f"- {when_s}: {what}")
    return "Your reminders, sir:\n" + "\n".join(lines)


def clear() -> "tuple[bool, str]":
    if not _save([]):
        return False, "I couldn't clear the reminders, sir — the write failed."
    return True, "All reminders cleared, sir."


def remove(query) -> "tuple[bool, str]":
    q = str(query or "").strip().lower()
    if not q:
        return False, "Which reminder, sir?"
    with _lock:
        items = _all()
        kept = [r for r in items if q not in (r.get("text") or "").lower()
                and q != (r.get("id") or "")]
        n = len(items) - len(kept)
        if not n:
            return False, "I couldn't find a reminder matching that, sir."
        if not _save(kept):
            return False, "I couldn't save that, sir — the write failed."
    return True, f"Removed {n} reminder{'s' if n != 1 else ''}, sir."
