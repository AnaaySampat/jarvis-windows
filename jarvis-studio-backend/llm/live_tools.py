"""Tool / function-calling bridge for the Gemini Live API (native-audio).

The Live API supports **first-class function calling**: we advertise a set of
``functionDeclarations`` in the ``setup`` frame; when the model decides to act it
sends a ``toolCall``; we run the action through the existing :mod:`actions`
framework and reply with a ``toolResponse``. The model then speaks a confirmation
**after** seeing the real result.

This is what lets native-audio JARVIS actually *do* things — take a screenshot,
open or close an app, open a file it saved, clear the chat, control Spotify — instead
of merely *saying* it did. Because the spoken confirmation is generated from the
tool's actual return value, it also stops the model hallucinating success (the
"Conversation history has been cleared…" reply when nothing happened).

``function_declarations()`` is sent at setup. ``to_spec(name, args)`` converts a
model function call into the action-spec dict that :func:`actions.run_action`
understands — the *same* dicts the text pipeline builds from ``[ACTION]`` tags —
so both paths share one execution + permission-gating code path.
"""

from __future__ import annotations


def _fn(name: str, description: str, properties: "dict | None" = None,
        required: "list | None" = None) -> dict:
    decl = {"name": name, "description": description}
    if properties:
        decl["parameters"] = {
            "type": "OBJECT",
            "properties": properties,
            "required": required or [],
        }
    return decl


def _str(desc: str, enum: "list | None" = None) -> dict:
    p = {"type": "STRING", "description": desc}
    if enum:
        p["enum"] = enum
    return p


def _capabilities() -> "tuple[bool, bool]":
    """(places_ok, web_ok) for the ACTIVE provider tier, resolved live so the
    palette only advertises place lookups that actually work:
      • Vertex+Groq → Places API (New) via ADC  AND  Gemini-grounded web search.
      • Gemini+Groq → web search (Gemini key) but NO Places (no Maps key / ADC).
      • Local LLM   → neither (no Gemini, no Places) → OpenStreetMap only.
    Both checks are cheap (cached) after the first call and degrade to False, so
    a missing dependency just trims the palette rather than raising."""
    try:
        import places_google
        places_ok = places_google.available()
    except Exception:  # noqa: BLE001
        places_ok = False
    try:
        from llm import groq_bridge
        web_ok = groq_bridge.web_search_available()
    except Exception:  # noqa: BLE001
        web_ok = False
    return places_ok, web_ok


def _location_lookup_tools(places_ok: bool, web_ok: bool) -> list:
    """The place / brand lookup tools, tailored to the tier (see _capabilities).

    The routing rule that differs by tier: who owns 'nearest <brand>'. When the
    Places API is available it owns it (real branch + exact distance); without
    Places, brands go to Gemini web search where that exists, else to a best-
    effort OpenStreetMap name match. web_search is only advertised at all where a
    Gemini path can actually ground it — offering a dead tool on the Local tier is
    exactly the 'says it did but didn't' failure we avoid."""
    tools = []

    # web_search — only where a Gemini path can ground it (Vertex ADC or key).
    if web_ok:
        if places_ok:
            web_desc = (
                "Look something up on the web and ANSWER it directly from live "
                "Google Search results — no browser window opens. Use for current/"
                "real-time facts (news, prices, scores, 'latest'/'today', recent "
                "events) and brand info like reviews, menus or opening hours. To "
                "find the NEAREST branch of a brand/chain, use find_places instead "
                "— it returns the actual closest one with a distance.")
        else:
            web_desc = (
                "Look something up on the web and ANSWER it directly from live "
                "Google Search results — no browser window opens. Use for current/"
                "real-time facts (news, prices, scores, 'latest'/'today', recent "
                "events), brand info (reviews, menus, hours), AND — importantly — "
                "to find WHICH branch of a named brand/chain is NEAREST the user "
                "('closest Blue Tokai to me', 'nearest Starbucks'). Prefer this "
                "over get_directions for nearest-branch questions: Google knows "
                "brand branches the offline map is missing. The user's area is "
                "added to the search automatically.")
        tools.append(_fn("web_search", web_desc,
            {"query": _str("What to look up (for 'nearest <brand>', just the "
                           "brand — the user's location is added for you).")},
            ["query"]))

    # find_places — categories on every tier; brands too when Places is available.
    if places_ok:
        tools.append(_fn("find_places",
            "Find places near the user and return the nearest with exact "
            "distances — works for broad CATEGORIES (restaurants, cafés, coffee, "
            "bakeries, shops, ATMs, pharmacies, fuel, hotels, parks…) AND for "
            "specific BRANDS/chains ('Blue Tokai', 'Starbucks'). Use it for any "
            "'nearest/closest <X>' question; it knows real brand-branch locations.",
            {"query": _str("A category ('coffee', 'pharmacy') OR a brand/place "
                           "name ('Blue Tokai', 'Starbucks').")},
            ["query"]))
    else:
        brand_note = (
            " IMPORTANT: it reads broad map CATEGORIES only, NOT specific brand "
            "names — for a named brand (e.g. 'Blue Tokai', 'Starbucks') use "
            "web_search instead, which knows brands and reviews."
            if web_ok else
            " It also matches a specific name best-effort when you pass one.")
        tools.append(_fn("find_places",
            "Find places near the user by CATEGORY — restaurants, cafés, coffee, "
            "bakeries, shops, ATMs, pharmacies, fuel, hotels, parks, etc. Returns "
            "the nearest with distances." + brand_note,
            {"query": _str("A CATEGORY to look for, e.g. 'restaurants', 'coffee', "
                           "'bakery', 'pharmacy'." + ("" if web_ok else
                           " You may also pass a specific name."))},
            ["query"]))

    # get_directions — to a specific place; brands are fine wherever Places resolves
    # them, otherwise steer brand-nearest to web_search where it exists.
    if places_ok:
        gd_desc = (
            "The precise driving distance + time from the user to a SPECIFIC "
            "place — an address, landmark, mall, station, the airport, or a named "
            "brand branch ('Phoenix Mall Kurla', 'Mumbai airport', 'Blue Tokai'). "
            "Use for 'how far is <place>' / 'how long to get there'.")
    else:
        gd_desc = (
            "The precise driving distance + time from the user to a SPECIFIC, "
            "definitely-mapped place: an address, a landmark, a mall, the airport, "
            "a station (e.g. 'Phoenix Mall', 'Mumbai airport', '<full address>'). "
            "Use for 'how far is <that specific place>' / 'how long to get there'."
            + (" DO NOT use it to find the nearest branch of a BRAND/chain "
               "('closest Blue Tokai', 'nearest Starbucks') — the offline map "
               "misses many brand branches; use web_search for those."
               if web_ok else ""))
    tools.append(_fn("get_directions", gd_desc,
        {"destination": _str("The specific place to measure to, e.g. 'Phoenix "
                             "Mall Kurla', 'Mumbai airport', a full address.")},
        ["destination"]))

    return tools


def function_declarations(places_ok: "bool | None" = None,
                          web_ok: "bool | None" = None) -> list:
    """The tool palette advertised to the model — tailored to the active provider
    tier so it only offers place lookups that actually work (a fresh list each
    call so callers can't mutate a shared object). ``places_ok`` / ``web_ok``
    override the live :func:`_capabilities` resolution (handy for tests)."""
    if places_ok is None or web_ok is None:
        _p, _w = _capabilities()
        places_ok = _p if places_ok is None else places_ok
        web_ok = _w if web_ok is None else web_ok
    return [
        _fn("take_screenshot",
            "Capture a screenshot of the whole screen and save it to the user's storage."),
        _fn("open_application",
            "Open / launch a desktop application by name — or a Windows Settings "
            "page by its name ('display settings', 'bluetooth settings').",
            {"name": _str("App or Settings page to open, e.g. 'chrome', 'spotify', "
                          "'notepad', 'wifi settings'.")}, ["name"]),
        _fn("close_application", "Close / quit a running application by name.",
            {"name": _str("App to close, e.g. 'chrome', 'spotify'.")}, ["name"]),
        _fn("open_website", "Open a URL in the default web browser.",
            {"url": _str("The full URL or domain to open.")}, ["url"]),
        _fn("set_volume", "Set the system master volume to a specific level.",
            {"level": {"type": "INTEGER", "description": "Volume from 0 to 100."}}, ["level"]),
        _fn("system_control",
            "Control the system or media: lock, sleep, mute, change volume, or media playback.",
            {"command": _str("The control to perform.",
                             ["lock", "sleep", "mute", "volume_up", "volume_down",
                              "play_pause", "next", "previous"])}, ["command"]),
        _fn("power_control",
            "Power the computer. Shutdown/restart/hibernate/logoff require the user's "
            "on-screen approval — call it anyway; the user will be prompted.",
            {"command": _str("Power action.",
                             ["shutdown", "restart", "sleep", "hibernate",
                              "lock", "logoff", "cancel"])}, ["command"]),
        _fn("open_saved_file",
            "Open a file JARVIS previously created or saved (a recording, screenshot, "
            "QR code, PDF, image) using the OS default app.",
            {"name": _str("Which saved file — a short description like 'the screen "
                          "recording' or 'the QR code', or an actual filename.")}, ["name"]),
        _fn("control_spotify", "Control Spotify playback.",
            {"command": _str("Playback command.", ["play", "pause", "next", "previous", "current"]),
             "query": _str("Optional song or artist to play (only for the 'play' command).")},
            ["command"]),
        _fn("make_qr_code", "Generate a QR code image for a URL or text.",
            {"text": _str("The EXACT url or text to encode.")}, ["text"]),
        _fn("generate_image", "Generate an image from a text description.",
            {"prompt": _str("Description of the image to create.")}, ["prompt"]),
        _fn("see_screen",
            "Look at what is currently on the screen and answer a question about it.",
            {"question": _str("What to look for or answer about the screen.")}, ["question"]),
        _fn("record", "Start or stop recording audio, webcam video, or the screen.",
            {"media": _str("What to record.", ["audio", "video", "screen"]),
             "action": _str("Start or stop the recording.", ["start", "stop"])},
            ["media", "action"]),
        _fn("control_interface", "Open or close a part of the JARVIS HUD interface.",
            {"action": _str("Which panel to toggle.",
                            ["open_camera", "close_camera", "open_chat", "close_chat",
                             "open_settings", "close_settings", "open_skills",
                             "close_skills", "open_memory", "close_memory", "listen"])}, ["action"]),
        _fn("clear_conversation",
            "Clear the conversation history / chat log and start fresh."),
        _fn("read_file",
            "Read a text file from an allowed directory (requires the user's approval).",
            {"path": _str("Path or name of the file to read.")}, ["path"]),
        _fn("list_directory",
            "List a directory inside the user's APPROVED folders (Settings → Read-only "
            "Terminal). To FIND a file by name, or to look in Desktop/Downloads/"
            "Documents, use computer_task instead.",
            {"path": _str("Path of the directory to list.")}, ["path"]),
        _fn("remember_fact",
            "Remember a durable fact about the user for future sessions (their name, "
            "preferences, important people, routines). Phrase it in the third person.",
            {"text": _str("The fact to remember, e.g. \"the user prefers metric units\".")},
            ["text"]),
        _fn("forget_fact", "Forget remembered facts that match the text, or 'everything'.",
            {"text": _str("Text to match against stored facts, or 'everything'.")}, ["text"]),
        _fn("read_clipboard",
            "Read what the user currently has copied to the clipboard, e.g. to "
            "summarise or act on it."),
        _fn("manage_routine",
            "Create, list, or remove a recurring routine — something JARVIS does on "
            "its own at a set time (e.g. a daily morning brief).",
            {"action": _str("What to do.", ["add", "list", "remove"]),
             "time": _str("Time of day for 'add', e.g. '08:00' or '7am'."),
             "days": _str("When it repeats: 'daily', 'weekdays', 'weekends', or "
                          "'mon,wed,fri'."),
             "prompt": _str("What JARVIS should do when it fires (for 'add'), or the "
                            "text to match (for 'remove').")},
            ["action"]),
        _fn("get_weather",
            "Get the current weather and a few days' forecast for the user's location."),
        # web_search + find_places + get_directions, tailored to the active tier
        # (Places API ownership of 'nearest <brand>' differs by tier).
        *_location_lookup_tools(places_ok, web_ok),
        _fn("set_my_location",
            "Pin or clear the user's location when they tell you where they are or "
            "say the detected location is wrong (e.g. 'set my location to Powai, "
            "Mumbai', 'I'm actually in Bandra West', 'my location is off'). Pinning "
            "fixes weather, nearby places and distances to the right spot until "
            "cleared. Use 'clear' to return to automatic detection.",
            {"action": _str("'set' to pin a location, 'clear' to go back to automatic.",
                            ["set", "clear"]),
             "place": _str("The place / area / full address to pin, e.g. 'Powai, "
                           "Mumbai' (required for 'set').")},
            ["action"]),
        _fn("get_news",
            "Get the latest news headlines, optionally on a topic.",
            {"topic": _str("Optional topic: world, business, tech, science, sport, "
                           "health, entertainment, or a keyword.")}),
        _fn("set_reminder",
            "Set a one-off reminder or timer that alerts the user at a time. Use for "
            "'remind me in 10 minutes', 'set a timer for 5 minutes', 'remind me at 3pm'.",
            {"when": _str("When to fire, e.g. 'in 10 minutes', '5 minutes', 'at 3pm'."),
             "text": _str("What to remind about (optional for a plain timer).")},
            ["when"]),
        _fn("manage_playbook",
            "Teach yourself a new repeatable skill (a named recipe of steps built from "
            "your existing actions), or list/remove playbooks. Use 'add' when the user "
            "describes a multi-step routine they want by a name.",
            {"action": _str("What to do.", ["add", "list", "remove"]),
             "name": _str("The playbook's name."),
             "steps": _str("The steps to follow, in plain English (for 'add')."),
             "triggers": _str("Comma-separated phrases that should trigger it (optional).")},
            ["action"]),
        _fn("customize_screen",
            "Restyle or rearrange the home-screen HUD on request. For 'show/hide/"
            "minimize/expand ALL the panels', use show_all or hide_all (no panel "
            "needed). For one panel, use show_panel/hide_panel with `panel`.",
            {"action": _str("What to change.",
                            ["set_theme", "set_background", "set_density",
                             "show_panel", "hide_panel", "show_all", "hide_all",
                             "toggle_panel", "move_panel", "reset"]),
             "value": _str("The new value: a colour (cyan/amber/red/green/purple/…/hex) "
                           "for set_theme; grid/solid/aurora/minimal for set_background; "
                           "normal/compact for set_density."),
             "panel": _str("Panel name for show/hide/toggle/move (system, power, agenda, "
                           "weather, network, terminal — or 'all' to show/hide every panel)."),
             "direction": _str("up/down/left/right for move_panel.")},
            ["action"]),
        _fn("browser_task",
            "Do a WHOLE task on the web via the silent autopilot: it drives a "
            "dedicated browser itself (opens pages, sees them, clicks, types, "
            "retries) and returns the outcome. Use it for playing/opening videos, "
            "searching and opening results, looking something up on a site, "
            "filling forms — ANYTHING that needs more than a single step. Give "
            "the COMPLETE goal in plain English. Relay its outcome in one short "
            "sentence; never describe the steps.",
            {"goal": _str("The whole task, e.g. 'open youtube and play lofi hip "
                          "hop radio' or 'find the cheapest flight to Goa on "
                          "google flights and tell me'.")},
            ["goal"]),
        _fn("computer_task",
            "Do a WHOLE task in a desktop app via the silent autopilot (it can "
            "focus or launch windows, click named controls, type, press hotkeys) — "
            "and file chores anywhere in the user's folders: find a file by name, "
            "list a folder (newest/largest), new folder, rename, move, copy. "
            "Use browser_task for websites. Give the complete goal in plain "
            "English and relay the outcome in one sentence.",
            {"goal": _str("The whole task as the user asked it — add no steps they "
                          "didn't (like saving), e.g. 'in Notepad, write a shopping "
                          "list: milk, eggs, bread'.")},
            ["goal"]),
        _fn("computer_control",
            "MANUAL single-step desktop control — prefer computer_task for real "
            "tasks; use this only for one isolated move the user spelled out "
            "(e.g. 'press ctrl+s', 'scroll down') or to 'disarm'. It acts only "
            "in the window already in front: never use it to open an app, "
            "folder or Settings page (open_application / open_folder). The numbered "
            "'list' is INTERNAL — never read it or its numbers aloud.",
            {"action": _str("What to do.",
                            ["list", "click", "type", "press", "scroll", "disarm"]),
             "target": _str("Control number from 'list', or its visible name "
                            "(for 'click')."),
             "text": _str("Text to type (for 'type')."),
             "keys": _str("Key or combo for 'press', e.g. 'enter', 'ctrl+s'."),
             "amount": {"type": "INTEGER", "description": "Scroll distance "
                        "(positive = down)."}},
            ["action"]),
        _fn("browser_control",
            "MANUAL single-step browser control — prefer browser_task for real "
            "tasks; use this only for one isolated move ('open youtube.com', "
            "'scroll down', 'go back', 'read this page', 'close the browser'). "
            "The numbered 'list' is INTERNAL — never read it or its numbers "
            "aloud.",
            {"action": _str("What to do.",
                            ["open", "list", "click", "type", "press", "scroll",
                             "back", "read", "current", "close"]),
             "url": _str("URL or search phrase (for 'open')."),
             "target": _str("Element number from 'list', or its visible text "
                            "(for 'click'/'type')."),
             "text": _str("Text to type (for 'type')."),
             "submit": {"type": "BOOLEAN", "description": "Press Enter after typing."},
             "keys": _str("Key to press for 'press', e.g. 'Enter', 'Tab'."),
             "amount": {"type": "INTEGER", "description": "Scroll distance in pixels "
                        "(negative = up)."}},
            ["action"]),

        # ── Simple one-shot actions (parity with the text [ACTION] catalogue) ──
        _fn("open_folder", "Open a folder / directory in the file explorer.",
            {"path": _str("Folder to open — a path, or a known name like 'desktop', "
                          "'documents', 'downloads'.")}, ["path"]),
        _fn("delete_file",
            "Delete a file (sends it to the Recycle Bin). The user must approve this "
            "on-screen — call it anyway; the user will be prompted.",
            {"path": _str("Path or name of the file to delete.")}, ["path"]),
        _fn("get_time", "Tell the user the current time."),
        _fn("get_day", "Tell the user today's date / day of the week."),
        _fn("get_ip", "Get the computer's IP address."),
        _fn("get_location", "Get the user's approximate location (city)."),
        _fn("internet_speed", "Run an internet speed test (download/upload)."),
        _fn("read_pdf",
            "Read the text of a PDF file so you can summarise or answer about it.",
            {"path": _str("Path or name of the PDF to read.")}, ["path"]),
        _fn("text_to_pdf", "Save some text as a PDF file in the user's Documents.",
            {"text": _str("The body text to put in the PDF."),
             "title": _str("Optional title / heading for the document.")}, ["text"]),
        _fn("manage_schedule",
            "Get, add, edit, remove or clear items in the user's per-day schedule / "
            "agenda (distinct from one-off reminders and recurring routines).",
            {"action": _str("What to do.", ["get", "add", "edit", "remove", "clear"]),
             "day": _str("Which day, e.g. 'today', 'monday' (defaults to today)."),
             "time": _str("Time of the item, e.g. '09:00' (for add/edit)."),
             "task": _str("The task text (for add; or to match for edit/remove)."),
             "new_time": _str("New time when editing an item."),
             "new_task": _str("New task text when editing an item.")},
            ["action"]),
    ]


# function name → builder that returns the actions.run_action() spec dict.
_SPEC = {
    "take_screenshot":   lambda a: {"type": "screenshot"},
    "open_application":  lambda a: {"type": "open_app", "target": a.get("name", "")},
    "close_application": lambda a: {"type": "close_app", "target": a.get("name", "")},
    "open_website":      lambda a: {"type": "open_url", "url": a.get("url", "")},
    "web_search":        lambda a: {"type": "search_web", "query": a.get("query", "")},
    "set_volume":        lambda a: {"type": "set_volume", "level": a.get("level")},
    "system_control":    lambda a: {"type": "system", "target": a.get("command", "")},
    "power_control":     lambda a: {"type": "power", "command": a.get("command", "")},
    "open_saved_file":   lambda a: {"type": "open_file", "target": a.get("name", "")},
    "control_spotify":   lambda a: {"type": "app", "app": "spotify",
                                    "command": a.get("command", ""), "query": a.get("query", "")},
    "make_qr_code":      lambda a: {"type": "qr_code", "text": a.get("text", "")},
    "generate_image":    lambda a: {"type": "generate_image", "prompt": a.get("prompt", "")},
    "see_screen":        lambda a: {"type": "see_screen", "question": a.get("question", "")},
    "record":            lambda a: {"type": "record", "media": a.get("media", "screen"),
                                    "do": a.get("action", "start")},
    "control_interface": lambda a: {"type": "ui", "do": a.get("action", "")},
    "clear_conversation": lambda a: {"type": "ui", "do": "clear_chat"},
    "read_file":         lambda a: {"type": "read_file", "path": a.get("path", "")},
    "list_directory":    lambda a: {"type": "list_dir", "path": a.get("path", "")},
    "remember_fact":     lambda a: {"type": "remember", "text": a.get("text", "")},
    "forget_fact":       lambda a: {"type": "forget", "text": a.get("text", "")},
    "read_clipboard":    lambda a: {"type": "clipboard"},
    "manage_routine":    lambda a: {"type": "routine", "do": a.get("action", "list"),
                                    "time": a.get("time", ""), "days": a.get("days", "daily"),
                                    "prompt": a.get("prompt", ""), "match": a.get("prompt", "")},
    "get_weather":       lambda a: {"type": "weather"},
    "find_places":       lambda a: {"type": "places", "query": a.get("query", "")},
    "get_directions":    lambda a: {"type": "directions", "destination": a.get("destination", "")},
    "set_my_location":    lambda a: {"type": "set_location", "do": a.get("action", "set"),
                                     "place": a.get("place", "")},
    "get_news":          lambda a: {"type": "news", "topic": a.get("topic", "")},
    "set_reminder":      lambda a: {"type": "reminder", "when": a.get("when", ""),
                                    "text": a.get("text", "")},
    "manage_playbook":   lambda a: {"type": "playbook", "do": a.get("action", "list"),
                                    "name": a.get("name", ""), "steps": a.get("steps", ""),
                                    "triggers": a.get("triggers", ""), "query": a.get("name", "")},
    "customize_screen":  lambda a: {"type": "screen", "do": a.get("action", ""),
                                    "value": a.get("value", ""), "panel": a.get("panel", ""),
                                    "direction": a.get("direction", "")},
    "browser_task":      lambda a: {"type": "browser_task", "goal": a.get("goal", "")},
    "computer_task":     lambda a: {"type": "computer_task", "goal": a.get("goal", "")},
    "computer_control":  lambda a: {"type": "computer", "do": a.get("action", ""),
                                    "target": a.get("target", ""), "text": a.get("text", ""),
                                    "keys": a.get("keys", ""), "amount": a.get("amount")},
    "browser_control":   lambda a: {"type": "browser", "do": a.get("action", ""),
                                    "url": a.get("url", ""), "target": a.get("target", ""),
                                    "text": a.get("text", ""), "submit": a.get("submit", False),
                                    "keys": a.get("keys", ""), "amount": a.get("amount")},
    "open_folder":       lambda a: {"type": "open_folder", "target": a.get("path", "")},
    "delete_file":       lambda a: {"type": "delete_file", "target": a.get("path", "")},
    "get_time":          lambda a: {"type": "time"},
    "get_day":           lambda a: {"type": "day"},
    "get_ip":            lambda a: {"type": "ip_address"},
    "get_location":      lambda a: {"type": "location"},
    "internet_speed":    lambda a: {"type": "internet_speed"},
    "read_pdf":          lambda a: {"type": "read_pdf", "path": a.get("path", "")},
    "text_to_pdf":       lambda a: {"type": "text_to_pdf", "text": a.get("text", ""),
                                    "title": a.get("title", "")},
    "manage_schedule":   lambda a: {"type": "schedule", "do": a.get("action", "get"),
                                    "day": a.get("day", ""), "time": a.get("time", ""),
                                    "task": a.get("task", ""), "match": a.get("task", ""),
                                    "new_time": a.get("new_time", ""),
                                    "new_task": a.get("new_task", "")},
}


def to_spec(name: str, args: "dict | None") -> dict:
    """Convert a Live-API function call into an action spec for run_action()."""
    args = args or {}
    builder = _SPEC.get(name)
    if builder is None:
        return {"type": name, **args}          # best-effort passthrough
    return builder(args)


def tool_names() -> set:
    """The set of declared tool names (used for capability gating)."""
    return {d["name"] for d in function_declarations()}


# ── OpenAI / Groq tool shape ──────────────────────────────────────────────────
# The Gemini `functionDeclarations` shape and the OpenAI `tools` shape are the
# same JSON Schema with one difference: Gemini upper-cases the type enum
# (OBJECT/STRING/INTEGER/BOOLEAN), OpenAI/JSON-Schema lower-cases it. We keep
# `function_declarations()` as the single source of truth and derive the OpenAI
# shape from it, so the two wire formats can never drift apart.

def _lower_types(schema):
    """Recursively lower-case JSON-Schema `type` values (OBJECT→object, …) and
    walk nested `properties`/`items`. Returns a NEW structure; never mutates the
    declaration (callers get a fresh list each turn, but be safe anyway)."""
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k == "type" and isinstance(v, str):
                out[k] = v.lower()
            elif k in ("properties",) and isinstance(v, dict):
                out[k] = {pk: _lower_types(pv) for pk, pv in v.items()}
            elif k in ("items",):
                out[k] = _lower_types(v)
            else:
                out[k] = v
        return out
    if isinstance(schema, list):
        return [_lower_types(v) for v in schema]
    return schema


def _decl_to_openai(decl: dict) -> dict:
    """One Gemini functionDeclaration → one OpenAI tool. OpenAI requires a
    `parameters` object even for argument-less tools."""
    params = decl.get("parameters")
    params = _lower_types(params) if params else {"type": "object", "properties": {}}
    return {"type": "function",
            "function": {"name": decl["name"],
                         "description": decl.get("description", ""),
                         "parameters": params}}


def openai_tools() -> list:
    """The tool palette in OpenAI/Groq `tools` shape, derived from the same
    declarations advertised to Gemini (a fresh list each call)."""
    return [_decl_to_openai(d) for d in function_declarations()]
