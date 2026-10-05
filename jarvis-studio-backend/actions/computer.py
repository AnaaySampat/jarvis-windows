"""Desktop senses + SUPERVISED computer control.

Read-only senses (always available, never touch mouse/keyboard):
  • ``active_window()`` / ``foreground_app()`` — what the user is looking at.
  • ``read_clipboard()`` — on demand only (clipboard can hold passwords).

Supervised control (JARVIS's hands on apps WITHOUT a plugin or browser page).
Safety model — designed so it is both safe and not naggy:
  • DISARMED by default. ONE Approve on the floating popup arms it for a
    bounded window (default 10 min, max 30); while armed every action runs
    without further prompts. Disarming is always free.
  • Acts through the Windows **UI Automation accessibility tree** — the same
    "list the real, named controls → click/type by what you see" recipe that
    made the browser reliable — never by guessing pixel coordinates. (A raw
    pixel fallback deliberately does not exist.)
  • REFUSES to act on JARVIS's own windows, so it can never click its own
    Approve button or drive its own UI by mistake.
  • pyautogui's FAILSAFE stays on: slamming the mouse into the top-left corner
    aborts any in-flight actuation instantly. Saying "disarm computer control"
    revokes the window early.
  • Prefer an app PLUGIN (actions/apps/) when one exists, and the browser for
    anything on the web — this is the last-resort generic path.

Everything is lazy-imported and best-effort: missing deps (`uiautomation`,
`pyautogui`) just disable the feature with a helpful message; nothing raises.
"""

from __future__ import annotations

import re
import time


def _is_jarvis_own_window(title: str) -> bool:
    """True ONLY for JARVIS's own HUD / overlay windows, matched by their EXACT
    brand title — the Tauri windows are titled 'J.A.R.V.I.S' / 'JARVIS', which both
    normalize to 'jarvis'. A window that merely CONTAINS 'jarvis' in its title (a
    Notepad or web page whose CONTENT mentions JARVIS, e.g. a doc reading 'Hello
    from JARVIS') is NOT ours and must stay controllable — the old substring check
    wrongly treated it as JARVIS's own window and blocked ALL control on it, and
    hid it from the window list (the Notepad-HTML failure)."""
    norm = (title or "").replace(".", "").replace(" ", "").lower()
    return norm == "jarvis"


def active_window() -> str:
    """Title of the window the user is currently focused on ('' if unknown).
    Fed into the live context so JARVIS knows what the user is looking at."""
    try:
        import pygetwindow as gw
        w = gw.getActiveWindow()
        title = (w.title or "").strip() if w else ""
        # Don't report our own HUD window as 'what the user is looking at'.
        return "" if _is_jarvis_own_window(title) else title
    except Exception:  # noqa: BLE001
        return ""


def _process_of(hwnd: int) -> str:
    """The process image owning ``hwnd``, lowercased, no ``.exe`` ('' if unknown)."""
    try:
        import ctypes
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return ""
        import psutil
        name = (psutil.Process(pid.value).name() or "").lower()
        return name[:-4] if name.endswith(".exe") else name
    except Exception:  # noqa: BLE001
        return ""


def foreground_app() -> dict:
    """The app the user is currently focused on — for the floating overlay pill.

    Returns ``{"app": <process name, lowercased, no .exe>, "title": <window
    title>, "on_jarvis": <True if that window IS the JARVIS HUD>, "hwnd": <int>}``. The overlay
    uses this to show itself only when the user is in another app (and to filter
    by app). ``on_jarvis`` is decided from the window TITLE only — the JARVIS HUD
    is titled "J.A.R.V.I.S" while the borderless pill itself has an empty title,
    so clicking the pill never makes it think it's "on JARVIS" and hide.

    Best-effort and never raises; everything empty/False on failure or non-Windows.
    """
    app, title = "", ""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return {"app": "", "title": "", "on_jarvis": False, "hwnd": 0}
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = (buf.value or "").strip()
        app = _process_of(hwnd)
    except Exception:  # noqa: BLE001
        return {"app": "", "title": "", "on_jarvis": False, "hwnd": 0}
    return {"app": app, "title": title, "on_jarvis": _is_jarvis_own_window(title),
            "hwnd": int(hwnd)}


# Transient / system windows that are never a real app to focus or list. They
# flash briefly during launches — e.g. the "PopupHost" a slow Java app like BlueJ
# pops WHILE it boots — and wait_for_window used to latch onto the first such
# window as "the app's window", then the operator flailed on it (no real controls,
# the window already gone). Matched on the normalized (lowercased, space/dot-
# stripped) title, the same normalization used below.
_JUNK_WINDOW_TITLES = {
    "popuphost", "defaultime", "msctfimeui", "programmanager",
    "windowsinputexperience", "microsofttextinputapplication",
    "taskview", "taskswitching", "searchapp", "startmenuexperiencehost",
    "newnotification", "nvidiageforceoverlay",
}


def list_windows() -> "list[str]":
    """Titles of the visible top-level windows (deduped, JARVIS's own + transient
    system popups excluded). Read-only — used as the autopilot's 'what could I
    focus' observation."""
    try:
        import pygetwindow as gw
        titles: "list[str]" = []
        for t in gw.getAllTitles():
            t = (t or "").strip()
            norm = t.replace(".", "").replace(" ", "").lower()
            if (t and not _is_jarvis_own_window(t) and t not in titles
                    and norm not in _JUNK_WINDOW_TITLES):
                titles.append(t)
        return titles[:15]
    except Exception:  # noqa: BLE001
        return []


def _force_foreground(hwnd: int) -> bool:
    """Bring ``hwnd`` to the foreground RELIABLY from a background process.

    Windows blocks a background process's bare ``SetForegroundWindow`` (the
    foreground LOCK) — which is why ``pygetwindow.activate()`` silently failed and
    the operator looped on 'focus' (the Notepad-HTML failure: focus never stuck).
    The standard workaround is to momentarily AttachThreadInput to BOTH the current
    foreground thread and the target thread, which lifts the lock for the call.
    Returns whether ``hwnd`` actually ended up foreground. Best-effort; never raises."""
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        SW_RESTORE = 9
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        if user32.GetForegroundWindow() == hwnd:
            return True
        cur = kernel32.GetCurrentThreadId()
        fg_thread = user32.GetWindowThreadProcessId(user32.GetForegroundWindow(), None)
        tgt_thread = user32.GetWindowThreadProcessId(hwnd, None)
        attached = []
        for th in {fg_thread, tgt_thread}:
            if th and th != cur and user32.AttachThreadInput(cur, th, True):
                attached.append(th)
        try:
            user32.BringWindowToTop(hwnd)
            user32.ShowWindow(hwnd, SW_RESTORE)
            user32.SetForegroundWindow(hwnd)
            user32.SetActiveWindow(hwnd)
        finally:
            for th in attached:
                user32.AttachThreadInput(cur, th, False)
        time.sleep(0.12)
        return user32.GetForegroundWindow() == hwnd
    except Exception:  # noqa: BLE001
        return False


def focus_window(title: str) -> "tuple[bool, str]":
    """Bring the first window whose title contains `title` to the foreground.

    An ACTUATION (it redirects keyboard focus), so it requires the armed-consent
    window — but unlike the other actuations it's allowed while JARVIS's own
    window is in front: that's exactly the moment you need it ("the user asked
    from the HUD; now focus Notepad"). It still refuses to focus JARVIS itself."""
    if not control_available():
        return False, _CONTROL_UNAVAILABLE
    if not is_armed():
        return False, ("Computer control isn't armed, sir — ask me again and "
                       "approve the prompt.")
    t = (title or "").strip().lower()
    if not t:
        return False, "Which window should I focus, sir?"
    # Already in front? Don't re-activate — return success plainly. Re-activating an
    # already-foreground window can FAIL the post-activate foreground check (Windows
    # won't always re-assert focus), yielding a misleading "tried to focus — check
    # it's in front" that makes the operator loop on focus (the Notepad trap).
    fg0 = foreground_app()
    if is_foreground(title, fg0):
        return True, f"“{fg0.get('title')}” is already in front."
    try:
        import pygetwindow as gw
        wins = [w for w in gw.getAllWindows()
                if (w.title or "").strip() and not _is_jarvis_own_window(w.title.strip())]
        # Exact title, then the app itself (process-aware — "word" must not pick a
        # "Wordle" browser tab while Word is open), then any title containing it.
        mine = set(app_windows(t))
        ranked = ([w for w in wins if w.title.strip().lower() == t]
                  + [w for w in wins if w.title.strip() in mine]
                  + [w for w in wins if t in w.title.strip().lower()])
        if not ranked:
            return False, f"I couldn't find a window matching “{title}”, sir."
        w = ranked[0]
        wt = w.title.strip()
        # Reliable foreground first (AttachThreadInput); fall back to
        # pygetwindow's activate() only if that couldn't be applied.
        hwnd = int(getattr(w, "_hWnd", 0) or 0)
        forced = _force_foreground(hwnd) if hwnd else False
        if not forced:
            try:
                if w.isMinimized:
                    w.restore()
                w.activate()
            except Exception:  # noqa: BLE001 — pygetwindow's activate() can
                pass           # raise spuriously on Windows even when it worked
            time.sleep(0.4)
        if forced or (foreground_app().get("title") or "").strip() == wt:
            return True, f"Focused “{wt}”."
        return True, f"Tried to focus “{wt}” — check it's in front."
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't focus that window: {exc}"


_BROWSER_PROCS = {"chrome", "msedge", "firefox", "brave", "opera", "vivaldi",
                  "iexplore", "arc", "chromium"}
# Hosts whose process says nothing about the app inside (UWP: Calculator, Settings,
# Photos all run under ApplicationFrameHost) — fall back to the title for these.
_HOST_PROCS = {"", "applicationframehost", "explorer"}


def app_window_matches(app: str, title: str, proc: str) -> bool:
    """Is this window (``title`` owned by process ``proc``) the app ``app``?

    Title substrings alone mistook a "Wordle – Google Chrome" tab for Word and a
    Notepad++ window for Notepad — and the operator then typed into the wrong app.
    So the owning PROCESS decides first: an exact process (or its launcher alias,
    word → winword) is a match; a browser is never some other app; a process that
    merely EXTENDS the name (notepad++, wordpad) is a different app. Only then
    does the title count, as a whole word — that path is for apps whose process
    doesn't carry their name (BlueJ runs as javaw) and UWP apps behind a host."""
    want = _norm_app_for_window(app)
    if not want:
        return False
    proc = (proc or "").lower()
    if proc not in _HOST_PROCS:
        from actions import app_launcher
        alias = str(app_launcher._ALIASES.get(want, "")).lower()
        if proc in (want, alias):
            return True
        # notepad++ / wordpad extend the name: a different app. (Not a bare
        # "contains" — Settings runs as systemsettings.)
        if proc in _BROWSER_PROCS or proc.startswith(want):
            return False
    return bool(re.search(rf"(?<![\w+]){re.escape(want)}(?![\w+])", (title or "").lower()))


def is_foreground(target: str, fg: "dict | None" = None) -> bool:
    """Is the window ``target`` names (an exact title, or an app) already in front?"""
    fg = fg if fg is not None else foreground_app()
    title = (fg.get("title") or "").strip()
    t = (target or "").strip().lower()
    return bool(t and title and (title.lower() == t
                                 or app_window_matches(t, title, fg.get("app", ""))))


def app_windows(app: str) -> "list[str]":
    """Titles of the visible windows that ARE ``app`` (see app_window_matches)."""
    try:
        import pygetwindow as gw
        out: "list[str]" = []
        for w in gw.getAllWindows():
            t = (w.title or "").strip()
            if (not t or t in out or _is_jarvis_own_window(t)
                    or t.replace(".", "").replace(" ", "").lower() in _JUNK_WINDOW_TITLES):
                continue
            if app_window_matches(app, t, _process_of(int(getattr(w, "_hWnd", 0) or 0))):
                out.append(t)
        return out
    except Exception:  # noqa: BLE001
        return []


def _norm_app_for_window(name: str) -> str:
    """Reduce an app name to a window-title HINT: drop any directory and the
    ``.exe`` suffix, lowercased. ``'notepad.exe'`` / ``r'C:\\…\\notepad.exe'`` →
    ``'notepad'``. The model often passes ``'notepad.exe'``, but the window title
    is just ``'Notepad'`` — without this the `.exe` never matches and the launch
    wrongly reports 'window hasn't appeared' (the Notepad-HTML failure)."""
    s = (name or "").strip().strip('"').replace("\\", "/").rsplit("/", 1)[-1].lower()
    if s.endswith(".exe"):
        s = s[:-4]
    return s.strip()


def wait_for_window(hint: str = "", before: "list[str] | None" = None,
                    timeout: float = 18.0) -> "tuple[bool, str]":
    """Wait for an app's window to appear (or be re-focused) after a launch.
    Read-only (no guard).

    Resolution order, re-checked each poll:
      1) a NEW window (vs ``before``) whose title carries the app name — strongest;
      2) the first NEW window once it's settled a beat (apps whose title doesn't
         carry their name);
      3) after a ~1s grace, an EXISTING window matching the name — the app was
         ALREADY running and the launch just re-focused it (this is what made
         're-open an already-open app' wrongly report 'hasn't appeared').
    The hint is normalized (``.exe``/path stripped) so 'notepad.exe' matches the
    'Notepad' window."""
    want = _norm_app_for_window(hint)
    prior = set(before or [])
    start = time.monotonic()
    deadline = start + max(1.0, timeout)
    first_new, first_new_at = "", 0.0
    while time.monotonic() < deadline:
        wins = list_windows()
        new = [t for t in wins if t not in prior]
        mine = app_windows(want) if want else []
        if want:
            for t in new:                       # (1) new window, name match
                if t in mine:
                    return True, t
        if new and not first_new:
            first_new, first_new_at = new[0], time.monotonic()
        if first_new and (not want or time.monotonic() - first_new_at > 2.5):
            return True, first_new              # (2) first new window, settled
        if want and time.monotonic() - start > 1.0 and mine:
            return True, mine[0]                # (3) already-open window, name match
        time.sleep(0.5)
    if first_new:
        return True, first_new
    mine = app_windows(want) if want else []
    return (True, mine[0]) if mine else (False, "")


# A window's own title-bar buttons — every window has them, so they say nothing
# about whether its content has loaded.
_CHROME_NAME_RE = re.compile(r"(?:minimi[sz]e|maximi[sz]e|restore|close|system)\b",
                             re.IGNORECASE)


def wait_for_controls(timeout: float = 3.0) -> int:
    """Wait for a just-opened window to finish painting its controls: until at
    least 3 are listed and the count holds steady between two looks. UWP
    Calculator shows only its title-bar buttons for its first second, and a step
    decided on that half-built window went to the slow vision model for
    nothing. Returns the last count (0 when the window exposes none)."""
    end = time.monotonic() + timeout
    last = -1
    while True:
        try:
            import uiautomation as auto
            with auto.UIAutomationInitializerInThread(debug=False):
                # The title bar's own buttons don't count: a Settings page still
                # loading shows those and nothing else.
                n = sum(1 for _i, (_c, _k, name) in zip(range(_LIST_MAX_ITEMS),
                                                         _walk_controls())
                        if not _CHROME_NAME_RE.match(name))
        except Exception:  # noqa: BLE001
            return 0
        if (n >= 3 and n == last) or time.monotonic() >= end:
            return n
        last = n
        time.sleep(0.25)


def read_clipboard() -> "tuple[bool, str]":
    """Current clipboard text. On-demand only (never auto-injected into prompts,
    since the clipboard can hold passwords/secrets)."""
    try:
        import pyperclip
        txt = pyperclip.paste() or ""
        return (True, txt) if txt.strip() else (True, "")
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't read the clipboard: {exc}"


# ════════════════════════════════════════════════════════════════════════════
# Supervised computer control (armed-consent; see the module docstring).
# ════════════════════════════════════════════════════════════════════════════

_ARM_DEFAULT_MIN = 10.0
_ARM_MAX_MIN = 30.0
_armed_until: float = 0.0

# Optional callback (wired by main.py) fired whenever the armed control window
# opens or closes, so the HUD can show a "computer control active" banner with a
# STOP button — mirrors browser.set_state_callback. Best-effort; never raises.
_state_cb = None  # type: ignore[var-annotated]

# The last UI listing, as (selector, name, control_type, ordinal) tuples.
# Click-by-number re-walks the live tree and matches these — UIA COM objects are
# thread-affine, so we never cache the objects themselves (actions run on
# arbitrary executor threads).
#
# `selector` is a STABLE identity for the control (see _selector_for): UIA's own
# RuntimeId where the app exposes one, else its AutomationId. Positional matching
# — the (name, type, ordinal) triple this used to rely on alone — silently
# repoints the moment anything is inserted, removed or re-ordered above the
# target: "click 7" then lands on whatever slid into 7th place. Matching by
# selector first makes that impossible, and the ordinal stays only as the
# fallback for controls that expose neither id.
#
# _ui_cache_hwnd binds the cache to the window it was read from: if the
# foreground window changed since the list, numbered clicks fail loudly instead
# of silently hitting a same-named control in a different app.
_ui_cache: "list[tuple[str, str, str, int]]" = []
_ui_cache_hwnd: int = 0


def _foreground_hwnd() -> int:
    try:
        import ctypes
        return int(ctypes.windll.user32.GetForegroundWindow() or 0)
    except Exception:  # noqa: BLE001
        return 0


def focus_signature() -> str:
    """Where the keyboard focus is right now — the foreground window plus the
    focused element's identity — so a caller can tell when a keystroke or click
    opened something that took the focus (a dialog, a new tab, a field)."""
    hwnd = _foreground_hwnd()
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread(debug=False):
            fc = auto.GetFocusedControl()
            return f"{hwnd}:{_selector_for(fc) if fc else ''}"
    except Exception:  # noqa: BLE001
        return str(hwnd)

_CONTROL_UNAVAILABLE = ("Computer control needs the uiautomation and pyautogui "
                        "packages, sir — install them with: pip install uiautomation pyautogui.")

# Control types worth showing/clicking — the interactive surface of an app.
_INTERESTING_TYPES = {
    "ButtonControl": "button", "SplitButtonControl": "button",
    "EditControl": "text field", "ComboBoxControl": "dropdown",
    # The main editing surface of editors (Notepad, code editors, word processors)
    # is a Document control, not an Edit — without this it never appeared in the
    # control list, so the operator couldn't target 'where to type' and flailed.
    "DocumentControl": "text area",
    "MenuItemControl": "menu item", "ListItemControl": "list item",
    "TabItemControl": "tab", "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio", "HyperlinkControl": "link",
    "TreeItemControl": "tree item",
}
# Read-only text worth showing alongside the controls: an app's readouts (the
# Calculator display "Display is 408", a status line, a dialog's message). Without
# them the operator could press buttons but never see what they did — a live
# Calculator run entered 1×1233 for "12 times 34" and could not notice.
_READOUT_MAX = 8
_READOUT_MAX_CHARS = 120
_WALK_MAX_DEPTH = 16
_WALK_MAX_NODES = 1200      # nodes scanned before giving up (huge apps)
_WALK_TIME_BUDGET = 4.0     # seconds
_LIST_MAX_ITEMS = 40


def control_available() -> bool:
    try:
        import uiautomation  # noqa: F401
        import pyautogui     # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def set_state_callback(cb) -> None:
    """Register a callback fired whenever the armed control window opens/closes.
    ``cb`` receives the dict from :func:`state`. Wired by main.py to push a HUD
    banner + STOP button. Best-effort — callback errors are swallowed."""
    global _state_cb
    _state_cb = cb


def state() -> dict:
    """Current control state for the HUD: whether mouse/keyboard control is armed
    (and, if so, the whole seconds left before the window auto-expires)."""
    armed = is_armed()
    return {"armed": armed, "available": control_available(),
            "seconds_left": max(0, int(_armed_until - time.monotonic())) if armed else 0}


def _emit_state() -> None:
    if _state_cb is None:
        return
    try:
        _state_cb(state())
    except Exception:  # noqa: BLE001 — HUD push is best-effort
        pass


def arm(minutes=None) -> "tuple[bool, str]":
    """Open the bounded control window (called after the user Approves once)."""
    global _armed_until
    if not control_available():
        return False, _CONTROL_UNAVAILABLE
    try:
        m = _ARM_DEFAULT_MIN if minutes is None or str(minutes).strip() == "" \
            else float(minutes)
    except (TypeError, ValueError):
        m = _ARM_DEFAULT_MIN
    m = max(1.0, min(m, _ARM_MAX_MIN))
    _armed_until = time.monotonic() + m * 60
    _emit_state()                               # light up the HUD STOP banner
    return True, (f"Computer control armed for {m:.0f} minutes, sir. Slam the mouse "
                  f"into the top-left corner to abort me at any time.")


def disarm() -> "tuple[bool, str]":
    global _armed_until
    was_armed = is_armed()
    _armed_until = 0.0
    if was_armed:
        _emit_state()                           # clear the HUD STOP banner
    return True, "Computer control disarmed, sir."


def is_armed() -> bool:
    return time.monotonic() < _armed_until


def _guard() -> "str | None":
    """The common pre-flight: deps present, consent window open, and the target
    is NOT JARVIS itself (so it can never click its own Approve dialog)."""
    if not control_available():
        return _CONTROL_UNAVAILABLE
    if not is_armed():
        return ("Computer control isn't armed, sir — ask me again and approve the "
                "prompt, or it may have auto-expired.")
    fg = foreground_app()
    if fg.get("on_jarvis"):
        return ("The JARVIS window is in focus — bring the app you want me to "
                "control to the front first, sir.")
    return None


def _walk_controls(readouts: "list | None" = None):
    """Yield (control, friendly_type) for the interesting, visible controls of
    the FOREGROUND window. Must be called inside a UIA thread initializer.

    ``readouts``, when given, collects the names of visible Text controls from the
    same walk (a second walk would double the 4s budget)."""
    import ctypes
    import uiautomation as auto
    hwnd = ctypes.windll.user32.GetForegroundWindow()
    if not hwnd:
        return
    root = auto.ControlFromHandle(hwnd)
    deadline = time.monotonic() + _WALK_TIME_BUDGET
    scanned = 0
    for c, _depth in auto.WalkControl(root, includeTop=False, maxDepth=_WALK_MAX_DEPTH):
        scanned += 1
        if scanned > _WALK_MAX_NODES or time.monotonic() > deadline:
            return
        try:
            kind = _INTERESTING_TYPES.get(c.ControlTypeName)
            if not kind:
                if (readouts is not None and len(readouts) < _READOUT_MAX
                        and c.ControlTypeName == "TextControl" and not c.IsOffscreen):
                    text = " ".join((c.Name or "").split())[:_READOUT_MAX_CHARS]
                    if any(ch.isalnum() for ch in text) and text not in readouts:
                        readouts.append(text)
                continue
            if c.IsOffscreen or not c.IsEnabled:
                continue
            name = (c.Name or "").strip()
        except Exception:  # noqa: BLE001 — stale/odd nodes mid-walk: skip them
            continue
        if not name and kind != "text field":
            continue            # unnamed controls are unclickable-by-name; skip
        yield c, kind, (name or f"(unnamed {kind})")


def _selector_for(c) -> str:
    """A stable identity for one UIA control, in preference order:

      1. its **RuntimeId** — UIA's own per-session element identity, unique by
         construction, so it can never resolve to a different control;
      2. its **AutomationId** + control type — set by the app itself, so it
         survives re-layout and (unlike Name) localisation;
      3. ``""`` — the caller falls back to positional (name, type, ordinal)
         matching, which is all this module used to have.

    Best-effort: a control that won't answer just gets no selector, and nothing
    raises mid-walk."""
    try:
        rid = c.GetRuntimeId()
        if rid:
            return "rid:" + ".".join(str(int(i)) for i in rid)
    except Exception:  # noqa: BLE001
        pass
    try:
        aid = (c.AutomationId or "").strip()
        if aid:
            return f"aid:{aid}|{c.ControlTypeName}"
    except Exception:  # noqa: BLE001
        pass
    return ""


def _control_point(c) -> "tuple[int, int] | None":
    """The control's CENTRE in the SAME normalized 0–1000 virtual-desktop frame
    :func:`click_xy` accepts, or None when it has no on-screen rectangle.

    Published in the listing so the operator can aim a coordinate click at a
    control precisely — reading a real position off the accessibility tree —
    instead of estimating one from a downscaled screenshot. That's the fallback
    path for controls whose Invoke pattern does nothing."""
    try:
        r = c.BoundingRectangle
        if r is None or r.isempty():
            return None
        cx, cy = r.xcenter(), r.ycenter()
    except Exception:  # noqa: BLE001
        return None
    left, top, width, height = _virtual_screen_rect()
    if not width or not height:
        return None
    nx = int(round((cx - left) / width * 1000))
    ny = int(round((cy - top) / height * 1000))
    if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
        return None            # off the virtual desktop (a stale/hidden rect)
    return nx, ny


def _doc_text(c, limit: int) -> "str | None":
    """A document/edit control's text (TextPattern, else ValuePattern), or None
    when it exposes neither."""
    try:
        return str(c.GetTextPattern().DocumentRange.GetText(limit) or "")
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(c.GetValuePattern().Value or "")[:limit]
    except Exception:  # noqa: BLE001
        return None


def _control_extra(c, kind: str) -> str:
    """Best-effort value/state for a control — lets the operator model see what
    a field currently holds and whether a toggle is on, so it can verify its own
    work. Empty string when the pattern isn't supported."""
    try:
        if kind == "text field":
            v = c.GetValuePattern().Value
            if v:
                v = str(v).replace("\n", " ").strip()
                return f'value="{v[:40]}"' if v else ""
        elif kind == "text area":
            # An editor's own content. Without it the operator (and the done-check)
            # only saw the window title, which Notepad sets to the FIRST LINE — a
            # finished three-line list read as "only 'milk' was typed".
            t = _doc_text(c, 240)
            if t is None:
                return ""
            t = " ⏎ ".join(ln.strip() for ln in t.splitlines() if ln.strip())
            return f'text="{t[:150]}"' if t else "empty"
        elif kind in ("checkbox", "radio"):
            state = c.GetTogglePattern().ToggleState
            return "on" if state == 1 else "off"
    except Exception:  # noqa: BLE001
        pass
    return ""


_SCROLL_PART_NAMES = {"line up", "line down", "page up", "page down", "line left",
                      "line right", "page left", "page right", "column left",
                      "column right"}
# Gathered before trimming to _LIST_MAX_ITEMS, so a long file list can't crowd a
# dialog's own buttons out of the listing.
_COLLECT_MAX = 80
_MAX_ROWS_SHOWN = 12


def _is_row_cell(c) -> bool:
    """A text field that is a list row's own cell (Explorer's Name / Date
    modified columns) — the row itself is listed already."""
    try:
        p = c.GetParentControl()
        return bool(p) and p.ControlTypeName in ("ListItemControl", "DataItemControl")
    except Exception:  # noqa: BLE001
        return False


def _prioritize(ctrls: list) -> "tuple[list, int]":
    """Trim (control, name, type, kind, point, ordinal) rows to _LIST_MAX_ITEMS:
    every non-row control first, at most _MAX_ROWS_SHOWN list rows and tree rows
    each, tree order kept. Returns (kept, how many were dropped)."""
    if len(ctrls) <= _LIST_MAX_ITEMS:
        return ctrls, 0
    rows = {"list item": 0, "tree item": 0}
    kept = []
    for row in ctrls:
        kind = row[3]
        if kind in rows:
            rows[kind] += 1
            if rows[kind] > _MAX_ROWS_SHOWN:
                continue
        kept.append(row)
    kept = kept[:_LIST_MAX_ITEMS]
    return kept, len(ctrls) - len(kept)


def list_ui() -> "tuple[bool, str, str]":
    """Numbered, named controls of the window the user is looking at — the
    desktop equivalent of the browser's element list. Returns (ok, short
    human message, full listing) — the LISTING is internal observation data
    for the model; the message is what a card may show (never the numbers)."""
    global _ui_cache, _ui_cache_hwnd
    err = _guard()
    if err:
        return False, err, ""
    fg = foreground_app()
    hwnd = _foreground_hwnd()
    truncated = False
    focus_line = ""
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread(debug=False):
            seen_ctrls = []
            readouts: "list[str]" = []
            ordinals: "dict[tuple[str, str], int]" = {}
            listed: "set[tuple]" = set()
            for c, kind, name in _walk_controls(readouts):
                ctype = c.ControlTypeName
                # Ordinals count EVERY walked control, as _find_control's
                # positional fallback does, so skipping one below can't repoint
                # a later number.
                ordinal = ordinals.get((name, ctype), 0)
                ordinals[(name, ctype)] = ordinal + 1
                # Scrollbar parts and a list row's own cells are noise: they took
                # a third of a Save dialog's listing (pushing its Save button past
                # the cap) and drew a type into a row's read-only "Name" cell.
                if name.lower() in _SCROLL_PART_NAMES or (
                        kind == "text field" and _is_row_cell(c)):
                    continue
                point = _control_point(c)
                # The same control exposed twice (Explorer lists its Address Bar
                # and Search box twice at one spot): one entry is enough.
                if point is not None and (name, kind, point) in listed:
                    continue
                listed.add((name, kind, point))
                seen_ctrls.append((c, name, ctype, kind, point, ordinal))
                if len(seen_ctrls) >= _COLLECT_MAX:
                    truncated = True
                    break
            kept, dropped = _prioritize(seen_ctrls)
            truncated = truncated or dropped > 0
            found = [(name, ctype, kind, _control_extra(c, kind), _selector_for(c), point,
                      ordinal) for c, name, ctype, kind, point, ordinal in kept]
            # Where would typing land right now? Shown so the operator model
            # knows whether it must target a field first.
            try:
                fc = auto.GetFocusedControl()
                if fc:
                    # Any focused element, not only listable ones: in Excel the
                    # focus is a cell ("A2"), and without it the operator lost
                    # track of where its next value would land.
                    fkind = (_INTERESTING_TYPES.get(fc.ControlTypeName)
                             or fc.ControlTypeName.replace("Control", "").lower())
                    fx = _control_extra(fc, fkind)
                    focus_line = (f"Focused control: {(fc.Name or '').strip() or '(unnamed)'} "
                                  f"[{fkind}]" + (f" ({fx})" if fx else ""))
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        _ui_cache, _ui_cache_hwnd = [], 0     # never leave a stale cache behind
        return False, f"I couldn't read that window's controls: {exc}", ""
    _ui_cache = [(selector, name, ctype, ordinal)
                 for name, ctype, _k, _x, selector, _p, ordinal in found]
    _ui_cache_hwnd = hwnd
    if not found:
        return True, (f"I can't see any named controls in “{fg.get('title') or 'this window'}” "
                      f"— it may not expose an accessibility tree, sir."), ""
    lines = [f"Window: {fg.get('title') or '(untitled)'} ({fg.get('app') or 'unknown app'})"]
    if focus_line:
        lines.append(focus_line)
    if readouts:
        # Unnumbered on purpose: text to READ, not something to click.
        lines.append("Visible text: " + " | ".join(readouts))
    for i, (name, _t, kind, extra, _sel, point, _o) in enumerate(found):
        suffix = f" ({extra})" if extra else ""
        # The control's centre in click_xy's own 0–1000 frame — so when a named
        # click can't drive it, the operator has a REAL position to aim at
        # instead of estimating one off the screenshot.
        at = f" @{point[0]},{point[1]}" if point else ""
        lines.append(f"{i + 1}. {name} [{kind}]{suffix}{at}")
    if truncated:
        lines.append("(more controls and rows exist than I list — 'read' gives every "
                     "row's text)")
    listing = "\n".join(lines)
    return True, f"Scanned “{fg.get('title')}” — {len(found)} controls.", listing


_EDITABLE_KINDS = ("text field", "text area", "dropdown")


# What a window READ collects besides documents/fields: the visible wording of
# labels, list rows (Explorer's files, an inbox), tree items and links.
_READ_NAMED_TYPES = {"TextControl", "ListItemControl", "DataItemControl",
                     "TreeItemControl", "HyperlinkControl", "HeaderItemControl"}
_READ_MAX_CHARS = 6000


def read_window(max_chars: int = _READ_MAX_CHARS) -> "tuple[bool, str]":
    """The readable text of the window in front — a document's or field's full
    text, then the visible labels, list rows and links. The desktop twin of the
    browser's page read, for goals that ASK something ("what does this email
    say", "read me that error", "which files are in here"). Touches nothing, but
    is gated like the actuations: it reads whatever the user has open."""
    err = _guard()
    if err:
        return False, err
    try:
        import ctypes
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread(debug=False):
            root = auto.ControlFromHandle(ctypes.windll.user32.GetForegroundWindow())
            parts: "list[str]" = []
            seen: "set[str]" = set()
            total = 0
            deadline = time.monotonic() + _WALK_TIME_BUDGET
            walk = auto.WalkControl(root, includeTop=False, maxDepth=_WALK_MAX_DEPTH)
            for n, (c, _depth) in enumerate(walk):
                if n > 3000 or total >= max_chars or time.monotonic() > deadline:
                    break
                try:
                    t = c.ControlTypeName
                    if t in ("DocumentControl", "EditControl"):
                        text = _doc_text(c, max_chars - total) or ""
                    elif t in _READ_NAMED_TYPES and not c.IsOffscreen:
                        text = c.Name or ""
                    else:
                        continue
                except Exception:  # noqa: BLE001 — stale nodes mid-walk
                    continue
                text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
                if text and text not in seen:
                    seen.add(text)
                    parts.append(text)
                    total += len(text) + 1
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't read that window: {exc}"
    return True, "\n".join(parts)[:max_chars]


def _find_control(target: str, editable: bool = False):
    """Locate a live control by cached number or by visible name (substring).
    Returns (control, label) or (None, error_message). UIA-thread caller only.

    ``editable`` restricts a NAME lookup to controls that take text: typing aims
    at a field, and a name like "Calculator" otherwise matched the "Minimize
    Calculator" button first.

    A numbered target is resolved against the LIVE tree, preferring the stable
    selector recorded when the list was taken (RuntimeId / AutomationId — see
    :func:`_selector_for`) and only falling back to positional (name, type,
    ordinal) matching for controls that expose neither. That ordering matters:
    positional matching silently repoints as soon as the tree shifts, so "click
    7" could land on whatever moved into 7th place — a wrong click the operator
    loop has no way to notice, because it looks like a success.

    Numbered targets also fail LOUDLY when the foreground window changed since
    the list, or when the recorded control is simply gone."""
    t = str(target if target is not None else "").strip()
    want_sel, want_name, want_type, want_ord = "", "", "", 0
    if t.isdigit():
        idx = int(t) - 1
        if not (0 <= idx < len(_ui_cache)):
            return None, (f"There's no element {t} in the last list, sir — list the "
                          f"controls again and use a fresh number.")
        if _ui_cache_hwnd and _foreground_hwnd() != _ui_cache_hwnd:
            return None, ("The focused window has changed since that list, sir — "
                          "list the controls again and use a fresh number.")
        want_sel, want_name, want_type, want_ord = _ui_cache[idx]
    low = t.lower()
    numbered = bool(want_name or want_sel)
    sel_matches = []
    positional = None
    fallback = None
    seen = 0
    for c, kind, name in _walk_controls():
        if editable and not numbered and kind not in _EDITABLE_KINDS:
            continue
        if numbered:
            if want_sel and _selector_for(c) == want_sel:
                sel_matches.append((c, f"“{name}”"))
                # A RuntimeId is unique by construction, so the first hit is THE
                # control — stop walking rather than pay a COM round-trip per
                # remaining node just to confirm what can't be ambiguous.
                if want_sel.startswith("rid:"):
                    break
                continue
            # Positional match is computed in the same pass so it's ready as a
            # fallback without re-walking the tree.
            if name == want_name and c.ControlTypeName == want_type:
                if seen == want_ord and positional is None:
                    positional = (c, f"“{name}”")
                seen += 1
        elif low and low in name.lower():
            if name.lower() == low:
                return c, f"“{name}”"
            fallback = fallback or (c, f"“{name}”")
    if numbered:
        if len(sel_matches) == 1:
            return sel_matches[0]
        if len(sel_matches) > 1:
            # Only possible for AutomationId-based selectors; a RuntimeId is
            # unique. Ambiguity is not a licence to guess.
            return None, (f"“{want_name}” matches more than one control now, sir — "
                          f"list the controls again and use a fresh number.")
        if positional is not None:
            return positional
        return None, (f"“{want_name or t}” isn't in this window any more, sir — the "
                      f"view has changed. List the controls again.")
    if fallback:
        return fallback
    return None, (f"I couldn't find “{t}” in this window, sir — the view may have "
                  f"changed. List the controls again.")


def click_ui(target, right: bool = False, double: bool = False) -> "tuple[bool, str]":
    """Click a control by its number (from list) or visible name. Prefers the
    accessibility Invoke pattern; falls back to a real (failsafe-guarded) click.
    ``right`` opens its context menu; ``double`` double-clicks (opens a file in
    Explorer, where a single Invoke only selects it) — both are real clicks."""
    err = _guard()
    if err:
        return False, err
    if target is None or str(target).strip() == "":
        return False, "What should I click, sir? List the controls first."
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread(debug=False):
            try:
                ctrl, label = _find_control(target)
            except Exception as exc:  # noqa: BLE001 — find-phase failure ≠ click failure
                return False, (f"I couldn't locate “{target}” in this window: {exc} "
                               f"— list the controls again, sir.")
            if ctrl is None:
                return False, label
            if right:
                ctrl.RightClick(simulateMove=False, waitTime=0.05)
                return True, f"Right-clicked {label} — its menu is open."
            if double:
                ctrl.DoubleClick(simulateMove=False, waitTime=0.05)
                return True, f"Double-clicked {label}."
            try:
                ctrl.GetInvokePattern().Invoke()
                return True, f"Clicked {label}."
            except Exception:  # noqa: BLE001 — not invokable; really click it
                ctrl.Click(simulateMove=False, waitTime=0.05)
                return True, f"Clicked {label}."
    except Exception as exc:  # noqa: BLE001
        return False, f"I found it but the click failed: {exc}"


def _virtual_screen_rect() -> "tuple[int, int, int, int]":
    """(left, top, width, height) of the WHOLE virtual desktop — all monitors —
    in the same pixel space pyautogui clicks in. This is the area the vision
    screenshot covers (mss grabs ``monitors[0]``), so a normalized point over the
    screenshot maps linearly onto this rect. Falls back to the primary screen."""
    try:
        import ctypes
        u = ctypes.windll.user32
        # SM_*VIRTUALSCREEN: 76 X, 77 Y, 78 CX (width), 79 CY (height).
        left, top = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
        width, height = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
        if width and height:
            return int(left), int(top), int(width), int(height)
    except Exception:  # noqa: BLE001
        pass
    try:
        import pyautogui
        w, h = pyautogui.size()
        return 0, 0, int(w), int(h)
    except Exception:  # noqa: BLE001
        return 0, 0, 0, 0


def click_xy(x, y, double: bool = False) -> "tuple[bool, str]":
    """Click at a point given in NORMALIZED 0–1000 screen coordinates — x runs
    0 (left edge) → 1000 (right), y runs 0 (top) → 1000 (bottom) of the WHOLE
    virtual desktop, the same frame the vision screenshot covers.

    This is the fallback for apps that expose no usable accessibility controls
    (Java/Swing like BlueJ, design canvases, games): the vision-first operator
    reads the screenshot and points. Because the input is a FRACTION of the
    screen, it's invariant to DPI scaling and resolution — no pixel math leaks in
    from the (possibly downscaled) screenshot. Best-effort: an LLM's estimated
    coordinates aren't pixel-perfect, so the operator should aim at the target's
    centre and verify the next screenshot."""
    err = _guard()
    if err:
        return False, err
    try:
        nx, ny = float(x), float(y)
    except (TypeError, ValueError):
        return False, ("Give the point as numbers in 0–1000, sir — x left→right, "
                       "y top→bottom.")
    if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
        return False, ("That point is off-screen, sir — coordinates are normalized "
                       "0–1000 (x left→right, y top→bottom).")
    left, top, width, height = _virtual_screen_rect()
    if not width or not height:
        return False, "I couldn't read the screen geometry, sir."
    px = int(round(left + (nx / 1000.0) * width))
    py = int(round(top + (ny / 1000.0) * height))
    # Keep clear of the four corners — those are pyautogui's FAILSAFE points, and
    # a click parked exactly there would trip the slam-to-corner abort. A real UI
    # target is never in the 1px corner, so a 3px inset is invisible.
    px = min(max(px, left + 3), left + width - 3)
    py = min(max(py, top + 3), top + height - 3)
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        if double:
            pyautogui.doubleClick(px, py)
        else:
            pyautogui.click(px, py)
        what = "Double-clicked" if double else "Clicked"
        return True, f"{what} at ({nx:.0f},{ny:.0f}) on screen — verify it landed."
    except Exception as exc:  # noqa: BLE001
        return False, f"The coordinate click failed: {exc}"


# ── Direct-drive input primitives (phone live-view remote control) ───────────
# Same normalized 0–1000 → virtual-desktop-pixel mapping and armed-consent guard
# as click_xy, split out so the remote-desktop handlers can right-click, move the
# cursor, and press/release the button for real drags (down → move… → up).

def _point_to_pixels(x, y) -> "tuple[int | None, int | None, str]":
    """Guarded normalized-0–1000 → on-screen pixel mapping shared by the direct
    input primitives. Returns (px, py, "") or (None, None, error_message)."""
    err = _guard()
    if err:
        return None, None, err
    try:
        nx, ny = float(x), float(y)
    except (TypeError, ValueError):
        return None, None, "Give the point as numbers in 0–1000, sir."
    if not (0 <= nx <= 1000 and 0 <= ny <= 1000):
        return None, None, "That point is off-screen, sir (0–1000)."
    left, top, width, height = _virtual_screen_rect()
    if not width or not height:
        return None, None, "I couldn't read the screen geometry, sir."
    px = int(round(left + (nx / 1000.0) * width))
    py = int(round(top + (ny / 1000.0) * height))
    # Stay off the 1px FAILSAFE corners (see click_xy).
    px = min(max(px, left + 3), left + width - 3)
    py = min(max(py, top + 3), top + height - 3)
    return px, py, ""


def right_click_xy(x, y) -> "tuple[bool, str]":
    px, py, err = _point_to_pixels(x, y)
    if err:
        return False, err
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.rightClick(px, py)
        return True, f"Right-clicked at ({x},{y})."
    except Exception as exc:  # noqa: BLE001
        return False, f"The right-click failed: {exc}"


def drag_xy(x, y, to_x, to_y) -> "tuple[bool, str]":
    """Press at one normalized point, glide to another, release — moving a file
    between folders, a slider, a window edge, a stroke in Paint."""
    px, py, err = _point_to_pixels(x, y)
    if err:
        return False, err
    qx, qy, err = _point_to_pixels(to_x, to_y)
    if err:
        return False, err
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(px, py)
        # dragTo glides there with the button held; the duration lets apps that
        # need intermediate mouse moves (Explorer, sliders) register the drag.
        pyautogui.dragTo(qx, qy, duration=0.4, button="left")
        return True, f"Dragged from ({x},{y}) to ({to_x},{to_y}) — verify it landed."
    except Exception as exc:  # noqa: BLE001
        release_inputs()
        return False, f"The drag failed: {exc}"


def move_xy(x, y) -> "tuple[bool, str]":
    """Move the cursor only (presses nothing) — used to stream a live drag."""
    px, py, err = _point_to_pixels(x, y)
    if err:
        return False, err
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(px, py)
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"The move failed: {exc}"


def mouse_down_xy(x, y) -> "tuple[bool, str]":
    """Press and HOLD the left button at a point (start of a drag)."""
    px, py, err = _point_to_pixels(x, y)
    if err:
        return False, err
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(px, py)
        pyautogui.mouseDown()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"Mouse-down failed: {exc}"


def mouse_up_xy(x, y) -> "tuple[bool, str]":
    """Release the left button at a point (end of a drag). Best-effort releases a
    held button even if the guard now refuses, so a drag can't leave it stuck."""
    px, py, err = _point_to_pixels(x, y)
    if err:
        release_inputs()
        return False, err
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.moveTo(px, py)
        pyautogui.mouseUp()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, f"Mouse-up failed: {exc}"


def release_inputs() -> None:
    """Release any held mouse button AND any held keyboard key — called when a
    control lease drops (disarm / phone disconnect) so nothing is left pressed after
    a drag or a game key-hold. Never raises."""
    try:
        import pyautogui
        pyautogui.mouseUp()
    except Exception:  # noqa: BLE001
        pass
    try:
        release_all_keys()
    except Exception:  # noqa: BLE001
        pass


# ── Typing engine (Win32 SendInput, KEYEVENTF_UNICODE) ───────────────────────
# pyautogui.write maps characters through the US keyboard layout: anything not
# on it (other layouts, “smart” punctuation, emoji, …) is silently DROPPED —
# the root cause of "it is very bad at typing". SendInput with the UNICODE
# flag injects the actual character codes, so every character lands no matter
# the layout, and it's fast enough for whole paragraphs.

_KEYEVENTF_EXTENDEDKEY = 0x0001
_KEYEVENTF_KEYUP = 0x0002
_KEYEVENTF_UNICODE = 0x0004
_KEYEVENTF_SCANCODE = 0x0008
_VK_RETURN, _VK_TAB, _VK_CONTROL, _VK_A, _VK_DELETE = 0x0D, 0x09, 0x11, 0x41, 0x2E
_VK_V = 0x56
_sendinput = None       # cached (events) -> bool closure


def _get_sendinput():
    """Build (once) a `send(events)` closure over the Win32 SendInput API.
    Events are (wVk, wScan, dwFlags) keyboard tuples. None when unavailable."""
    global _sendinput
    if _sendinput is not None:
        return _sendinput
    try:
        import ctypes
        from ctypes import wintypes

        ULONG_PTR = ctypes.c_size_t

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = (("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                        ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                        ("dwExtraInfo", ULONG_PTR))

        class MOUSEINPUT(ctypes.Structure):    # only for correct union sizing
            _fields_ = (("dx", wintypes.LONG), ("dy", wintypes.LONG),
                        ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                        ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR))

        class _U(ctypes.Union):
            _fields_ = (("ki", KEYBDINPUT), ("mi", MOUSEINPUT))

        class INPUT(ctypes.Structure):
            _fields_ = (("type", wintypes.DWORD), ("u", _U))

        user32 = ctypes.windll.user32
        size = ctypes.sizeof(INPUT)

        def send(events: "list[tuple[int, int, int]]") -> bool:
            if not events:
                return True
            arr = (INPUT * len(events))()
            for i, (vk, scan, flags) in enumerate(events):
                arr[i].type = 1     # INPUT_KEYBOARD
                arr[i].u.ki = KEYBDINPUT(vk, scan, flags, 0, 0)
            return int(user32.SendInput(len(events), arr, size)) == len(events)

        _sendinput = send
    except Exception:  # noqa: BLE001
        _sendinput = None
    return _sendinput


def _send_text(text: str) -> bool:
    """Type `text` into the focused element via SendInput. Newlines/tabs become
    real Enter/Tab presses; everything else goes as raw unicode (surrogate
    pairs included). Sent in small batches so the app's queue keeps up."""
    send = _get_sendinput()
    if send is None:
        return False
    batch: "list[tuple[int, int, int]]" = []
    for ch in text.replace("\r\n", "\n"):
        if ch in ("\n", "\r", "\t"):
            vk = _VK_TAB if ch == "\t" else _VK_RETURN
            batch += [(vk, 0, 0), (vk, 0, _KEYEVENTF_KEYUP)]
        else:
            data = ch.encode("utf-16-le")
            for j in range(0, len(data), 2):
                scan = int.from_bytes(data[j:j + 2], "little")
                batch += [(0, scan, _KEYEVENTF_UNICODE),
                          (0, scan, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP)]
        if len(batch) >= 100:
            if not send(batch):
                return False
            batch = []
            time.sleep(0.02)
    return send(batch)


def _send_chord(vks: "list[int]") -> bool:
    """Press virtual keys in order, release in reverse (e.g. Ctrl+A)."""
    send = _get_sendinput()
    if send is None:
        return False
    down = [(vk, 0, 0) for vk in vks]
    up = [(vk, 0, _KEYEVENTF_KEYUP) for vk in reversed(vks)]
    return send(down + up)


# ── Clipboard paste: the text-entry path that survives modern editors ────────
# Every keystroke-level method garbles text in Windows 11 Notepad (measured
# 2026-10-05): unicode SendInput turned "milk⏎eggs⏎bread" into "milk⏎ddddddddd"
# — after a space or Enter each queued character came out as the LAST one — and
# real virtual-key strokes lost shift states and line order at speed. A paste is
# one atomic insert and came through exact (accents, quotes, newlines) in Notepad,
# and Calculator evaluates a pasted "1234*5678=". The user's clipboard is saved
# first and put back after.

_CF_UNICODETEXT = 13
# Formats that are GDI handles, not memory — they can't be copied byte-wise, and
# Windows re-synthesizes the bitmap ones from the CF_DIB we do keep.
_CLIP_HANDLE_FORMATS = {2, 3, 9, 14, 0x80, 0x82, 0x83, 0x8E}
_CLIP_SNAPSHOT_MAX = 32 * 1024 * 1024
_clip_api = None


def _clip():
    """(user32, kernel32) with handle-correct prototypes — private DLL instances,
    so the 64-bit restypes never leak into other ctypes users."""
    global _clip_api
    if _clip_api is None:
        import ctypes
        from ctypes import c_void_p, c_uint, c_size_t, c_int
        u, k = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
        u.OpenClipboard.argtypes, u.OpenClipboard.restype = [c_void_p], c_int
        u.EnumClipboardFormats.argtypes, u.EnumClipboardFormats.restype = [c_uint], c_uint
        u.GetClipboardData.argtypes, u.GetClipboardData.restype = [c_uint], c_void_p
        u.SetClipboardData.argtypes, u.SetClipboardData.restype = [c_uint, c_void_p], c_void_p
        k.GlobalAlloc.argtypes, k.GlobalAlloc.restype = [c_uint, c_size_t], c_void_p
        k.GlobalLock.argtypes, k.GlobalLock.restype = [c_void_p], c_void_p
        k.GlobalUnlock.argtypes = [c_void_p]
        k.GlobalSize.argtypes, k.GlobalSize.restype = [c_void_p], c_size_t
        k.GlobalFree.argtypes = [c_void_p]
        _clip_api = (u, k)
    return _clip_api


def _open_clipboard() -> bool:
    u, _k = _clip()
    for _ in range(25):                 # another app may be holding it briefly
        if u.OpenClipboard(None):
            return True
        time.sleep(0.02)
    return False


def _clipboard_snapshot() -> "list[tuple[int, bytes]] | None":
    """Every memory-backed format on the clipboard (None if it can't be opened)."""
    import ctypes
    u, k = _clip()
    if not _open_clipboard():
        return None
    out: "list[tuple[int, bytes]]" = []
    total = 0
    try:
        fmt = u.EnumClipboardFormats(0)
        while fmt:
            if fmt not in _CLIP_HANDLE_FORMATS:
                h = u.GetClipboardData(fmt)
                size = k.GlobalSize(h) if h else 0
                if size and total + size <= _CLIP_SNAPSHOT_MAX:
                    p = k.GlobalLock(h)
                    if p:
                        try:
                            out.append((fmt, ctypes.string_at(p, size)))
                            total += size
                        finally:
                            k.GlobalUnlock(h)
            fmt = u.EnumClipboardFormats(fmt)
    finally:
        u.CloseClipboard()
    return out


def _clipboard_put(items: "list[tuple[int, bytes]]") -> bool:
    """Replace the clipboard with ``items`` ((format, bytes) pairs)."""
    import ctypes
    u, k = _clip()
    if not _open_clipboard():
        return False
    try:
        u.EmptyClipboard()
        for fmt, data in items:
            h = k.GlobalAlloc(0x0002, max(1, len(data)))        # GMEM_MOVEABLE
            p = k.GlobalLock(h) if h else None
            if not p:
                continue
            ctypes.memmove(p, data, len(data))
            k.GlobalUnlock(h)
            if not u.SetClipboardData(fmt, h):
                k.GlobalFree(h)           # ownership passes only on success
        return True
    finally:
        u.CloseClipboard()


def _paste_text(text: str, ctrl=None) -> bool:
    """Insert ``text`` at the focus by pasting it, then put the user's clipboard
    back. False (nothing sent) when the clipboard isn't available. The restore
    waits until the text shows in ``ctrl`` (or a beat, when that can't be read):
    the app reads the clipboard when it gets round to the Ctrl+V, and restoring
    first would paste the user's old clipboard instead."""
    snap = _clipboard_snapshot()
    if snap is None or not _clipboard_put([(_CF_UNICODETEXT, (text + "\0").encode("utf-16-le"))]):
        return False
    try:
        before = _doc_text(ctrl, 1_000_000) if ctrl is not None else None
        _send_chord([_VK_CONTROL, _VK_V])
        # "Changed", not "contains the text": a field that already held the same
        # words would otherwise look pasted before the app had read the clipboard.
        end = time.monotonic() + 2.0
        while before is not None and time.monotonic() < end:
            time.sleep(0.05)
            if _doc_text(ctrl, 1_000_000) != before:
                break
        else:
            time.sleep(0.45)
    finally:
        _clipboard_put(snap)
    return True


def _value_matches(ctrl, text: str) -> "bool | None":
    """Did the text land in the control? True/False when the control exposes its
    text (TextPattern for documents, else ValuePattern), None when it can't —
    absence of proof ≠ failure."""
    v = _doc_text(ctrl, 1_000_000)
    if v is None:
        return None
    want = " ".join(text.split()).lower()
    got = " ".join(v.split()).lower()
    if not want:
        return got == ""
    return want[:40] in got


_TEXT_TYPES = {"EditControl", "DocumentControl", "ComboBoxControl"}
# Focus targets that Enter ACTIVATES: typing "name\n" with one of these focused
# opens the file / presses the button instead of entering text. (Live: Explorer's
# focus sat on a Desktop shortcut and "JarvisBench\n" launched it.) Not
# DataItemControl: a spreadsheet's cell is one, and it takes typing.
_ACTIVATABLE_TYPES = {"ListItemControl", "TreeItemControl",
                      "ButtonControl", "SplitButtonControl", "HyperlinkControl",
                      "MenuItemControl", "TabItemControl", "CheckBoxControl",
                      "RadioButtonControl"}


def _await_value(ctrl, text: str, timeout: float = 1.5) -> "bool | None":
    """_value_matches, polled until it holds. SendInput only QUEUES keystrokes;
    the app applies them on its own UI thread, so one check 0.1s later read
    Notepad mid-way through a three-line list and reported a successful type as
    "didn't land" — the operator then retyped, select-all'd and looped (3 of 7
    tasks in the 2026-10-05 benchmark)."""
    end = time.monotonic() + timeout
    while True:
        got = _value_matches(ctrl, text)
        if got is not False or time.monotonic() >= end:
            return got
        time.sleep(0.1)


def type_text(text: str, target=None, clear: bool = False) -> "tuple[bool, str]":
    """Type text on the desktop. With `target` (a control number from the last
    list, or a visible name) the control is focused first and the landing is
    VERIFIED through its accessibility value; without one, text goes to
    whatever already has focus. `clear=True` replaces the field's content
    (via the ValuePattern when possible, else select-all + type)."""
    err = _guard()
    if err:
        return False, err
    raw = "" if text is None else str(text)
    # Control characters are keys, not text: "\u0001" was a model's stab at
    # ctrl+a, and pasted it lands as junk in the user's document.
    text = "".join(ch for ch in raw if ch in "\n\r\t" or ord(ch) >= 32)
    tgt = str(target).strip() if target not in (None, "") else ""
    if raw and not text and not clear:
        return False, "That was only control characters — use press for keys like ctrl+a."
    if not text and not clear:
        return False, "What should I type, sir?"
    try:
        import uiautomation as auto
        with auto.UIAutomationInitializerInThread(debug=False):
            ctrl, label = None, "the focused element"
            if tgt:
                try:
                    found, lbl = _find_control(tgt, editable=True)
                except Exception as exc:  # noqa: BLE001
                    return False, f"I couldn't locate “{tgt}” in this window: {exc}"
                if found is None and tgt.isdigit():
                    return False, lbl          # a stale number: fail loudly
                if found is not None and found.ControlTypeName in _TEXT_TYPES:
                    ctrl, label = found, lbl
                else:
                    # Not a text field. Either the name matched no field (it named
                    # the window: "type 12*34= into Calculator") or the number is a
                    # button/menu item, which focusing and then typing would PRESS
                    # on any newline. The keys go where the focus already is.
                    tgt = ""
            if tgt:
                try:
                    ctrl.SetFocus()
                    time.sleep(0.15)
                except Exception:  # noqa: BLE001 — focus is best-effort; typing
                    pass           # may still land if the control already had it
            else:
                try:
                    ctrl = auto.GetFocusedControl()
                except Exception:  # noqa: BLE001
                    ctrl = None

            # Trailing newlines are a SUBMIT ("type the message and send it"):
            # they're pressed as real Enter keys after the text, because a pasted
            # newline is just a line break in a chat box.
            body = text.replace("\r\n", "\n")
            enters = len(body) - len(body.rstrip("\n"))
            body = body.rstrip("\n")
            try:
                ftype = ctrl.ControlTypeName if ctrl is not None else ""
            except Exception:  # noqa: BLE001
                ftype = ""
            if not tgt and ftype in _ACTIVATABLE_TYPES:
                # Text there goes nowhere, and an Enter opens or presses the thing.
                fname = (getattr(ctrl, "Name", "") or "").strip()[:60]
                return False, (f"Nothing that takes text has the focus — it's on “{fname}”, "
                               f"which Enter would open or press. Click (or target) the "
                               f"text field first, then type.")
            done = False
            if clear and ctrl is not None:
                # Fastest, layout-proof path: set the whole value in one go.
                try:
                    ctrl.GetValuePattern().SetValue(body)
                    done = bool(_await_value(ctrl, body, timeout=0.5))
                except Exception:  # noqa: BLE001
                    pass
                if not done:
                    # No (working) ValuePattern → select-all, then type over it.
                    _send_chord([_VK_CONTROL, _VK_A])
                    time.sleep(0.05)
                    if not body:
                        _send_chord([_VK_DELETE])

            if body and not done and not _paste_text(body, ctrl) and not _send_text(body):
                try:                       # ancient fallback — ASCII only
                    import pyautogui
                    pyautogui.FAILSAFE = True
                    pyautogui.write(body, interval=0.02)
                except Exception as exc:  # noqa: BLE001
                    return False, f"Typing failed: {exc}"
            verified = _await_value(ctrl, body) if ctrl is not None else None
            for _ in range(enters):
                _send_chord([_VK_RETURN])
                time.sleep(0.05)
            if verified is False and tgt:
                return False, (f"I typed into {label} but the text didn't land, "
                               f"sir — the field may be read-only or filtered.")
            into = f" into {label}" if tgt else ""
            # Say the Enter out loud: a cut-off preview hid it, and the model pressed
            # Enter again — into the editor, after the Save dialog had closed.
            pressed = f"pressed Enter{f' ×{enters}' if enters > 1 else ''}" if enters else ""
            if not body:
                return True, f"P{pressed[1:]}{into}."
            shown = body.replace("\n", "⏎")
            shown = shown if len(shown) <= 60 else shown[:57] + "…"
            return True, f"Typed “{shown}”{into}{' and ' + pressed if pressed else ''}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Typing failed: {exc}"


_KEY_ALIASES = {
    "control": "ctrl", "escape": "esc", "return": "enter", "spacebar": "space",
    "windows": "win", "cmd": "win", "meta": "win", "page up": "pageup",
    "page down": "pagedown", "arrow up": "up", "arrow down": "down",
    "arrow left": "left", "arrow right": "right", "del": "delete",
}


def press_keys(keys: str) -> "tuple[bool, str]":
    """Press a key or combo, e.g. 'enter', 'ctrl+s', 'alt+tab'."""
    err = _guard()
    if err:
        return False, err
    k = (keys or "").strip().lower()
    if not k:
        return False, "Which key should I press, sir?"
    parts = [_KEY_ALIASES.get(p.strip(), p.strip())
             for p in k.replace(" + ", "+").split("+") if p.strip()]
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        if len(parts) > 1:
            pyautogui.hotkey(*parts)
        else:
            pyautogui.press(parts[0])
        return True, f"Pressed {'+'.join(parts)}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Key press failed: {exc}"


# ── Held-key input for games (scancode SendInput) ────────────────────────────
# press_keys above TAPS a key via pyautogui (virtual-key SendInput). Many games read
# the keyboard through DirectInput/Raw Input and IGNORE virtual-key events — and even
# when they don't, a tap is wrong when you need to HOLD a key (walk forward, aim,
# sprint). key_down/key_up inject real hardware SCAN CODES and stay down until
# released, so WASD movement and held modifiers work in games. Scan codes are looked
# up from the OS (MapVirtualKey) so there's no hand-maintained table to drift.

# Named keys → virtual-key codes (letters/digits resolve via VkKeyScan below).
_VK_NAMED = {
    "space": 0x20, "enter": 0x0D, "esc": 0x1B, "tab": 0x09, "backspace": 0x08,
    "delete": 0x2E, "capslock": 0x14,
    "shift": 0xA0, "lshift": 0xA0, "rshift": 0xA1,
    "ctrl": 0xA2, "lctrl": 0xA2, "rctrl": 0xA3,
    "alt": 0xA4, "lalt": 0xA4, "ralt": 0xA5,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
    "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
}
# VKs whose scancode must carry the EXTENDED flag (arrows, nav cluster, right-side
# modifiers) — without it the game sees the wrong physical key (numpad vs arrow).
_EXTENDED_VKS = {0x25, 0x26, 0x27, 0x28, 0x21, 0x22, 0x23, 0x24, 0x2D, 0x2E,
                 0xA3, 0xA5, 0x90, 0x6F}

# Scancodes currently held down (with their extended flag) so a dropped lease can
# release every one — a stuck key in a game is worse than a missed press.
_held_keys: "set[tuple[int, bool]]" = set()


def _key_to_scan(name: str) -> "tuple[int, bool] | None":
    """Resolve a key name to (scancode, is_extended) via the OS keyboard layout.
    Accepts named keys ('w', 'space', 'shift', 'up', 'f1'); None if unknown."""
    n = _KEY_ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())
    if not n:
        return None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        vk = _VK_NAMED.get(n)
        if vk is None:
            if len(n) != 1:
                return None
            res = user32.VkKeyScanW(ord(n))     # low byte = VK for this char
            if res == -1:
                return None
            vk = res & 0xFF
        scan = int(user32.MapVirtualKeyW(vk, 0))   # MAPVK_VK_TO_VSC
        if not scan:
            return None
        return scan, vk in _EXTENDED_VKS
    except Exception:  # noqa: BLE001
        return None


def key_down(key) -> "tuple[bool, str]":
    """Press and HOLD a key (game movement/modifiers). Injects a hardware scancode
    so DirectInput games register it; stays down until key_up / release_all_keys."""
    err = _guard()
    if err:
        return False, err
    send = _get_sendinput()
    if send is None:
        return False, _CONTROL_UNAVAILABLE
    resolved = _key_to_scan(str(key or ""))
    if resolved is None:
        return False, f"I don't recognise the key {key!r}, sir."
    scan, ext = resolved
    flags = _KEYEVENTF_SCANCODE | (_KEYEVENTF_EXTENDEDKEY if ext else 0)
    if not send([(0, scan, flags)]):
        return False, "That key press didn't register, sir."
    _held_keys.add((scan, ext))
    return True, ""


def key_up(key) -> "tuple[bool, str]":
    """Release a held key. Best-effort even if the guard now refuses (mirrors
    mouse_up), so a game key is never left stuck down."""
    send = _get_sendinput()
    if send is None:
        return False, _CONTROL_UNAVAILABLE
    resolved = _key_to_scan(str(key or ""))
    if resolved is None:
        return False, f"I don't recognise the key {key!r}, sir."
    scan, ext = resolved
    flags = _KEYEVENTF_SCANCODE | _KEYEVENTF_KEYUP | (_KEYEVENTF_EXTENDEDKEY if ext else 0)
    ok = send([(0, scan, flags)])
    _held_keys.discard((scan, ext))
    return (True, "") if ok else (False, "That key release didn't register, sir.")


def release_all_keys() -> None:
    """Release every key key_down left held (lease drop / disarm). Never raises."""
    send = _get_sendinput()
    if send is None:
        _held_keys.clear()
        return
    for scan, ext in list(_held_keys):
        flags = _KEYEVENTF_SCANCODE | _KEYEVENTF_KEYUP | (_KEYEVENTF_EXTENDEDKEY if ext else 0)
        try:
            send([(0, scan, flags)])
        except Exception:  # noqa: BLE001
            pass
    _held_keys.clear()


def scroll_amount(amount) -> "tuple[bool, str]":
    """Scroll the focused window. Positive = down (matching the browser action);
    accepts wheel notches or browser-style pixel values."""
    err = _guard()
    if err:
        return False, err
    try:
        n = int(amount)
    except (TypeError, ValueError):
        n = 600
    if abs(n) > 20:             # pixel-style value → wheel notches
        n = n // 100 or (1 if n > 0 else -1)
    n = max(-20, min(20, n))
    try:
        import pyautogui
        pyautogui.FAILSAFE = True
        pyautogui.scroll(-n * 120)   # pyautogui: positive = up; 120 = one notch
        return True, f"Scrolled {'down' if n >= 0 else 'up'}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Scrolling failed: {exc}"
