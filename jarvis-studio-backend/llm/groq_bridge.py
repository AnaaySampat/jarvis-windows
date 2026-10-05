"""Groq cloud LLM bridge.

Drop-in replacement for the old offline Ollama bridge. Talks to Groq's
OpenAI-compatible chat-completions endpoint. Exposes the same public API the
rest of the backend expects: initialize / send_prompt / get_sysinfo /
set_model / set_tts / reset_history.

The most capable model on Groq's free plan is `openai/gpt-oss-120b` (120B,
131k context). It is a *reasoning* model: it returns its chain-of-thought in a
separate `reasoning` field and the user-facing answer in `content`. We run it
at `reasoning_effort="low"` to keep voice latency snappy and only ever read
`content`.
"""

import asyncio
import datetime as _dt
import json
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

import httpx

import app_secrets
import jarvis_paths
import storage
import memory_store
from server.websocket_server import emit
from . import gemini_bridge
from . import live_tools
from . import model_discovery
from . import model_ranker
from . import quota
from . import vertex_auth

CONFIG_PATH = jarvis_paths.config_path()

GROQ_BASE_URL = "https://api.groq.com/openai/v1"

# Every cloud provider besides Gemini speaks the OpenAI chat-completions dialect,
# so one wire path serves them all. Groq ids stay bare (back-compat with saved
# configs); the others are stored as "<provider>:<id>" because their catalogs
# overlap — NVIDIA and OpenRouter both serve "openai/gpt-oss-20b", for one.
OPENAI_COMPAT = {
    "groq": GROQ_BASE_URL,
    "nvidia": "https://integrate.api.nvidia.com/v1",
    "mistral": "https://api.mistral.ai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    # Paid-only providers. Anthropic and Meta serve chat completions through their
    # documented OpenAI-compatibility layers.
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "xai": "https://api.x.ai/v1",
    "meta": "https://api.llama.com/compat/v1",
}
PREFIXED_PROVIDERS = ("nvidia", "mistral", "openrouter", "openai", "anthropic", "xai", "meta")


def split_model(model: str) -> "tuple[str, str]":
    """("nvidia", "meta/llama-x") for "nvidia:meta/llama-x"; ("", model) otherwise."""
    for p in PREFIXED_PROVIDERS:
        if (model or "").startswith(p + ":"):
            return p, model[len(p) + 1:]
    return "", model or ""

# Speech-to-text model for voice input (Groq-hosted Whisper). Turbo = fastest +
# very accurate; far better than the local faster-whisper "base" model.
STT_MODEL = "whisper-large-v3-turbo"

# Default model for the app. gemini-3.5-flash — served via Vertex AI's global
# endpoint on the user's Google Cloud credits (see vertex_auth) — is the flagship:
# excellent at conversational replies AND the structured [ACTION]/[CHART] JSON this
# app leans on, with a large context and none of Groq's free-tier token throttling.
# It replaces gemini-2.5-pro, which (measured) answered in ~3.5s — far too slow for
# a voice assistant; 3.5-flash returns in ~0.9-1.6s and is a newer generation.
# Groq's Llama stays an automatic fallback (used if Vertex is unavailable) and
# remains selectable in Settings; gpt-oss-120b stays selectable for deep reasoning.
DEFAULT_MODEL = "gemini-3.5-flash"

# ── "auto" model routing ──────────────────────────────────────────────────────
# When the user selects the "auto" model, we pick the best concrete model PER
# REQUEST. Preference order: Vertex Gemini (premium, on the user's GCP credits) →
# a Gemini API key → Groq → bare default. Key facts that drive the choice:
#   • gemini-3.5-flash is the flagship AND the mid-tier default: huge context, best
#     agentic/tool-calling (≈84% MCP Atlas), excellent structured [ACTION]/[CHART]
#     JSON, and ~0.9s replies (thinking off). It supersedes 2.5-flash on both speed
#     and quality, so real command/Q&A turns now use it instead of the older mid.
#   • gemini-2.5-flash-lite is the fast lane for TRIVIAL turns: measured the FASTEST
#     of the family on the global endpoint (~0.56s warm). The newer 3.1-flash-lite
#     benchmarked SLOWER here (~0.96s) and trivial greetings need speed over smarts,
#     so the fast lane stays on 2.5-flash-lite. (Flip it to gemini-3.1-flash-lite if
#     cost/recency matters more than the ~0.4s.)
#   • Groq fallback (only when neither Vertex nor a Gemini key is available) uses
#     openai/gpt-oss-120b (120B reasoning flagship) + gpt-oss-20b (fast). These
#     REPLACE llama-3.3-70b / llama-3.1-8b, which Groq deprecated on 2026-06-17.
AUTO_MODEL = "auto"
# Three tiers per provider, chosen by task COMPLEXITY (see _task_complexity):
#   fast  — only trivial greetings/acks (no tools attached)
#   mid   — the default for real commands + everyday Q&A (reliable tool calling)
#   flagship — genuinely complex/multi-step/deep-reasoning turns
_AUTO_BIG_GROQ = "openai/gpt-oss-120b"         # fallback flagship: 120B reasoning, big context
_AUTO_MID_GROQ = "openai/gpt-oss-20b"          # efficient everyday Groq fallback
_AUTO_FAST_GROQ = "openai/gpt-oss-20b"         # fallback fast: snappy trivial turns
_AUTO_BIG_GEMINI = "gemini-3.5-flash"          # flagship — complex/rich/agentic/deep turns
_AUTO_MID_GEMINI = "gemini-2.5-flash"          # capable everyday multimodal model
_AUTO_FAST_GEMINI = "gemini-2.5-flash-lite"    # fastest measured — trivial chatter only

# Per-message model quality control in the chat window. These are preferences,
# not a provider inventory: model discovery filters them against the models that
# are available for the user's active credentials before a request is made.
_TASK_TIER_DEFAULTS = {
    "dumb": {
        "gemini": ("gemini-2.5-flash-lite", "gemini-3.1-flash-lite"),
        "groq": ("openai/gpt-oss-20b",),
    },
    "moderate": {
        "gemini": ("gemini-2.5-flash",),
        "groq": ("meta-llama/llama-4-scout-17b-16e-instruct", "openai/gpt-oss-20b"),
    },
    "very_smart": {
        "gemini": ("gemini-3.5-flash", "gemini-2.5-pro"),
        "groq": ("openai/gpt-oss-120b",),
    },
}

# History sent to the model (stored history cap uses _MAX_HISTORY_STORED).
# The ACTIVE chat is one transcript shared by every model: switching mid-chat
# (Settings pin, per-message tier, auto routing) must still see earlier turns.
# Provider differences are how much of each OLD turn is kept verbatim, and a
# Groq char budget so the free-tier TPM meter doesn't explode — never a hard
# "forget turns 1–N because this model is Groq" cut.
_MAX_HISTORY_STORED = 80           # user+assistant pairs kept for this chat
_GEMINI_HISTORY_TURNS = 80         # send the full stored transcript on Gemini/Vertex
_GROQ_HISTORY_TURNS = 80           # same window; clip + char budget, don't drop the chat
_GEMINI_FULL_TURNS = 16            # recent Gemini turns kept verbatim in the prompt
_GROQ_FULL_TURNS = 4
_GEMINI_CLIP_CHARS = 2800          # older Gemini turns: keep plenty of detail, not a stub
_GROQ_CLIP_CHARS = 400
_GROQ_HISTORY_CHAR_BUDGET = 24000  # ~6k tokens of prior chat on Groq
_SLIM_HISTORY_GEMINI = 10          # agentic follow-up context on Gemini
_SLIM_HISTORY_GROQ = 3

# Trivial greetings → fast lite model only under "auto" (everything else → flagship).
_TRIVIAL_TURN_RE = re.compile(
    r"^(hi|hello|hey|thanks|thank you|thankyou|ok|okay|yes|no|bye|goodbye|"
    r"good\s*(morning|night|evening)|yo|sup)[\s!.?]*$",
    re.IGNORECASE,
)

# Turns that benefit from Gemini's internal reasoning channel (not action commands).
_DEEP_CHAT_RE = re.compile(
    r"\b(why|how|explain|what\s+if|compare|difference|opinion|think|thoughts|"
    r"pros\s+and\s+cons|help\s+me\s+understand|walk\s+me\s+through|in\s+detail|"
    r"elaborate|deeper|reason|because|should\s+i|recommend|best\s+way|"
    r"what\s+do\s+you\s+think|analyze|analyse|break\s+down)\b",
    re.IGNORECASE,
)

# Heuristics that mark a turn as "big" (needs the heavyweight model): rich-output
# or analysis requests, file/document work, code, or simply a long message.
_BIG_TASK_RE = re.compile(
    r"\b(chart|graph|plot|table|compare|flowchart|diagram|schedule|agenda|plan|"
    r"steps?|how\s+to|summar|analys|analyz|explain|detail|essay|story|write|"
    r"code|program|script|debug|read|pdf|document|file|translate|list)\b",
    re.IGNORECASE,
)

# Groq's free tier caps tokens-per-minute (8000 TPM for gpt-oss-120b) and counts
# the *requested* max_completion_tokens against it, so a big ceiling rate-limits
# fast. Voice answers are short; 1024 leaves room for low-effort reasoning + a
# concise reply (or a compact CHART/TABLE JSON) while keeping several turns/min.
_MAX_COMPLETION_TOKENS = 1024
# Vertex/Gemini bills ACTUAL output tokens (not the requested ceiling) and has no
# Groq-style TPM cap, so there's no reason to ration here — give Gemini room for
# long answers, charts, essays and multi-step [ACTION] plans. Only the Groq path
# keeps the tight cap above.
_GEMINI_MAX_COMPLETION_TOKENS = 8192
_REASONING_EFFORT = "low"
_REQUEST_TIMEOUT = 60.0

# One shared HTTP client for all LLM calls so consecutive turns reuse the same
# keep-alive connection to api.groq.com instead of paying a fresh TCP+TLS
# handshake every turn. Created lazily inside the running loop; closed on
# shutdown via aclose_http().
_http_client: "httpx.AsyncClient | None" = None


def _client() -> "httpx.AsyncClient":
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(timeout=_REQUEST_TIMEOUT)
    return _http_client


async def aclose_http() -> None:
    """Close the shared HTTP client (call on backend shutdown)."""
    global _http_client
    if _http_client is not None and not _http_client.is_closed:
        await _http_client.aclose()
    _http_client = None


def _is_reasoning_model(model: str) -> bool:
    """Only gpt-oss-* are reasoning models that accept `reasoning_effort`.
    Sending it to others (e.g. Llama) now returns HTTP 400, so gate on this."""
    return "gpt-oss" in (model or "").lower()


def _add_reasoning(payload: dict) -> dict:
    """Attach reasoning_effort only when the selected model supports it."""
    if _is_reasoning_model(payload.get("model", "")):
        payload["reasoning_effort"] = _REASONING_EFFORT
    return payload

# Shared voice + a dense capability map. The old prompt was ~4k tokens of essays
# the model skimmed; this is short enough to stay in working memory AND names every
# real power so it knows what it can do without the JSON catalog.
_PERSONA = (
    "You are JARVIS on the user's Windows PC (Tony Stark's JARVIS: capable, composed, "
    "quietly witty). Spoken HUD replies: 1–2 sentences for commands/acks; full depth "
    "for questions. Address the user as 'sir' (or their name — one form per reply). "
    "LIVE STATE + remembered facts are private senses — personalize, never dump "
    "status. Do the specific thing asked now; never claim success without a tool result. "
    "Dangerous work (power/delete/file reads) is still called — the app asks approval. "
    "Output ONLY spoken JARVIS plus tags/tools — no chain-of-thought or meta."
)

# Always on the system prompt so Q&A turns still know the palette. Function names
# match live_tools; [ACTION] types match ACTION_CATALOG.
CAPABILITY_INDEX = (
    "YOU CAN (use a tool / [ACTION]; never invent the result):\n"
    "PC: open/close apps or a Settings page ('display settings'), open_folder, open "
    "saved files, volume/media/lock, power, "
    "screenshot, see_screen, clipboard, QR, generate_image, record (until user says stop), "
    "delete_file, silence, sleep_mode.\n"
    "FACTS (call, don't guess): time, day, IP, location, internet_speed, weather, news, "
    "places, directions, set_location, clipboard, read_pdf / read_file / list_dir, "
    "text_to_pdf.\n"
    "WEB: web_search = live answer, no window (news/prices/'latest'). "
    "browser_task = whole site job (play a video, form, cart) — one goal, never pair "
    "with open_url. One-shots: browser open/search/scroll/read/close/back.\n"
    "DESKTOP: computer_task = whole app goal (launch, click, type) or file chore (find "
    "a file by name, list/rename/move/copy, new folder). Guided/step-by-step: "
    "arm computer, then one click/type/press per instruction — not the autopilot.\n"
    "MEMORY: remember/forget; schedule get/add/edit/remove; reminder/timer; routines; "
    "playbooks (learn a named recipe).\n"
    "HUD: screen theme/background/density/panels; ui "
    "open_camera/chat/settings/skills/capabilities/power/terminal/activity, "
    "expand/collapse panels, clear_chat, listen, stop_speaking. Spotify via app play/query.\n"
    "MULTI: one tool/action per asked task, same reply, in order. "
    "Prefer autopilot goals over driving the page yourself."
)

SYSTEM_CORE = (
    _PERSONA + "\n\n"
    "DO something on the machine → emit [ACTION]{json}[/ACTION] (JSON shapes follow "
    "when the turn is action-shaped). Chat/teach/advise → plain text. "
    "Rich output → [CHART]/[TABLE]/[SCHEDULE]/[FLOWCHART]. "
    "Ask one short clarifying question only if genuinely ambiguous.\n\n"
    + CAPABILITY_INDEX
)

# Native function-calling path: schemas carry parameters; this map carries WHEN.
SYSTEM_CORE_TOOLS = (
    _PERSONA + "\n\n"
    "DONE on the machine → CALL the matching function; do not narrate it; wait for "
    "the result. Several asks → several calls, in order. Chat/teach/advise → text only.\n\n"
    + CAPABILITY_INDEX
)

# Rich-output formats — frontend-rendered text, not function calls.
RICH_OUTPUT_REF = (
    "RICH OUTPUT (compact JSON):\n"
    '[CHART type="bar|line|pie|area"]{"title":"...","labels":["A","B"],"values":[10,20]}[/CHART]\n'
    '[SCHEDULE]{"title":"...","items":[{"time":"09:00","task":"...","duration":"1h"}]}[/SCHEDULE]\n'
    '[TABLE]{"title":"...","columns":["Name","Price"],"rows":[["Apple","$2"]]}[/TABLE]\n'
    '[FLOWCHART]{"title":"...","nodes":[{"id":"a","label":"Start"},{"id":"b","label":"Do"}],'
    '"edges":[{"from":"a","to":"b"}]}[/FLOWCHART]  (also give prose steps for "how to")'
)

# JSON shapes for tag mode only (native tools use schemas). Gated on action-shaped
# turns. Routing lives in CAPABILITY_INDEX so this stays a cheat-sheet.
ACTION_CATALOG = (
    "ACTION JSON — wrap each as [ACTION]{...}[/ACTION]. One block per asked task. "
    "Folders: desktop, documents, downloads, pictures, music, videos. "
    "qr_code.text = the user's exact link, never example.com.\n"
    '{"type":"open_app","target":"chrome"} · {"type":"open_app","target":"bluetooth settings"} · '
    '{"type":"close_app","target":"chrome"}\n'
    '{"type":"open_url","target":"https://github.com"} · {"type":"open_folder","target":"downloads"}\n'
    '{"type":"web_search","query":"..."}  — live answer, no window\n'
    '{"type":"system","command":"lock|screenshot|volume_up|volume_down|mute|play_pause|next|previous"}\n'
    '{"type":"volume","level":40} · {"type":"power","command":"shutdown|restart|cancel|sleep|hibernate|logoff|lock"}\n'
    '{"type":"delete_file","target":"C:/..."} · {"type":"time"} · {"type":"day"} · '
    '{"type":"ip_address"} · {"type":"location"} · {"type":"internet_speed"} · {"type":"screenshot"}\n'
    '{"type":"see_screen","question":"..."} · {"type":"qr_code","text":"<exact>"}\n'
    '{"type":"read_pdf","path":"..."} · {"type":"list_dir","path":"..."} · '
    '{"type":"read_file","path":"..."} · {"type":"text_to_pdf","title":"...","text":"..."}\n'
    '{"type":"record","media":"audio|video|screen","do":"start|stop"}  — until user says stop\n'
    '{"type":"schedule","do":"get|add|edit|remove|clear","day":"today","time":"09:00",'
    '"task":"...","match":"...","new_time":"...","new_task":"..."}\n'
    '{"type":"open_file","target":"qr_1234.png"} · {"type":"generate_image","prompt":"..."}\n'
    '{"type":"silence","seconds":60} · {"type":"sleep_mode"}\n'
    '{"type":"remember","text":"the user\'s sister is Mia"} · {"type":"forget","text":"sister|everything"}\n'
    '{"type":"clipboard"} · {"type":"routine","do":"add|list|remove","time":"08:00",'
    '"days":"weekdays","prompt":"...","match":"..."}\n'
    '{"type":"browser_task","goal":"open youtube and play lofi"}  — sole web action for that job\n'
    '{"type":"computer_task","goal":"in Notepad, type a packing list"}\n'
    '{"type":"browser","do":"open|search|scroll|back|read|close","url":"...","query":"...","site":"youtube","amount":600}\n'
    '{"type":"computer","do":"arm|disarm|click|type|press|scroll","target":"...","text":"...","keys":"ctrl+s"}\n'
    '{"type":"weather"} · {"type":"places","query":"restaurants"} · '
    '{"type":"directions","destination":"..."} · {"type":"news","topic":"world"}\n'
    '{"type":"reminder","when":"in 10 minutes","text":"call mum"} · {"type":"timer","when":"5 minutes"}\n'
    '{"type":"reminder","do":"list"} · {"type":"reminder","do":"remove","text":"call mum"}\n'
    '{"type":"playbook","do":"add","name":"movie night","triggers":["movie night"],"steps":"..."}\n'
    '{"type":"playbook","do":"list"} · {"type":"playbook","do":"remove","name":"movie night"}\n'
    '{"type":"screen","do":"set_theme|set_background|set_density|hide_panel|show_panel|'
    'toggle_panel|move_panel|set|reset","value":"amber|aurora|compact","panel":"weather",'
    '"direction":"up","theme":"red","background":"minimal","density":"compact"}\n'
    '{"type":"ui","do":"open_camera|close_camera|open_chat|close_chat|open_settings|'
    'close_settings|listen|stop_speaking|clear_chat|open_skills|close_skills|open_capabilities|'
    'open_power|close_power|open_memory|close_memory|open_terminal|close_terminal|open_activity|close_activity|'
    'expand_system|collapse_system|expand_power_panel|collapse_power_panel|expand_weather|'
    'collapse_weather|expand_network|collapse_network|expand_agenda|collapse_agenda|'
    'collapse_all|expand_all"}\n'
    '{"type":"app","app":"spotify","command":"play|pause|next|previous|current","query":"song"}\n'
    "read_file/list_dir: allowed folders only (Settings → Read-only Terminal); reads need approval."
)

# Full prompt (core + catalog) — kept for any caller that still expects one blob.
SYSTEM_PROMPT = SYSTEM_CORE + "\n\n" + ACTION_CATALOG

# How many prior turns (user+assistant pairs) to keep in memory.
_MAX_HISTORY_TURNS = _MAX_HISTORY_STORED
_history: list[dict] = []

# Token diet for Groq's rolling history. Gemini/Vertex gets a much larger window
# (see _history_profile) — no need to starve context when billing actual tokens.
_HISTORY_FULL_TURNS = _GROQ_FULL_TURNS        # default; gemini profile overrides
_HISTORY_CLIP_CHARS = _GROQ_CLIP_CHARS
_SLIM_HISTORY_TURNS = _SLIM_HISTORY_GROQ


def _history_profile(model: str = "", text: str = "") -> "tuple[int, int, int]":
    """(max_turns, full_verbatim_turns, clip_chars) for the active provider."""
    m = model or resolve_model(text)
    if _provider_for(m) == "gemini":
        return _GEMINI_HISTORY_TURNS, _GEMINI_FULL_TURNS, _GEMINI_CLIP_CHARS
    return _GROQ_HISTORY_TURNS, _GROQ_FULL_TURNS, _GROQ_CLIP_CHARS


def _slim_history_turns(model: str = "", text: str = "") -> int:
    m = model or resolve_model(text)
    return _SLIM_HISTORY_GEMINI if _provider_for(m) == "gemini" else _SLIM_HISTORY_GROQ


def _needs_action_catalog(text: str) -> bool:
    """Inject the heavy action reference only when the turn likely needs it."""
    t = (text or "").strip()
    if not t:
        return False
    if _ACTIONY_RE.search(t) or _BIG_TASK_RE.search(t):
        return True
    return bool(re.search(r"\b(chart|graph|table|flowchart|schedule|qr|screenshot|"
                          r"record|remind|routine|playbook|browser|open|close)\b",
                          t, re.IGNORECASE))


def _is_trivial_turn(text: str) -> bool:
    t = (text or "").strip()
    return len(t) <= 40 and bool(_TRIVIAL_TURN_RE.match(t))


def _is_deep_chat(text: str) -> bool:
    """Knowledge/reasoning turns — not PC commands."""
    t = (text or "").strip()
    if not t or _ACTIONY_RE.search(t):
        return False
    if "?" in t or len(t) > 140:
        return True
    return bool(_DEEP_CHAT_RE.search(t))


def _trim_to_char_budget(msgs: list, budget: int) -> list:
    """Drop oldest user+assistant pairs until ``budget`` chars remain.

    Keeps pairs so the prompt still alternates. Used only on Groq's TPM diet —
    Gemini sends the whole stored chat."""
    if budget <= 0 or not msgs:
        return msgs
    total = sum(len(m.get("content") or "") for m in msgs)
    out = list(msgs)
    while len(out) > 2 and total > budget:
        dropped = out.pop(0)
        total -= len(dropped.get("content") or "")
        if out and out[0].get("role") == "assistant":
            dropped = out.pop(0)
            total -= len(dropped.get("content") or "")
    return out


def _history_for_prompt(max_turns: "int | None" = None, model: str = "",
                        text: str = "") -> list:
    """History as sent to the model (stored history stays complete).

    Window is the whole stored chat unless ``max_turns`` trims an agentic
    follow-up. Switching models mid-chat must not hide earlier turns."""
    _cap, full_turns, clip = _history_profile(model, text)
    stored_turns = (len(_history) + 1) // 2
    turns = max_turns if max_turns is not None else min(stored_turns, _cap)
    msgs = _history[-turns * 2:] if turns else []
    keep_full = full_turns * 2
    out = []
    for i, m in enumerate(msgs):
        content = m.get("content") or ""
        if i < len(msgs) - keep_full and len(content) > clip:
            content = content[:clip].rstrip() + " …[trimmed]"
        out.append({"role": m.get("role"), "content": content})
    m = model or resolve_model(text)
    if max_turns is None and _provider_for(m) != "gemini":
        out = _trim_to_char_budget(out, _GROQ_HISTORY_CHAR_BUDGET)
    return out


def _max_tokens_for(text: str, slim: bool = False, model: str = "") -> int:
    """Per-request completion budget."""
    m = model or resolve_model(text)
    if _provider_for(m) == "gemini":
        if slim:
            return 1200
        if _is_deep_chat(text) or _BIG_TASK_RE.search(text or "") or len(text or "") > 400:
            return _GEMINI_MAX_COMPLETION_TOKENS
        return 4096
    if slim:
        return 600
    if _BIG_TASK_RE.search(text or "") or len(text or "") > 400:
        return _MAX_COMPLETION_TOKENS
    return 512

# Compact system prompt for AGENTIC FOLLOW-UP steps (the observe→act loop). These
# turns are mechanical — "given the result of the last action, emit the next one or
# finish" — so they don't need the full ~4k-token persona/feature catalogue. Using
# this slim version on every follow-up step (instead of the big SYSTEM_PROMPT) cuts
# the per-step token cost by ~75%, which is the main thing that was burning through
# Groq's per-minute token budget on multi-step browser tasks. The full prompt still
# governs the first turn of each user request; history carries the original goal.
_AGENTIC_SYSTEM_PROMPT = (
    "You are JARVIS, continuing a task you already started. You're mid-way through "
    "an observe→act loop: you'll be shown the RESULT of your last action(s), then "
    "decide the next step. Reason from the actual results shown — never invent "
    "success. If everything is done, confirm briefly and take NO further action; "
    "otherwise take ONLY the next step needed — CALL the matching function when you "
    "have tools, else emit ONLY the next [ACTION]{...}[/ACTION].\n"
    "\n"
    "For remaining web/desktop work, prefer one silent autopilot goal:\n"
    "  [ACTION]{\"type\":\"browser_task\",\"goal\":\"...\"}[/ACTION]\n"
    "  [ACTION]{\"type\":\"computer_task\",\"goal\":\"...\"}[/ACTION]\n"
    "Quick browser one-shots: {\"type\":\"browser\",\"do\":\"open|search|scroll|read|close\",...}. "
    "Other types use the same [ACTION]{\"type\":...} shape."
)


def _persist_history() -> None:
    """Write the current conversation to disk so it survives a restart."""
    memory_store.save_history(_history)

# A compact, frequently-refreshed snapshot of everything on the HUD (telemetry,
# weather, agenda, status). main.py pushes this in; it's injected as a system
# message so JARVIS is "aware" of the live screen state on every turn.
_live_context: str = ""


def set_live_context(text: str) -> None:
    global _live_context
    _live_context = (text or "").strip()


# A second, per-turn context channel: any playbook(s) relevant to THIS request
# (see playbooks.py). main.py sets it just before each turn; cleared otherwise.
_skill_context: str = ""


def set_skill_context(text: str) -> None:
    global _skill_context
    _skill_context = (text or "").strip()


# Default HUD appearance/layout. The user AND JARVIS can change this at runtime
# (see the `screen` action); it persists in config.json so the look survives a
# restart. `panels` is a visibility map; `order` is the per-rail arrangement.
DEFAULT_SCREEN = {
    "accent": "#00e5ff", "accent2": "#6fe9ff", "rgb": [0, 229, 255],
    "background": "grid", "density": "normal",
    "panels": {"system": True, "power": True, "agenda": True,
               "weather": True, "network": True, "terminal": True},
    "order": {"left": ["system", "power", "agenda"],
              "right": ["weather", "network", "terminal"]},
}

# Floating overlay pill: a tiny status widget that shows on OTHER apps (never on
# the JARVIS HUD itself). mode "except" = show everywhere except apps in `apps`;
# "only" = show ONLY on apps in `apps`; "all" = everywhere. Default: show
# everywhere with an empty hide-list.
DEFAULT_OVERLAY = {
    "enabled": True,
    "mode": "except",     # "except" | "only" | "all"
    "apps": [],           # process-name substrings to hide on (or show only on)
}

DEFAULT_CONFIG = {
    "model": DEFAULT_MODEL,
    "tts": "piper",          # offline neural British voice — free, unlimited
    "model_override": None,
    # Directories JARVIS's read-only terminal may list / read from. Empty by
    # default — the user adds folders in Settings.
    "allowed_dirs": [],
    # Where JARVIS saves everything it creates (empty until the user picks one
    # at first run; storage.py falls back to ~/Jarvis meanwhile).
    "storage_dir": "",
    # Always-on continuous voice (no wake word) + natural conversation mode.
    "always_on": False,
    "conversation_mode": False,
    # Proactive system alerts (CPU/RAM/battery spoken warnings). OFF by default —
    # many users find the CPU/RAM ones noisy; toggle on in Settings if wanted.
    "system_alerts": False,
    # Speech-to-text engine: "groq" = Groq Whisper Turbo (cloud, accurate, needs
    # the Groq key) with a local fallback; "local" = offline faster-whisper.
    "stt": "groq",
    # Vertex AI (Gemini on Google Cloud) — serves Gemini via the user's ADC login
    # so usage draws from their GCP project + trial credits instead of an API key.
    # When on AND ADC resolves, Gemini models route through Vertex (see vertex_auth).
    # vertex_project "" = use ADC's own project; vertex_region defaults to us-central1.
    "use_vertex": True,
    "vertex_project": "",
    "vertex_region": "us-central1",
    # Provider EDITION (Phase 4) — one app, a runtime Mode the user picks:
    #   "vertex"  → Gemini via ADC (GCP credits) + Groq fallback
    #   "gemini"  → Gemini Developer-API key + Groq fallback (NO Vertex)
    #   "offline" → a local Ollama model only; no cloud calls
    # "" (empty) = derive from the legacy use_vertex flag for back-compat, so an
    # existing install keeps its exact behaviour until the user picks a Mode.
    "provider_mode": "",
    # The local model used in offline mode ("" = auto-pick the first installed
    # Ollama model). Accepts "ollama:<name>", "local:<name>", or a bare model name.
    "offline_model": "",
    # Home-screen look & layout (user/JARVIS editable).
    "screen": dict(DEFAULT_SCREEN),
    # Floating overlay pill (shows on other apps).
    "overlay": dict(DEFAULT_OVERLAY),
    # The model that drives the silent browser/desktop autopilot steps (see
    # autopilot.py). Empty = auto (big Groq model when a Groq key exists, else
    # Gemini Flash-Lite). Accepts any Groq/Gemini model id, or a LOCAL model via
    # Ollama as "ollama:<name>" (e.g. "ollama:llama3.1") served at ollama_url.
    "autopilot_model": "",
    "ollama_url": "http://localhost:11434",
    # Native function/tool calling for the chat path (vs legacy [ACTION] tags).
    # Global kill-switch; per-model support is decided by tools_enabled().
    "native_tools": True,
    # A user-pinned location {"lat","lon","label"} that OVERRIDES browser-GPS / IP
    # location everywhere (weather, places, directions, the autopilot browser).
    # None = automatic. Set via Settings or the "set my location" voice command.
    "manual_location": None,
}


def native_tools_enabled() -> bool:
    return bool(_config.get("native_tools", True))


def set_native_tools(on: bool) -> bool:
    _config["native_tools"] = bool(on)
    _save_config(_config)
    return _config["native_tools"]


_PROVIDER_MODES = ("vertex", "gemini", "offline")


def provider_mode() -> str:
    """The active provider edition: 'vertex' | 'gemini' | 'offline'.

    An explicit ``provider_mode`` config value wins. Empty migrates from the
    legacy ``use_vertex`` flag (True → vertex, False → gemini) so existing installs
    behave EXACTLY as before until the user picks a Mode — 'offline' is never the
    automatic default, it must be chosen."""
    m = str(_config.get("provider_mode") or "").strip().lower()
    if m in _PROVIDER_MODES:
        return m
    return "vertex" if _config.get("use_vertex", True) else "gemini"


def set_provider_mode(mode: str) -> str:
    """Persist the provider edition. Also mirrors the legacy ``use_vertex`` flag so
    any code still reading it (and vertex_auth's back-compat path) stays consistent.
    Unknown values fall back to 'vertex'."""
    m = str(mode or "").strip().lower()
    if m not in _PROVIDER_MODES:
        m = "vertex"
    _config["provider_mode"] = m
    _config["use_vertex"] = (m == "vertex")
    _save_config(_config)
    return m


def set_offline_model(model: str) -> str:
    """Persist the local model used in offline mode ("" = auto-pick first installed)."""
    _config["offline_model"] = str(model or "").strip()
    _save_config(_config)
    return _config["offline_model"]


def set_ollama_url(url: str) -> str:
    """Persist the local Ollama server URL (offline chat + model discovery target)."""
    u = str(url or "").strip().rstrip("/")
    _config["ollama_url"] = u or "http://localhost:11434"
    _save_config(_config)
    return _config["ollama_url"]


def _is_local_model(model: str) -> bool:
    return (model or "").lower().startswith(("ollama:", "local:"))


def get_manual_location() -> "dict | None":
    """The user's pinned location override, or None when on automatic."""
    loc = _config.get("manual_location")
    return loc if isinstance(loc, dict) and loc.get("lat") is not None else None


def set_manual_location(loc: "dict | None") -> "dict | None":
    """Persist (or clear, with None) the pinned location override."""
    _config["manual_location"] = loc if isinstance(loc, dict) else None
    _save_config(_config)
    return _config["manual_location"]


def get_always_on() -> bool:
    return bool(_config.get("always_on"))


def set_always_on(on: bool) -> bool:
    _config["always_on"] = bool(on)
    _save_config(_config)
    return _config["always_on"]


def get_conversation_mode() -> bool:
    return bool(_config.get("conversation_mode"))


def set_conversation_mode(on: bool) -> bool:
    _config["conversation_mode"] = bool(on)
    _save_config(_config)
    return _config["conversation_mode"]


def get_system_alerts() -> bool:
    return bool(_config.get("system_alerts"))


def set_system_alerts(on: bool) -> bool:
    _config["system_alerts"] = bool(on)
    _save_config(_config)
    return _config["system_alerts"]


def get_stt() -> str:
    """Effective speech-to-text engine: 'groq' only if that's selected AND a Groq
    key exists; otherwise 'local' (offline faster-whisper)."""
    pref = (_config.get("stt") or "groq").lower()
    if pref == "groq" and _api_key:
        return "groq"
    return "local"


def set_stt(mode: str) -> str:
    _config["stt"] = "local" if str(mode).lower() == "local" else "groq"
    _save_config(_config)
    return _config["stt"]


async def transcribe_audio(wav_bytes: bytes) -> "str | None":
    """Transcribe spoken audio via Groq Whisper (whisper-large-v3-turbo).

    Returns the recognised text, or None on any failure (no key, network, bad
    response) so the caller can fall back to the local model. Never raises."""
    if not _api_key or not wav_bytes:
        return None
    try:
        # Reuse the shared keep-alive client: a fresh TCP+TLS handshake on EVERY
        # voice turn added a noticeable fixed delay before transcription started.
        resp = await _client().post(
            f"{GROQ_BASE_URL}/audio/transcriptions",
            headers={"Authorization": f"Bearer {_api_key}"},
            data={"model": STT_MODEL, "language": "en",
                  "response_format": "json", "temperature": "0"},
            files={"file": ("speech.wav", wav_bytes, "audio/wav")},
        )
        if resp.status_code != 200:
            print(f"[STT] Groq transcription HTTP {resp.status_code}: "
                  f"{resp.text[:200]}", flush=True)
            return None
        return (resp.json().get("text") or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[STT] Groq transcription failed: {exc}", flush=True)
        return None


def get_screen() -> dict:
    scr = _config.get("screen")
    if not isinstance(scr, dict):
        return dict(DEFAULT_SCREEN)
    # Fill any missing keys from defaults so older configs upgrade cleanly.
    merged = {**DEFAULT_SCREEN, **scr}
    merged["panels"] = {**DEFAULT_SCREEN["panels"], **(scr.get("panels") or {})}
    merged["order"] = {**DEFAULT_SCREEN["order"], **(scr.get("order") or {})}
    return merged


def set_screen(patch: dict) -> dict:
    """Apply a (possibly partial) screen-config patch and persist it. Understands
    `reset`, `move` (panel reorder), and nested `panels` maps. Returns the full
    resolved screen config so callers can broadcast it to every client."""
    scr = get_screen()
    if not isinstance(patch, dict):
        return scr
    if patch.get("reset"):
        scr = dict(DEFAULT_SCREEN)
        scr["panels"] = dict(DEFAULT_SCREEN["panels"])
        scr["order"] = {k: list(v) for k, v in DEFAULT_SCREEN["order"].items()}
        _config["screen"] = scr
        _save_config(_config)
        return scr
    mv = patch.get("move")
    if isinstance(mv, dict):
        _apply_move(scr, mv.get("panel"), mv.get("direction"))
    for k in ("accent", "accent2", "rgb", "background", "density"):
        if k in patch:
            scr[k] = patch[k]
    if isinstance(patch.get("panels"), dict):
        cur = dict(scr.get("panels", {}))
        for name, v in patch["panels"].items():
            # "toggle" flips the panel's CURRENT visibility (the screen action's
            # toggle_panel verb can't know the state, so it's resolved here).
            cur[name] = (not cur.get(name, True)) if v == "toggle" else bool(v)
        scr["panels"] = cur
    if isinstance(patch.get("order"), dict):
        scr["order"] = {**scr.get("order", {}), **patch["order"]}
    _config["screen"] = scr
    _save_config(_config)
    return scr


def get_overlay() -> dict:
    o = _config.get("overlay")
    if not isinstance(o, dict):
        return dict(DEFAULT_OVERLAY)
    merged = {**DEFAULT_OVERLAY, **o}
    if merged.get("mode") not in ("except", "only", "all"):
        merged["mode"] = "except"
    if not isinstance(merged.get("apps"), list):
        merged["apps"] = []
    return merged


def set_overlay(patch: dict) -> dict:
    """Apply a (partial) overlay-pill config patch and persist it. Returns the full
    resolved config so callers can broadcast it."""
    o = get_overlay()
    if isinstance(patch, dict):
        if "enabled" in patch:
            o["enabled"] = bool(patch["enabled"])
        if patch.get("mode") in ("except", "only", "all"):
            o["mode"] = patch["mode"]
        if isinstance(patch.get("apps"), list):
            o["apps"] = [str(a).strip().lower() for a in patch["apps"] if str(a).strip()]
    _config["overlay"] = o
    _save_config(_config)
    return o


def _apply_move(scr: dict, panel: str, direction: str) -> None:
    """Reorder/relocate a panel within or across the two rails."""
    if not panel or not direction:
        return
    order = {k: list(v) for k, v in (scr.get("order") or {}).items()}
    left, right = order.get("left", []), order.get("right", [])
    rail = "left" if panel in left else ("right" if panel in right else None)
    if rail is None:
        return
    arr = order[rail]
    i = arr.index(panel)
    if direction in ("up", "down"):
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(arr):
            arr[i], arr[j] = arr[j], arr[i]
    elif direction in ("left", "right"):
        dest = "left" if direction == "left" else "right"
        if dest != rail:
            arr.pop(i)
            order[dest].append(panel)
    scr["order"] = order

RAM_CHECK_INTERVAL = 300   # 5 minutes in seconds
LOW_RAM_THRESHOLD_MB = 1024

_selected_model: str = DEFAULT_MODEL
_api_key: str = ""             # Groq key — the FIRST one; see _groq_keys
_gemini_key: str = ""          # Gemini / Google key — the first one
_config: dict = {}

# Free-tier quota is metered per key, so the user may paste several. Storage is
# unchanged (still one string per secret); app_secrets.get_list splits it. The
# singles above remain the "do we have a credential at all" gates that _key_for
# and the STT / vision-OCR callers already depend on.
_groq_keys: "list[str]" = []
_gemini_keys: "list[str]" = []
_extra_keys: "dict[str, list[str]]" = {p: [] for p in PREFIXED_PROVIDERS}


def _load_keys() -> None:
    """Refresh every key list (and the legacy singles) from the secret store."""
    global _api_key, _gemini_key, _groq_keys, _gemini_keys
    _groq_keys = app_secrets.get_list("groq_api_key")
    _gemini_keys = app_secrets.get_list("gemini_api_key")
    for p in PREFIXED_PROVIDERS:
        _extra_keys[p] = app_secrets.get_list(f"{p}_api_key")
    _api_key = _groq_keys[0] if _groq_keys else ""
    _gemini_key = _gemini_keys[0] if _gemini_keys else ""


# ── Provider routing ──────────────────────────────────────────────────────────

def _provider_for(model: str) -> str:
    if _is_local_model(model):
        return "ollama"
    prefixed, _ = split_model(model)
    if prefixed:
        return prefixed
    return "gemini" if gemini_bridge.is_gemini_model(model) else "groq"


# Models that mishandle OpenAI-style tool calling (kept tiny; add ids here if a
# Groq model is found to choke on the `tools` param).
_NO_TOOLS_MODELS: set = set()


def tools_enabled(model: str) -> bool:
    """Whether to drive this model with NATIVE function/tool calling rather than
    the legacy ``[ACTION]`` text tags. The seam Phase-4 ``provider_mode`` plugs
    into. Defaults ON; the ``native_tools`` config flag is a global kill-switch.

      • gemini / gemma  → True (functionDeclarations over generateContent)
      • groq            → True except a small deny-list
      • ollama:/local:/unknown → False (no reliable tool support → tag fallback)
    """
    if not _config.get("native_tools", True):
        return False
    m = (model or "").lower()
    if m.startswith(("ollama:", "local:")):
        return False
    prov = _provider_for(model)
    if prov == "gemini":
        return not gemini_bridge.is_native_audio_model(model) and model_ranker.tools_ok(model)
    if prov == "groq":
        return m not in _NO_TOOLS_MODELS and model_ranker.tools_ok(model)
    if prov in PREFIXED_PROVIDERS:
        return model_ranker.tools_ok(model)
    return False


def _is_big_task(text: str) -> bool:
    """Whether this turn warrants the heavyweight model (long input, lots of
    history, a rich-output / analysis / file request, or a COMMAND turn)."""
    t = (text or "").strip()
    if len(t) > 220:
        return True
    if len(_history) >= 10:
        return True
    if _is_deep_chat(t):
        return True
    if _ACTIONY_RE.search(t):
        return True
    return bool(_BIG_TASK_RE.search(t))


# Explicit planning / depth cues that signal a genuinely hard turn (beyond a bare
# keyword). Kept separate from _DEEP_CHAT_RE/_BIG_TASK_RE so they STACK in the score.
_COMPLEX_CUE_RE = re.compile(
    r"\b(plan|strateg(?:y|ise|ize)|design|architect|research|in\s+detail|"
    r"step[\s-]by[\s-]step|thoroughly|comprehensive|trade[\s-]?offs?|figure\s+out|"
    r"work\s+out|optim(?:ise|ize)|troubleshoot|deep\s+dive|brainstorm|outline|"
    r"pros\s+and\s+cons|implications?|in[\s-]depth|evaluate|assess)\b",
    re.IGNORECASE,
)
# Conjunctions that join sub-tasks in one request (local copy; main has its own).
_TASK_CLAUSE_RE = re.compile(
    r"\b(?:and(?:\s+then)?|then|also|plus|after\s+that|afterwards)\b|[;,]",
    re.IGNORECASE,
)

# Score at/above which a turn earns the flagship model (else it gets mid). Tuned so
# everyday commands + simple Q&A stay on mid; only genuinely hard turns escalate.
_FLAGSHIP_COMPLEXITY = 4


def _task_complexity(text: str) -> int:
    """A cheap, LOCAL score (no model call) of how hard this turn is, used to pick
    the model tier under 'auto'. Stacks several weak signals — length, multi-step
    structure, reasoning depth, rich-output/file/code, explicit planning cues,
    distinct-action count and ongoing context. Trivial greetings score 0; a deep
    multi-step research request scores high. This replaces the old binary
    _is_big_task (every command word forced the flagship)."""
    t = (text or "").strip()
    if not t or _is_trivial_turn(t):
        return 0
    score = 0
    words = len(t.split())
    score += 3 if words > 60 else 2 if words > 30 else 1 if words > 15 else 0
    clauses = [c for c in _TASK_CLAUSE_RE.split(t) if c and c.strip()]
    score += 2 if len(clauses) >= 3 else 1 if len(clauses) == 2 else 0
    if _DEEP_CHAT_RE.search(t):       # reasoning words (why/how/compare…), NOT a bare '?'
        score += 2
    if _BIG_TASK_RE.search(t):        # chart/code/essay/document/analyse…
        score += 1
    if _COMPLEX_CUE_RE.search(t):     # explicit planning / depth cues
        score += 2
    if len({m.lower() for m in _ACTIONY_RE.findall(t)}) >= 3:   # many distinct actions
        score += 1
    if len(_history) >= 12:           # deep into a long / agentic exchange
        score += 1
    return score


def _offline_model() -> str:
    """The local model used in offline mode: the explicit ``offline_model`` config,
    else the selected model if it's already local, else the first installed Ollama
    model from discovery, else a sensible default. Always returns an ``ollama:`` id."""
    m = str(_config.get("offline_model") or "").strip()
    if m:
        return m if _is_local_model(m) else f"ollama:{m}"
    if _is_local_model(_selected_model):
        return _selected_model
    return model_discovery.first_ollama() or "ollama:llama3.1"


def _task_tier_model(tier: str) -> str:
    """Pick an available model for a user-selected chat tier.

    The provider selected in Settings is preferred when it can satisfy the tier;
    the tier is per-message and never changes that persistent setting.
    """
    tier = model_discovery.normalize_task_tier(tier)
    selected_provider = _provider_for(_selected_model)
    providers = []
    if (selected_provider == "gemini" and (vertex_auth.enabled() or _gemini_key)) \
            or (selected_provider == "groq" and _api_key):
        providers.append(selected_provider)
    if vertex_auth.enabled() or _gemini_key:
        providers.append("gemini")
    if _api_key:
        providers.append("groq")
    providers = list(dict.fromkeys(providers))

    available = set(model_discovery.available_model_ids())
    for provider in providers:
        preferred = _TASK_TIER_DEFAULTS[tier][provider]
        for model in preferred:
            if not available or model in available:
                return model
        discovered = [model for model in model_discovery.available_model_ids()
                      if _provider_for(model) == provider
                      and model_discovery.task_tier_for_model(model) == tier]
        if discovered:
            return discovered[0]
        # Some providers may not expose all three capability bands. In that case
        # use an actually available model from the requested provider instead of
        # sending a request to a guessed, unavailable model id.
        provider_models = [model for model in model_discovery.available_model_ids()
                           if _provider_for(model) == provider]
        if provider_models:
            return provider_models[0]

    # Discovery may not have completed during startup. Use deterministic defaults
    # so the normal route ladder can still recover from an unavailable candidate.
    if providers:
        return _TASK_TIER_DEFAULTS[tier][providers[0]][0]
    return _TASK_TIER_DEFAULTS[tier]["gemini"][0]


# ── Smart routing (model_ranker) ─────────────────────────────────────────────
# Once a ranking exists, "auto" and the per-message tier stop using the hardcoded
# per-provider tiers above: the turn's difficulty picks a tier, and the ranked
# models that are good enough for it are tried weakest-sufficient tier first.
_TASK_TIER_TO_RANK = {"dumb": "fast", "moderate": "mid", "very_smart": "flagship"}


def _needed_tier(text: str = "", task_tier: str = "") -> str:
    if task_tier:
        return _TASK_TIER_TO_RANK[model_discovery.normalize_task_tier(task_tier)]
    if _is_trivial_turn(text):
        return "fast"
    return "flagship" if _task_complexity(text) >= _FLAGSHIP_COMPLEXITY else "mid"


def _ranked_ladder(text: str = "", task_tier: str = "") -> "list[str]":
    """Ranked models that can serve this turn, in try-order ([] = no ranking yet).
    Non-trivial turns carry the tool palette, so only tool-capable models qualify."""
    if not model_ranker.has_ranking():
        return []
    available = {m for m in model_discovery.available_model_ids()
                 if _keys_for(_provider_for(m))}
    return model_ranker.ladder(_needed_tier(text, task_tier),
                               needs_tools=not _is_trivial_turn(text),
                               available=available)


def resolve_model(text: str = "", task_tier: str = "") -> str:
    """The concrete model id to use for this turn.

    In OFFLINE mode every turn routes to a local Ollama model (no cloud calls).
    Otherwise returns the selected model unchanged unless it's "auto", in which case
    we pick a tier by task COMPLEXITY: trivial greetings → fast; everyday commands &
    Q&A → mid; genuinely complex / multi-step / deep-reasoning turns → flagship.
    Native tools (Phase 1) make even simple commands reliable on the mid tier, so a
    lone command no longer forces the flagship the way the old binary heuristic did."""
    if provider_mode() == "offline":
        return _offline_model()
    if task_tier or (_selected_model or "").lower() == AUTO_MODEL:
        ranked = _ranked_ladder(text, task_tier)
        if ranked:
            # The first model with a route that isn't benched right now.
            for m in ranked:
                if any(quota.usable(quota.Route(_provider_for(m), m, i))
                       for i in range(len(_keys_for(_provider_for(m))))):
                    return m
            return ranked[0]
    if task_tier:
        return _task_tier_model(task_tier)
    if (_selected_model or "").lower() != AUTO_MODEL:
        return _selected_model
    gemini = vertex_auth.enabled() or bool(_gemini_key)
    # Trivial greetings/acks: the cheapest, fastest model (no tools attached).
    if _is_trivial_turn(text):
        if gemini:
            return _AUTO_FAST_GEMINI
        return _AUTO_FAST_GROQ if _api_key else _AUTO_FAST_GEMINI
    flagship = _task_complexity(text) >= _FLAGSHIP_COMPLEXITY
    if gemini:
        return _AUTO_BIG_GEMINI if flagship else _AUTO_MID_GEMINI
    if _api_key:
        return _AUTO_BIG_GROQ if flagship else _AUTO_MID_GROQ
    return _AUTO_BIG_GEMINI if flagship else _AUTO_MID_GEMINI


def routing_note(text: str = "", task_tier: str = "") -> str:
    """One-line routing decision for logs: the model + why it was chosen."""
    m = resolve_model(text, task_tier)
    if provider_mode() == "offline":     # offline mode routes to the local model, not pinned/complexity
        return f"{m} · offline"
    if ((task_tier or (_selected_model or "").lower() == AUTO_MODEL)
            and model_ranker.has_ranking()):
        return f"{m} · needs {_needed_tier(text, task_tier)} (ranked)"
    if task_tier:
        return f"{m} · {model_discovery.normalize_task_tier(task_tier)} tier"
    if (_selected_model or "").lower() != AUTO_MODEL:
        return f"{m} (pinned)"
    if _is_trivial_turn(text):
        return f"{m} · trivial"
    return f"{m} · complexity {_task_complexity(text)}"


# ── Route ladder ──────────────────────────────────────────────────────────────
# resolve_model() decides what we'd LIKE to answer with. The ladder decides what
# we actually try when that first choice is unavailable — and, crucially, remembers
# across turns (llm/quota.py) which routes are already spent so a free-tier user
# stops paying a 429 to rediscover the same dead route every single turn.
#
# A route is (provider, model, key index). Free quota is metered per key, so a
# user who pastes three Groq keys gets three times the headroom for free.

class RouteError(Exception):
    """One route failed. Carries the raw facts the bench decision needs."""

    def __init__(self, message: str, status: int = 0,
                 retry_after: "float | None" = None, detail: str = ""):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.detail = detail or message


# Wording that means "you are out of quota" when the provider sent no usable
# status (Gemini's transport-level errors, Ollama's plain-text refusals).
_QUOTA_WORDS_RE = re.compile(
    r"rate limit|too many requests|quota|resource_exhausted|out of tokens", re.I)

# Statuses that will not fix themselves within a normal cooldown, so the route is
# parked for a day rather than re-tried every couple of minutes. A key the user
# corrects in Settings clears these immediately (set_api_keys → quota.clear).
_FATAL_STATUSES = (401, 403, 404)


def _keys_for(provider: str) -> "list[str]":
    """Credentials that can serve this provider, in the order the user entered them.

    Vertex ADC is keyless — when it's the only Google path, one empty-string entry
    represents it (gemini_bridge treats "" as "use ADC"), so the ladder can carry a
    Vertex route without special-casing it everywhere.
    """
    if provider == "gemini":
        if _gemini_keys:
            return list(_gemini_keys)
        return [""] if vertex_auth.enabled() else []
    if provider == "groq":
        return list(_groq_keys)
    if provider in PREFIXED_PROVIDERS:
        return list(_extra_keys[provider])
    if provider == "ollama":
        return [""]
    return []


def _credential(route: "quota.Route") -> str:
    keys = _keys_for(route.provider)
    return keys[route.key_index] if 0 <= route.key_index < len(keys) else ""


def _model_ladder(text: str = "", pinned: str = "", task_tier: str = "") -> "list[str]":
    """Models to try for this turn, best first.

    Head: ``pinned`` when a caller already knows the model it wants (the autopilot
    passes its own), else whatever resolve_model() picked. Tail: the smart-routing
    ranking when one exists (good-enough tier first, then stronger, then weaker —
    across every provider); before the first ranking, the legacy degradation — the
    SAME provider's other tiers, then the other provider's. A local model closes
    the list when one is installed. Answering on a smaller model beats telling the
    user to come back later.
    """
    # ``pinned`` may be several models: a caller's own preference order (the
    # autopilot's fast or vision lane), tried before the general ladder.
    heads = [p.strip() for p in ((pinned,) if isinstance(pinned, str) else (pinned or ()))
             if p and p.strip()]
    primary = heads[0] if heads else resolve_model(text, task_tier)
    if provider_mode() == "offline":
        return [primary]

    ranked = _ranked_ladder(text, task_tier)
    if ranked:
        chain = [*(heads or [primary]), *ranked]
    else:
        lead = heads or [primary]
        if task_tier:
            tier = model_discovery.normalize_task_tier(task_tier)
            gemini_tier = list(_TASK_TIER_DEFAULTS[tier]["gemini"])
            groq_tier = list(_TASK_TIER_DEFAULTS[tier]["groq"])
        else:
            flagship = _task_complexity(text) >= _FLAGSHIP_COMPLEXITY
            gemini_tier = [_AUTO_BIG_GEMINI if flagship else _AUTO_MID_GEMINI, _AUTO_FAST_GEMINI]
            groq_tier = [_AUTO_BIG_GROQ if flagship else _AUTO_MID_GROQ, _AUTO_FAST_GROQ]
        if _provider_for(lead[-1]) == "groq":
            chain = [*lead, *groq_tier, *gemini_tier]
        else:
            chain = [*lead, *gemini_tier, *groq_tier]

    # Cache-only lookup (no network) — "" when no local server was ever reachable,
    # which keeps a phantom Ollama route out of the ladder.
    local = model_discovery.first_ollama()
    if local:
        chain.append(local)

    ordered: "list[str]" = []
    for model in map(quota.live_model, chain):
        if model and model not in ordered:
            ordered.append(model)
    return ordered


def routes_for(text: str = "", pinned: str = "", task_tier: str = "") -> "list[quota.Route]":
    """Every route worth trying for this turn, best first — before quota filtering."""
    routes: "list[quota.Route]" = []
    for model in _model_ladder(text, pinned, task_tier):
        provider = _provider_for(model)
        for index in range(len(_keys_for(provider))):
            routes.append(quota.Route(provider, model, index))
    return routes


def usable_routes(text: str = "", pinned: str = "", task_tier: str = "") -> "list[quota.Route]":
    """routes_for() minus what we already know is spent. Empty means genuinely
    exhausted — that, and only that, is when the user hears about a rate limit."""
    return [r for r in routes_for(text, pinned, task_tier) if quota.usable(r)]


# Longest a turn waits for a briefly-benched route before calling it spent.
_SHORT_WAIT_S = 15.0


def route_wait_s(model: str = "") -> float:
    """Seconds until the soonest route for ``model`` (default: the autopilot's) can
    be tried again — 0 when one is usable now or none is configured. Lets a long
    task pace itself through a per-minute limit instead of dying mid-way."""
    pinned = (model or get_autopilot_model()).strip()
    return min((quota.ready_in(r) for r in routes_for(pinned=pinned)), default=0.0)


async def _walk_routes(text: str = "", pinned: str = "", task_tier: str = "",
                       skip: "set[str] | None" = None):
    """Yield routes to try, best first, skipping models in ``skip`` (the caller adds
    a model whose failure was model-level).

    A per-minute limit benches its route for seconds, so when every route is out
    only BRIEFLY the right answer is one short wait — not "I've used up the free
    quota" over a ceiling that resets in 8s. At most one wait (<=15s), then one more
    pass; a route that failed without a bench (ready now) never earns a wait.
    """
    skip = set() if skip is None else skip
    for attempt in range(2):
        for route in usable_routes(text, pinned, task_tier):
            if route.model not in skip:
                yield route
        if attempt:
            return
        wait = min((quota.ready_in(r) for r in routes_for(text, pinned, task_tier)
                    if r.model not in skip), default=0.0)
        if not 0 < wait <= _SHORT_WAIT_S:
            return
        print(f"[Route] every route briefly rate-limited; waiting {wait:.1f}s", flush=True)
        await asyncio.sleep(wait + 0.25)


def _bench_route(route: "quota.Route", status: int = 0, message: str = "",
                 retry_after: "float | None" = None) -> bool:
    """Take a failed route out of rotation. Returns True when the failure is
    MODEL-level (404) — every key would fail this model identically, so the caller
    skips its siblings instead of burning one attempt per key on a dead model."""
    quota.learn_limit(route, message)

    # Google names the replacement in a retired model's 404. Record it so the
    # ladder calls the live id from the next turn on, instead of the whole Gemini
    # tier parking for a day and piling every request onto Groq's TPM ceiling.
    replacement = quota.retired_replacement(status, message)
    if replacement:
        quota.record_remap(route.model, replacement)
        print(f"[Route] {route.model} is retired -> using {replacement} from now on",
              flush=True)

    fatal = status in _FATAL_STATUSES
    # 413 is Groq's "request too large ... on tokens per minute (TPM): Limit 30000"
    # — a real quota ceiling, not a malformed request, so it feeds the ladder.
    is_quota = status in (429, 413) or (not status and bool(_QUOTA_WORDS_RE.search(message)))

    if fatal:
        held = quota.bench(route, duration=quota.DAY, quota_signal=False)
    else:
        held = quota.bench(route, retry_after or quota.retry_after_in(message),
                           quota_signal=is_quota, window=quota.limit_window(message))

    print(f"[Route] {route.provider}/{route.model}#{route.key_index} benched "
          f"{quota.format_eta(held)} (HTTP {status or '—'})", flush=True)
    return status == 404


def _exhausted(text: str = "") -> str:
    """What JARVIS says when every route really is spent — with a real time, not
    'give me a moment'. Reached only after the whole ladder has been walked."""
    return quota.exhausted_message()


def active_provider() -> str:
    return _provider_for(resolve_model())


def get_model() -> str:
    return _selected_model


def model_leaks_reasoning(text: str = "", task_tier: str = "") -> bool:
    """True for open instruct models with no hidden-reasoning channel that tend to
    dump their chain-of-thought into the visible reply — Gemma, and the fast
    Llama-3.1-8B. Used to enable the reasoning-stripping sanitizer + non-streamed
    display for just those models. Resolves "auto" for the given turn first."""
    m = resolve_model(text, task_tier).lower()
    return "gemma" in m or "llama-3.1-8b" in m


def reply_is_quote_wrapped(text: str = "", task_tier: str = "") -> bool:
    """True when the effective model is Gemma, whose final reply we treat as the
    quoted phrase only (strip everything outside the quotes)."""
    return "gemma" in resolve_model(text, task_tier).lower()


# Imperative/command phrasing → the turn likely needs precise [ACTION]/CHART JSON,
# where a low temperature drops far fewer of multiple tasks and malforms less.
# Ordinary conversation keeps some warmth. Cheap word check, run per turn.
_ACTIONY_RE = re.compile(
    r"\b(open|close|launch|start|stop|play|pause|search|google|set|turn|mute|"
    r"unmute|volume|screenshot|capture|record|remind|reminder|timer|schedule|"
    r"shut\s*down|shutdown|restart|reboot|sleep|lock|delete|remember|forget|click|"
    r"type|scroll|browse|browser|navigate|go\s+to|show|hide|expand|collapse|"
    r"clear|wipe|empty|reset|watch|find|look\s+up|check|visit|buy|order|book|"
    r"generate|create|make|draw|read|list|directions|news|weather|nearby|"
    # common imperatives the original list missed — 'change the HUD to red',
    # 'switch the theme', 'recolour', 'rename', 'enable', 'increase the volume'…
    r"change|switch|adjust|recolou?r|colou?r|rename|move|swap|increase|decrease|"
    r"raise|lower|enable|disable|toggle|customi[sz]e|dim|brighten|resize|"
    r"rearrange|update|edit|apply)\b",
    re.IGNORECASE,
)
_ACTION_TEMPERATURE = 0.0
_CHAT_TEMPERATURE = 0.75
_DEEP_CHAT_TEMPERATURE = 0.85


def _temperature_for(text: str) -> float:
    """Lower temperature for command-like turns; slightly higher for open reasoning."""
    if _ACTIONY_RE.search(text or ""):
        return _ACTION_TEMPERATURE
    if _is_deep_chat(text):
        return _DEEP_CHAT_TEMPERATURE
    return _CHAT_TEMPERATURE


def _gemini_thinking_budget(model: str, text: str = "") -> "Optional[int]":
    """Per-turn thinkingBudget for Gemini flash models.

    Action/command turns stay at 0 for speed and tight JSON. Deep Q&A turns get
  room to reason internally (like the Gemini app) without leaking chain-of-thought."""
    m = (model or "").lower()
    if "lite" in m:
        return None
    if "flash" not in m:
        return None
    if _ACTIONY_RE.search(text or ""):
        return 0
    if _is_deep_chat(text):
        return 2048
    return 0


# ── Config helpers ────────────────────────────────────────────────────────────

def _load_config() -> dict:
    jarvis_paths.migrate_config_from_repo()
    if CONFIG_PATH.exists():
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return {**DEFAULT_CONFIG, **data}
        except Exception as exc:  # noqa: BLE001
            print(f"[Config] Couldn't read {CONFIG_PATH}: {exc}", flush=True)
    return dict(DEFAULT_CONFIG)


def _save_config(cfg: dict):
    try:
        tmp = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        os.replace(tmp, CONFIG_PATH)
    except Exception as exc:  # noqa: BLE001
        print(f"[Config] Couldn't write {CONFIG_PATH}: {exc}", flush=True)


# ── System resource detection (Whisper + TTS still run locally) ───────────────

def _get_free_ram_mb() -> int:
    # psutil is already a hard dependency (telemetry uses it) and is a fast,
    # cross-platform, subprocess-free read — prefer it. Fall back to the shell
    # probes below only if it's somehow unavailable.
    try:
        import psutil
        return psutil.virtual_memory().available // (1024 * 1024)
    except Exception:  # noqa: BLE001
        pass

    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.run(
                ["powershell", "-Command",
                 "(Get-CimInstance Win32_OperatingSystem).FreePhysicalMemory / 1024"],
                capture_output=True, text=True, timeout=10,
            )
            return int(float(result.stdout.strip()))
        elif system in ("Linux", "Darwin"):
            result = subprocess.run(
                ["sh", "-c", "free -m | awk '/^Mem:/ {print $7}'"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                return int(result.stdout.strip())
    except Exception as exc:
        print(f"[RAM] Detection failed: {exc}")
    return 4096  # safe fallback


# ── Vertex startup pre-warm ───────────────────────────────────────────────────

async def _prewarm_vertex() -> None:
    """Best-effort, at startup: mint the ADC bearer token and open the keep-alive
    TLS connection to the Vertex global endpoint, so the FIRST user turn doesn't
    pay the one-time cold start (token refresh + handshake — a second or two).
    Runs as a background task; quietly no-ops when Vertex is off or the network is
    down (the small connect timeout caps any wait)."""
    try:
        if not vertex_auth.enabled():
            return
        await vertex_auth.get_access_token_async()
        # A 1-token throwaway generate opens the pooled connection + validates the
        # path; cost is negligible. Failures (offline, etc.) are swallowed below.
        await gemini_bridge.gemini_send(
            [{"role": "user", "content": "hi"}],
            _AUTO_FAST_GEMINI, _gemini_key, max_tokens=1, temperature=0)
        print("[Vertex] Pre-warmed (token + connection ready).", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[Vertex] Pre-warm skipped ({exc}).", flush=True)


# ── Background RAM monitor ────────────────────────────────────────────────────

async def _ram_monitor_loop():
    while True:
        await asyncio.sleep(RAM_CHECK_INTERVAL)
        free_mb = _get_free_ram_mb()
        if free_mb < LOW_RAM_THRESHOLD_MB:
            msg = f"Low system RAM: only {free_mb} MB available. Performance may be degraded."
            print(f"\n[Warning] {msg}")
            await emit("warning", msg)


# ── Public API ────────────────────────────────────────────────────────────────

async def initialize():
    global _selected_model, _api_key, _gemini_key, _config

    # Lift any keys still hard-coded in legacy files into app-data secrets.json,
    # then load config (now key-free).
    app_secrets.migrate_from_config()
    _config = _load_config()

    # Keys come from the secret store (env -> app-data secrets.json). Never log them.
    _load_keys()

    # Restore which routes are still benched from last session — a 24h daily-quota
    # bench that a restart forgets would re-hammer a dead key on the first turn.
    quota.load()

    _selected_model = (
        _config.get("model_override")
        or _config.get("model")
        or DEFAULT_MODEL
    )

    # Restore last session's conversation so JARVIS picks up where it left off.
    try:
        saved = memory_store.load_history()
        if saved:
            _history.extend(saved[-_MAX_HISTORY_TURNS * 2:])
            print(f"[Memory] Restored {len(_history)} messages from last session.")
    except Exception as exc:  # noqa: BLE001
        print(f"[Memory] couldn't restore history: {exc}")

    if not has_llm_credentials():
        await emit("warning", "No API keys configured yet — add your Groq and/or "
                              "Gemini key in the setup screen or Settings.")
        print("[LLM] WARNING: no API keys — requests will fail until one is added.")
    elif vertex_auth.enabled():
        print(f"[Vertex] ADC active — project: {vertex_auth.project() or '(quota project)'}")

    # Best-effort connectivity / key check so we fail loud at startup, not on
    # the first user prompt.
    if _api_key:
        try:
            model_to_check = resolve_model()
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{GROQ_BASE_URL}/models",
                    headers={"Authorization": f"Bearer {_api_key}"},
                )
                if resp.status_code == 200:
                    ids = [m["id"] for m in resp.json().get("data", [])]
                    if _provider_for(model_to_check) == "groq" and model_to_check not in ids:
                        print(f"[Groq] WARNING: model '{model_to_check}' not in "
                              f"this plan's list; using it anyway.")
                    print(f"[Groq] Connected.")
                elif resp.status_code in (401, 403):
                    await emit("warning", "Groq API key rejected (401/403). Check the key.")
                    print(f"[Groq] Auth failed: {resp.status_code}")
                else:
                    print(f"[Groq] Models check returned {resp.status_code}.")
        except Exception as exc:
            print(f"[Groq] Could not reach Groq at startup: {exc}")

    asyncio.create_task(_ram_monitor_loop())
    # Warm the Vertex token + connection in the background so the first turn is
    # snappy (no startup blocking — offline just no-ops after a short timeout).
    asyncio.create_task(_prewarm_vertex())
    print(f"[Groq] Bridge ready  —  model: {_selected_model}")


def reset_history() -> None:
    """Forget the conversation so far (new topic / 'start over').

    Clears the persisted transcript too, but NOT long-term facts — "start over"
    means a fresh chat, not amnesia about who the user is."""
    _history.clear()
    memory_store.clear_history()


def set_history(messages: "list | None") -> None:
    """Replace the active conversation with a loaded one (reopening an archived
    chat from the Recents list). Keeps only well-formed turns, clipped to the
    stored cap, and persists immediately so a restart resumes the reopened chat."""
    cleaned = [m for m in (messages or [])
               if isinstance(m, dict) and m.get("role") and "content" in m]
    _history[:] = cleaned[-_MAX_HISTORY_TURNS * 2:]
    _persist_history()


# ── Long-term user facts (delegates to memory_store; keeps this the hub) ──────

def remember_fact(text: str) -> "tuple[bool, str]":
    return memory_store.add_fact(text)


def forget_fact(query: str) -> "tuple[bool, str]":
    return memory_store.forget_fact(query)


def list_facts() -> list:
    return memory_store.all_facts()


def get_history() -> list:
    """A copy of the in-memory conversation (for restoring the visible HUD log)."""
    return list(_history)


def remember_turn_outcome(user_text: str, outcome: str) -> None:
    """Record THIS turn's substantive spoken outcome (a web-search answer, a
    nearby-places list, a weather reading…) into conversation history, so later
    turns keep context.

    Why this is needed: ``stream_prompt`` only logs a turn when the model produced
    visible TEXT. A native tool-only turn (the model just calls web_search/find_
    places with no preamble) records NOTHING — so a follow-up like "find one close
    to me" had no idea the topic was croissants. State the model can re-read live
    (panel visibility, theme) lives in LIVE STATE and isn't recorded here; only
    transient lookup ANSWERS are.

    Idempotent w.r.t. stream_prompt's own logging: if this turn's user+assistant
    pair is already at the tail, the outcome is appended to that assistant message;
    otherwise the user turn + an assistant turn carrying the answer are added."""
    user_text = (user_text or "").strip()
    outcome = (outcome or "").strip()
    if not outcome:
        return
    if len(outcome) > 600:
        outcome = outcome[:597].rstrip() + "…"
    h = _history
    if (len(h) >= 2 and h[-1].get("role") == "assistant"
            and h[-2].get("role") == "user"
            and (h[-2].get("content") or "").strip() == user_text):
        # stream_prompt already logged this turn (model spoke a preamble) — fold the
        # real answer into it so history reflects what was actually conveyed.
        if outcome not in h[-1]["content"]:
            h[-1]["content"] = (h[-1]["content"].rstrip() + "\n\n" + outcome).strip()
    elif h and h[-1].get("role") == "user" and (h[-1].get("content") or "").strip() == user_text:
        h.append({"role": "assistant", "content": outcome})
    else:
        if user_text:
            h.append({"role": "user", "content": user_text})
        h.append({"role": "assistant", "content": outcome})
    del h[: max(0, len(h) - _MAX_HISTORY_TURNS * 2)]
    _persist_history()


def note_outcome(text: str) -> None:
    """Record what an action actually produced (e.g. the real saved file path) so
    JARVIS can reference it on a later turn — otherwise it only sees its own prose
    and guesses filenames. Appended to the last assistant turn in history."""
    text = (text or "").strip()
    if not text:
        return
    note = f"\n\n[Action results you should remember: {text}]"
    for i in range(len(_history) - 1, -1, -1):
        if _history[i].get("role") == "assistant":
            _history[i]["content"] += note
            _persist_history()
            return
    _history.append({"role": "assistant", "content": note.strip()})
    _persist_history()


def get_api_key() -> str:
    """Expose the resolved Groq key (e.g. for the OCR Vision fallback)."""
    return _api_key


def get_gemini_key() -> str:
    return _gemini_key


def _key_for(model: str) -> str:
    """A usable credential for this model's provider, as a truthiness GATE (not
    necessarily a literal key). Gemini is usable with a Gemini API key OR Vertex
    ADC — so when only Vertex is configured, return a sentinel so callers that
    gate on `_key_for(...)` don't reject a perfectly valid key-less Vertex setup.
    (The sentinel is never sent on the wire; the Gemini bridge fetches the ADC
    token itself.) Local Ollama models need no key — a sentinel keeps the
    "no LLM configured" guard from rejecting a valid offline setup."""
    if _is_local_model(model):
        return "ollama"
    provider = _provider_for(model)
    if provider == "gemini":
        return _gemini_key or ("vertex" if vertex_auth.enabled() else "")
    if provider in PREFIXED_PROVIDERS:
        return (_extra_keys[provider] or [""])[0]
    return _api_key


def _active_key() -> str:
    """The key for whichever provider the selected (auto-resolved) model belongs to."""
    return _key_for(resolve_model())


def has_llm_credentials() -> bool:
    """True when at least one LLM path is configured (Groq key, Gemini key, or Vertex
    ADC). Offline mode needs no cloud credential — a local Ollama model is enough."""
    if provider_mode() == "offline":
        return True
    return bool(_api_key or _gemini_key or vertex_auth.enabled()
                or any(_extra_keys.values()))


def needs_setup() -> bool:
    """True when the app should show first-run onboarding: no usable API key, or
    the user hasn't chosen a storage location yet."""
    return not has_llm_credentials() or not storage.is_configured()


def set_api_keys(updates: dict) -> None:
    """Persist API keys to the secret store and refresh the in-memory copies."""
    app_secrets.set_many(updates or {})
    _load_keys()
    # Saving keys is the user telling us they've fixed the cause — an escalated
    # bench from before must not keep a freshly-valid route benched for a day.
    quota.clear()


def remove_api_keys(names) -> "list[str]":
    """Forget these keys (Settings ▸ Remove). Returns any that an environment
    variable still provides. Model discovery + ranking re-run afterwards (main)."""
    still = app_secrets.remove(names)
    _load_keys()
    return still


def set_storage_dir(path: str) -> str:
    """Persist the user's chosen storage root and create the folder tree."""
    p = str(path or "").strip().strip('"')
    _config["storage_dir"] = p
    _save_config(_config)
    storage.ensure_dirs()
    return p


def _places_source() -> str:
    """'google' when Google Places (New) is usable (Maps key or Vertex ADC), else
    'osm'. Lazy import keeps groq_bridge free of a places dependency at load time."""
    try:
        import places_google
        return places_google.source()
    except Exception:  # noqa: BLE001
        return "osm"


def get_sysinfo() -> dict:
    """Snapshot for the Settings panel + onboarding state."""
    ram = _get_free_ram_mb()
    return {
        "provider": active_provider(),
        "model": _selected_model,
        "model_override": _config.get("model_override"),
        # Per-key discovered model list for the GUI dropdown (Phase 3). Synchronous
        # cached read — refreshed on startup / key entry / Settings open. Grouped
        # {label, opts:[{value,label}]} with "Auto" first; never empty (curated
        # fallback before the first refresh).
        "available_models": model_discovery.cached(),
        "task_model_tiers": model_discovery.task_tier_groups(),
        "tts": _config.get("tts", "edge-tts"),
        "wakeWord": "hey_jarvis",
        "ram": round(ram / 1024, 1) if ram else None,
        "vram": None,
        "allowed_dirs": _config.get("allowed_dirs", []),
        # Onboarding / Settings state — booleans only, never the keys themselves.
        "has_groq_key": bool(_api_key),
        "has_gemini_key": bool(_gemini_key),
        # How many credentials each provider has. Free-tier quota is metered per
        # key, so the count is the user's actual headroom — without it, multi-key
        # is invisible and there's no way to tell a saved second key took effect.
        "groq_key_count": len(_groq_keys),
        "gemini_key_count": len(_gemini_keys),
        **{f"has_{p}_key": bool(_extra_keys[p]) for p in PREFIXED_PROVIDERS},
        **{f"{p}_key_count": len(_extra_keys[p]) for p in PREFIXED_PROVIDERS},
        # Smart routing: the LLM-made ranking the auto router walks (model_ranker).
        "routing": model_ranker.summary(),
        # Which routes are benched and why (llm/quota.py). Purely diagnostic, but
        # without it "I've used up the free quota" is unverifiable and a route
        # benched for a day has no visible way back.
        "quota": quota.snapshot(),
        # Vertex AI active (ADC resolved + Mode=vertex): Gemini models work with
        # NO Gemini key, billing to the user's Google Cloud project/credits.
        "vertex": vertex_auth.enabled(),
        # Provider EDITION (Phase 4) + the offline model the GUI Mode selector drives.
        "provider_mode": provider_mode(),
        "offline_model": str(_config.get("offline_model") or ""),
        "ollama_url": _ollama_url(),
        "has_elevenlabs_key": bool(app_secrets.get("elevenlabs_api_key")),
        "has_google_maps_key": bool(app_secrets.get("google_maps_key")),
        "places_source": _places_source(),
        "storage_dir": str(storage.get_root()),
        "storage_configured": storage.is_configured(),
        "needs_setup": needs_setup(),
        "native_audio": gemini_bridge.is_native_audio_model(_selected_model),
        "native_tools": native_tools_enabled(),
        "manual_location": get_manual_location(),
        "image_model": gemini_bridge.is_image_model(_selected_model),
        "always_on": bool(_config.get("always_on")),
        "conversation_mode": bool(_config.get("conversation_mode")),
        "system_alerts": bool(_config.get("system_alerts")),
        "stt": _config.get("stt", "groq"),
        "stt_active": get_stt(),
        "screen": get_screen(),
        "overlay": get_overlay(),
        # What the silent browser/desktop autopilot decides its steps with:
        # "" = auto; "ollama:<name>" = a local model (see autopilot.py).
        "autopilot_model": str(_config.get("autopilot_model") or ""),
        "autopilot_model_active": get_autopilot_model(),
        # Operator step reasoning: -1 dynamic, 0 off, N fixed tokens.
        "autopilot_thinking": int(_config.get("autopilot_thinking",
                                              _AUTOPILOT_THINKING_DEFAULT)),
        "autopilot_vision": autopilot_vision_enabled(),
    }


async def set_model(model: str) -> bool:
    """Switch the active model (Groq or Gemini) at runtime."""
    global _selected_model
    model = (model or "").strip()
    if not model:
        return False
    _selected_model = model
    _config["model_override"] = model
    _config["model"] = model
    _save_config(_config)
    print(f"[LLM] Model -> {model}  (provider: {_provider_for(model)})", flush=True)
    return True


def set_tts(pref: str) -> None:
    _config["tts"] = pref
    _save_config(_config)


def set_autopilot_model(model: str) -> str:
    """Persist the autopilot step-decision model ("" = auto; "ollama:<name>"
    routes steps to a local Ollama server — fully offline operation)."""
    _config["autopilot_model"] = str(model or "").strip()
    _save_config(_config)
    return _config["autopilot_model"]


def set_autopilot_thinking(value) -> int:
    """Persist the operator's per-step thinking budget: -1 dynamic, 0 off, N fixed."""
    try:
        v = int(value)
    except (TypeError, ValueError):
        v = _AUTOPILOT_THINKING_DEFAULT
    v = -1 if v < 0 else v
    _config["autopilot_thinking"] = v
    _save_config(_config)
    return v


def set_autopilot_vision(on: bool) -> bool:
    """Persist whether the autopilot may glance at page screenshots with vision."""
    _config["autopilot_vision"] = bool(on)
    _save_config(_config)
    return bool(on)


def set_allowed_dirs(dirs: list) -> list:
    """Persist the read-only terminal's directory allowlist. Returns the cleaned
    list actually stored (existing directories only)."""
    cleaned = []
    for d in dirs or []:
        d = str(d).strip().strip('"')
        if d and os.path.isdir(os.path.expanduser(d)) and d not in cleaned:
            cleaned.append(d)
    _config["allowed_dirs"] = cleaned
    _save_config(_config)
    return cleaned


def get_allowed_dirs() -> list:
    return list(_config.get("allowed_dirs", []))


def _system_messages(user_text: str = "") -> list:
    """Core persona + optional action catalog + live senses + memory.

    Native-tool mode: SYSTEM_CORE_TOOLS (persona + capability map) + rich-output
    formats; function schemas carry parameters. Tag mode: SYSTEM_CORE (same map)
    + gated ACTION_CATALOG JSON shapes. Static blocks stay FIRST for Gemini cache."""
    native = tools_enabled(resolve_model(user_text))
    if native:
        msgs = [{"role": "system", "content": SYSTEM_CORE_TOOLS},
                {"role": "system", "content": RICH_OUTPUT_REF}]
    else:
        msgs = [{"role": "system", "content": SYSTEM_CORE},
                {"role": "system", "content": RICH_OUTPUT_REF}]
        if _needs_action_catalog(user_text):
            msgs.append({"role": "system", "content": ACTION_CATALOG})
    msgs.append({"role": "system", "content": "FILE STORAGE (where you save things):\n"
                + storage.describe()})
    facts = memory_store.facts_block()
    if facts:
        msgs.append({"role": "system",
                     "content": "WHAT YOU KNOW ABOUT THE USER (durable facts you've "
                                "remembered across sessions — use them naturally to "
                                "personalise replies; do NOT recite them unless "
                                "relevant):\n" + facts})
    if _live_context:
        msgs.append({"role": "system",
                     "content": "LIVE STATE (your private senses right now — for YOUR "
                                "reference only). NEVER repeat, quote, print or read out "
                                "this block; use it only to answer if the user explicitly "
                                "asks about it:\n"
                                + _live_context})
    if _skill_context:
        msgs.append({"role": "system", "content": _skill_context})
    if _config.get("conversation_mode"):
        msgs.append({"role": "system",
                     "content": "CONVERSATION MODE is on: reply like a natural spoken "
                                "chat — warm and present, but still as intelligent as a "
                                "top-tier assistant. Brief for small talk; substantive when "
                                "the question warrants it."})
    return msgs


_LANG_NAMES = {
    "en": "English", "ja": "Japanese", "es": "Spanish", "fr": "French",
    "de": "German", "zh": "Chinese", "ko": "Korean", "hi": "Hindi",
    "it": "Italian", "pt": "Portuguese", "ru": "Russian", "ar": "Arabic",
    "auto": "the source language",
}


async def translate_text(text: str, from_lang: str = "auto", to_lang: str = "en") -> str:
    """Translate OCR'd (or any) text via Groq. Used by the camera-OCR feature.

    Kept separate from send_prompt so it doesn't pollute the conversation history
    and so it can use a deterministic, translation-only system prompt.
    """
    text = (text or "").strip()
    if not text:
        return ""
    model = resolve_model("translate " + text)
    if not _key_for(model):
        return "(no API key configured — can't translate)"

    from_name = _LANG_NAMES.get(from_lang, from_lang)
    to_name = _LANG_NAMES.get(to_lang, to_lang)
    system = (
        f"You are a professional translator. Translate the user's text from "
        f"{from_name} into {to_name}. Output ONLY the translation — no notes, no "
        f"quotes, no transliteration. Preserve line breaks."
    )

    if _provider_for(model) == "gemini":
        try:
            out = await gemini_bridge.gemini_send(
                [{"role": "system", "content": system}, {"role": "user", "content": text}],
                gemini_bridge.rest_model(model), _gemini_key, max_tokens=1024, temperature=0.1)
        except gemini_bridge.GeminiError as exc:
            return f"(translation failed: {exc})"
        return (out or "").strip()

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        "max_completion_tokens": 1024,
        "temperature": 0.1,
        "stream": False,
    }
    _add_reasoning(payload)
    try:
        resp = await _client().post(
            f"{GROQ_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {_api_key}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            return f"(translation failed: HTTP {resp.status_code})"
        data = resp.json()
        return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:  # noqa: BLE001
        return f"(translation error: {exc})"


async def summarize_error(raw: str, context: str = "") -> str:
    """Turn a raw/technical error (stack trace, HTTP body, OS message) into ONE
    short, calm, plain-English line in JARVIS's voice. Used so failures are
    explained briefly instead of reciting the whole error to the user.

    Best-effort: if no key/model is available or the call fails, we fall back to a
    trimmed version of the raw text so the user still hears *something* sensible."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    # A cheap, reliable model — prefer Groq's fast Llama, else Gemini Flash-Lite
    # (Vertex ADC counts as Gemini-capable even without an API key).
    model = _AUTO_FAST_GROQ if _api_key else (
        _AUTO_FAST_GEMINI if (_gemini_key or vertex_auth.enabled()) else "")
    fallback = raw if len(raw) <= 160 else raw[:157].rstrip() + "…"
    if not model:
        return fallback
    system = (
        "You are JARVIS. The user's last action just failed. In ONE short, calm "
        "sentence of plain English, tell them what went wrong and — only if it's "
        "obvious — what they could do about it. No stack traces, no error codes, "
        "no jargon, no quotes. Address them as 'sir'. Keep it under 25 words."
    )
    user = (f"What I was doing: {context}\n" if context else "") + f"The raw error: {raw}"
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    try:
        if _provider_for(model) == "gemini":
            out = await gemini_bridge.gemini_send(msgs, gemini_bridge.rest_model(model),
                                                  _gemini_key, max_tokens=80, temperature=0.2)
            return (out or "").strip() or fallback
        payload = _add_reasoning({
            "model": model, "messages": msgs,
            "max_completion_tokens": 80, "temperature": 0.2, "stream": False,
        })
        resp = await _client().post(
            f"{GROQ_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {_api_key}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            return fallback
        return (resp.json()["choices"][0]["message"].get("content") or "").strip() or fallback
    except Exception as exc:  # noqa: BLE001
        print(f"[LLM] summarize_error failed: {exc}", flush=True)
        return fallback


def get_autopilot_model() -> str:
    """The model that drives autopilot (operator-loop) step decisions.

    Configurable via config "autopilot_model"; empty auto-picks the best operator
    model. We now PREFER a strong multimodal Gemini (gemini-3.5-flash): it lets the
    loop run vision-first (the model sees the browser page screenshot each step,
    not just a text DOM listing) and is excellent at the structured-JSON commands
    the loop needs. Groq's Llama-70B is the text-only fallback when neither Vertex
    (ADC) nor a Gemini key is available."""
    m = str(_config.get("autopilot_model") or "").strip()
    if m:
        return m
    # Offline edition: drive the operator loop with the local model too, so
    # browser/desktop tasks work with no cloud calls (text-only — no vision-first).
    if provider_mode() == "offline":
        return _offline_model()
    if vertex_auth.enabled() or _gemini_key:
        return _AUTO_BIG_GEMINI
    if _api_key:
        return _AUTO_BIG_GROQ
    return ""


def autopilot_vision_enabled() -> bool:
    """Whether the autopilot may glance at PAGE screenshots with the vision
    model when the element list isn't enough (config "autopilot_vision",
    default on — it's budgeted to a few looks per task and captures only
    JARVIS's own browser page, never the desktop)."""
    return bool(_config.get("autopilot_vision", True))


def get_autopilot_engine() -> str:
    """Both browser and desktop autopilot tasks use the JSON/DOM operator. The
    Gemini Computer Use engine was removed (its preview model was slower and less
    reliable than the vision-first DOM operator on gemini-3.5-flash). Kept as a
    function returning a constant so existing callers/telemetry still resolve."""
    return "json"


# Operator step reasoning. Flash models default thinking OFF for chat latency, but
# the autopilot's per-step "which control do I act on next" decision is a genuine
# reasoning task — with thinking off the operator measurably picks worse (even
# invalid) commands. The default is DYNAMIC (-1): the model spends as much
# reasoning as each step needs — little on an obvious click, a lot on an ambiguous
# page (measured ~84 thought tokens on an easy step vs ~260 on a hard one) —
# instead of a flat budget that wastes time on easy steps and caps the hard ones.
# Config "autopilot_thinking": -1 dynamic (default), 0 off, or a fixed N. Gemini
# operators only.
_AUTOPILOT_THINKING_DEFAULT = -1


def autopilot_thinking_budget() -> "Optional[int]":
    """Thinking budget for the operator's step decision: -1 = dynamic (the model
    decides how hard to think per step), 0 = off, N = a fixed cap. None when the
    operator model has no thinking channel (Gemma / non-Gemini), so the field is
    simply never sent.

    Flash-Lite DOES think (measured 2026-10-04: 2.5- and 3.1-flash-lite both take
    a budget, ~150–340 thought tokens) — and without it 2.5-flash-lite clicked
    'Sign in' instead of the video it was asked for, so 'Dynamic' means dynamic
    on lite operators too."""
    model = get_autopilot_model()
    if _provider_for(model) != "gemini":
        return None
    m = model.lower()
    if "gemma" in m:                       # no thinking channel — don't send it
        return None
    try:
        val = int(_config.get("autopilot_thinking", _AUTOPILOT_THINKING_DEFAULT))
    except (TypeError, ValueError):
        val = _AUTOPILOT_THINKING_DEFAULT
    return -1 if val < 0 else val          # any negative → dynamic


def _ollama_url() -> str:
    return str(_config.get("ollama_url") or "http://localhost:11434").rstrip("/")


async def _ollama_chat(messages: list, model: str, max_tokens: int,
                       temperature: float) -> str:
    """One non-streaming chat completion against a local Ollama server."""
    resp = await _client().post(
        f"{_ollama_url()}/api/chat",
        json={"model": model, "messages": messages, "stream": False,
              "options": {"temperature": temperature, "num_predict": max_tokens}},
    )
    if resp.status_code != 200:
        print(f"[Ollama] HTTP {resp.status_code}: {resp.text[:200]}", flush=True)
        return ""
    data = resp.json()
    return ((data.get("message") or {}).get("content") or "").strip()


async def _ollama_stream(messages: list, model: str, max_tokens: int,
                         temperature: float, record_history: bool, user_text: str):
    """Stream a chat reply from a local Ollama model (offline mode / an ``ollama:``
    selection), yielding ``(delta, full)`` like the cloud paths so the pipeline,
    TTS and history all work unchanged. Ollama streams JSONL: one object per line
    with ``{"message":{"content":...},"done":bool}``. Vision turns are flattened to
    text (local models are text-only). Best-effort: a connection failure yields one
    friendly line and stops. The reply carries ``[ACTION]`` tags (the non-native
    system prompt is used for local models), parsed by the existing tag path."""
    name = model.split(":", 1)[1] if ":" in model else model
    full = ""
    try:
        async with _client().stream(
            "POST", f"{_ollama_url()}/api/chat",
            json={"model": name, "messages": _text_only_messages(messages),
                  "stream": True,
                  "options": {"temperature": temperature, "num_predict": max_tokens}},
        ) as resp:
            if resp.status_code != 200:
                await resp.aread()
                print(f"[Ollama] stream HTTP {resp.status_code}: {resp.text[:200]}",
                      flush=True)
                msg = (f"I couldn't reach the local model “{name}” — it may not be "
                       f"pulled. Run `ollama pull {name}`, sir.")
                yield msg, msg
                return
            async for line in resp.aiter_lines():
                line = (line or "").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("error"):
                    if not full:
                        msg = f"The local model returned an error: {obj['error']}"
                        yield msg, msg
                    return
                delta = ((obj.get("message") or {}).get("content")) or ""
                if delta:
                    full += delta
                    yield delta, full
                if obj.get("done"):
                    break
    except Exception as exc:  # noqa: BLE001
        print(f"[Ollama] stream failed: {exc}", flush=True)
        if not full:
            msg = ("I couldn't reach the local model — make sure Ollama is running "
                   f"at {_ollama_url()}, sir.")
            yield msg, msg
        return

    if full and record_history:
        _history.append({"role": "user", "content": user_text})
        _history.append({"role": "assistant", "content": full})
        del _history[: max(0, len(_history) - _MAX_HISTORY_TURNS * 2)]
        _persist_history()


def autopilot_model_is_vision() -> bool:
    """True when the operator-loop model can take a screenshot inline (a Gemini
    multimodal model). The autopilot uses this to decide whether to feed the
    browser page screenshot into each step decision (vision-first)."""
    return _provider_for(get_autopilot_model()) == "gemini"


def autopilot_aux_tier() -> str:
    """Task tier for the autopilot's advisory side call (the plan) — "" keeps it
    on the operator model.

    On auto it goes to the fast tier: a separate per-model quota, and a free
    Gemini key allows only 20 requests a DAY per flash model, one of which every
    task spent on its plan. A pinned operator (or offline mode) keeps every call
    on the model the user chose."""
    if str(_config.get("autopilot_model") or "").strip() or provider_mode() == "offline":
        return ""
    return "dumb"


# The autopilot makes several SEQUENTIAL calls per task, so a model's speed counts
# as much as its benchmark score. One real desktop step, measured 2026-10-05 on the
# free tiers: Groq answered in 0.2–0.8s (qwen3.8-27b correct, gpt-oss-120b
# clumsier); every Gemini model took 3–9s, 3.7-flash 41s. Groq's free limits are
# per model (1000 requests/day, 8000 tokens/min), so a short task fits one burst.
_FAST_PROVIDERS = ("groq",)
_FAST_MIN_SCORE = 10        # gpt-oss-120b and up; smaller models misjudge steps
_EYES_PROVIDERS = ("gemini",)


def model_score(model: str) -> float:
    """A model's smart-routing benchmark score (0 when it isn't ranked)."""
    info = model_ranker.info(model) if model else None
    return float((info or {}).get("score") or 0)


# The ranking knows how SMART a model is, not how busy it is: on 2026-10-05 the
# top-ranked previews (gemini-3.8/3.7-flash) answered 503 "high demand" or took
# 41s, while 3-flash-preview answered the same step in 2s. The vision lane tries
# the quick ones first, by each model's observed call time (quota.latency).
def _latency_band(model: str) -> int:
    """0 = quick or not yet seen, 1 = slow, 2 = very slow / overloaded."""
    s = quota.latency(model)
    return 0 if s is None or s < 8 else 1 if s < 20 else 2


def _lane_candidates(lane: str) -> "list[str]":
    """Every model the lane could use, best first, before quota checks: by score
    for the fast lane, and for the vision lane by observed speed band, then score."""
    if str(_config.get("autopilot_model") or "").strip() or provider_mode() == "offline":
        return []
    providers = _FAST_PROVIDERS if lane == "fast" else _EYES_PROVIDERS
    state = model_ranker.load() if model_ranker.has_ranking() else None
    if state:
        available = set(model_discovery.available_model_ids())
        rows = sorted((m for m in state.get("models", []) if m.get("id") in available),
                      key=lambda m: -float(m.get("score") or 0))
        # Gemma (served by the Gemini API) answered a step in prose, not JSON.
        cands = [m["id"] for m in rows if _provider_for(m["id"]) in providers
                 and (lane != "fast" or float(m.get("score") or 0) >= _FAST_MIN_SCORE)
                 and "gemma" not in m["id"].lower()]
    else:
        cands = ([_AUTO_BIG_GROQ] if lane == "fast"
                 else [_AUTO_BIG_GEMINI, _AUTO_MID_GEMINI, _AUTO_FAST_GEMINI])
    if lane != "fast":
        cands.sort(key=_latency_band)                    # stable: score order kept
    return cands


def _lane_routes(model: str) -> "list[quota.Route]":
    return [quota.Route(_provider_for(model), model, i)
            for i in range(len(_keys_for(_provider_for(model))))]


def autopilot_lane_models(lane: str) -> "list[str]":
    """The usable models for one autopilot lane, best-ranked first:

    * ``"fast"`` — fast text models (Groq) for routine steps, where the control
      list names everything the step needs;
    * ``"eyes"`` — multimodal models (Gemini) for steps that need the screenshot.

    [] when lanes don't apply: an operator the user pinned is always used as-is,
    offline mode stays local, and a lane with no key (or all routes benched right
    now) is empty, so the caller falls back to its normal operator."""
    return [m for m in _lane_candidates(lane)
            if any(quota.usable(r) for r in _lane_routes(m))]


def autopilot_lane_wait(lane: str) -> float:
    """Seconds until the lane's soonest benched route is back (0: none known)."""
    waits = [quota.ready_in(r) for m in _lane_candidates(lane) for r in _lane_routes(m)]
    return min((w for w in waits if w > 0), default=0.0)


# Fast-fail timeout for the autopilot's short, mechanical Gemini calls (plan /
# step decision / verify). Far below the 60s client default so a stalled API
# hands off to the Groq fallback in seconds instead of blocking the whole task.
# Don't tighten it: on the free tier a congested-but-healthy call measured 18s
# (a 6-token reply, no thinking) on 2026-10-04 — 20s would have cut it.
_QUICK_GEMINI_TIMEOUT = 30.0


# Dynamic thinking on Gemini 2.5 Flash/Pro runs long on an operator step: 4–21s a
# step as the autopilot's fallback (2026-10-04), where 3.5-flash and the lite
# models think ~150–350 tokens. A budget acts as a ceiling, not a target (1024 →
# 150–230 thoughts on the lite models), so this keeps the reasoning, not the tail.
_DYNAMIC_THINK_CAP_25 = 1024


def _route_thinking(model: str, user: str, budget):
    """The thinking budget to send to ONE route of the ladder. Resolved per route,
    because the ladder crosses models: the first route's budget used to ride along
    to every fallback ("auto" sized for a lite model left 3.5-flash on its slow
    default; dynamic went uncapped to 2.5-flash)."""
    if budget == "auto":
        return _gemini_thinking_budget(model, user)
    m = (model or "").lower()
    if budget is not None and budget < 0 and "gemini-2.5" in m and "lite" not in m:
        return _DYNAMIC_THINK_CAP_25
    return budget


def _text_only_messages(messages: list) -> list:
    """Flatten any vision-shaped turns ([{type:text},{type:image_url}]) down to a
    plain string content, dropping the image. Used when falling back from a vision
    Gemini model to a TEXT-only Groq model, which 400s on image content."""
    out: list = []
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, list):
            text = " ".join(p.get("text", "") for p in c
                            if isinstance(p, dict) and p.get("type") == "text")
            out.append({"role": m.get("role", "user"), "content": text})
        else:
            out.append(m)
    return out


async def _complete(route: "quota.Route", messages: list, max_tokens: int,
                    temperature: float, thinking_budget=None,
                    timeout: "float | None" = None) -> str:
    """One non-streaming completion on ONE route.

    The single place a cloud/local provider distinction lives for the ladder.
    Records what the response said about remaining quota, and raises RouteError —
    never a provider-specific exception — so the caller's loop stays uniform.
    """
    if route.provider == "ollama":
        name = route.model.split(":", 1)[1] if ":" in route.model else route.model
        out = await _ollama_chat(_text_only_messages(messages), name,
                                 max_tokens, temperature)
        if not out:
            raise RouteError("The local model returned nothing.")
        quota.on_success(route)
        return out

    if route.provider == "gemini":
        try:
            out = await gemini_bridge.gemini_send(
                messages, gemini_bridge.rest_model(route.model), _credential(route),
                max_tokens=max_tokens, temperature=temperature,
                thinking_budget=thinking_budget, timeout=timeout)
        except gemini_bridge.GeminiError as exc:
            raise RouteError(str(exc), status=exc.status,
                             retry_after=exc.retry_after,
                             detail=exc.detail or str(exc)) from exc
        quota.on_success(route)
        return (out or "").strip()

    url, headers, payload = _openai_request(route, messages, max_tokens, temperature,
                                            stream=False)
    post_kw = {"headers": headers, "json": payload}
    if timeout is not None:
        post_kw["timeout"] = timeout
    try:
        resp = await _client().post(url, **post_kw)
    except Exception as exc:  # noqa: BLE001 — transport, not quota
        raise RouteError(f"{type(exc).__name__}: {exc}") from exc

    quota.note_response(route, resp.status_code, resp.headers)
    if resp.status_code != 200:
        raise RouteError(f"{route.provider} HTTP {resp.status_code}",
                         status=resp.status_code,
                         retry_after=_retry_after(resp), detail=resp.text[:2000])
    quota.on_success(route)
    return (resp.json()["choices"][0]["message"].get("content") or "").strip()


def _openai_request(route: "quota.Route", messages: list, max_tokens: int,
                    temperature: float, stream: bool) -> "tuple[str, dict, dict]":
    """(url, headers, payload) for an OpenAI-compatible provider. Groq keeps its
    exact historical payload (max_completion_tokens + reasoning_effort on gpt-oss);
    the others get the portable subset — Mistral documents only ``max_tokens``, and
    an unknown field is a 400 on the strict ones."""
    payload = {"model": split_model(route.model)[1], "messages": messages,
               "temperature": temperature, "stream": stream}
    if route.provider == "groq":
        payload["max_completion_tokens"] = max_tokens
        _add_reasoning(payload)
    elif route.provider == "openai":
        # OpenAI's reasoning models (o-series, gpt-5) reject max_tokens and any
        # non-default temperature with a 400.
        payload["max_completion_tokens"] = max_tokens
        if re.match(r"(o\d|gpt-5)", payload["model"]):
            payload.pop("temperature")
    else:
        payload["max_tokens"] = max_tokens
    headers = {"Authorization": f"Bearer {_credential(route)}",
               "Content-Type": "application/json"}
    return f"{OPENAI_COMPAT[route.provider]}/chat/completions", headers, payload


def _retry_after(resp) -> "float | None":
    """The provider's own requested wait, in seconds, when it sent one."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        return float(str(raw).strip())
    except ValueError:
        return None


async def quick_completion(system: str, user: str, model: str = "",
                           max_tokens: int = 400, temperature: float = 0.0,
                           image_b64: str = "", thinking_budget="auto",
                           task_tier: str = "",
                           fallbacks: "tuple[str, ...]" = ()) -> str:
    """One ISOLATED, non-streaming completion: no conversation history, no
    persona prompt, nothing recorded. The autopilot loop uses this for its silent
    step decisions; anything internal/mechanical can too. Returns "" on any
    failure so callers can treat empty as "no decision" and recover.

    `model` may be a Groq or Gemini id, or "ollama:<name>" for a local model.
    `image_b64` is attached to the user turn for vision-capable Gemini models — the
    vision-first operator loop passes the page screenshot so the model SEES the page
    while choosing the next command. Accepts either a full ``data:`` URL (what
    browser.screenshot_b64 returns) or raw JPEG base64. Ignored for non-Gemini
    models (which would 400 on inline image content).

    `thinking_budget` (Gemini only): "auto" uses the per-model default (thinking OFF
    on flash for latency); pass an int to OVERRIDE — the autopilot's step decision
    passes a small positive budget so the operator actually reasons about the next
    UI action (measured: thinking-off produced invalid/worse commands). None leaves
    the model default untouched.

    `task_tier` (with no `model`) picks that tier's best usable model and walks
    that tier's ladder, instead of the autopilot operator's.

    `fallbacks` are tried right after `model`, before the general ladder — the
    autopilot's lane order (other fast models, or other vision models)."""
    model = (model or (resolve_model(user, task_tier) if task_tier else "")
             or get_autopilot_model()).strip()
    if not model:
        return ""
    # Built whenever there's an image, and flattened per route below: a call led
    # by a text model may still fall back to a Gemini route that can use it.
    use_image = bool(image_b64)
    if use_image:
        img_url = (image_b64 if image_b64.startswith("data:")
                   else f"data:image/jpeg;base64,{image_b64}")
        user_turn = {"role": "user", "content": [
            {"type": "text", "text": user},
            {"type": "image_url", "image_url": {"url": img_url}},
        ]}
    else:
        user_turn = {"role": "user", "content": user}
    msgs = [{"role": "system", "content": system}, user_turn]

    # Walk the ladder from the requested model down. The autopilot fires this on
    # every step, so a spent route here used to cost a 429 per step; now the first
    # failure benches it and later steps skip straight to a route that works.
    last = ""
    skip_models: "set[str]" = set()
    pinned = (model, *fallbacks) if fallbacks else model
    async for route in _walk_routes(user, pinned=pinned, task_tier=task_tier,
                                    skip=skip_models):
        # Groq text models reject inline image content (HTTP 400 "content must be
        # a string"). Flatten so a vision-first operator step DEGRADES to the DOM
        # listing rather than dying — it can still act on the numbered elements.
        msgs_for_route = (_text_only_messages(msgs)
                          if use_image and route.provider != "gemini" else msgs)
        t0 = time.monotonic()
        try:
            out = await _complete(route, msgs_for_route, max_tokens, temperature,
                                  thinking_budget=_route_thinking(route.model, user,
                                                                  thinking_budget),
                                  timeout=_QUICK_GEMINI_TIMEOUT)
        except RouteError as exc:
            last = str(exc)
            if not exc.status or exc.status >= 500:
                # Overloaded or timed out: slow, as far as the lane order goes.
                quota.note_latency(route.model, max(time.monotonic() - t0, 25.0))
            if _bench_route(route, exc.status, exc.detail, exc.retry_after):
                skip_models.add(route.model)
            continue
        except Exception as exc:  # noqa: BLE001 — never let a step decision crash
            last = f"{type(exc).__name__}: {exc}"
            _bench_route(route, 0, last)
            continue
        quota.note_latency(route.model, time.monotonic() - t0)
        if out:
            return out
        # HTTP 200 with no content is an upstream failure, not an answer: bench it
        # briefly so the next autopilot step doesn't pay for the same empty route.
        last = "empty completion"
        _bench_route(route, 0, last)
    if last:
        print(f"[LLM] quick_completion exhausted every route: {last}", flush=True)
    return ""


async def send_prompt(text: str, task_tier: str = "") -> str:
    model = resolve_model(text, task_tier)
    if not _key_for(model):
        return "I'm not connected to an LLM yet — no API key is configured."

    messages = [*_system_messages(text), *_history_for_prompt(model=model, text=text),
                {"role": "user", "content": text}]

    temperature = _temperature_for(text)
    max_tokens = _max_tokens_for(text, model=model)

    # When every route is benched, the walk yields nothing and the turn ends in
    # _exhausted — the ONLY path that mentions a rate limit, with a real time.
    full_response = ""
    skip_models: "set[str]" = set()
    async for route in _walk_routes(text, task_tier=task_tier, skip=skip_models):
        try:
            full_response = await _complete(
                route, messages, max_tokens, temperature,
                thinking_budget=_gemini_thinking_budget(route.model, text))
        except RouteError as exc:
            print(f"[LLM] {route.provider}/{route.model} failed: {exc}", flush=True)
            if _bench_route(route, exc.status, exc.detail, exc.retry_after):
                skip_models.add(route.model)
            continue
        if full_response:
            break
        _bench_route(route, 0, "empty completion")

    if not full_response:
        return _exhausted(text)

    # Remember this turn, trimming to the most recent N turns (2 msgs each).
    _history.append({"role": "user", "content": text})
    _history.append({"role": "assistant", "content": full_response})
    del _history[: max(0, len(_history) - _MAX_HISTORY_TURNS * 2)]
    _persist_history()

    return full_response


VISION_MODEL = "meta-llama/llama-4-scout-17b-16e-instruct"


async def vision_query(image_url: str, question: str, task_tier: str = "") -> str:
    """Ask a vision model about an image (e.g. a screenshot). Used for JARVIS's
    'eyes' on the desktop. Routes image uploads through the selected task tier's
    Gemini vision model, or Groq's dedicated vision model. Returns text or ''."""
    if not image_url:
        return ""
    # An image always goes to a vision-capable endpoint.  For a chat upload, use
    # its selected tier to choose the Gemini vision model rather than consulting
    # the unrelated, persistent Settings selection.
    active_model = resolve_model(question, task_tier)
    if _provider_for(active_model) == "gemini" and (_gemini_key or vertex_auth.enabled()):
        vmodel = gemini_bridge.rest_model(active_model)
        try:
            return await gemini_bridge.gemini_vision(image_url, question, vmodel, _gemini_key)
        except gemini_bridge.GeminiError:
            return ""
    if not _api_key:
        return ""
    payload = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": question or "Describe what's on this screen."},
                {"type": "image_url", "image_url": {"url": image_url}},
            ],
        }],
        "max_completion_tokens": 600,
        "temperature": 0.3,
        "stream": False,
    }
    try:
        resp = await _client().post(
            f"{GROQ_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {_api_key}",
                     "Content-Type": "application/json"},
            json=payload,
        )
        if resp.status_code != 200:
            print(f"[Vision] HTTP {resp.status_code}: {resp.text[:200]}")
            return ""
        data = resp.json()
        return (data["choices"][0]["message"].get("content") or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[Vision] failed: {exc}")
        return ""


# The model that answers grounded web-search queries. Grounding is a Gemini
# feature (Google Search tool), so this is always a Gemini model regardless of
# the chat model selected: the user's Gemini model when they're on one, else the
# fast auto-Gemini. Flash-Lite has no thinking channel but grounds fine and is
# the lowest-latency option for a quick spoken answer.
_GROUNDING_SYSTEM = (
    "You are JARVIS answering a question that needs CURRENT, real-world information, "
    "using live Google Search results you have been given. Answer the user's "
    "question directly and concisely in JARVIS's voice — it will be spoken aloud, so "
    "1-3 short sentences, an occasional 'sir', no markdown, no bullet lists, no URLs "
    "or citation markers in the prose (the app shows sources separately). Lead with "
    "the actual answer (the number, name, date, fact), then at most one line of "
    "context. If the results genuinely don't answer it, say so briefly."
)


def web_search_available() -> bool:
    """Grounded web search needs a Gemini path (Vertex ADC or a Gemini key)."""
    return bool(vertex_auth.enabled() or _gemini_key)


def _grounding_model() -> str:
    """The Gemini model to ground with: the user's selected Gemini model if they're
    on one, otherwise the fast auto-Gemini."""
    sel = (_selected_model or "").strip()
    if gemini_bridge.is_gemini_model(sel) and not gemini_bridge.is_native_audio_model(sel):
        return sel
    return _AUTO_FAST_GEMINI


async def web_answer(query: str) -> "tuple[bool, str, list]":
    """Answer a question from LIVE Google Search via Gemini grounding — no browser.

    Returns (ok, answer_text, sources) where sources is a list of {"title","uri"}.
    This is JARVIS's "look it up for my own knowledge" path: current events, prices,
    scores, weather elsewhere, 'latest'/'today' facts — anything past the model's
    training cutoff or changing in real time — answered directly with citations,
    instead of opening a browser the user then has to read themselves."""
    query = (query or "").strip()
    if not query:
        return False, "What should I look up, sir?", []
    if not web_search_available():
        return (False, "Live web search needs Gemini, sir — sign in with Google Cloud "
                "(Vertex) or add a Gemini key in Settings, and I'll pull it straight "
                "from the web.", [])
    model = _grounding_model()
    # Tell the model TODAY so "best/top/latest/current" questions return the newest
    # result, not a stale cached page (the bug where "highest-rated restaurant" in
    # 2026 answered with the 2024 list). It must state which year/edition it reflects.
    today = _dt.date.today().strftime("%A, %d %B %Y")
    # Tell the model WHERE the user is so "near me / nearest / around here" resolves
    # to their actual city instead of a generic, often wrong-country result — the
    # "nearest trampoline park" that came back with Australian parks for a Mumbai
    # user. Best-effort: no known location simply omits the line.
    where = ""
    try:
        import places
        _lat, _lon, _place = places.get_user_coords()
        where = (_place or "").strip()
    except Exception:  # noqa: BLE001
        where = ""
    # The rolling conversation gives the search context ("and the one before that?"),
    # but this turn is NOT recorded as a normal reply — main.py speaks the answer.
    msgs = [
        {"role": "system", "content": _GROUNDING_SYSTEM},
        {"role": "system", "content":
            f"Today's date is {today}. For 'best/top/highest/latest/current/who is "
            "now' questions, prefer the MOST RECENT information and rankings, and "
            "state which year or edition your answer reflects. Never give an "
            "outdated ranking as if it were current."},
    ]
    if where:
        msgs.append({"role": "system", "content":
                     f"The user is in {where}. For 'near me', 'nearest', 'around "
                     "here' or other location-relative questions, assume this "
                     "location unless they name a different place."})
    msgs += [
        *_history_for_prompt(max_turns=_slim_history_turns(model, query),
                             model=model, text=query),
        {"role": "user", "content": query},
    ]
    try:
        text, sources, queries = await gemini_bridge.gemini_grounded(
            msgs, gemini_bridge.rest_model(model), _gemini_key,
            max_tokens=800, temperature=0.3,
            thinking_budget=_gemini_thinking_budget(model, query))
    except gemini_bridge.GeminiError as exc:
        return False, str(exc), []
    if not (text or "").strip():
        return False, "I couldn't find a clear answer to that just now, sir.", []
    if queries:
        print(f"[WebSearch] grounded on: {queries}", flush=True)
    return True, text.strip(), sources


# ── Smart-routing ranking ─────────────────────────────────────────────────────
# Benchmarks (model_catalog) score almost every model instantly; only models the
# catalog doesn't know are sent to a search-grounded LLM, once each.
_RANK_MAX_TOKENS = 12000
# Searching ~30 models is a minutes-long job, not a chat turn.
_RANK_TIMEOUT = 240.0
# Transient Gemini failures (overloaded 503, timeouts) are retried on the SAME
# grounded model after these waits; a quota 429 moves straight to the next model.
_RANK_RETRY_WAITS = (5.0, 20.0)
# After a failed estimate, don't retry on every discovery (Settings open, restart)
# for this long — the Re-rank button always retries.
_ESTIMATE_RETRY_S = 6 * 3600


async def _ask_ranker(prompt: str) -> "tuple[str, str]":
    """(answer, model used) from Gemini + Google Search, or ("", "") when no grounded
    model answers. There is deliberately NO from-memory fallback: measured, an
    ungrounded ranking put 2024's Mixtral-8x22B and Llama-2-70B at the top. An
    unknown model without an estimate just gets a conservative placeholder score."""
    if not web_search_available():
        return "", ""
    msgs = [{"role": "system", "content": model_ranker.ESTIMATE_SYSTEM},
            {"role": "user", "content": prompt}]
    for model in (_AUTO_BIG_GEMINI, _AUTO_MID_GEMINI, _AUTO_FAST_GEMINI):
        for wait in (0.0, *_RANK_RETRY_WAITS):
            if wait:
                await asyncio.sleep(wait)
            try:
                text, _sources, queries = await gemini_bridge.gemini_grounded(
                    msgs, gemini_bridge.rest_model(model), _gemini_key,
                    max_tokens=_RANK_MAX_TOKENS, temperature=0.2,
                    thinking_budget=None if "lite" in model else -1,
                    timeout=_RANK_TIMEOUT)
            except gemini_bridge.GeminiError as exc:
                print(f"[Ranker] {model} estimate failed "
                      f"(HTTP {exc.status or '-'}): {exc}", flush=True)
                if exc.status and exc.status < 500:
                    break              # quota / auth: this model won't recover soon
                continue               # overloaded / timeout: retry after a wait
            if text:
                print(f"[Ranker] {model} searched {len(queries)} queries", flush=True)
                return text, model
    return "", ""


def _rankable_models() -> "list[str]":
    """Every discovered cloud chat model whose provider has a credential."""
    return [m for p, ids in model_discovery.catalog().items() if _keys_for(p) for m in ids]


async def rank_models(force: bool = False, on_update=None) -> bool:
    """Recompute the smart-routing ranking for the models the keys reach now.

    Cheap and deterministic, so it runs after every discovery — a key added or
    removed, a provider's catalog changing. Models the benchmark catalog doesn't
    know are estimated by a search-grounded LLM once and cached; ``force`` (the
    Re-rank button) re-asks about all of them. Returns True when stored.

    Two phases when a lookup is needed: the benchmark ranking is stored (and
    ``on_update`` awaited, so Settings/routing see it) BEFORE the slow web lookup,
    so a newly added provider's known models are usable at once."""
    if model_ranker.ranking_in_progress or provider_mode() == "offline":
        return False
    models = _rankable_models()
    if not models:
        model_ranker.last_result = "Nothing to rank yet — add an API key first."
        return False
    state = model_ranker.load() or {}
    estimated = {} if force else model_ranker.estimates()
    todo = [n for n in model_ranker.unknown_models(models) if n not in estimated]
    failed_at = None if force else state.get("estimate_failed_at")
    recently_failed = bool(failed_at) and time.time() - failed_at < _ESTIMATE_RETRY_S
    by, note = "", ""
    model_ranker.ranking_in_progress = True
    try:
        if todo and web_search_available() and (force or not recently_failed):
            model_ranker.store(model_ranker.compute(models, model_discovery.caps(), estimated,
                                                    model_ranker.no_tools()),
                               estimated, "", failed_at)
            if on_update:
                await on_update()
            today = _dt.date.today().strftime("%d %B %Y")
            print(f"[Ranker] estimating {len(todo)} model(s) the benchmark catalog "
                  f"doesn't cover: {', '.join(todo)}", flush=True)
            got: dict = {}
            for batch in model_ranker.batches(todo):
                for _attempt in range(2):      # one retry for an off-scale/garbled batch
                    text, by = await _ask_ranker(model_ranker.estimate_prompt(batch, today))
                    parsed = model_ranker.parse_estimates(text, batch)
                    if parsed or not text:
                        got.update(parsed)
                        break
            estimated.update(got)
            missing = [n for n in todo if n not in got]
            failed_at = time.time() if missing else None
            if missing:
                note = (f" {len(missing)} couldn't be estimated (web search unavailable) — "
                        "placed conservatively; Re-rank later to retry.")
        elif todo and not web_search_available():
            note = f" {len(todo)} unknown model(s) placed conservatively (needs a Gemini key to look them up)."
        rows = model_ranker.compute(models, model_discovery.caps(), estimated,
                                    model_ranker.no_tools())
        model_ranker.store(rows, estimated, by, failed_at)
        counts = model_ranker.summary()["counts"]
        model_ranker.last_result = (
            f"Ranked {len(rows)} models: {counts['benchmark']} from benchmarks"
            + (f", {counts['llm']} estimated" if counts["llm"] else "")
            + (f", {counts['guess']} unscored" if counts["guess"] else "") + "." + note)
        print(f"[Ranker] {model_ranker.last_result} Top: "
              + ", ".join(f"{r['id']}({r['tier']},{r['score']:g})" for r in rows[:5]),
              flush=True)
        return True
    finally:
        model_ranker.ranking_in_progress = False


def _mentions_tools(detail: str) -> bool:
    """A 400 that is about tool/function calling (not a malformed request)."""
    d = (detail or "").lower()
    return "tool" in d or "function call" in d or "function_call" in d


def _flush_tool_acc(tool_acc: dict, tool_sink: "list | None") -> None:
    """Turn reassembled OpenAI tool-call fragments into normalised
    ``{"id","name","args"}`` dicts on ``tool_sink``. A fragment whose arguments
    don't parse as JSON is dropped (logged) rather than crashing the turn — the
    caller falls back to the [ACTION] path when nothing usable was produced."""
    if tool_sink is None or not tool_acc:
        return
    for idx in sorted(tool_acc):
        slot = tool_acc[idx]
        if not slot.get("name"):
            continue
        raw = (slot.get("args") or "").strip()
        try:
            args = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            print(f"[Groq] tool-call args parse failed for {slot['name']}: "
                  f"{raw[:120]}", flush=True)
            continue
        tool_sink.append({"id": slot.get("id") or f"call_{idx}",
                          "name": slot["name"], "args": args})


async def _stream_route(route: "quota.Route", messages: list, max_tokens: int,
                        temperature: float, text: str, use_tools: bool,
                        tool_sink: "list | None"):
    """Stream ``(delta, full)`` from ONE route.

    Raises RouteError for a failure that happened BEFORE any byte was produced —
    the only kind the ladder can retry, since a partially-streamed reply can't be
    cleanly restarted on another model.
    """
    if route.provider == "ollama":
        async for delta, full in _ollama_stream(messages, route.model, max_tokens,
                                                temperature, False, text):
            yield delta, full
        return

    if route.provider == "gemini":
        try:
            async for delta, full in gemini_bridge.gemini_stream(
                    messages, gemini_bridge.rest_model(route.model), _credential(route),
                    max_tokens=max_tokens, temperature=temperature,
                    thinking_budget=_gemini_thinking_budget(route.model, text),
                    tools=live_tools.function_declarations() if use_tools else None,
                    tool_sink=tool_sink if use_tools else None):
                yield delta, full
        except gemini_bridge.GeminiError as exc:
            if use_tools and exc.status == 400 and _mentions_tools(exc.detail):
                model_ranker.mark_no_tools(route.model)
            raise RouteError(str(exc), status=exc.status, retry_after=exc.retry_after,
                             detail=exc.detail or str(exc)) from exc
        quota.on_success(route)
        return

    url, headers, payload = _openai_request(route, messages, max_tokens, temperature,
                                            stream=True)
    if use_tools:
        payload["tools"] = live_tools.openai_tools()
        payload["tool_choice"] = "auto"

    full = ""
    tool_acc: dict = {}   # index -> {"id","name","args"} fragments, reassembled below
    try:
        async with _client().stream("POST", url, headers=headers, json=payload) as resp:
            quota.note_response(route, resp.status_code, resp.headers)
            if resp.status_code != 200:
                await resp.aread()
                # A model that turns out not to support tool calling: remember it,
                # so later action turns route past it instead of re-paying this 400.
                if use_tools and resp.status_code == 400 and _mentions_tools(resp.text):
                    model_ranker.mark_no_tools(route.model)
                raise RouteError(f"{route.provider} HTTP {resp.status_code}",
                                 status=resp.status_code,
                                 retry_after=_retry_after(resp),
                                 detail=resp.text[:2000])
            async for line in resp.aiter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[len("data:"):].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                    dlt = obj["choices"][0].get("delta") or {}
                except (json.JSONDecodeError, KeyError, IndexError):
                    continue
                # Reassemble streamed OpenAI tool-call fragments (name arrives
                # once, arguments stream as a JSON string across chunks).
                if use_tools and dlt.get("tool_calls"):
                    for tc in dlt["tool_calls"]:
                        slot = tool_acc.setdefault(tc.get("index", 0),
                                                   {"id": "", "name": "", "args": ""})
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
                delta = dlt.get("content") or ""
                if delta:
                    full += delta
                    yield delta, full
            _flush_tool_acc(tool_acc, tool_sink)
    except RouteError:
        raise
    except Exception as exc:  # noqa: BLE001 — transport/parse, not quota
        if full:
            return          # partial reply already delivered; keep what we have
        raise RouteError(f"{type(exc).__name__}: {exc}") from exc
    quota.on_success(route)


async def stream_prompt(text: str, record_history: bool = True, slim: bool = False,
                        tool_sink: "list | None" = None, task_tier: str = ""):
    """Async generator that streams the model reply token-by-token.

    Yields ``(delta, full_raw)`` per chunk so the pipeline can show text and
    speak sentences as they arrive. Updates conversation history when done.
    On error it yields one friendly message so callers can treat it uniformly.

    ``tool_sink`` opts the turn into NATIVE function/tool calling: when a list is
    passed AND :func:`tools_enabled` is true for the resolved model, the tool
    palette is advertised to the model and any function calls it makes are
    appended to the list as normalised ``{"id","name","args"}`` dicts. The
    ``(delta, full)`` text yield is unchanged, so callers that don't pass a sink
    behave exactly as before (and TTS streaming is untouched).

    ``record_history=False`` runs the turn WITHOUT writing it into (or persisting)
    the conversation history. The agentic follow-up loop uses this for its internal
    "Result of your last action(s)…" scaffolding turns, which carry raw file/page
    dumps — recording those bloated the rolling window and resurfaced them as user
    chat bubbles on reload.

    ``slim=True`` swaps the full system stack for the compact
    ``_AGENTIC_SYSTEM_PROMPT`` — used by the agentic follow-up loop. On Gemini
    it still carries more history than Groq (see ``_slim_history_turns``). The
    action catalog is omitted in slim mode to save tokens.
    """
    model = resolve_model(text, task_tier)
    if not _key_for(model):
        msg = "I'm not connected to an LLM yet — no API key is configured."
        yield msg, msg
        return

    # Slim agentic steps use provider-aware history depth (more on Gemini).
    system_msgs = ([{"role": "system", "content": _AGENTIC_SYSTEM_PROMPT}]
                   if slim else _system_messages(text))
    slim_turns = _slim_history_turns(model, text) if slim else None
    hist = _history_for_prompt(max_turns=slim_turns, model=model, text=text)
    messages = [*system_msgs, *hist, {"role": "user", "content": text}]

    temperature = _temperature_for(text)
    max_tokens = _max_tokens_for(text, slim=slim, model=model)

    # When every route is benched, the walk yields nothing and the turn ends in
    # _exhausted — the ONLY place the user hears about a rate limit, with a real time.
    full = ""
    skip_models: "set[str]" = set()
    async for route in _walk_routes(text, task_tier=task_tier, skip=skip_models):
        # Attach the tool palette on every turn that ISN'T a trivial greeting. We
        # do NOT gate this on the narrow action-keyword heuristic: the native
        # persona always tells the model to "call the matching function", so if a
        # turn that actually wants an action ("change the HUD to red") arrives
        # without tools attached, the model invents a JSON tool-call and prints it
        # as text instead of acting. Better to carry the schemas (cached by Gemini
        # across turns) than to ever leave the model told-to-act-but-given-no-tools.
        # Trivial greetings ("hi", "thanks") stay tool-free and tiny. Recomputed
        # per route because tool support is a property of the model, not the turn.
        use_tools = (tool_sink is not None and tools_enabled(route.model)
                     and (slim or not _is_trivial_turn(text)))
        # Local models are non-native (tools_enabled→False), so they get the
        # [ACTION]-tag system prompt and the existing tag path runs any actions.
        try:
            async for delta, full in _stream_route(route, messages, max_tokens,
                                                   temperature, text, use_tools,
                                                   tool_sink):
                yield delta, full
        except RouteError as exc:
            print(f"[LLM] {route.provider}/{route.model} stream failed: {exc}",
                  flush=True)
            if _bench_route(route, exc.status, exc.detail, exc.retry_after):
                skip_models.add(route.model)
            # A call the failed route already emitted must not run alongside the
            # next route's own answer.
            if tool_sink is not None:
                tool_sink.clear()
            continue
        # A tool-only reply (a function call, no spoken preamble) IS an answer.
        # Treating it as empty re-asked the next route — running the action twice
        # — and then told the user their quota was spent.
        if full or tool_sink:
            break
        _bench_route(route, 0, "empty completion")

    if not full and not tool_sink:
        msg = _exhausted(text)
        yield msg, msg
        return

    if full and record_history:
        _history.append({"role": "user", "content": text})
        _history.append({"role": "assistant", "content": full})
        del _history[: max(0, len(_history) - _MAX_HISTORY_TURNS * 2)]
        _persist_history()


# Compact greeting persona — deliberately NOT the full SYSTEM_PROMPT. The greeting
# only needs JARVIS's voice plus the live readings; sending the entire ~4k-token
# action catalogue made the call slow and made Flash-Lite (which handles huge
# prompts poorly) wander. A short prompt is faster AND more reliable.
_GREETING_SYSTEM = (
    "You are JARVIS, the user's AI assistant — composed, quietly witty, warm, in the "
    "spirit of Tony Stark's JARVIS. The app just started and the user is looking at "
    "their HUD. Greet them in 1-2 short spoken sentences with an occasional 'sir'. If "
    "the LIVE STATE shows something genuinely worth flagging — the next agenda item, "
    "unusually high CPU or memory, or low battery — weave in ONE such thing naturally; "
    "otherwise just greet them. Plain speech only: no markdown, no tags, no lists, no "
    "reciting raw readings."
)

# Spoken when the greeting model call fails OR is too slow, so the user ALWAYS gets
# a greeting on startup. The old path returned "" on any hiccup (cold Vertex start,
# a rate-limit blip) and then stayed completely silent — the "I almost never get the
# greeting" complaint.
_GREETING_FALLBACKS = (
    "Good to see you, sir. All systems are online — how can I help?",
    "JARVIS online, sir. Standing by.",
    "Systems nominal, sir. What can I do for you?",
)
# A cold first call (Vertex token mint + TLS, or a Groq TPM blip) can stall; the
# user should never wait long — or get silence — just for a hello.
_GREETING_TIMEOUT_S = 8.0
_greet_count = 0


def _fallback_greeting() -> str:
    global _greet_count
    g = _GREETING_FALLBACKS[_greet_count % len(_GREETING_FALLBACKS)]
    _greet_count += 1
    return g


def _greeting_model() -> str:
    """The model used for the startup greeting. A 1-2 sentence hello doesn't need
    the flagship, so prefer the FASTEST available path: Gemini Flash-Lite (no
    thinking channel, lowest latency) on Vertex/Gemini, else Groq's quick model.
    This is independent of the user's chat model — the greeting is isolated."""
    if vertex_auth.enabled() or _gemini_key:
        return _AUTO_FAST_GEMINI
    if _api_key:
        return _AUTO_FAST_GROQ
    return resolve_model()


async def _run_greeting(model: str, greet_msgs: list) -> str:
    """One greeting completion on `model` (thinking OFF for any flash model)."""
    if _provider_for(model) == "gemini":
        out = await gemini_bridge.gemini_send(
            greet_msgs, gemini_bridge.rest_model(model), _gemini_key,
            max_tokens=200, temperature=0.7,
            thinking_budget=_gemini_thinking_budget(model, ""))
        return (out or "").strip()
    payload = _add_reasoning({
        "model": model, "messages": greet_msgs,
        "max_completion_tokens": 200, "temperature": 0.7, "stream": False,
    })
    resp = await _client().post(
        f"{GROQ_BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {_api_key}",
                 "Content-Type": "application/json"},
        json=payload)
    if resp.status_code != 200:
        return ""
    return (resp.json()["choices"][0]["message"].get("content") or "").strip()


async def startup_greeting() -> str:
    """A one-off, context-aware greeting spoken when the app starts.

    ALWAYS returns a greeting — a live, LIVE-STATE-aware one from the fast model
    when possible, or a canned JARVIS line if that call fails or stalls (so the
    user is never met with silence). Not added to history; emits no tags.
    """
    model = _greeting_model()
    if not _key_for(model):
        return _fallback_greeting()
    ctx = _live_context or "(no live readings yet)"
    greet_msgs = [
        {"role": "system", "content": _GREETING_SYSTEM},
        {"role": "system",
         "content": "LIVE STATE (mention at most one notable item, only if "
                    "noteworthy):\n" + ctx},
    ]
    facts = memory_store.facts_block()
    if facts:
        greet_msgs.append({"role": "system",
                           "content": "ABOUT THE USER (personalise only if it "
                                      "feels natural; don't recite):\n" + facts})
    greet_msgs.append({"role": "user", "content": "Greet me now."})
    try:
        text = await asyncio.wait_for(_run_greeting(model, greet_msgs),
                                      _GREETING_TIMEOUT_S)
    except Exception as exc:  # noqa: BLE001 — timeout, GeminiError, transport, etc.
        print(f"[Greeting] model path failed ({exc!r}); using fallback line.", flush=True)
        return _fallback_greeting()
    return text or _fallback_greeting()


async def generate_image(prompt: str) -> "tuple[bool, str, Optional[str]]":
    """Generate an image from a prompt. Prefers **Imagen 4** on Vertex (no API key
    needed — runs on the user's GCP credits via ADC) and falls back to the Gemini
    flash-image model when only a Gemini API key is configured. Works regardless of
    the selected chat model. Returns (ok, message, data_url).
    """
    # Vertex/ADC alone is enough (Imagen needs no key). Only block when there's
    # NEITHER a Gemini key NOR Vertex — previously this gated on the key only, so
    # Vertex-only setups were wrongly told to add a key while Imagen sat unused.
    if not _gemini_key and not vertex_auth.enabled():
        return (False, "Image generation needs Vertex (gcloud ADC) or a Gemini API "
                       "key — set one up in Settings.", None)
    return await gemini_bridge.gemini_generate_image(prompt, _gemini_key)
