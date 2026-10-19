"""Routines — saved steps JARVIS runs by name, at a set time, or both.

"Run movie night" (a voice macro) and "every weekday at 08:00, give me a morning
brief" (a timed briefing) are one store. A routine is only saved text: running it hands
that text to JARVIS's normal pipeline as an ordinary turn, so every step goes through
the same tools and approval prompts — there is no second execution path. Distinct from
the agenda (which only *reminds* you of an item): a routine actually *runs* a request
and speaks the result. Named, untimed routines and editing came from the Android app
(2026-10-18); timed routines without a name, from before, still work.

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


def _name(r: dict) -> str:
    """A routine's name; a timed one saved before names existed goes by its steps."""
    return (r.get("name") or "").strip() or (r.get("prompt") or "")[:40]


def when(r: dict) -> str:
    """'Every day at 08:00', 'Weekdays at 07:30', or 'When you ask'."""
    if r.get("trigger") == "home":
        return "When you get home (on your phone)"
    if not r.get("time"):
        return "When you ask"
    days = r.get("days")
    label = ("Every day" if days == "daily" else
             "Weekdays" if days == ["mon", "tue", "wed", "thu", "fri"] else
             "Weekends" if days == ["sat", "sun"] else
             ", ".join(d.capitalize() for d in days or []))
    return f"{label} at {r['time']}" + (" (on your phone)" if r.get("origin") == "phone" else "")


def add_routine(time_str, prompt, days="daily", name="") -> "tuple[bool, str]":
    return save_routine(name, prompt, time_str, days)


def save_routine(name, prompt, time_str="", days="daily", rid: str = "",
                 origin: str = "", updated: int = 0, trigger: str = "") -> "tuple[bool, str]":
    """Add a routine, or replace the one with id `rid` (keeping its place and today's
    run). A routine needs a time, a name to be run by, or both. `origin` is the device
    whose clock fires it ("pc" or "phone", memory_sync); `updated` (ms) dates a change
    synced from the phone. `trigger` "home" (or a time like "when I get home") runs it
    when the phone gets home: only the phone can tell, so it's the phone's routine."""
    name = re.sub(r"\s+", " ", str(name or "")).strip()[:60]
    prompt = (prompt or "").strip()
    if not prompt:
        return False, "What should the routine do, sir?"
    home = trigger == "home" or bool(re.search(r"\b(home|arriv)", str(time_str or ""), re.I))
    if rid and not str(time_str or "").strip() and not trigger:
        # The HUD's form has no "when I get home": an edit there keeps it.
        home = home or any(r.get("id") == rid and r.get("trigger") == "home" for r in all_routines())
    if home:
        time_str, origin = "", "phone"
        if not name:
            return False, "Give the routine a name, sir."
    t = _norm_time(time_str) if str(time_str or "").strip() else ""
    if str(time_str or "").strip() and not t:
        return False, "I need a valid time like 08:00, sir."
    if not t and not name:
        return False, "Give the routine a name to run it by, or a time like 08:00, sir."
    nd = _norm_days(days)
    with _lock:
        routines = all_routines()
        if name and any(r.get("id") != rid and (r.get("name") or "").lower() == name.lower()
                        for r in routines):
            return False, f"There's already a routine called “{name}”, sir."
        rec = {"id": rid or f"r{int(time.time() * 1000)}", "name": name, "time": t,
               "prompt": prompt, "days": nd, "enabled": True, "last": "",
               "origin": origin or "pc", "updated": updated or int(time.time() * 1000),
               **({"trigger": "home"} if home else {})}
        idx = next((i for i, r in enumerate(routines) if rid and r.get("id") == rid), None)
        if rid and idx is None:
            return False, "That routine is gone, sir."
        if idx is None:
            routines.append(rec)
        else:
            rec["last"] = routines[idx].get("last", "")
            rec["origin"] = origin or ("phone" if home else routines[idx].get("origin") or "pc")
            routines[idx] = rec
        if not _save(routines):
            return False, "I couldn't save that, sir — the write failed."
    if name:
        import memory_sync
        memory_sync.revived("routines", memory_sync.routine_key(name))
    label = f"“{name}”" if name else f"“{prompt}”"
    if home:
        return True, (f"Routine {label} saved for when you get home: your phone notices "
                      "and offers to run it. Say its name and I'll run it here too.")
    return True, (f"Routine {label} saved: {when(rec).lower()}." if t else
                  f"Routine {label} saved. Say its name and I'll run it.")


def find(query) -> "tuple[dict | None, str]":
    """(routine, '') by id, exact name, or the only name/steps containing the words;
    else (None, why)."""
    q = str(query or "").strip().lower()
    routines = all_routines()
    if not q:
        return None, "Which routine, sir?"
    for r in routines:
        if q in (r.get("id", "").lower(), (r.get("name") or "").lower()):
            return r, ""
    hits = [r for r in routines if q in _name(r).lower() or q in (r.get("prompt") or "").lower()]
    if len(hits) == 1:
        return hits[0], ""
    if hits:
        return None, ("Which one, sir? " + ", ".join(f"“{_name(r)}”" for r in hits[:5]) + ".")
    return None, "I couldn't find a routine matching that, sir."


def run_turn(r: dict) -> str:
    """The turn a routine runs as. A legacy timed routine is its prompt as it was."""
    if not r.get("name"):
        return r.get("prompt") or ""
    return (f"Run my routine “{r['name']}”. Do these now with your tools, in order, and "
            f"tell me what actually happened: {r.get('prompt', '')}")


def items() -> list:
    """For the HUD's Routines list."""
    return [{"id": r.get("id", ""), "name": _name(r), "when": when(r), "prompt": r.get("prompt", ""),
             "time": r.get("time", ""), "days": r.get("days", "daily"),
             "last": r.get("last", ""), "named": bool(r.get("name")),
             "origin": r.get("origin") or "pc"}
            for r in all_routines()]


def mentioned(text: str) -> bool:
    """Whether a request says one of the named routines' names."""
    said = f" {re.sub(r'[^a-z0-9]+', ' ', (text or '').lower())} "
    return any(f" {re.sub(r'[^a-z0-9]+', ' ', r['name'].lower()).strip()} " in said
               for r in all_routines() if (r.get("name") or "").strip())


def prompt_hint() -> str:
    """Named routines for the system prompt, so saying a name alone runs it."""
    names = [f"“{r['name']}”" for r in all_routines() if r.get("name")]
    if not names:
        return ""
    return ("SAVED ROUTINES\nThe user's routines: " + ", ".join(names[:30]) + ". When the "
            "user says a routine's name on its own or asks to run one, call manage_routine "
            "with action 'run' and that name.")


def remove_routine(query, at: "int | None" = None) -> "tuple[bool, str]":
    query = str(query or "").strip().lower()
    if not query:
        return False, "Which routine should I remove, sir?"
    with _lock:
        routines = all_routines()
        kept = [r for r in routines
                if query not in (r.get("prompt") or "").lower()
                and query != (r.get("name") or "").lower()
                and query != (r.get("time") or "")
                and query != (r.get("id") or "")]
        removed = len(routines) - len(kept)
        if removed == 0:
            return False, "I couldn't find a routine matching that, sir."
        if not _save(kept):
            return False, "I couldn't save that, sir — the write failed."
    import memory_sync                 # deletions reach the phone as tombstones
    memory_sync.gone("routines", [memory_sync.routine_key(r.get("name", ""))
                                  for r in routines if r not in kept], at=at)
    return True, f"Removed {removed} routine{'s' if removed != 1 else ''}, sir."


def clear_routines() -> "tuple[bool, str]":
    gone = all_routines()
    if not _save([]):
        return False, "I couldn't clear the routines, sir — the write failed."
    import memory_sync
    memory_sync.gone("routines", [memory_sync.routine_key(r.get("name", "")) for r in gone])
    return True, "All routines cleared, sir."


def list_text() -> str:
    routines = all_routines()
    if not routines:
        return "You have no routines set, sir."
    lines = [f"- {_name(r)} — {when(r)}: {r.get('prompt')}"
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
            if (r.get("origin") or "pc") != "pc":
                continue                     # a phone routine: the phone's alarm fires it
            days = r.get("days")
            if days != "daily" and wd not in (days or []):
                continue
            r["last"] = today
            changed = True
            fired.append(r)
        if changed:
            _save(routines)
    return fired
