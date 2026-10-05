"""Which operator should run a whole-task goal — the BROWSER or the DESKTOP one.

Until this existed the choice was whatever the chat model put in its action type,
and the chat model gets it wrong in the one direction that hurts most: it sends
desktop work to the browser. Two real runs from the same session —

    goal 'Open Notepad and type "hello …"'  → browser → https://notepad.new/ → ERR_NAME_NOT_RESOLVED
    goal 'open notepad'                     → browser → googled "online notepad"

— on a machine where Notepad is installed and the launcher index already knew it.
Nothing consulted that index before committing to a surface.

This module is the cheap deterministic check that runs first. It only ever
OVERRIDES the model when it has positive evidence (a URL, a known web service, or
an app the machine actually has installed); on anything ambiguous it defers to
whatever the model chose, so it can add certainty but never new guesswork.

Pure and side-effect free apart from the launcher's own cached index, which makes
it directly unit-testable — see test_task_router.py.
"""

from __future__ import annotations

import re

# Sites/services that are the WEB even when a same-named app exists (a YouTube or
# Gmail "app" is usually a browser shortcut, and driving the real site is what the
# user meant). Checked before the installed-app lookup for exactly that reason.
_WEB_SERVICES = {
    "youtube", "gmail", "google", "google docs", "google sheets", "google slides",
    "google drive", "google maps", "amazon", "reddit", "wikipedia", "twitter",
    "instagram", "facebook", "netflix", "github", "linkedin", "chatgpt", "claude",
    "stack overflow", "stackoverflow", "twitch", "ebay", "flipkart", "canva",
    "figma", "notion", "hotstar", "primevideo", "prime video", "whatsapp web",
}

# Apps that ARE browsers: a goal naming one is browser work whichever way it's
# phrased ("open chrome and search…" is not a desktop-UIA task).
_BROWSER_APPS = {"chrome", "google chrome", "edge", "microsoft edge", "firefox",
                 "brave", "opera", "safari", "chromium"}

# An explicit URL, or a bare domain like "youtube.com/feed".
_URL_RE = re.compile(r"\bhttps?://|\bwww\.|\b[a-z0-9][a-z0-9\-]{1,}\.(?:com|org|net|io|co|"
                     r"in|dev|app|ai|gov|edu|me|tv|uk)\b", re.IGNORECASE)

# Phrases that name the surface outright.
_WEB_PHRASE_RE = re.compile(
    r"\b(?:in|on|via|using|through)\s+(?:the\s+)?(?:browser|web|internet|chrome|edge|firefox)\b"
    r"|\bweb ?site\b|\bweb page\b|\bwebpage\b|\bonline\b|\bsearch the web\b",
    re.IGNORECASE)
_DESKTOP_PHRASE_RE = re.compile(
    r"\b(?:desktop app|native app|installed app|on my (?:pc|computer|desktop|machine))\b"
    r"|\bfile explorer\b|\btask manager\b|\bcontrol panel\b|\bcommand prompt\b",
    re.IGNORECASE)

# Candidate app names: what follows a launch/context verb, up to a connective.
# "Open Notepad and type …" → "Notepad"; "in Excel, sum …" → "Excel".
_CANDIDATE_RE = re.compile(
    r"\b(?:open|launch|start|run|use|in|on|into|with|using)\s+"
    r"(?:the\s+|my\s+|a\s+)?"
    r"([A-Za-z][A-Za-z0-9+.\-]*(?:\s+[A-Za-z0-9+.\-]+){0,2})",
    re.IGNORECASE)

# Words that end a candidate — they're grammar, not part of an app's name.
_STOP_WORDS = {
    "and", "then", "to", "the", "a", "an", "for", "with", "from", "into", "of",
    "type", "write", "open", "click", "press", "save", "search", "find", "go",
    "it", "that", "this", "my", "me", "please", "sir", "app", "application",
    "window", "file", "new", "up", "down", "back", "again",
}


# The words being TYPED are content, not a surface. Without stripping them,
# 'Open Notepad and type "visit example.com"', '…write a note about YouTube' and
# '…type I am online' all routed to the browser off their payload. Quoted text
# goes, and so does everything from a writing verb to the end — except a trailing
# "in/into <App>", which names the target ("type hello in Notepad").
_QUOTED_RE = re.compile(r"\"[^\"]*\"|“[^”]*”|'[^']*'")
_PAYLOAD_RE = re.compile(
    r"\b(?:type|typing|write|writing|enter|paste|saying|that says|about)\b.*?"
    r"(?=\s(?:in|into)\s+(?:the\s+|my\s+)?[A-Za-z][\w+.\-]*(?:\s+[A-Za-z][\w+.\-]*)?"
    r"\s*[.!]?$|$)",
    re.IGNORECASE | re.DOTALL)


def _target_clause(goal: str) -> str:
    """The part of a goal that says WHERE to work, with the typed payload removed."""
    return _PAYLOAD_RE.sub(" ", _QUOTED_RE.sub(" ", goal or "")).strip()


def _candidates(goal: str) -> "list[str]":
    """App-name candidates from a goal, longest form first.

    "Open Visual Studio Code and …" yields 'Visual Studio Code', 'Visual Studio',
    'Visual' — so a multi-word app name gets its chance before the first word
    accidentally matches something else."""
    out: "list[str]" = []
    for m in _CANDIDATE_RE.finditer(goal or ""):
        words = [w for w in m.group(1).split() if w]
        # Trim trailing grammar ("notepad and" → "notepad").
        while words and words[-1].lower() in _STOP_WORDS:
            words.pop()
        for n in range(len(words), 0, -1):
            phrase = " ".join(words[:n])
            if phrase.lower() not in _STOP_WORDS and phrase not in out:
                out.append(phrase)
    # A bare goal that is just an app name ("notepad") has no verb to anchor on.
    bare = (goal or "").strip()
    if bare and len(bare.split()) <= 3 and bare not in out:
        out.append(bare)
    return out


def looks_like_web_target(target: str) -> bool:
    """True when `target` names somewhere on the WEB — a URL, a bare domain, or a
    known service. Used to keep the one-shot "open X" fast path from firing on a
    bare app name, which is how "open notepad" became a search for a web notepad
    in the first place; an unrecognised name falls through to the full operator
    loop, which can still hand the task to the desktop."""
    t = (target or "").strip().lower()
    if not t:
        return False
    return bool(_URL_RE.search(t)) or t in _WEB_SERVICES


def route_task(kind: str, goal: str) -> "tuple[str, str]":
    """Return ``(kind, reason)`` — the surface this goal should run on.

    ``kind`` is the model's choice ("browser" or "computer") and is returned
    unchanged unless there is positive evidence for the other surface. ``reason``
    is a short explanation, empty when nothing was overridden."""
    kind = "computer" if str(kind).lower().startswith(("computer", "desktop")) else "browser"
    g = _target_clause(goal)
    if not g:
        return kind, ""
    low = g.lower()

    # 1. An explicit URL or an outright "in the browser" is decisive.
    if _URL_RE.search(g) or _WEB_PHRASE_RE.search(g):
        return "browser", ("the goal names a URL or the browser itself"
                           if kind != "browser" else "")

    # 2. A named web service beats a same-named installed shortcut.
    for svc in _WEB_SERVICES:
        if re.search(rf"\b{re.escape(svc)}\b", low):
            return "browser", (f"“{svc}” is a website" if kind != "browser" else "")

    # 3. An explicitly desktop-only surface.
    if _DESKTOP_PHRASE_RE.search(g):
        return "computer", ("the goal names a desktop-only surface"
                            if kind != "computer" else "")

    # 4. Does the machine actually HAVE an app by this name? This is the check
    #    that was missing, and the one that fixes "open notepad" → notepad.new.
    for cand in _candidates(g):
        if cand.lower() in _BROWSER_APPS:
            return "browser", (f"“{cand}” is a web browser" if kind != "browser" else "")
        try:
            from actions import app_launcher
            found = app_launcher.installed_app_name(cand)
        except Exception:  # noqa: BLE001 — a launcher problem must not block routing
            found = ""
        if found:
            return "computer", (f"“{found}” is installed on this PC"
                                if kind != "computer" else "")

    # 5. No evidence either way — trust the model.
    return kind, ""
