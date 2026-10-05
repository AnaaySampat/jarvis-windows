"""Playbooks — JARVIS's self-extensible "skills" library.

This is the safe, working version of "a file of prompts JARVIS can add to so it
gains new features". A **playbook** is a named, plain-English recipe that tells
JARVIS how to accomplish a task using the actions it ALREADY has — no code is
generated or executed. For example:

    name:     "movie night"
    triggers: ["movie night", "watch a film", "film night"]
    steps:    "1) Open the browser to netflix.com. 2) Lower the volume to 40.
               3) Tell the user to pick something."

When the user's request matches a playbook's triggers, that recipe is injected
into the model's context for the turn, so JARVIS follows the steps. JARVIS (or
the user) can teach a NEW playbook at runtime via the ``playbook`` action — that
is how it "adds a new feature to itself", safely.

Built-in playbooks ship in code; user-taught ones persist via :mod:`memory_store`
to ``<storage-root>/memory/playbooks.json``. Best-effort; never raises.
"""

from __future__ import annotations

import re
import time

import memory_store

_FILE = "playbooks.json"

# A few useful recipes out of the box (composed only from existing actions). The
# user/JARVIS can add more, remove these (a removed builtin is remembered as
# hidden), or override them with a same-named playbook.
_BUILTIN = [
    {"id": "b_movie", "name": "movie night",
     "triggers": ["movie night", "watch a movie", "watch a film", "film night"],
     "steps": "Open the browser to netflix.com (browser open). Then set the system "
              "volume to about 40. Then ask the user what they feel like watching."},
    {"id": "b_focus", "name": "focus mode",
     "triggers": ["focus mode", "help me focus", "deep work", "study mode"],
     "steps": "Mute notifications by closing distracting apps if asked, set volume "
              "to 30, and offer to play focus music on Spotify. Keep replies short."},
    {"id": "b_brief", "name": "morning brief",
     "triggers": ["morning brief", "brief me", "what's my day", "daily briefing"],
     "steps": "Give a concise briefing: the weather (use the weather action), the "
              "user's agenda for today, and the top news headlines (news action). "
              "Keep it to a few sentences."},
    {"id": "b_directions", "name": "go somewhere",
     "triggers": ["how do i get to", "directions to", "navigate to", "route to"],
     "steps": "Use the directions action to get distance and drive time to the place "
              "the user named, then offer to open it on a map in the browser."},
    {"id": "b_youtube", "name": "play on youtube",
     "triggers": ["play on youtube", "youtube video of", "watch on youtube",
                  "find a youtube video"],
     "steps": "Open the browser and search YouTube for the title the user named "
              "(use the search action with site youtube — do NOT type into YouTube's "
              "own search box, which re-renders and drops keystrokes). On the results "
              "page, click the FIRST real video result (a long descriptive title), "
              "never a channel, a Short, or a sidebar item. Confirm the video page "
              "opened."},
]


def _user_list() -> list:
    data = memory_store.read_json(_FILE, [])
    return data if isinstance(data, list) else []


def _save(items: list) -> bool:
    return memory_store.write_json(_FILE, items)


def _hidden_ids() -> set:
    data = memory_store.read_json("playbooks_hidden.json", [])
    return set(data) if isinstance(data, list) else set()


def all_playbooks() -> list:
    """Built-ins (minus hidden) overlaid with user-taught ones (user wins on name)."""
    hidden = _hidden_ids()
    user = _user_list()
    user_names = {(p.get("name") or "").lower() for p in user}
    merged = [p for p in _BUILTIN
              if p["id"] not in hidden and (p["name"].lower() not in user_names)]
    return merged + user


def _norm_triggers(triggers, name: str) -> list:
    out = []
    if isinstance(triggers, str):
        triggers = [t.strip() for t in re.split(r"[,;]", triggers)]
    for t in (triggers or []):
        t = str(t).strip().lower()
        if t and t not in out:
            out.append(t)
    nm = (name or "").strip().lower()
    if nm and nm not in out:
        out.append(nm)
    return out


def add_playbook(name, steps, triggers=None) -> "tuple[bool, str]":
    name = (name or "").strip()
    steps = (steps or "").strip()
    if not name:
        return False, "What should I call this playbook, sir?"
    if not steps:
        return False, "Tell me the steps for the playbook, sir."
    items = _user_list()
    # Replace an existing user playbook of the same name (case-insensitive).
    items = [p for p in items if (p.get("name") or "").lower() != name.lower()]
    items.append({"id": f"pb{int(time.time() * 1000)}", "name": name,
                  "triggers": _norm_triggers(triggers, name), "steps": steps})
    if not _save(items):
        return False, "I couldn't save that playbook, sir — the write failed."
    return True, f"Learned a new playbook, “{name}”, sir. I'll use it when it fits."


def _pick(items: list, q: str) -> "tuple[dict | None, list]":
    """The ONE item ``q`` names — exact name or id first, else a unique substring
    match — plus every candidate when the substring is ambiguous. Deleting every
    name containing the query ("gym" took "gym" AND "gym playlist") while reporting
    the singular "Forgot the playbook" was the bug this replaces."""
    exact = [p for p in items
             if q in ((p.get("name") or "").lower(), (p.get("id") or "").lower())]
    if exact:
        return exact[0], []
    loose = [p for p in items if q in (p.get("name") or "").lower()]
    return (loose[0], []) if len(loose) == 1 else (None, loose)


def remove_playbook(query) -> "tuple[bool, str]":
    q = str(query or "").strip().lower()
    if not q:
        return False, "Which playbook should I forget, sir?"
    ambiguous: list = []
    for items, save, said in (
            (_user_list(), _save, "Forgot the “{}” playbook, sir."),
            (_auto_list(), lambda kept: memory_store.write_json(_AUTO_FILE, kept),
             "Forgot the steps I'd learned for “{}”, sir.")):
        hit, many = _pick(items, q)
        if hit:
            if not save([p for p in items if p is not hit]):
                return False, "I couldn't update my playbooks, sir — the write failed."
            return True, said.format(hit.get("name") or query)
        ambiguous += many
    # Otherwise hide a built-in by name.
    hit, many = _pick([b for b in _BUILTIN if b["id"] not in _hidden_ids()], q)
    if hit:
        hidden = _hidden_ids()
        hidden.add(hit["id"])
        if not memory_store.write_json("playbooks_hidden.json", list(hidden)):
            return False, "I couldn't update my playbooks, sir — the write failed."
        return True, f"Disabled the built-in “{hit['name']}” playbook, sir."
    ambiguous += many
    if ambiguous:
        names = ", ".join(f"“{p.get('name')}”" for p in ambiguous[:6])
        return False, f"More than one playbook matches “{query}”, sir: {names}. Which one?"
    return False, "I couldn't find a playbook matching that, sir."


_STOPWORDS = {"a", "an", "the", "on", "of", "to", "for", "in", "and", "i", "me", "my"}


def _trig_match(trig: str, text: str) -> bool:
    """A trigger fires when it appears verbatim, OR when all of its meaningful
    words appear as whole words in the request. Real requests put the object in
    the middle — 'play LOFI HIP HOP on youtube', 'open youtube and play X' —
    so a verbatim-only match left 'play on youtube' dead for exactly the asks
    it was written for. The relaxed path needs at least two content words so a
    short trigger can't fire on everything."""
    if trig in text:
        return True
    words = [w for w in trig.split() if w not in _STOPWORDS]
    if len(words) < 2:
        return False
    return all(re.search(rf"\b{re.escape(w)}\b", text) for w in words)


def find_relevant(user_text: str, limit: int = 2) -> list:
    """Playbooks whose triggers appear in the user's request."""
    text = (user_text or "").lower()
    if not text:
        return []
    hits = []
    for p in all_playbooks():
        for trig in p.get("triggers", []):
            if trig and _trig_match(str(trig).lower(), text):
                hits.append(p)
                break
    return hits[:limit]


def context_block(user_text: str) -> str:
    """A system-message string of any relevant playbooks, or '' if none match."""
    rel = find_relevant(user_text)
    if not rel:
        return ""
    lines = [f"• {p['name']}: {p['steps']}" for p in rel]
    return ("RELEVANT PLAYBOOK(S) for this request — follow these steps using your "
            "normal actions:\n" + "\n".join(lines))


def autopilot_hint(goal: str) -> str:
    """Guidance for the silent autopilot loop: the steps of any playbook whose
    triggers match the GOAL, framed as OPTIONAL hints.

    The operator only has its own browser/desktop commands, so it follows the
    recipe steps that fit and ignores any that don't (a playbook may mix in
    actions — volume, Spotify — the operator can't take). When no taught
    playbook matches, falls back to a trace auto-captured from a previous
    successful run of a similar task. Returns '' when nothing matches, so a
    non-matching task is unaffected."""
    rel = find_relevant(goal)
    if rel:
        lines = [f"• {p['name']}: {p['steps']}" for p in rel]
        return ("PLAYBOOK HINT — a known recipe for a task like this. Follow the "
                "steps that fit your available commands; ignore any that don't:\n"
                + "\n".join(lines))
    auto = find_auto(goal)
    if not auto:
        return ""
    return ("PREVIOUS SUCCESSFUL RUN — a task like this once succeeded with these "
            "steps. Pages and element names may have changed, so re-derive the "
            "numbers from the live page and adapt; treat this as a map, not "
            "orders:\n• " + str(auto.get("steps") or ""))


# ── Auto-captured playbooks (compile-on-success) ──────────────────────────────
# When a silent autopilot task SUCCEEDS (and passes the completion check), the
# sanitized command trace — text labels and URLs only, never element numbers —
# is kept as a draft recipe. It steers ONLY future autopilot runs of a similar
# goal (via autopilot_hint's fallback): it is never injected into ordinary chat
# turns, and a user-taught or built-in playbook always wins over it.

_AUTO_FILE = "playbooks_auto.json"
_AUTO_MAX = 20


def _auto_list() -> list:
    data = memory_store.read_json(_AUTO_FILE, [])
    return data if isinstance(data, list) else []


def learned() -> list:
    """Playbooks auto-captured from completed tasks (for the Memory view)."""
    return _auto_list()


def capture_auto(goal: str, trace: list) -> None:
    """Store a successful run's trace as an auto playbook. Best-effort; skips
    one-step traces (nothing to compile) and goals a real playbook already
    covers (the recipe would only shadow the taught steps)."""
    try:
        goal = (goal or "").strip()
        lines = [str(t).strip() for t in (trace or []) if str(t).strip()]
        if not goal or len(lines) < 2:
            return
        if find_relevant(goal):
            return
        steps = " ".join(f"{i + 1}) {ln}." for i, ln in enumerate(lines[:10]))
        name = "auto: " + (goal if len(goal) <= 48 else goal[:45] + "…")
        items = [p for p in _auto_list()
                 if (p.get("name") or "").lower() != name.lower()]
        items.append({"id": f"auto{int(time.time() * 1000)}", "auto": True,
                      "name": name, "triggers": _norm_triggers([goal], ""),
                      "steps": steps})
        memory_store.write_json(_AUTO_FILE, items[-_AUTO_MAX:])
    except Exception:  # noqa: BLE001 — capture must never break the task report
        pass


def find_auto(goal: str) -> "dict | None":
    """The newest auto-captured playbook whose trigger matches the goal."""
    text = (goal or "").lower()
    if not text:
        return None
    for p in reversed(_auto_list()):
        for trig in p.get("triggers", []):
            if trig and _trig_match(str(trig).lower(), text):
                return p
    return None


def list_text() -> str:
    items = all_playbooks()
    autos = _auto_list()
    if not items and not autos:
        return "You have no playbooks yet, sir. Teach me one with “learn a playbook”."
    lines = [f"- {p['name']}" for p in items]
    lines += [f"- {p['name']} (learned from a completed task)" for p in autos]
    return "Your playbooks, sir:\n" + "\n".join(lines)
