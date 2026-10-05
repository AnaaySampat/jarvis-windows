"""Persistent memory: conversation history + long-term user facts.

This is what makes JARVIS feel like it *knows* you. Two JSON files live under
``<storage-root>/memory/`` (the user's chosen ~/Jarvis root — NOT the OneDrive
repo, so sync can't clobber it):

    history.json   list[{role, content}]   — the rolling LLM conversation, so a
                                              restart resumes the same chat
                                              instead of starting blank.
    facts.json     list[{id, text, ts}]    — durable things to always know about
                                              the user (preferences, names,
                                              recurring context), injected into
                                              every prompt.

Deliberately dependency-free: facts are recalled by simple recency (always
injected, newest last), which is plenty for a personal assistant and avoids a
heavyweight vector DB. Writes are atomic (temp file + ``os.replace``) so a crash
mid-write can't corrupt the store. Every function is best-effort and never
raises into its caller — memory is an enhancement, never a crash source.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import storage

_MEM_SUBDIR = "memory"
_HISTORY_FILE = "history.json"
_FACTS_FILE = "facts.json"

# Cap stored facts so the injected block can't grow unbounded, and clamp each
# fact's length so one runaway "remember" can't bloat every prompt.
_MAX_FACTS = 200
_MAX_FACT_LEN = 300


def _mem_dir() -> Path:
    d = storage.get_root() / _MEM_SUBDIR
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[Memory] couldn't create {d}: {exc}", flush=True)
    return d


def _read(name: str, default):
    try:
        return json.loads((_mem_dir() / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default
    except Exception as exc:  # noqa: BLE001
        print(f"[Memory] couldn't read {name}: {exc}", flush=True)
        return default


def _write(name: str, data) -> bool:
    """Atomic write: serialise to a PER-WRITER temp file then replace, so an
    interrupted write never leaves a half-written (corrupt) JSON file behind, and
    two threads writing the same file concurrently (e.g. a reminder add on an
    executor thread + the telemetry tick saving via write_json) don't clobber a
    shared ``name.tmp`` mid-write. The final os.replace is atomic, so the last
    writer simply wins — no corruption. Returns False when the write failed, so
    a caller never tells the user "noted" about something that wasn't saved."""
    try:
        d = _mem_dir()
        tmp = d / f"{name}.{os.getpid()}.{threading.get_ident()}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, d / name)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[Memory] couldn't write {name}: {exc}", flush=True)
        return False


# Generic public helpers so sibling modules (e.g. routines.py) can share the
# same atomic, corruption-resistant JSON store under <storage-root>/memory/.
def read_json(name: str, default):
    return _read(name, default)


def write_json(name: str, data) -> bool:
    return _write(name, data)


# ── Conversation history ──────────────────────────────────────────────────────

def load_history() -> list:
    """The persisted conversation (well-formed message dicts only)."""
    data = _read(_HISTORY_FILE, [])
    if not isinstance(data, list):
        return []
    return [m for m in data
            if isinstance(m, dict) and m.get("role") and "content" in m]


# Debounced background writer for history. save_history() is on the hot path
# (every turn), and a synchronous atomic JSON write was happening on the event
# loop. Instead we stash the latest snapshot and let a daemon thread flush it,
# coalescing a burst of rapid turns into a single disk write. Facts keep the
# direct path — they change rarely and callers want the read-back immediately.
_hist_lock = threading.Lock()
_hist_pending: "list | None" = None
_hist_event = threading.Event()
_HIST_DEBOUNCE = 1.5
_writer_started = False


def _history_writer_loop() -> None:
    global _hist_pending
    while True:
        _hist_event.wait()
        time.sleep(_HIST_DEBOUNCE)        # coalesce a burst of turns into one write
        # Hold the lock ACROSS the write so the daemon, flush_history() and
        # clear_history() are mutually exclusive: they can never interleave on the
        # history file, and a clear can't be resurrected by an in-flight daemon
        # write (after a clear, _hist_pending is None → nothing is written). The
        # write is a small JSON on a background thread, so the brief block of a
        # concurrent save_history() (lock-only) is negligible.
        with _hist_lock:
            data = _hist_pending
            _hist_pending = None
            _hist_event.clear()
            if data is not None:
                _write(_HISTORY_FILE, data)


def _ensure_writer() -> None:
    global _writer_started
    if not _writer_started:
        _writer_started = True
        threading.Thread(target=_history_writer_loop, name="jarvis-history-writer",
                         daemon=True).start()


def save_history(history: list) -> None:
    """Queue the conversation for a debounced background write (off the caller's
    thread and the event loop). Coalesces rapid turns into a single disk write."""
    global _hist_pending
    _ensure_writer()
    with _hist_lock:
        _hist_pending = list(history or [])
        _hist_event.set()


def flush_history() -> None:
    """Write any pending history snapshot synchronously now (call on shutdown so
    the last turn isn't lost when the daemon writer is killed)."""
    global _hist_pending
    with _hist_lock:
        data = _hist_pending
        _hist_pending = None
        _hist_event.clear()
        if data is not None:
            _write(_HISTORY_FILE, data)


def clear_history() -> None:
    global _hist_pending
    with _hist_lock:                      # drop any queued write so it can't resurrect
        _hist_pending = None
        _hist_event.clear()
        _write(_HISTORY_FILE, [])


# ── Conversation archive (the "Recents" list) ─────────────────────────────────
# Past conversations the user can reopen. One atomic JSON file holds them all
# (metadata + messages): a personal assistant accrues few enough that a single
# file is simpler and safer than a directory of fragments. Newest first; capped.
_CONV_FILE = "conversations.json"
_MAX_CONVERSATIONS = 40
_conv_lock = threading.RLock()


def _read_conversations() -> list:
    data = _read(_CONV_FILE, [])
    return data if isinstance(data, list) else []


def _clean_messages(messages) -> list:
    return [m for m in (messages or [])
            if isinstance(m, dict) and m.get("role") and "content" in m]


def _conv_meta(c: dict) -> dict:
    """Lightweight view of an archived conversation — no message bodies."""
    return {"id": c.get("id"), "title": c.get("title") or "Conversation",
            "ts": c.get("ts") or 0, "count": len(c.get("messages") or [])}


def list_conversations() -> list:
    """Archived conversations as metadata only (for the Recents list), newest first."""
    return [_conv_meta(c) for c in _read_conversations()
            if isinstance(c, dict) and c.get("id")]


def archive_conversation(messages: list, title: str) -> "dict | None":
    """Snapshot a conversation into the archive. No-op (returns None) for an empty
    conversation. Returns the new entry's metadata."""
    msgs = _clean_messages(messages)
    if not msgs:
        return None
    entry = {"id": f"c{int(time.time() * 1000)}",
             "title": (title or "Conversation").strip()[:80] or "Conversation",
             "ts": time.time(), "messages": msgs}
    with _conv_lock:
        convs = _read_conversations()
        convs.insert(0, entry)
        del convs[_MAX_CONVERSATIONS:]
        _write(_CONV_FILE, convs)
    return _conv_meta(entry)


def pop_conversation(cid: str) -> list:
    """Remove a conversation from the archive and return its messages — used when
    reopening it (it becomes the active conversation again). [] if not found."""
    with _conv_lock:
        convs = _read_conversations()
        found, kept = None, []
        for c in convs:
            if found is None and isinstance(c, dict) and c.get("id") == cid:
                found = c
            else:
                kept.append(c)
        if found is None:
            return []
        _write(_CONV_FILE, kept)
        return _clean_messages(found.get("messages"))


def delete_conversation(cid: str) -> bool:
    """Permanently drop a conversation from the archive."""
    with _conv_lock:
        convs = _read_conversations()
        kept = [c for c in convs if not (isinstance(c, dict) and c.get("id") == cid)]
        if len(kept) == len(convs):
            return False
        _write(_CONV_FILE, kept)
        return True


def clear_conversations() -> None:
    """Empty the whole Recents archive."""
    with _conv_lock:
        _write(_CONV_FILE, [])


# ── Long-term user facts ──────────────────────────────────────────────────────

# Facts change only through add_fact/forget_fact, but facts_block() reads them on
# every LLM turn — cache the parsed list and invalidate on write. The lock makes
# the read-modify-write in add_fact/forget_fact atomic: remember/forget run on
# executor threads (voice tools + text pipeline), so without it two concurrent
# adds could read the same list, append, and both save — silently dropping a
# fact. Re-entrant so the public all_facts()/_save_facts() can be called inside.
_facts_cache: "list | None" = None
_facts_lock = threading.RLock()


def all_facts() -> list:
    global _facts_cache
    with _facts_lock:
        if _facts_cache is None:
            data = _read(_FACTS_FILE, [])
            _facts_cache = data if isinstance(data, list) else []
        return list(_facts_cache)    # a copy — callers must not mutate the cache


def _save_facts(facts: list) -> bool:
    global _facts_cache
    with _facts_lock:
        trimmed = facts[-_MAX_FACTS:]
        if not _write(_FACTS_FILE, trimmed):
            return False
        _facts_cache = trimmed
        return True


def add_fact(text: str) -> "tuple[bool, str]":
    """Remember a durable fact about the user. De-dupes case-insensitively."""
    text = (text or "").strip().strip('"')[:_MAX_FACT_LEN]
    if not text:
        return False, "There was nothing to remember, sir."
    with _facts_lock:                # read-modify-write must be atomic
        facts = all_facts()
        if any((f.get("text") or "").strip().lower() == text.lower() for f in facts):
            return True, "I already have that noted, sir."
        # Unique even within one clock tick (~15 ms on Windows): the Memory view
        # forgets by id, and a shared id would forget both facts.
        n = int(time.time() * 1000)
        taken = {f.get("id") for f in facts}
        while f"f{n}" in taken:
            n += 1
        facts.append({"id": f"f{n}", "text": text, "ts": time.time()})
        if not _save_facts(facts):
            return False, "I couldn't save that to memory, sir — the write failed."
    return True, "Noted, sir — I'll remember that."


def forget_fact(query: str) -> "tuple[bool, str]":
    """Drop facts whose text contains ``query`` (or all of them)."""
    query = (query or "").strip().strip('"').lower()
    if not query:
        return False, "What would you like me to forget, sir?"
    with _facts_lock:                # read-modify-write must be atomic
        facts = all_facts()
        if query in ("all", "everything", "all of it"):
            if not _save_facts([]):
                return False, "I couldn't clear my memory, sir — the write failed."
            return True, "I've cleared everything I'd remembered about you, sir."
        kept = [f for f in facts if query not in (f.get("text") or "").lower()]
        removed = len(facts) - len(kept)
        if removed == 0:
            return False, "I couldn't find anything matching that to forget, sir."
        if not _save_facts(kept):
            return False, "I couldn't update my memory, sir — the write failed."
    return True, "Forgotten, sir."


def delete_fact(fid: str) -> bool:
    """Drop exactly one fact by id (the Memory view's per-row Forget)."""
    with _facts_lock:
        facts = all_facts()
        kept = [f for f in facts if f.get("id") != fid]
        return len(kept) < len(facts) and _save_facts(kept)


def facts_block() -> str:
    """The facts formatted for injection into the prompt (one bullet each)."""
    lines = [f"- {(f.get('text') or '').strip()}" for f in all_facts() if f.get("text")]
    return "\n".join(lines)
