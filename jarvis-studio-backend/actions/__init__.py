"""Action parsing + execution.

The LLM emits machine-actionable intents wrapped in [ACTION]{json}[/ACTION]
tags, e.g.

    Sure, opening Chrome for you. [ACTION]{"type":"open_app","target":"chrome"}[/ACTION]

`execute_actions(text)` finds every such tag, runs the action on Windows, and
returns:
    clean_text  – the response with all [ACTION] blocks stripped (for the UI text
                  and TTS)
    results     – list of {type, target, ok, message} dicts describing what
                  happened, so the UI can render little action cards.
"""

from __future__ import annotations

import json
import re
from typing import List, Tuple

from . import app_launcher, skills, recorder, fs_access, computer, browser

_ACTION_RE = re.compile(r"\[ACTION\b[^\]]*\]\s*(.*?)\s*\[/ACTION\]", re.DOTALL | re.IGNORECASE)
_BARE_TRAILING_ACTION_RE = re.compile(r"\[ACTION\b[^\]]*\]\s*([\s\S]*)$", re.IGNORECASE)


def _scan_specs(inner: str, stop_after_first_error: bool = False) -> "tuple[List[dict], int]":
    """Pull every top-level JSON value (a dict, or the dicts inside a list) out of an
    [ACTION] block body, returning ``(specs, chars_consumed)``.

    Models don't always cooperate with "one block per task": a compound request
    ("open chrome AND play music") can come back as two crammed objects ({...}{...}),
    a comma-separated pair ({...}, {...}), or a JSON array ([{...},{...}]). Scanning
    for every value (instead of one regex match) means the second task ("…and Y")
    never silently vanishes.

    ``stop_after_first_error``: once at least one spec has been parsed, stop at the
    first non-JSON run instead of skipping ahead. Used for the bare-trailing-[ACTION]
    case (a reply ending in ``[ACTION]{...}`` with no ``[/ACTION]``), where prose
    follows the JSON and ``chars_consumed`` must mark exactly where it ends.
    """
    decoder = json.JSONDecoder()
    specs: "List[dict]" = []
    idx, n = 0, len(inner)
    consumed = 0
    while idx < n:
        while idx < n and inner[idx] in " \t\r\n,;":   # skip separators
            idx += 1
        if idx >= n:
            break
        try:
            val, end = decoder.raw_decode(inner, idx)
        except json.JSONDecodeError:
            # Junk between specs (prose, a stray "and", or one malformed object).
            # For the trailing-prefix case stop once we already have specs; otherwise
            # skip to the next JSON value start (idx+1 guarantees forward progress)
            # rather than dropping every remaining spec.
            if stop_after_first_error and specs:
                break
            nxt = min((p for p in (inner.find("{", idx + 1),
                                   inner.find("[", idx + 1)) if p != -1), default=-1)
            if nxt == -1:
                break
            idx = nxt
            continue
        if isinstance(val, list):
            specs.extend(v for v in val if isinstance(v, dict))
        elif isinstance(val, dict):
            specs.append(val)
        if end <= idx:        # no progress — bail to avoid an infinite loop
            break
        idx = end
        consumed = end
    return specs, consumed


def _parse_specs(inner: str) -> "List[dict]":
    """Parse one OR MORE action specs out of a single [ACTION] block body."""
    return _scan_specs(inner.strip())[0]


def _parse_specs_prefix(inner: str) -> "tuple[List[dict], int]":
    """Parse leading action JSON and report how many chars it consumed, for the
    common model slip where the reply ends with a bare ``[ACTION]{...}`` and never
    emits ``[/ACTION]``. We consume the JSON and leave any following prose alone."""
    return _scan_specs(inner, stop_after_first_error=True)

# Actions that must NEVER run without the user's explicit approval (a GUI
# Approve/Deny dialog gates them — see main.request_permission).
_DESTRUCTIVE_POWER = {
    "shutdown", "shut_down", "turn_off", "power_off", "restart", "reboot",
    "hibernate", "logoff", "log_off", "sign_out", "logout",
}


def needs_permission(spec: dict) -> "Tuple[bool, str, str]":
    """Return (gated, kind, human description) for actions that require approval.

    Gated: destructive power (shutdown/restart/hibernate/logoff) and any file
    deletion. Reversible/benign things (lock, sleep, cancel) are NOT gated.
    """
    atype = str(spec.get("type", "")).lower().strip()
    target = spec.get("target") or spec.get("app") or spec.get("path") \
        or spec.get("query") or spec.get("command") or spec.get("name") or ""
    if atype in ("close_app", "close", "kill", "quit_app"):
        return True, "close_app", f"close {target or 'that app'}"
    if atype in ("open_app", "open", "launch"):
        if app_launcher.open_app_needs_permission(str(target)):
            return True, "open_app", f"open {target}"
    if atype in ("open_folder", "folder", "directory"):
        if app_launcher.open_folder_needs_permission(str(target)):
            return True, "open_folder", f"open the folder {target}"
    if atype in ("app", "app_control", "control_app", "app_command", "app_cmd"):
        if app_launcher.open_app_needs_permission(str(target)):
            return True, "open_app", f"open {target}"
    if atype in ("power", "shutdown", "restart"):
        cmd = str(spec.get("command") or spec.get("target") or atype).lower().strip().replace(" ", "_")
        if cmd in _DESTRUCTIVE_POWER or atype in ("shutdown", "restart"):
            return True, "power", f"{cmd.replace('_', ' ')} the computer"
    if atype in ("delete_file", "delete", "remove_file", "trash"):
        return True, "delete", f"delete “{target or 'that file'}”"
    # Reading a file is read-only, but the user wants to approve every read.
    if atype in ("read_file", "cat", "view_file", "read_pdf", "pdf_read", "open_pdf"):
        target = spec.get("path") or spec.get("target") or "that file"
        return True, "read", f"read “{target}”"
    if atype in ("clipboard", "read_clipboard", "get_clipboard", "paste"):
        return True, "clipboard", "read the clipboard"
    if atype in ("screenshot", "screen_capture", "see_screen", "look_at_screen",
                 "analyze_screen", "screen_vision", "what_is_on_screen"):
        return True, "screen", "capture/read the screen"
    if atype in ("record", "recording", "record_audio", "record_video", "record_screen"):
        do = str(spec.get("do") or spec.get("action") or "start").lower().strip()
        if do not in ("stop", "end", "finish"):
            media = spec.get("media") or spec.get("target") or atype.replace("record_", "")
            return True, "recording", f"start {media} recording"
    # Browser control needs the user's consent ONCE — to open the browser. After
    # that, JARVIS uses it freely (click/type/scroll/navigate) with no further
    # prompts, exactly as the user expects: "if I allow it to use the browser, it
    # should be able to use it freely." The ask must come BEFORE the browser
    # launches, so even read-only actions (list/read) are gated when no window is
    # open yet — a `list` would otherwise silently launch Chromium behind the
    # user's back and the prompt would appear only afterwards.
    if atype in ("browser", "web", "webbrowser"):
        do = str(spec.get("do") or spec.get("command") or spec.get("action")
                 or "").lower().strip().replace(" ", "_")
        # Closing is always safe; once consented, everything is free.
        if do in _BROWSER_CLOSE or browser.is_approved():
            return False, "", ""
        # Read-only inspection of an already-open browser stays free.
        if do in _BROWSER_READONLY and browser.is_open():
            return False, "", ""
        # Not consented and a launch is implied → ask once. The description
        # discloses the vision glance because the same standing approval also
        # covers autopilot tasks started within the window.
        return True, "browser", ("open and control the browser (during a task I "
                                 "may glance at a screenshot of the page with my "
                                 "vision model)")
    # Whole-task autopilot goals share the same one-Approve consent model as
    # their step-by-step counterparts: approving "control the browser" covers
    # the entire silent task (and the consent window outlives it).
    if atype in _AUTOPILOT_BROWSER_TYPES:
        if browser.is_approved():
            return False, "", ""
        return True, "browser", ("open and control the browser"
                                 + _goal_playbook_suffix(spec)
                                 + " (during the task I may glance at a screenshot "
                                   "of the page with my vision model)")
    if atype in _AUTOPILOT_COMPUTER_TYPES:
        if computer.is_armed():
            return False, "", ""
        return True, "computer", ("take control of the mouse and keyboard for a "
                                  "few minutes (auto-expires)"
                                  + _goal_playbook_suffix(spec))
    # Computer control (mouse/keyboard on desktop apps): same one-Approve model —
    # the first action asks once and ARMS a bounded control window; while armed,
    # everything runs prompt-free. Disarming/state are always free.
    if atype in _COMPUTER_TYPES:
        do = str(spec.get("do") or spec.get("command") or spec.get("action")
                 or "").lower().strip().replace(" ", "_")
        if do in ("disarm", "off", "stop", "disable", "state", "status") \
                or computer.is_armed():
            return False, "", ""
        return True, "computer", ("take control of the mouse and keyboard for a "
                                  "few minutes (auto-expires)")
    return False, "", ""


def _goal_playbook_suffix(spec: dict) -> str:
    """' — following your “X” playbook' when an autopilot goal matches one, so
    the Approve dialog says WHICH recipe will steer the task (or that a learned
    trace from a previous success will). Best-effort: any problem returns ''."""
    goal = str(spec.get("goal") or spec.get("task") or spec.get("target")
               or spec.get("text") or spec.get("query") or "").strip()
    if not goal:
        return ""
    try:
        import playbooks
        rel = playbooks.find_relevant(goal)
        if rel:
            return f" — following your “{rel[0].get('name')}” playbook"
        if playbooks.find_auto(goal):
            return " — reusing the steps that worked for this last time"
    except Exception:  # noqa: BLE001
        pass
    return ""


# Action types that drive the supervised desktop control (actions/computer.py).
_COMPUTER_TYPES = ("computer", "desktop", "computer_control", "gui_control",
                   "control", "mouse", "keyboard")

# Whole-task goals handed to the silent autopilot (see autopilot.py). run_action
# can't execute these synchronously (the loop awaits an LLM per step), so it
# returns a deferred {"needs_autopilot": ...} result that main.py picks up —
# the same pattern see_screen/generate_image use.
_AUTOPILOT_BROWSER_TYPES = ("browser_task", "web_task", "browser_goal")
_AUTOPILOT_COMPUTER_TYPES = ("computer_task", "desktop_task", "computer_goal")


# Closing the browser is always safe and never gated.
_BROWSER_CLOSE = {"close", "quit", "close_browser"}

# Browser actions that only read the page (never act). Free once a window is open;
# while none is open they still trigger the one-time "open the browser?" prompt so
# the browser is never launched without the user's say-so.
_BROWSER_READONLY = {
    "list", "list_elements", "list_ui", "elements", "read_ui", "see", "see_page",
    "scan", "read", "read_page", "get_text", "page_text", "content", "current",
    "current_page", "where", "url", "",
}


def _describe_browser(spec: dict, do: str) -> str:
    """Human description of a browser action for the Approve/Deny dialog."""
    if do in ("open", "open_url", "goto", "go_to", "navigate", "visit", "browse",
              "open_browser", "launch", "start", "open_blank"):
        tgt = spec.get("url") or spec.get("target") or spec.get("query") or "a page"
        return f"open the browser to {tgt}"
    if do in ("click", "click_element", "press_element", "tap"):
        return f"click “{spec.get('target') or spec.get('name') or spec.get('text') or 'an element'}” in the browser"
    if do in ("type", "type_text", "write", "fill", "enter_text"):
        t = str(spec.get("text") or spec.get("value") or "")
        return f"type “{t if len(t) <= 40 else t[:37] + '…'}” in the browser"
    if do in ("press", "key", "hotkey", "send_keys", "shortcut"):
        return f"press {spec.get('keys') or spec.get('key') or 'a key'} in the browser"
    if do in ("scroll",):
        return "scroll the browser page"
    if do in ("back", "go_back"):
        return "go back in the browser"
    return "control the browser"


def run_action(spec: dict) -> dict:
    """Execute a single action spec now (used after the user approves a gated one)."""
    return _run_one(spec)


# Named accent themes for the HUD → (primary hex, secondary hex, rgb triplet).
_THEMES = {
    "cyan": ("#00e5ff", "#6fe9ff", [0, 229, 255]),
    "blue": ("#3b82f6", "#93c5fd", [59, 130, 246]),
    "amber": ("#ffb648", "#ffd591", [255, 182, 72]),
    "gold": ("#ffd24a", "#ffe89a", [255, 210, 74]),
    "red": ("#ff4d57", "#ff9197", [255, 77, 87]),
    "crimson": ("#ff2d55", "#ff8095", [255, 45, 85]),
    "green": ("#22e39a", "#8bf0cb", [34, 227, 154]),
    "lime": ("#9ee64d", "#caf29a", [158, 230, 77]),
    "purple": ("#a872ff", "#cdaaff", [168, 114, 255]),
    "magenta": ("#ff5cf0", "#ffa9f6", [255, 92, 240]),
    "pink": ("#ff77c8", "#ffb3e0", [255, 119, 200]),
    "white": ("#e8f4ff", "#ffffff", [232, 244, 255]),
    "orange": ("#ff8a3d", "#ffbb85", [255, 138, 61]),
}

_HEX_RE = re.compile(r"^#?[0-9a-fA-F]{6}$")
_PANEL_ALIASES = {
    "system": "system", "sys": "system", "stats": "system", "cpu": "system",
    "power": "power", "battery": "power",
    "agenda": "agenda", "schedule": "agenda", "calendar": "agenda",
    "weather": "weather", "forecast": "weather",
    "network": "network", "net": "network", "connection": "network",
    "terminal": "terminal", "commands": "terminal", "log": "terminal",
}


def _hex_to_rgb(h: str) -> list:
    h = h.lstrip("#")
    return [int(h[i:i + 2], 16) for i in (0, 2, 4)]


_SCREEN_BACKGROUNDS = ("grid", "solid", "aurora", "minimal", "stars")
_ALL_PANELS = ("system", "power", "agenda", "weather", "network", "terminal")


def _match_theme(val) -> "tuple | None":
    """Resolve a colour mention to (accent, accent2, rgb): an exact theme name, a
    hex code, or a theme word ANYWHERE in the phrase ('dark blue please' → blue)."""
    v = str(val or "").lower().strip()
    if not v:
        return None
    if v in _THEMES:
        return _THEMES[v]
    if _HEX_RE.match(v):
        hx = v if v.startswith("#") else "#" + v
        return hx, hx, _hex_to_rgb(hx)
    for name, theme in _THEMES.items():
        if re.search(rf"\b{name}\b", v):
            return theme
    return None


def _match_panel(*vals) -> str:
    """Resolve a panel mention to its canonical name; tolerant of phrases like
    'the weather panel'. Returns '' when nothing matches."""
    for v in vals:
        v = str(v or "").lower().strip()
        if not v:
            continue
        if v in _PANEL_ALIASES:
            return _PANEL_ALIASES[v]
        for alias, name in _PANEL_ALIASES.items():
            if re.search(rf"\b{alias}\b", v):
                return name
    return ""


def _match_background(val) -> str:
    v = str(val or "").lower().strip()
    return next((b for b in _SCREEN_BACKGROUNDS if b in v), "")


def _match_density(val) -> str:
    v = str(val or "").lower().strip()
    if "compact" in v or "dense" in v or "tight" in v:
        return "compact"
    if "normal" in v or "spacious" in v or "default" in v or "regular" in v:
        return "normal"
    return ""


def _combined_screen_patch(spec: dict, val: str) -> "Tuple[dict | None, str]":
    """Build a patch from whatever recognisable properties the spec carries —
    so one action can restyle several things ('make it red and minimal') and an
    unexpected verb still works as long as the values are recognisable."""
    patch: dict = {}
    said: list = []
    theme = _match_theme(spec.get("theme") or spec.get("accent") or spec.get("color")
                         or spec.get("colour")) or _match_theme(val)
    if theme:
        a, a2, rgb = theme
        patch.update({"accent": a, "accent2": a2, "rgb": rgb})
        said.append("recoloured the display")
    bg = _match_background(spec.get("background") or spec.get("bg")) or _match_background(val)
    if bg:
        patch["background"] = bg
        said.append(f"set the {bg} background")
    den = _match_density(spec.get("density")) or _match_density(val)
    if den:
        patch["density"] = den
        said.append(f"{den} density")
    if isinstance(spec.get("panels"), dict):
        pm = {n: bool(v) for n, v in spec["panels"].items() if n in _ALL_PANELS}
        if pm:
            patch["panels"] = pm
            said.append("updated the panels")
    if not patch:
        return None, ("I'm not sure how to change that, sir — try a colour, a "
                      "background (grid/solid/aurora/minimal), density, or a panel.")
    return patch, ("Done, sir — " + ", ".join(said) + ".")


def _screen_patch(do: str, spec: dict) -> "Tuple[dict | None, str]":
    """Build a partial screen-config patch (deep-merged by the frontend) from a
    customization request. Returns (patch, spoken message); patch None = failure.
    Deliberately forgiving: fuzzy colour/panel/background matching, multi-property
    specs, show/hide ALL, and a real toggle — models phrase these loosely."""
    val = str(spec.get("value") or spec.get("color") or spec.get("target") or "").lower().strip()

    if do in ("set_accent", "set_color", "set_colour", "accent", "color", "colour",
              "set_theme", "theme", "recolor", "recolour"):
        theme = _match_theme(val) or _match_theme(spec.get("theme"))
        if not theme:
            return None, (f"I don't know the colour “{val}”, sir. Try cyan, amber, red, "
                          "green, purple, or a hex code.")
        a, a2, rgb = theme
        return ({"accent": a, "accent2": a2, "rgb": rgb},
                f"Changed the display to {val if val in _THEMES else a}, sir.")

    if do in ("set_background", "background", "bg", "set_bg", "wallpaper"):
        opt = _match_background(val) or _match_background(spec.get("background"))
        if not opt:
            return None, "Background options are grid, solid, aurora, or minimal, sir."
        return {"background": opt}, f"Background set to {opt}, sir."

    if do in ("set_density", "density", "compact", "spacious"):
        opt = "compact" if do == "compact" else ("normal" if do == "spacious" else
                                                 _match_density(val))
        if opt not in ("normal", "compact"):
            return None, "Density can be normal or compact, sir."
        return {"density": opt}, f"Display set to {opt} density, sir."

    if do in ("show_panel", "show", "unhide", "show_all", "hide_panel", "hide",
              "hide_all", "toggle_panel", "toggle"):
        visible = do in ("show_panel", "show", "unhide", "show_all")
        toggle = do.startswith("toggle")
        # "show/hide all the panels" — "all" may arrive in the value OR the panel
        # field. A native tool call sets {action:"show_panel", panel:"all"}, so we
        # MUST check the panel field too (checking only `val` was why "expand all
        # the panels" fell through to "Which panel, sir?").
        _all_words = ("all", "everything", "all panels", "panels", "every", "them all")
        panel_field = str(spec.get("panel") or "").lower().strip()
        if do.endswith("_all") or val in _all_words or panel_field in _all_words:
            if toggle:
                return None, "Tell me which panel to toggle, sir."
            return ({"panels": {n: visible for n in _ALL_PANELS}},
                    f"{'Showing' if visible else 'Hiding'} all the panels, sir.")
        name = _match_panel(val, spec.get("panel"))
        if not name:
            return None, "Which panel, sir? (system, power, agenda, weather, network, terminal)"
        if toggle:
            # set_screen resolves "toggle" against the panel's current state.
            return {"panels": {name: "toggle"}}, f"Toggled the {name} panel, sir."
        return ({"panels": {name: visible}},
                f"{'Showing' if visible else 'Hiding'} the {name} panel, sir.")

    if do in ("move_panel", "move", "reorder", "rearrange", "swap"):
        name = _match_panel(val, spec.get("panel"))
        direction = str(spec.get("direction") or spec.get("to") or "").lower().strip()
        if not name or direction not in ("up", "down", "left", "right"):
            return None, "Tell me which panel to move and a direction (up/down/left/right), sir."
        return ({"move": {"panel": name, "direction": direction}},
                f"Moved the {name} panel {direction}, sir.")

    if do in ("reset", "default", "defaults", "restore"):
        return {"reset": True}, "Reset the display to defaults, sir."

    # "set"/"apply"/"style"/"customize"/unknown verb — apply whatever recognisable
    # properties the spec (or the value itself) carries, possibly several at once.
    return _combined_screen_patch(spec, val)


def _basename(path) -> str:
    import os
    return os.path.basename(str(path).strip().strip('"').rstrip("/\\")) or str(path)


def _safe_int(value, default: int) -> int:
    """Coerce an LLM-supplied value to int, falling back on garbage. The model
    sometimes emits {"days":"three"} / {"count":"a few"}; a bare int() there
    would raise straight out of the dispatcher and sink the whole response."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _run_one(spec: dict) -> dict:
    atype = str(spec.get("type", "")).lower().strip()
    # The single "target" most actions use; some skills read the whole spec.
    target = spec.get("target") or spec.get("app") or spec.get("url") \
        or spec.get("query") or spec.get("command") or spec.get("text") \
        or spec.get("path") or spec.get("level") or spec.get("seconds") \
        or spec.get("name") or ""          # a tool call that misnames itself keeps its args

    # ── Launchers / window control ────────────────────────────────────────────
    if atype in ("open_app", "open", "launch"):
        ok, msg = app_launcher.open_app(target)
    elif atype in ("close_app", "close", "kill", "quit_app"):
        ok, msg = skills.close_app(target)
    elif atype in ("open_url", "url", "website", "link"):
        # Keep everything in ONE browser once a session is live: if JARVIS's own
        # (Playwright) browser is already open or consented, navigate THAT instead
        # of the OS default browser. Otherwise a follow-up "open X" mid-task pops
        # the page in the user's Chrome while the autopilot keeps driving its own
        # window — the "you opened it in MY browser, use your test browser" mix-up.
        # A casual open with no browser session still uses the OS browser.
        if browser.available() and (browser.is_open() or browser.is_approved()):
            ok, msg = browser.open_url(target)
        else:
            ok, msg = app_launcher.open_url(target)
    elif atype in ("web_search", "search_web", "web_answer", "lookup", "look_up", "search"):
        # Deferred: main.py runs the grounded Gemini search (async) and speaks the
        # answer back with sources — JARVIS's own "look it up for my knowledge"
        # path. No browser opens (that's the whole point: a fact lookup shouldn't
        # dump the user onto a results page they then have to read themselves).
        return {"type": "web_search", "needs_web_search": True, "ok": None,
                "query": (spec.get("query") or spec.get("text")
                          or spec.get("topic") or target),
                "message": "Searching the web…"}
    elif atype in ("browse", "open_search", "browser_search"):
        # Explicit "open a browser tab and search" — rarely needed now that
        # web_search answers directly, kept for when the user wants the page open.
        # Same rule as open_url: prefer JARVIS's own browser when a session is live
        # so the search lands where it's being controlled, not a stray OS tab.
        if browser.available() and (browser.is_open() or browser.is_approved()):
            ok, msg = browser.search(target)
        else:
            ok, msg = skills.search_web(target)
    elif atype in ("open_folder", "folder", "directory"):
        ok, msg = app_launcher.open_folder(target)

    # ── System / power / volume ───────────────────────────────────────────────
    elif atype in ("system", "system_command", "media"):
        ok, msg = app_launcher.system_command(target)
    elif atype in ("power", "shutdown", "restart"):
        ok, msg = skills.power(target or atype)
    elif atype in ("delete_file", "delete", "remove_file", "trash"):
        ok, msg = skills.delete_file(target)
    elif atype in ("volume", "set_volume"):
        lvl = spec.get("level", spec.get("target"))
        ok, msg = skills.set_volume(lvl)

    # ── Information ───────────────────────────────────────────────────────────
    elif atype in ("time", "current_time", "clock"):
        ok, msg = skills.current_time()
    elif atype in ("day", "date", "current_day"):
        ok, msg = skills.current_day()
    elif atype in ("ip", "ip_address"):
        ok, msg = skills.ip_address()
    elif atype in ("location", "where_am_i", "geolocate"):
        ok, msg = skills.location()
    elif atype in ("internet_speed", "speed_test", "speedtest"):
        ok, msg = skills.internet_speed()

    # ── Weather (current + multi-day forecast, read back by the model) ─────────
    elif atype in ("weather", "forecast", "temperature", "whats_the_weather"):
        import places as _places, weather as _weather
        lat, lon, place = _places.get_user_coords()
        summary = _weather.forecast_summary(lat, lon, place,
                                            days=_safe_int(spec.get("days"), 3))
        if summary:
            return {"type": "weather", "ok": True, "message": summary,
                    "feed_to_model": {"name": "weather", "content": summary}}
        ok, msg = False, ("I can't get the forecast yet, sir — I may not have your "
                          "location.")

    # ── Nearby places (restaurants, cafés, shops, ATMs…) via OpenStreetMap ─────
    elif atype in ("places", "nearby", "find_places", "find_nearby", "restaurants",
                   "places_nearby", "near_me"):
        import places as _places
        lat, lon, _place = _places.get_user_coords()
        query = spec.get("query") or spec.get("target") or spec.get("type") or target
        ok, msg, items = _places.find_nearby(query, lat, lon)
        return {"type": "places", "target": query, "ok": ok, "message": msg,
                **({"feed_to_model": {"name": "nearby places", "content": msg}}
                   if ok and items else {})}

    # ── Directions / travel time to a place ───────────────────────────────────
    elif atype in ("directions", "navigate", "route", "how_to_get_to", "distance_to"):
        import places as _places
        lat, lon, _place = _places.get_user_coords()
        dest = spec.get("destination") or spec.get("to") or spec.get("place") \
            or spec.get("query") or spec.get("target") or target
        ok, msg = _places.directions(dest, lat, lon)

    # ── Pin / clear the user's location (deferred: main.py geocodes + applies) ──
    elif atype in ("set_location", "set_my_location", "pin_location", "my_location",
                   "update_location"):
        do = str(spec.get("do") or spec.get("action") or "set").lower().strip()
        return {"type": "set_location", "needs_location_set": True, "do": do,
                "place": spec.get("place") or spec.get("query") or spec.get("target") or target,
                "message": "Updating your location…"}

    # ── News headlines (free RSS) ─────────────────────────────────────────────
    elif atype in ("news", "headlines", "get_news", "news_headlines"):
        import news as _news
        ok, msg, items = _news.get_headlines(spec.get("topic") or spec.get("query") or target,
                                             _safe_int(spec.get("count"), 5))
        return {"type": "news", "ok": ok, "message": msg,
                **({"feed_to_model": {"name": "news headlines", "content": msg}}
                   if ok and items else {})}

    # ── One-shot reminders & timers ───────────────────────────────────────────
    elif atype in ("reminder", "remind", "timer", "set_reminder", "set_timer"):
        import reminders as _reminders
        do = str(spec.get("do") or spec.get("command") or "").lower().strip()
        if do in ("list", "show", "get"):
            ok, msg = True, _reminders.list_text()
        elif do in ("clear", "clear_all"):
            ok, msg = _reminders.clear()
        elif do in ("remove", "cancel", "delete"):
            ok, msg = _reminders.remove(spec.get("text") or spec.get("query") or target)
        else:
            when = spec.get("when") or spec.get("time") or spec.get("delay") or target
            is_timer = atype in ("timer", "set_timer") or do == "timer" \
                or bool(spec.get("timer"))
            ok, msg = _reminders.add_reminder(when, spec.get("text") or spec.get("task") or "",
                                              is_timer=is_timer)

    # ── Playbooks: teach JARVIS a new recipe (self-extension) ─────────────────
    elif atype in ("playbook", "playbooks", "learn", "learn_playbook", "teach",
                   "skill"):
        import playbooks as _playbooks
        do = str(spec.get("do") or spec.get("command") or "").lower().strip()
        if do in ("remove", "delete", "forget"):
            ok, msg = _playbooks.remove_playbook(spec.get("name") or spec.get("query") or target)
        elif do in ("list", "show", "get", ""):
            if spec.get("steps") or spec.get("name"):     # an add with no explicit do
                ok, msg = _playbooks.add_playbook(spec.get("name") or target,
                                                  spec.get("steps") or spec.get("instructions"),
                                                  spec.get("triggers"))
            else:
                ok, msg = True, _playbooks.list_text()
        else:   # add / create / teach
            ok, msg = _playbooks.add_playbook(spec.get("name") or target,
                                              spec.get("steps") or spec.get("instructions"),
                                              spec.get("triggers"))

    # ── Home-screen customization (theme/panels/layout) → forwarded to the HUD ─
    elif atype in ("screen", "theme", "customize", "customise", "appearance", "hud"):
        do = str(spec.get("do") or spec.get("command") or "").lower().strip().replace(" ", "_")
        patch, msg = _screen_patch(do, spec)
        return {"type": "screen", "do": do, "ok": bool(patch is not None),
                "message": msg, **({"screen_patch": patch} if patch is not None else {})}

    # ── Creation / capture ────────────────────────────────────────────────────
    elif atype in ("qr_code", "qr", "qrcode"):
        ok, msg, data_url = skills.qr_code(spec.get("text") or target)
        return {"type": "qr_code", "target": target, "ok": ok, "message": msg,
                **({"image": data_url} if data_url else {})}
    elif atype in ("screenshot", "screen_capture"):
        ok, msg = skills.screenshot()
    elif atype in ("see_screen", "look_at_screen", "analyze_screen", "screen_vision", "what_is_on_screen"):
        # Deferred: main.py captures the screen and asks the vision model (async).
        return {"type": "see_screen", "needs_vision": True, "ok": None,
                "question": spec.get("question") or spec.get("query") or target
                            or "Describe what is on the screen and anything notable.",
                "message": "Looking at your screen…"}
    elif atype in ("read_pdf", "pdf_read", "open_pdf"):
        ok, msg, content = skills.read_pdf(spec.get("path") or target)
        return {"type": "read_pdf", "target": target, "ok": ok, "message": msg,
                **({"feed_to_model": {"name": _basename(spec.get("path") or target),
                                      "content": content}} if ok and content else {})}

    # ── Read-only terminal: list directories / read files (safety-gated) ──────
    elif atype in ("list_dir", "ls", "dir", "list_directory", "browse_dir", "browse_files"):
        path = spec.get("path") or target
        ok, msg, listing = fs_access.list_dir(path)
        if not ok and not fs_access.is_allowed(path):
            from . import files
            if files._resolve(path)[0] is not None:
                # One of the user's own folders, just not the read-only terminal's:
                # the autopilot's files command looks there under its own consent.
                # (A weak chat model kept picking this tool for "how many PDFs in my
                # Downloads?" and the turn ended at "add it in Settings".) main.py
                # turns this into a computer_task in the user's own words; the voice
                # model reads the message and calls computer_task itself.
                return {"type": "list_dir", "target": path, "ok": True,
                        "autopilot_fallback": True, "speak": "",
                        "message": (f"{path} isn't one of the read-only folders — use "
                                    "computer_task with the user's request (the autopilot "
                                    "looks there with their consent).")}
        return {"type": "list_dir", "target": path, "ok": ok, "message": msg,
                **({"feed_to_model": {"name": str(path), "content": listing}}
                   if ok and listing else {})}
    elif atype in ("read_file", "cat", "view_file"):
        ok, msg, content = fs_access.read_file(spec.get("path") or target)
        return {"type": "read_file", "target": spec.get("path") or target, "ok": ok,
                "message": msg,
                **({"feed_to_model": {"name": _basename(spec.get("path") or target),
                                      "content": content}} if ok and content else {})}

    # ── Open a file JARVIS created (OS default app), resolved in storage ──────
    elif atype in ("open_file", "open_saved", "reveal_file", "show_file"):
        ok, msg = skills.open_saved_file(spec.get("target") or spec.get("path") or target)

    # ── Generate an image (Gemini image models). Deferred to the async pipeline.
    elif atype in ("generate_image", "gen_image", "create_image", "make_image", "image"):
        return {"type": "generate_image", "needs_image_gen": True, "ok": None,
                "prompt": spec.get("prompt") or spec.get("text") or target
                          or "a high-quality illustration",
                "message": "Generating an image…"}

    elif atype in ("text_to_pdf", "make_pdf", "create_pdf", "save_pdf"):
        ok, msg = skills.text_to_pdf(spec)
    elif atype in ("record", "recording", "record_audio", "record_video", "record_screen"):
        # Let an explicit type like record_screen imply the media.
        if atype == "record_audio":
            spec.setdefault("media", "audio")
        elif atype == "record_video":
            spec.setdefault("media", "video")
        elif atype == "record_screen":
            spec.setdefault("media", "screen")
        ok, msg = recorder.record(spec)

    # ── Schedule / standby ────────────────────────────────────────────────────
    elif atype in ("schedule", "agenda"):
        ok, msg = skills.schedule(spec)

    # ── Read the clipboard on demand (not auto-injected — may hold secrets) ────
    elif atype in ("clipboard", "read_clipboard", "get_clipboard", "paste"):
        ok, content = computer.read_clipboard()
        if not ok:
            return {"type": "clipboard", "ok": False, "message": content}
        if not content:
            return {"type": "clipboard", "ok": True, "message": "Your clipboard is empty, sir."}
        return {"type": "clipboard", "ok": True, "message": "Reading your clipboard…",
                "feed_to_model": {"name": "clipboard contents", "content": content}}

    # ── Scheduled routines (recurring proactive briefings/actions) ─────────────
    elif atype in ("routine", "routines", "schedule_routine"):
        import routines as _routines
        do = str(spec.get("do") or spec.get("command") or "").lower().strip()
        if do in ("add", "create", "set", "new"):
            ok, msg = _routines.add_routine(
                spec.get("time"),
                spec.get("prompt") or spec.get("task") or spec.get("text"),
                spec.get("days") or spec.get("day") or "daily")
        elif do in ("list", "get", "show", ""):
            ok, msg = True, _routines.list_text()
        elif do in ("remove", "delete", "cancel"):
            ok, msg = _routines.remove_routine(
                spec.get("match") or spec.get("query") or spec.get("time") or spec.get("prompt"))
        elif do in ("clear", "clear_all", "remove_all"):
            ok, msg = _routines.clear_routines()
        else:
            ok, msg = False, f"Unknown routine action '{do}'."
        return {"type": "routine", "do": do, "ok": ok, "message": msg}

    # ── Long-term memory: remember / forget durable facts about the user ───────
    elif atype in ("remember", "memorize", "note_fact", "save_memory", "remember_fact"):
        import memory_store
        ok, msg = memory_store.add_fact(spec.get("text") or spec.get("fact") or target)
    elif atype in ("forget", "forget_fact", "delete_memory", "unremember"):
        import memory_store
        ok, msg = memory_store.forget_fact(spec.get("text") or spec.get("query") or target)
    elif atype in ("silence", "be_quiet", "mute_timer"):
        ok, msg = skills.silence(spec.get("seconds", target))
    elif atype in ("sleep_mode", "standby", "go_to_sleep"):
        ok, msg = skills.sleep_mode()

    # ── External app control (Spotify, …) via the pluggable app framework.
    #    Falls back to simply opening the app when there's no integration. ──────
    elif atype in ("app", "app_control", "control_app", "app_command", "app_cmd"):
        from .apps import dispatch as _app_dispatch, available
        app_name = str(spec.get("app") or spec.get("target") or "").lower().strip()
        if app_name and app_name not in available():
            ok, msg = app_launcher.open_app(app_name)
        else:
            ok, msg = _app_dispatch(spec)

    # ── Whole-task autopilot goals (silent operator loop — see autopilot.py) ──
    # Deferred: the loop awaits an LLM per step, so main.py runs it (same
    # pattern as needs_vision / needs_image_gen). The "message" is the card
    # text if something drops the deferred result; it is never spoken.
    elif atype in _AUTOPILOT_BROWSER_TYPES or atype in _AUTOPILOT_COMPUTER_TYPES:
        kind = "computer" if atype in _AUTOPILOT_COMPUTER_TYPES else "browser"
        goal = str(spec.get("goal") or spec.get("task") or spec.get("target")
                   or spec.get("text") or spec.get("query") or "").strip()
        if not goal:
            return {"type": f"{kind}_task", "ok": False,
                    "message": "I wasn't given a goal for that task, sir."}
        # The chat model's browser-vs-desktop choice is a guess, and its wrong
        # guesses are expensive (a desktop goal sent to the browser opens a
        # made-up URL, or googles for a web version of an app that's installed).
        # A deterministic check overrides it only on positive evidence.
        try:
            from task_router import route_task
            kind, why = route_task(kind, goal)
        except Exception:  # noqa: BLE001 — routing is an optimisation, never a blocker
            why = ""
        if why:
            print(f"[Router] {goal[:60]!r} → {kind} ({why})", flush=True)
        return {"type": f"{kind}_task", "ok": True, "needs_autopilot": True,
                "kind": kind, "goal": goal, "routed": why,
                "message": f"Working on it: {goal[:80]}", "speak": ""}

    # ── Computer control (mouse/keyboard via the UIA tree — see computer.py) ──
    elif atype in _COMPUTER_TYPES:
        do = str(spec.get("do") or spec.get("command") or spec.get("action")
                 or "").lower().strip().replace(" ", "_")
        tgt = spec.get("target") or spec.get("name") or spec.get("element") \
            or spec.get("n") or spec.get("index")
        if do in ("arm", "enable", "start", "on"):
            ok, msg = computer.arm(spec.get("minutes"))
        elif do in ("disarm", "off", "stop", "disable"):
            ok, msg = computer.disarm()
        elif do in ("list", "list_ui", "elements", "see", "scan", "read_ui", ""):
            ok, msg, listing = computer.list_ui()
            # The numbered listing is an INTERNAL observation: it goes to the
            # model via feed_to_model only. The card gets a short note and
            # "speak":"" keeps it out of the TTS — never read numbers aloud.
            # Count the numbered entries (the listing now carries header lines:
            # window title + focused control).
            n = sum(1 for ln in listing.splitlines()
                    if ln[:1].isdigit() and "." in ln[:4]) if listing else 0
            card = (f"Scanned the window — {n} controls." if ok and listing else msg)
            return {"type": "computer", "do": "list", "ok": ok, "message": card,
                    "speak": "",
                    **({"feed_to_model": {"name": "desktop ui controls",
                                          "content": listing}} if ok and listing else {})}
        elif do in ("click", "click_element", "press_element", "tap", "double_click"):
            ok, msg = computer.click_ui(tgt or spec.get("text"))
        elif do in ("type", "type_text", "write", "enter_text", "fill"):
            ok, msg = computer.type_text(spec.get("text") or spec.get("value") or "",
                                         target=tgt, clear=bool(spec.get("clear")))
        elif do in ("press", "key", "keys", "hotkey", "shortcut", "send_keys"):
            ok, msg = computer.press_keys(spec.get("keys") or spec.get("key")
                                          or spec.get("combo") or tgt or "")
        elif do in ("scroll",):
            ok, msg = computer.scroll_amount(spec.get("amount") or spec.get("dy")
                                             or spec.get("target") or 600)
        else:
            ok, msg = False, f"Unknown computer action '{do}'."
        return {"type": "computer", "do": do, "ok": ok, "message": msg,
                "target": str(tgt or spec.get("text") or "")}

    # ── Browser control (JARVIS's hands — see browser.py) ─────────────────────
    elif atype in ("browser", "web", "webbrowser"):
        do = str(spec.get("do") or spec.get("command") or spec.get("action")
                 or "").lower().strip().replace(" ", "_")
        if do in ("open", "open_url", "goto", "go_to", "navigate", "visit", "browse"):
            ok, msg = browser.open_url(spec.get("url") or spec.get("target")
                                       or spec.get("query") or "")
        elif do in ("open_browser", "launch", "start", "open_blank"):
            ok, msg = browser.open_blank()
        elif do in ("list", "list_elements", "list_ui", "elements", "read_ui",
                    "see", "see_page", "scan"):
            ok, msg, listing = browser.list_elements()
            # Internal observation: full listing only in feed_to_model; the card
            # gets a one-liner and "speak":"" keeps numbered lists out of the TTS.
            n = max(0, len(listing.splitlines()) - 1) if listing else 0
            card = (f"Scanned the page — {n} elements." if ok and listing else msg)
            return {"type": "browser", "do": "list", "ok": ok, "message": card,
                    "speak": "",
                    **({"feed_to_model": {"name": "browser page elements",
                                          "content": listing}} if ok and listing else {})}
        elif do in ("click", "click_element", "press_element", "tap"):
            ok, msg = browser.click(spec.get("target") or spec.get("name")
                                    or spec.get("element") or spec.get("text")
                                    or spec.get("n") or spec.get("index"))
        elif do in ("search", "search_for", "find", "query", "look_up", "lookup"):
            ok, msg = browser.search(
                spec.get("query") or spec.get("text") or spec.get("q")
                or spec.get("value") or spec.get("target") or "",
                site=spec.get("site") or spec.get("on") or spec.get("engine")
                or spec.get("where"))
        elif do in ("type", "type_text", "write", "fill", "enter_text"):
            ok, msg = browser.type_text(
                spec.get("text") or spec.get("value") or "",
                target=spec.get("target") or spec.get("field") or spec.get("name")
                or spec.get("index"),
                submit=bool(spec.get("submit") or spec.get("enter")))
        elif do in ("press", "key", "hotkey", "send_keys", "shortcut"):
            ok, msg = browser.press(spec.get("keys") or spec.get("key")
                                    or spec.get("combo") or spec.get("target") or "")
        elif do in ("scroll",):
            ok, msg = browser.scroll(spec.get("amount") or spec.get("dy")
                                     or spec.get("target") or 600)
        elif do in ("back", "go_back"):
            ok, msg = browser.go_back()
        elif do in ("current", "current_page", "where", "url"):
            ok, msg = browser.current_page()
        elif do in ("read", "read_page", "get_text", "page_text", "content"):
            ok, content = browser.read_page()
            if not ok:
                return {"type": "browser", "do": "read", "ok": False, "message": content}
            if not content:
                return {"type": "browser", "do": "read", "ok": True,
                        "message": "There's no readable text on this page, sir."}
            return {"type": "browser", "do": "read", "ok": True,
                    "message": "Reading the page…", "speak": "",
                    "feed_to_model": {"name": "browser page text", "content": content}}
        elif do in ("close", "quit", "close_browser"):
            ok, msg = browser.close()
        else:
            ok, msg = False, f"Unknown browser action '{do}'."
        return {"type": "browser", "do": do, "ok": ok, "message": msg,
                "target": spec.get("url") or spec.get("target")
                or spec.get("text") or spec.get("name") or ""}

    # ── In-app UI control (JARVIS driving its own HUD) ─────────────────────────
    # Also accept the bare forms {"type":"clear_chat"} / {"type":"new_conversation"}
    # / {"type":"reset"}: models sometimes use the action NAME as the top-level
    # type instead of {"type":"ui","do":"clear_chat"}. Adopt the type as the `do`.
    elif atype in ("ui", "ui_action", "interface", "clear_chat", "clear_conversation",
                   "new_conversation", "reset"):
        do = str(spec.get("do") or spec.get("command") or target).lower().strip().replace(" ", "_")
        if atype not in ("ui", "ui_action", "interface"):
            do = do or atype
            if do in ("clear_conversation", "clear"):
                do = "clear_chat"
        # "minimize/maximize the panels" → the collapse/expand the HUD understands.
        do = re.sub(r"^minimi[sz]e", "collapse", do)
        do = re.sub(r"^(maximi[sz]e|restore)", "expand", do)
        # Normalise every "collapse/expand everything" phrasing to <verb>_all.
        do = re.sub(r"^(collapse|expand|toggle)_(all_panels|panels|all_tabs|tabs|everything)$",
                    r"\1_all", do)
        labels = {
            "open_camera": "Opening the optic camera.", "open_ocr": "Opening the optic camera.",
            "close_camera": "Closing the camera.", "close_ocr": "Closing the camera.",
            "open_chat": "Opening the conversation log.", "open_conversation": "Opening the conversation log.",
            "close_chat": "Closing the conversation log.",
            "open_settings": "Opening settings.", "close_settings": "Closing settings.",
            "listen": "Listening.", "mic": "Listening.",
            "stop_speaking": "Stopping.", "stop": "Stopping.",
            "clear_chat": "Clearing the conversation.", "new_conversation": "Starting a new conversation.",
            # Overlays
            "open_skills": "Opening the skills panel.", "close_skills": "Closing the skills panel.",
            "open_capabilities": "Here's what I can do.", "close_capabilities": "Closing.",
            "open_power": "Opening power options.", "close_power": "Closing power options.",
            "open_memory": "Here's everything I remember.", "close_memory": "Closing memory.",
            "open_terminal": "Opening the command log.", "close_terminal": "Closing the command log.",
            "open_activity": "Opening the activity panel.", "close_activity": "Closing the activity panel.",
            # Collapsible data panels — expand_/collapse_/toggle_ + section name
            "expand_system": "Expanding the system panel.", "collapse_system": "Collapsing the system panel.",
            "expand_power_panel": "Expanding power.", "collapse_power_panel": "Collapsing power.",
            "expand_weather": "Expanding weather.", "collapse_weather": "Collapsing weather.",
            "expand_network": "Expanding the network panel.", "collapse_network": "Collapsing the network panel.",
            "expand_agenda": "Expanding the agenda.", "collapse_agenda": "Collapsing the agenda.",
            "collapse_all": "Minimising all the panels.", "expand_all": "Expanding all the panels.",
        }
        # `do` is returned so main.py can forward it to the frontend as a ui_action.
        return {"type": "ui", "do": do, "target": do, "ok": True,
                "message": labels.get(do, "Done.")}

    else:
        ok, msg = False, f"Unknown action type '{atype}'."

    return {"type": atype, "target": target, "ok": ok, "message": msg}


# One-shot types an autopilot task makes redundant. When the model emits BOTH
# (e.g. {"type":"open_url"} + {"type":"browser_task"}), running both opened the
# right page and then "changed its mind": the one-shot navigated (often in the
# user's OS browser), then the autopilot started over in ITS browser and went
# wherever its own first step chose. The task subsumes the one-shots — drop them.
_BROWSER_ONESHOT_TYPES = frozenset((
    "browser", "web", "webbrowser",
    "open_url", "url", "website", "link",
    "browse", "open_search", "browser_search",
))
# NOTE: grounded web_search / search_web are deliberately NOT here. They no longer
# open a browser (they answer from live Google Search), so pairing one with a
# browser_task is a legitimate "look this up AND do that on a site" — never a
# duplicate navigation to strip.
_COMPUTER_SAFE_DOS = frozenset(("disarm", "off", "stop", "disable", "state", "status"))


def _strip_subsumed(specs: "List[dict]") -> "List[dict]":
    """Drop one-shot browser/computer specs that ride along with a whole-task
    autopilot spec in the same reply (browser 'close' and computer disarm/state
    are kept — they're never what the autopilot would redo)."""
    types = {str(s.get("type") or "").lower().strip() for s in specs}
    out = specs
    if types & set(_AUTOPILOT_BROWSER_TYPES):
        def keep_b(s: dict) -> bool:
            t = str(s.get("type") or "").lower().strip()
            if t not in _BROWSER_ONESHOT_TYPES:
                return True
            do = str(s.get("do") or s.get("command") or s.get("action")
                     or "").lower().strip().replace(" ", "_")
            return do in _BROWSER_CLOSE
        out = [s for s in out if keep_b(s)]
    if types & set(_AUTOPILOT_COMPUTER_TYPES):
        def keep_c(s: dict) -> bool:
            t = str(s.get("type") or "").lower().strip()
            if t not in _COMPUTER_TYPES:
                return True
            do = str(s.get("do") or s.get("command") or s.get("action")
                     or "").lower().strip().replace(" ", "_")
            return do in _COMPUTER_SAFE_DOS
        out = [s for s in out if keep_c(s)]
    return out


def execute_actions(text: str) -> Tuple[str, List[dict]]:
    """Strip + run every [ACTION] block. Returns (clean_text, results).

    Specs are GATHERED from all blocks first and only then run, so the
    subsume filter sees the whole turn — the redundant one-shot and the
    browser_task it rides with usually arrive in SEPARATE blocks (the
    one-block-per-task rule)."""
    results: List[dict] = []
    gathered: List[dict] = []

    def _take(match: "re.Match") -> str:
        specs = _parse_specs(match.group(1))
        if not specs:
            # A malformed/empty block is a transient model glitch, not an action
            # the user took. Log it but DON'T surface a scary "malformed action"
            # error card — it littered the chat even when the same turn's other
            # actions ran fine. The block is still stripped from the spoken text.
            print(f"[Actions] ignored a malformed [ACTION] block: "
                  f"{match.group(1)[:120]!r}", flush=True)
            return ""
        # A block may carry several specs (a compound request) — keep them in order.
        gathered.extend(specs)
        return ""

    clean = _ACTION_RE.sub(_take, text)

    def _take_bare_trailing(match: "re.Match") -> str:
        specs, consumed = _parse_specs_prefix(match.group(1))
        if not specs or consumed <= 0:
            print(f"[Actions] ignored a malformed trailing [ACTION] block: "
                  f"{match.group(1)[:120]!r}", flush=True)
            return ""
        gathered.extend(specs)
        return match.group(1)[consumed:]

    clean = _BARE_TRAILING_ACTION_RE.sub(_take_bare_trailing, clean)

    results = run_specs(gathered)
    # Collapse whitespace left behind by removed tags.
    clean = re.sub(r"[ \t]{2,}", " ", clean)
    clean = re.sub(r"[ \t]+\n", "\n", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean).strip()
    return clean, results


def run_specs(specs: "List[dict]") -> List[dict]:
    """Run a list of already-parsed action specs through the SAME subsume filter,
    permission gate and dispatch that :func:`execute_actions` applies to ``[ACTION]``
    tags. Returns the identical result dicts (gated ones carry ``needs_permission``
    for main.py to approve). This is the shared core for BOTH the legacy tag path
    and the native function-calling path (whose tool calls become specs via
    ``live_tools.to_spec``), so the two can't drift apart."""
    results: List[dict] = []
    for spec in _strip_subsumed([s for s in (specs or []) if isinstance(s, dict)]):
        atype = str(spec.get("type") or "").lower().strip()
        if not atype:
            print(f"[Actions] ignored action with empty type: {spec!r}", flush=True)
            continue
        gated, kind, desc = needs_permission(spec)
        if gated:
            # Don't run it — hand it to main.py to ask the user for approval.
            results.append({"type": spec.get("type"), "spec": spec, "needs_permission": True,
                            "kind": kind, "description": desc, "ok": None,
                            "message": f"Awaiting your approval to {desc}."})
        else:
            results.append(_run_one(spec))
    return results
