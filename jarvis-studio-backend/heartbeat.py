"""Heartbeat: JARVIS checks in on its own (OpenClaw's heartbeat).

Every N minutes inside the active hours, one small model call reads the user's
checklist — HEARTBEAT.md in the storage folder — against the live HUD state (time,
agenda, battery, CPU, weather) and the remembered facts, and either says something
worth saying or stays silent (HEARTBEAT_OK). Examples of a checklist line: "tell me 10
minutes before anything on my agenda", "if the battery is under 20% and not charging,
say so", "remind me to stand up if I've been here two hours".

- Off by default: every check spends a call from the quota chat uses (the fast tier).
- An empty checklist never costs a call.
- No tools: a heartbeat can only speak, never act.
- An alert identical to one in the last three hours isn't said again.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import time

import jarvis_paths
import storage

FILE = "HEARTBEAT.md"
_SETTINGS = "heartbeat.json"
_DEFAULTS = {"enabled": False, "every_min": 30, "start": "08:00", "end": "22:00"}
_REPEAT_S = 3 * 3600
_OK = "HEARTBEAT_OK"

_TEMPLATE = """# JARVIS heartbeat checklist

<!--
Every half hour or so (Settings → Voice → Heartbeat) JARVIS reads this list against
what the HUD knows right now — the time, your agenda, battery, CPU, memory, weather —
and speaks up only when something here needs you. Write one thing per line, outside
this comment. Anything inside a comment like this one is ignored, and with nothing
else in the file no check runs at all. Examples:

- Tell me 10 minutes before anything on my agenda.
- If the battery is under 20% and not charging, say so.
- If the CPU has been above 90% for a while, tell me which app it is if you know.
-->
"""

_SYSTEM = (
    "You are JARVIS doing a scheduled check-in (a heartbeat), not answering a request. "
    "Read the user's CHECKLIST against the LIVE STATE. If something on the checklist "
    "needs the user's attention right now, reply with one or two short spoken sentences "
    "to them (an occasional 'sir'). Otherwise reply with exactly " + _OK + ". Only "
    "flag what the checklist asks about AND the live state shows: never invent facts, "
    "never bring up old tasks, and don't repeat something listed under ALREADY SAID "
    "unless it has changed. Plain speech: no markdown, no lists."
)

_alerts: "list[tuple[float, str]]" = []     # (when, normalized text) said recently
_last_run = 0.0


def path():
    return storage.get_root() / FILE


def _settings_path():
    return jarvis_paths.app_data_dir() / _SETTINGS


def settings() -> dict:
    try:
        raw = json.loads(_settings_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or corrupt: defaults
        raw = {}
    out = dict(_DEFAULTS)
    out.update({k: raw[k] for k in _DEFAULTS if k in raw})
    return out


def _hhmm(value, fallback: str) -> str:
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(value or ""))
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return fallback
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def save_settings(patch: dict) -> dict:
    cur = settings()
    if "enabled" in patch:
        cur["enabled"] = bool(patch["enabled"])
    if "every_min" in patch:
        try:
            cur["every_min"] = min(240, max(10, int(patch["every_min"])))
        except (TypeError, ValueError):
            pass
    for k in ("start", "end"):
        if k in patch:
            cur[k] = _hhmm(patch[k], cur[k])
    try:
        p = _settings_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(cur), encoding="utf-8")
    except OSError as exc:
        print(f"[Heartbeat] couldn't save settings: {exc}", flush=True)
    return cur


def ensure_file():
    """HEARTBEAT.md, created with a commented template the first time it's needed."""
    p = path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(_TEMPLATE, encoding="utf-8")
    return p


def checklist() -> str:
    """The checklist's real lines: no comments, headings or blank lines. '' = nothing
    to check (and no model call)."""
    try:
        text = path().read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    lines = [ln.strip() for ln in text.splitlines()]
    return "\n".join(ln for ln in lines if ln and not ln.startswith("#"))


def _in_hours(now: _dt.datetime, start: str, end: str) -> bool:
    t = now.strftime("%H:%M")
    if start == end:
        return True
    return start <= t < end if start < end else (t >= start or t < end)


def due(now: "float | None" = None) -> bool:
    """Enabled, inside the active hours, and a full interval since the last check."""
    now = time.time() if now is None else now
    s = settings()
    if not s["enabled"]:
        return False
    if not _in_hours(_dt.datetime.fromtimestamp(now), s["start"], s["end"]):
        return False
    return now - _last_run >= s["every_min"] * 60


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


async def check(live_state: str, facts: str, complete, now: "float | None" = None) -> str:
    """Run one heartbeat. `complete(system, user)` is the model call (async, returns
    text, '' on failure). Returns what to say, or '' to stay quiet."""
    global _last_run
    now = time.time() if now is None else now
    _last_run = now
    items = checklist()
    if not items:
        return ""
    _alerts[:] = [(t, a) for t, a in _alerts if now - t < _REPEAT_S]
    said = "\n".join(f"- {a}" for _t, a in _alerts) or "(nothing yet)"
    user = (f"CHECKLIST:\n{items}\n\nLIVE STATE:\n{live_state or '(no readings)'}\n\n"
            f"ABOUT THE USER:\n{facts or '(nothing remembered)'}\n\n"
            f"ALREADY SAID (last 3 hours):\n{said}\n\n"
            f"Time now: {_dt.datetime.fromtimestamp(now).strftime('%H:%M, %A %d %B')}.")
    reply = (await complete(_SYSTEM, user) or "").strip()
    if not reply or _OK in reply.upper().replace(" ", "_"):
        return ""
    reply = re.sub(r"\s+", " ", reply)[:300]
    if any(_norm(reply) == a for _t, a in _alerts):
        return ""
    _alerts.append((now, _norm(reply)))
    return reply
