"""Per-key model discovery — the GUI model dropdown shows only models the ACTIVE
credentials can actually serve, instead of a hardcoded list that goes stale or
offers models a given buyer's key can't call (Phase 3 of the model overhaul).

Sources, each best-effort and never raising (a failed/absent provider is simply
omitted, and a failed call falls back to a small curated set):
  • Groq    — ``GET {GROQ_BASE_URL}/models`` (Bearer key) → chat model ids.
  • Gemini  — ``GET {gemini_bridge.API_BASE}/models?key=KEY`` → models whose
              ``supportedGenerationMethods`` include ``generateContent``.
  • Vertex  — no simple per-key list API; a curated verified-callable set kept in
              sync with ``gemini_bridge``'s module docstring (ADC grants broad
              access, so the curated list is the practical source).
  • Ollama  — ``GET {ollama_url}/api/tags`` → local model names as ``ollama:<name>``.

Results are grouped in the SAME shape the GUI dropdown wants
(``{"label", "opts":[{"value","label"}]}``) with an "Auto" group always first,
cached in memory keyed by a credentials hash so :func:`cached` can be read
synchronously (no network) from ``get_sysinfo``. :func:`refresh` does the network
and is called on startup, on key entry, and when the Settings panel opens.

Tier note (the three monetization editions): the groups that appear ARE the tier —
Vertex+Groq shows the Vertex curated set + Groq; Gemini+Groq shows the key-
discovered Gemini set + Groq; a Local edition shows the Ollama group. So this
doubles as the per-edition model surface.
"""

from __future__ import annotations

import hashlib
import re

import httpx

# "Auto" is always offered first — it's the recommended default (complexity routing).
_AUTO_GROUP = {"label": "Auto", "opts": [
    {"value": "auto", "label": "Auto — pick the best model per request"}]}

# Vertex has no per-key list endpoint → a curated verified-callable set, kept in
# sync with gemini_bridge.py's module docstring (those are confirmed callable on
# the Vertex global endpoint). Also the fallback for a failed Gemini-key probe.
_VERTEX_CURATED = [
    ("gemini-3.5-flash",      "Gemini 3.5 Flash — flagship (rich / agentic)"),
    ("gemini-3.1-flash-lite", "Gemini 3.1 Flash-Lite — fastest"),
    ("gemini-2.5-flash",      "Gemini 2.5 Flash — capable mid-tier"),
    ("gemini-2.5-flash-lite", "Gemini 2.5 Flash-Lite — cheap / fast"),
    ("gemini-2.5-pro",        "Gemini 2.5 Pro"),
    ("gemini-live-2.5-flash-native-audio",
     "Gemini Live Native Audio — listens & replies natively (preview)"),
]

# Fallback Groq set when the live /models call fails (still gives a usable dropdown).
# llama-3.3-70b / llama-3.1-8b were deprecated 2026-06-17 → gpt-oss is the migration.
_GROQ_CURATED = [
    ("openai/gpt-oss-120b", "GPT-OSS 120B — deep reasoning"),
    ("openai/gpt-oss-20b",  "GPT-OSS 20B — fast"),
]

# Substrings marking a NON-chat model — filtered out so the dropdown lists only
# models you'd actually converse with (not speech/embedding/image/guard models).
_GROQ_DROP = ("whisper", "distil-whisper", "tts", "guard", "embed",
              "moderation", "playai", "orpheus", "canopylabs")
_GEMINI_DROP = ("embedding", "aqa", "imagen", "veo", "-tts", "image", "learnlm")

# NVIDIA / Mistral / OpenRouter list everything they host, most of it not a chat
# model. Obvious non-chat ids are dropped here; the ranker drops the rest.
_EXTRA_DROP = ("embed", "guard", "safety", "reward", "parse", "clip", "whisper", "tts",
               "retriever", "rerank", "detector", "calibration", "deplot", "kosmos",
               "moderation", "ocr", "translate", "vila", "neva", "fuyu", "starcoder",
               "codegemma", "codellama", "diffusion", "video", "transcribe")
_EXTRA_LABELS = {"nvidia": "NVIDIA", "mistral": "Mistral",
                 "openrouter": "OpenRouter (free models)", "openai": "OpenAI",
                 "anthropic": "Anthropic Claude", "xai": "xAI Grok", "meta": "Meta Llama"}

# OpenAI's /models has no capability field and lists every model it serves. Keep
# the chat families, drop the ones chat/completions can't call (Responses-only
# -pro/codex, audio/realtime) and dated snapshots of an alias that's listed too.
_OPENAI_CHAT = re.compile(r"^(gpt-|o\d|chatgpt-)")
_OPENAI_DROP = re.compile(r"audio|realtime|search|codex|-pro\b|instruct|transcribe|"
                          r"tts|image|-\d{4}-\d{2}-\d{2}$|-\d{4}$")

# The chat composer lets a person choose a capability tier for just the task
# they are about to send. Keep the classification beside discovery so it applies
# equally to curated fallbacks and provider-discovered models.
TASK_TIERS = ("dumb", "moderate", "very_smart")


def normalize_task_tier(value: str) -> str:
    """Return a supported per-task tier, defaulting to ``moderate``."""
    tier = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {"smart": "very_smart", "very": "very_smart", "medium": "moderate"}
    tier = aliases.get(tier, tier)
    return tier if tier in TASK_TIERS else "moderate"


def task_tier_for_model(model: str) -> str:
    """Classify a chat model by its practical speed/capability tier.

    Provider model lists are dynamic, so this uses stable naming cues instead of
    a brittle allow-list. Unknown models land in the useful middle tier.
    """
    m = (model or "").lower()
    if any(token in m for token in (
        "flash-lite", "flash_lite", "gpt-oss-20b", "-8b", "-7b", "-4b", "-3b",
    )):
        return "dumb"
    if any(token in m for token in (
        "-pro", "gpt-oss-120b", "-405b", "-120b", "-72b", "-70b", "-35b",
        "gemini-3.5",
    )):
        return "very_smart"
    return "moderate"


def available_model_ids() -> list[str]:
    """The cached, chat-capable model ids currently exposed by discovery."""
    out: list[str] = []
    for group in (_cache.get("groups") or cached()):
        for opt in group.get("opts", []):
            model = str(opt.get("value") or "").strip()
            if model and model != "auto" and model not in out:
                out.append(model)
    return out


def task_tier_groups() -> list[dict]:
    """Group the currently available chat models for the per-message router."""
    labels = {
        "dumb": "Dumb",
        "moderate": "Moderate",
        "very_smart": "Very smart",
    }
    models = available_model_ids()
    return [{
        "value": tier,
        "label": labels[tier],
        "models": [model for model in models if task_tier_for_model(model) == tier],
    } for tier in TASK_TIERS]

# Last discovered groups + the credential hash they were computed for, so a
# repeat refresh with unchanged creds is a no-op (avoids needless network).
# "caps" holds what a provider itself says a model can do ({id: {"tools": bool}}),
# which the smart-routing ranker trusts over its own guess.
_cache: dict = {"hash": None, "groups": None, "caps": {}}


async def _aget_json(url: str, headers: "dict | None" = None, timeout: float = 8.0):
    """Best-effort GET → parsed JSON, or None on any failure / non-200."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers or {})
            if resp.status_code != 200:
                return None
            return resp.json()
    except Exception:  # noqa: BLE001 — discovery is best-effort; never raise
        return None


def _cred_hash() -> str:
    """A stable hash of the credentials that determine the model list, so a key
    change invalidates the cache and an unchanged set skips re-fetching."""
    from . import groq_bridge as gb
    from . import vertex_auth
    try:
        ollama = gb._ollama_url()
    except Exception:  # noqa: BLE001
        ollama = ""
    parts = [
        gb._api_key or "",
        gb._gemini_key or "",
        "vertex" if vertex_auth.enabled() else "",
        ollama,
        *("|".join(gb._extra_keys.get(p) or []) for p in gb.PREFIXED_PROVIDERS),
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


async def refresh(force: bool = False) -> list:
    """Query each available provider, rebuild the grouped model list, and cache it.
    Returns the groups. Skips the network when creds are unchanged (unless
    ``force``). Always returns at least the Auto group."""
    from . import groq_bridge as gb
    from . import gemini_bridge
    from . import vertex_auth

    h = _cred_hash()
    if not force and _cache["groups"] is not None and _cache["hash"] == h:
        return _cache["groups"]

    groups = [_AUTO_GROUP]

    # ── Google Gemini ── Vertex (curated) takes precedence as the active path;
    # otherwise discover from the Developer-API key.
    if vertex_auth.enabled():
        groups.append({"label": "Google Gemini (Vertex · your credits)",
                       "opts": [{"value": v, "label": l} for v, l in _VERTEX_CURATED]})
    elif gb._gemini_key:
        data = await _aget_json(f"{gemini_bridge.API_BASE}/models?key={gb._gemini_key}")
        opts = []
        for m in (data or {}).get("models", []):
            name = (m.get("name") or "").split("/")[-1]   # "models/gemini-2.5-flash" → id
            methods = m.get("supportedGenerationMethods") or []
            low = name.lower()
            # Only gemini*/gemma* ids: the key also lists other Google models
            # (Lyria music, agents like deep-research) that the router would
            # otherwise mistake for Groq models by their name.
            if (not name or "generateContent" not in methods
                    or not low.startswith(("gemini", "gemma"))):
                continue
            if any(d in low for d in _GEMINI_DROP):
                continue
            opts.append({"value": name, "label": m.get("displayName") or name})
        if not opts:                                       # probe failed → curated (no live model)
            opts = [{"value": v, "label": l} for v, l in _VERTEX_CURATED if "live" not in v]
        groups.append({"label": "Google Gemini (API key)", "opts": opts})

    # ── Groq (cloud fallback) ──
    if gb._api_key:
        data = await _aget_json(f"{gb.GROQ_BASE_URL}/models",
                                headers={"Authorization": f"Bearer {gb._api_key}"})
        opts = []
        for m in (data or {}).get("data", []):
            mid = m.get("id") or ""
            if not mid or any(d in mid.lower() for d in _GROQ_DROP):
                continue
            opts.append({"value": mid, "label": mid})
        opts.sort(key=lambda o: o["value"])
        if not opts:
            opts = [{"value": v, "label": l} for v, l in _GROQ_CURATED]
        groups.append({"label": "Groq (cloud · fallback)", "opts": opts})

    # ── NVIDIA / Mistral / OpenRouter ── OpenAI-compatible; ids get a provider
    # prefix because these catalogs overlap each other and Groq's.
    caps: dict = {}
    for provider in gb.PREFIXED_PROVIDERS:
        keys = gb._extra_keys.get(provider) or []
        if not keys:
            continue
        headers = {"Authorization": f"Bearer {keys[0]}"}
        if provider == "anthropic":
            # Anthropic's /v1/models is its native API, not the OpenAI layer.
            headers = {"x-api-key": keys[0], "anthropic-version": "2023-06-01"}
        data = await _aget_json(f"{gb.OPENAI_COMPAT[provider]}/models", headers=headers)
        opts = []
        for m in (data or {}).get("data", []):
            mid = str(m.get("id") or "")
            if not mid or any(d in mid.lower() for d in _EXTRA_DROP):
                continue
            value = f"{provider}:{mid}"
            if provider == "openrouter":
                # Free models only: a paid OpenRouter model would bill the user's
                # credits on every hard turn the router sends its way.
                if (not mid.endswith(":free")
                        or "tools" not in (m.get("supported_parameters") or [])):
                    continue
                caps[value] = {"tools": True}
            elif provider == "openai" and (not _OPENAI_CHAT.match(mid)
                                           or _OPENAI_DROP.search(mid)):
                continue
            elif provider == "mistral":
                c = m.get("capabilities") or {}
                if c.get("completion_chat") is False:
                    continue
                if "function_calling" in c:
                    caps[value] = {"tools": bool(c["function_calling"])}
            opts.append({"value": value, "label": m.get("display_name") or mid})
        opts.sort(key=lambda o: o["value"])
        if opts:
            groups.append({"label": _EXTRA_LABELS[provider], "opts": opts})

    # ── Local (Ollama · offline) ── only when the local server is reachable.
    try:
        url = gb._ollama_url()
    except Exception:  # noqa: BLE001
        url = ""
    if url:
        data = await _aget_json(f"{url}/api/tags", timeout=2.5)
        opts = [{"value": f"ollama:{m.get('name')}", "label": f"{m.get('name')} (local)"}
                for m in (data or {}).get("models", []) if m.get("name")]
        if opts:
            groups.append({"label": "Local (Ollama · offline)", "opts": opts})

    _cache["hash"] = h
    _cache["groups"] = groups
    _cache["caps"] = caps
    return groups


def caps() -> dict:
    """Provider-published capabilities from the last refresh ({id: {"tools": bool}})."""
    return dict(_cache.get("caps") or {})


def catalog() -> "dict[str, list[str]]":
    """Every discovered cloud chat model, by provider — what the ranker ranks.
    Local and Live-API (native-audio) models are excluded: neither is routed by
    the ranking."""
    from . import groq_bridge as gb
    out: "dict[str, list[str]]" = {}
    for model in available_model_ids():
        provider = gb._provider_for(model)
        if provider == "ollama" or "native-audio" in model or "-live" in model:
            continue
        out.setdefault(provider, []).append(model)
    return out


def cached() -> list:
    """The last discovered groups, synchronously and with NO network — for
    ``get_sysinfo``. Before the first :func:`refresh`, a small curated fallback so
    the dropdown is never empty."""
    if _cache["groups"] is not None:
        return _cache["groups"]
    return [
        _AUTO_GROUP,
        {"label": "Google Gemini",
         "opts": [{"value": v, "label": l} for v, l in _VERTEX_CURATED]},
        {"label": "Groq (cloud · fallback)",
         "opts": [{"value": v, "label": l} for v, l in _GROQ_CURATED]},
    ]


def first_ollama() -> str:
    """The first discovered local model id ("ollama:<name>"), or "" if none cached.
    Synchronous (reads the last refresh) — used by the offline-mode auto-pick so a
    bare 'auto' resolves to whatever model the user actually has installed."""
    for group in (_cache["groups"] or []):
        for opt in group.get("opts", []):
            v = str(opt.get("value") or "")
            if v.lower().startswith("ollama:"):
                return v
    return ""
