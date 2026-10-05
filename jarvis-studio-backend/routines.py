"""Scheduled spoken routines — recurring proactive briefings or actions.

A routine fires JARVIS's normal pipeline with a saved prompt at a set time on set
days, e.g. "every weekday at 08:00 → give me a morning brief". This is distinct
from the agenda (which only *reminds* you of an item): a routine actually *runs*
a request and speaks the result.

Persisted via :mod:`memory_store` to ``<storage-root>/memory/routines.json``.
``due()`` is self-de-duping: it stamps each fired routine with today's date and
won't return it again until the next day, so the 1.5s telemetry tick can call it
freely. Best-effort throughout; never raises into the caller.
"""

from __future__ import annotations

import datetime as _dt
import re
import threading
import time

import memory_store

_FILE = "routines.json"
_WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

# due() is polled from the telemetry loop while add/remove run on executor
# threads; serialise the read→modify→write so a routine isn't double-fired or an
# edit lost. See the matching note in reminders.py.
_lock = threading.Lock()


def all_routines() -> list:
    data = memory_store.read_json(_FILE, [])
    return data if isinstance(data, list) else []


def _save(routines: list) -> bool:
    return memory_store.write_json(_FILE, routines)


def _norm_time(s) -> str:
    """Parse '8', '8:30', '08:30', '8am', '7 pm' → 24h 'HH:MM' ('' if invalid)."""
    s = str(s or "").strip().lower().replace(".", ":")
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?\s*(am|pm)?$", s)
    if not m:
        return ""
    h, mi, ap = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ap == "pm" and h < 12:
        h += 12
    if ap == "am" and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return ""
    return f"{h:02d}:{mi:02d}"


def _norm_days(days) -> "str | list":
    """Normalise a day spec → 'daily' or a list of 3-letter weekday codes."""
    if not days:
        return "daily"
    if isinstance(days, str):
        d = days.strip().lower()
        if d in ("daily", "everyday", "every day", "all", "every_day"):
            return "daily"
        if d in ("weekday", "weekdays"):
            return ["mon", "tue", "wed", "thu", "fri"]
        if d in ("weekend", "weekends"):
            return ["sat", "sun"]
        days = [x.strip() for x in d.replace(" ", ",").split(",") if x.strip()]
    out = []
    for x in days:
        x = str(x).strip().lower()[:3]
        if x in _WEEKDAYS and x not in out:
            out.append(x)
    return out or "daily"


def _days_label(days) -> str:
    return "every day" if days == "daily" else ", ".join(days)


def add_routine(time_str, prompt, days="daily") -> "tuple[bool, str]":
    t = _norm_time(time_str)
    if not t:
        return False, "I need a valid time like 08:00, sir."
    prompt = (prompt or "").strip()
    if not prompt:
        return False, "What should the routine do, sir?"
    nd = _norm_days(days)
    with _lock:
        routines = all_routines()
        routines.append({"id": f"r{int(time.time() * 1000)}", "time": t, "prompt": prompt,
                         "days": nd, "enabled": True, "last": ""})
        if not _save(routines):
            return False, "I couldn't save that, sir — the write failed."
    return True, f"Routine set for {t} {_days_label(nd)}: “{prompt}”."


def remove_routine(query) -> "tuple[bool, str]":
    query = str(query or "").strip().lower()
    if not query:
        return False, "Which routine should I remove, sir?"
    with _lock:
        routines = all_routines()
        kept = [r for r in routines
                if query not in (r.get("prompt") or "").lower()
                and query != (r.get("time") or "")
                and query != (r.get("id") or "")]
        removed = len(routines) - len(kept)
        if removed == 0:
            return False, "I couldn't find a routine matching that, sir."
        if not _save(kept):
            return False, "I couldn't save that, sir — the write failed."
    return True, f"Removed {removed} routine{'s' if removed != 1 else ''}, sir."


def clear_routines() -> "tuple[bool, str]":
    if not _save([]):
        return False, "I couldn't clear the routines, sir — the write failed."
    return True, "All routines cleared, sir."


def list_text() -> str:
    routines = all_routines()
    if not routines:
        return "You have no routines set, sir."
    lines = [f"- {r.get('time')} ({_days_label(r.get('days'))}): {r.get('prompt')}"
             for r in routines if r.get("enabled", True)]
    return "Your routines, sir:\n" + "\n".join(lines) if lines else "You have no active routines, sir."


def due(now: "_dt.datetime | None" = None) -> list:
    """Routines that should fire right now. Stamps each as fired today so it won't
    be returned again until tomorrow (safe to poll every second)."""
    now = now or _dt.datetime.now()
    cur = now.strftime("%H:%M")
    today = now.strftime("%Y-%m-%d")
    wd = now.strftime("%a").lower()[:3]
    with _lock:
        routines = all_routines()
        fired, changed = [], False
        for r in routines:
            if not r.get("enabled", True) or r.get("time") != cur or r.get("last") == today:
                continue
            days = r.get("days")
            if days != "daily" and wd not in (days or []):
                continue
            r["last"] = today
            changed = True
            fired.append(r)
        if changed:
            _save(routines)
    return fired
