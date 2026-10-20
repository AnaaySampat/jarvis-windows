"""One memory across the phone and the PC: remembered facts and named routines, merged
over the paired phone's signed link.

The phone starts every exchange (``memory.sync``: what changed there since it last
sent, and the PC's cursor from last time); the PC applies it and answers with what
changed here since that cursor (``memory.synced``). Both sides run this same merge:

- **Facts** are keyed by their text, normalised (case, spacing, a trailing full stop),
  since the phone's facts have no ids. A fact's time is when it was learned.
- **Routines** are keyed by name; unnamed timed routines from before names existed
  stay on this PC. A routine also carries ``origin`` — the device whose clock fires it
  when it's timed — so a synced 07:00 briefing doesn't run on both. Today's run
  (``last``) and on/off stay per device.
- **Newest change wins**, and a deletion is a change too: forgetting leaves a dated
  tombstone here (90 days), so a fact deleted on one device doesn't come back from the
  other. Times are each device's clock; skew only matters for edits made on both
  devices within the same few seconds.

Playbooks are not synced: the phone's are typed procedures, the PC's are text steps.
"""

from __future__ import annotations

import re
import threading
import time

import memory_store

_FILE = "sync_tombstones.json"
_KEEP_MS = 90 * 86_400_000
_MAX_TOMBS = 2000
_MAX_ENTRIES = 1000
_lock = threading.RLock()


def now_ms() -> int:
    return int(time.time() * 1000)


def fact_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower()).rstrip(" .!")[:300]


def routine_key(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())[:60]


_WEEK = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


def days_out(days) -> str:
    """This PC's days ('daily' or ['mon', …]) → the wire's 'daily' / 'mon,wed,fri'."""
    if days == "daily" or not days:
        return "daily"
    picked = [d for d in _WEEK if d in (days or [])]
    return "daily" if len(picked) == 7 or not picked else ",".join(picked)


# ── Tombstones ────────────────────────────────────────────────────────────────

def _tombs() -> dict:
    data = memory_store.read_json(_FILE, {})
    out = {"facts": {}, "routines": {}}
    if isinstance(data, dict):
        for kind in out:
            got = data.get(kind)
            if isinstance(got, dict):
                out[kind] = {str(k): int(v) for k, v in got.items() if isinstance(v, (int, float))}
    return out


def _save_tombs(tombs: dict) -> None:
    cutoff = now_ms() - _KEEP_MS
    for kind, rows in tombs.items():
        kept = sorted(((k, v) for k, v in rows.items() if v >= cutoff), key=lambda kv: kv[1])
        tombs[kind] = dict(kept[-_MAX_TOMBS:])
    memory_store.write_json(_FILE, tombs)


def gone(kind: str, keys, at: "int | None" = None) -> None:
    """Record that these facts/routines (by key) were deleted here."""
    keys = [k for k in keys if k]
    if not keys:
        return
    with _lock:
        tombs = _tombs()
        for k in keys:
            tombs[kind][k] = max(tombs[kind].get(k, 0), at or now_ms())
        _save_tombs(tombs)


def fact_tombstones() -> dict:
    """Forgotten facts: key → when (ms)."""
    return _tombs()["facts"]


def revived(kind: str, key: str) -> None:
    """A fact/routine with this key exists again: forget its tombstone."""
    with _lock:
        tombs = _tombs()
        if tombs[kind].pop(key, None) is not None:
            _save_tombs(tombs)


# ── What changed here ─────────────────────────────────────────────────────────

def _routine_ts(r: dict) -> int:
    if isinstance(r.get("updated"), (int, float)):
        return int(r["updated"])
    digits = re.sub(r"\D", "", str(r.get("id") or ""))
    return int(digits) if digits else 1


def changes(since: int) -> dict:
    """Facts and named routines added, changed or deleted here since `since` (ms)."""
    tombs = _tombs()
    facts = [{"key": fact_key(f["text"]), "text": f["text"],
              "ts": int(float(f.get("ts") or 0) * 1000) or 1}
             for f in memory_store.all_facts() if f.get("text")]
    facts = [f for f in facts if f["ts"] >= since]      # inclusive: a repeat is harmless
    facts += [{"key": k, "deleted": True, "ts": t} for k, t in tombs["facts"].items() if t >= since]
    import routines
    rts = []
    for r in routines.all_routines():
        if not (r.get("name") or "").strip() or _routine_ts(r) < since:
            continue
        rts.append({"key": routine_key(r["name"]), "name": r["name"], "prompt": r.get("prompt", ""),
                    "time": r.get("time", ""), "days": days_out(r.get("days")),
                    **({"trigger": "home"} if r.get("trigger") == "home" else {}),
                    "origin": r.get("origin") or "pc", "ts": _routine_ts(r)})
    rts += [{"key": k, "deleted": True, "ts": t} for k, t in tombs["routines"].items() if t >= since]
    return {"facts": facts[:_MAX_ENTRIES], "routines": rts[:_MAX_ENTRIES]}


# ── Applying the other device's changes ───────────────────────────────────────

def _int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _apply_fact(e: dict, tombs: dict) -> bool:
    key, ts = fact_key(str(e.get("key") or e.get("text") or "")), _int(e.get("ts"))
    if not key or ts <= 0:
        return False
    local = [f for f in memory_store.all_facts() if fact_key(f.get("text", "")) == key]
    if e.get("deleted"):
        doomed = [f for f in local if int(float(f.get("ts") or 0) * 1000) <= ts]
        for f in doomed:
            memory_store.delete_fact(f["id"], at=ts)
        if not local:
            gone("facts", [key], at=ts)        # remember it, so an older add stays out
        return bool(doomed)
    text = str(e.get("text") or "").strip()
    if not text or local or tombs["facts"].get(key, 0) >= ts:
        return False
    ok, _ = memory_store.add_fact(text, at=ts / 1000)
    return ok


def _apply_routine(e: dict, tombs: dict) -> bool:
    import routines
    key, ts = routine_key(str(e.get("key") or e.get("name") or "")), _int(e.get("ts"))
    if not key or ts <= 0:
        return False
    local = next((r for r in routines.all_routines() if routine_key(r.get("name", "")) == key), None)
    if local is not None and _routine_ts(local) >= ts:
        return False
    if e.get("deleted"):
        if local is None:
            gone("routines", [key], at=ts)
            return False
        return routines.remove_routine(local["id"], at=ts)[0]
    if local is None and tombs["routines"].get(key, 0) >= ts:
        return False
    origin = "phone" if e.get("origin") == "phone" else "pc"
    ok, _ = routines.save_routine(str(e.get("name") or "")[:60], str(e.get("prompt") or "")[:2000],
                                  str(e.get("time") or ""), str(e.get("days") or "daily"),
                                  rid=local["id"] if local else "", origin=origin, updated=ts,
                                  trigger="home" if e.get("trigger") == "home" else "none")
    return ok


def apply(payload: dict) -> bool:
    """Merge another device's changes in. True when anything here changed."""
    changed = False
    with _lock:
        tombs = _tombs()
        for e in (payload.get("facts") or [])[:_MAX_ENTRIES]:
            if isinstance(e, dict):
                changed |= _apply_fact(e, tombs)
        for e in (payload.get("routines") or [])[:_MAX_ENTRIES]:
            if isinstance(e, dict):
                changed |= _apply_routine(e, tombs)
    return changed


def handle(payload: dict) -> "tuple[dict, bool]":
    """A phone's memory.sync → (the memory.synced reply, whether anything here changed).
    The reply's `now` is the phone's cursor for next time (this PC's clock)."""
    payload = payload if isinstance(payload, dict) else {}
    stamp = now_ms()
    changed = apply(payload)
    return {"now": stamp, **changes(max(_int(payload.get("since")), 0))}, changed
