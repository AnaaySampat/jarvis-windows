"""Curated model catalog: real benchmark scores for the model families JARVIS can
reach, so smart routing doesn't depend on an LLM's guess.

Scores are the Artificial Analysis Intelligence Index (artificialanalysis.ai,
snapshot below). Measured, an LLM ranking from search alone was far off:
gpt-oss-120b 33-65 (real: 12 at high effort), Gemini 3.1 Pro above 3.8 Flash
(real: 30 vs 41), Nemotron 3 Super 44-57 (real: 13). The table is the ground truth;
the ranker asks an LLM only about models that aren't in it.

Where AA lists several reasoning efforts, the score is the one JARVIS actually
runs at: Groq gpt-oss gets reasoning_effort=low; other providers use their
default, taken as the middle of the published range. Older models that AA no
longer tracks sit below their tracked successor (marked "older" in the note).

To update: re-read the leaderboard, edit KNOWN, bump SNAPSHOT.
"""

from __future__ import annotations

import re

SNAPSHOT = "Sep 2026"

# At/above this a model is "flagship" (unless it is a small/fast model).
FLAGSHIP_MIN = 35.0

# (pattern on the normalized id, score, tier, note). First match wins, so a more
# specific pattern must come before a general one.
KNOWN: "list[tuple[str, float, str, str]]" = [
    # ── Google Gemini / Gemma ─────────────────────────────────────────────────
    (r"^gemini-3\.8-flash-lite", 24, "fast", "AA est. (3.5 Flash-Lite 22-24)"),
    (r"^gemini-3\.8-flash", 41, "flagship", "AA 41"),
    (r"^gemini-3\.7-flash", 39, "flagship", "AA 37-40"),
    (r"^gemini-3\.6-flash", 34, "mid", "AA 34"),
    (r"^gemini-3\.5-flash-lite", 22, "fast", "AA 22"),
    (r"^gemini-3\.5-flash", 33, "mid", "AA 33-34"),
    (r"^gemini-3\.1-pro", 30, "mid", "AA 30"),
    (r"^gemini-3\.1-flash-lite", 16, "fast", "AA 16"),
    (r"^gemini-3-flash", 26, "mid", "AA 26"),
    (r"^gemini-2\.5-pro", 16, "mid", "AA 16"),
    (r"^gemini-2\.5-flash-lite", 9, "fast", "AA 9"),
    (r"^gemini-2\.5-flash", 13, "mid", "AA 13"),
    (r"^gemini-2\.0-flash-lite", 5, "fast", "older"),
    (r"^gemini-2\.0-flash", 7, "fast", "older"),
    (r"^gemma-4-31b", 19, "mid", "AA 19"),
    (r"^gemma-4-26b", 17, "mid", "AA 17"),
    (r"^gemma-4-12b", 14, "fast", "AA 14"),
    (r"^gemma-4-e4b", 9, "fast", "AA 9"),
    (r"^gemma-4-e2b", 8, "fast", "AA 8"),
    (r"^gemma-3n", 5, "fast", "AA 5"),
    (r"^gemma-3-", 5, "fast", "AA 4-5"),
    (r"^(recurrent)?gemma-2", 2, "fast", "older"),
    # ── OpenAI open weights (Groq runs them at reasoning_effort=low) ─────────
    (r"^gpt-oss-120b", 10, "mid", "AA 10 (low effort)"),
    (r"^gpt-oss-20b", 9, "fast", "AA 9-10"),
    (r"^gpt-oss-safeguard", -1, "", "classifier"),
    # ── Qwen / Kimi / GLM / DeepSeek / MiniMax ────────────────────────────────
    (r"^qwen3\.8-27b", 26, "mid", "AA 20-34 by effort"),
    (r"^qwen3\.6-35b", 18, "mid", "AA 18"),
    (r"^qwen3\.5-9b", 14, "fast", "AA 14"),
    (r"^qwen3\.5-4b", 13, "fast", "AA 13"),
    (r"^kimi-k3", 39, "flagship", "AA 34-44 by effort"),
    (r"^kimi-k2\.7", 26, "mid", "AA 26"),
    (r"^glm-5\.3-flash", 42, "flagship", "AA 42"),
    (r"^glm-5\.3", 40, "flagship", "AA 45 max effort"),
    (r"^deepseek-v4\.1-flash", 35, "flagship", "AA 39 max effort"),
    (r"^deepseek-coder", 1, "fast", "older"),
    (r"^minimax-m3", 29, "mid", "AA 29"),
    # ── NVIDIA Nemotron ───────────────────────────────────────────────────────
    (r"^nemotron-3-ultra", 23, "mid", "AA 23"),
    (r"^nemotron-3\.5-lightning", 13, "fast", "AA 13"),
    (r"^nemotron-3-super", 13, "mid", "AA 13"),
    (r"^nemotron-3-nano-omni", 10, "fast", "AA 10"),
    (r"^nemotron-(3-nano|nano-3)", 9, "fast", "AA 9"),
    (r"^llama-3\.1-nemotron-ultra", 8, "mid", "AA 8"),
    (r"^llama-3\.1-nemotron-70b", 5, "mid", "older"),
    (r"^llama-3\.1-nemotron-51b", 4, "mid", "older"),
    (r"^nemotron-4-340b", 4, "mid", "older"),
    (r"^mistral-nemo-minitron", 2, "fast", "older"),
    (r"^llama3-chatqa", 2, "mid", "older"),
    # ── Meta Llama ────────────────────────────────────────────────────────────
    (r"^llama-4-maverick", 10, "mid", "AA 10"),
    (r"^llama-4-scout", 8, "fast", "AA 8"),
    (r"^llama-3\.3-70b", 8, "mid", "AA 8"),
    (r"^llama-3\.1-405b", 7, "mid", "AA 7"),
    (r"^llama-3\.2-90b", 6, "mid", "AA 6"),
    (r"^llama-3\.2-11b", 5, "fast", "AA 5"),
    (r"^llama-?2", 1, "fast", "older"),
    (r"^muse-glimmer", 17, "mid", "AA 17"),
    # ── Mistral (Mistral API ids and NVIDIA-hosted ones) ──────────────────────
    (r"^mistral-large-2(-|$)", 4, "mid", "older"),
    (r"^mistral-large-(latest|3|25)", 9, "mid", "AA 9"),
    (r"^mistral-large$", 4, "mid", "older"),
    (r"^mistral-medium", 14, "mid", "AA 14"),
    (r"^mistral-small", 11, "fast", "AA 9-11"),
    (r"^magistral-medium", 12, "mid", "AA 12"),
    (r"^magistral-small", 9, "fast", "AA 9"),
    (r"^ministral-(3-)?14b|^ministral-14b", 6, "fast", "AA 6"),
    (r"^ministral", 5, "fast", "AA 5"),
    (r"^mistral-7b", 2, "fast", "older"),
    (r"^mistral-nemo-\d", 3, "fast", "older"),     # not mistral-nemotron (2025)
    (r"^mixtral", 3, "mid", "older"),
    (r"^(codestral|devstral)", 6, "mid", "coding model"),
    (r"^pixtral", 5, "mid", "older"),
    # ── Others seen on NVIDIA / OpenRouter / Groq ─────────────────────────────
    (r"^inkling-small", 28, "fast", "AA 28"),
    (r"^inkling", 25, "mid", "AA 25"),
    (r"^ling-3\.0-flash-fin", 23, "mid", "AA 23"),
    (r"^ling-3\.0-flash", 25, "mid", "AA 25"),
    (r"^north-mini-code", 10, "fast", "AA 10"),
    (r"^lfm-?2\.5-2\.6b", 8, "fast", "AA 8"),
    (r"^jamba-1\.7", 6, "mid", "AA 6"),
    (r"^jamba", 4, "mid", "older"),
    (r"^phi-4", 6, "fast", "AA 6"),
    (r"^phi-3", 2, "fast", "older"),
    (r"^granite-4\.2-30b", 15, "mid", "AA 15"),
    (r"^granite-4\.2-8b", 11, "fast", "AA 11"),
    (r"^granite", 2, "fast", "older"),
    (r"^(yi-large|dbrx)", 2, "mid", "older"),
    (r"^(sea-lion|zamba2|allam)", 2, "fast", "older / small"),
    (r"^palmyra", 4, "mid", "domain model"),
]

# Not general chat models — never routed to, whatever an LLM says.
NON_CHAT = re.compile(
    r"embed|guard|safety|reward|rerank|retriev|whisper|tts|transcribe|parse|ocr|"
    r"clip|robotics|computer-use|image|imagen|veo|lyria|detector|calibration|"
    r"deplot|kosmos|diffusion|safeguard|cosmos-reason|deep-research|antigravity|"
    r"nano-banana|orpheus|fuyu|neva|vila|translate|starcoder|codegemma|codellama")

# Aliases that silently re-point to another model; the concrete ids are ranked.
_ALIAS = re.compile(r"-latest$")
_ALIAS_OK = re.compile(r"^(mistral|magistral|ministral|codestral|devstral|pixtral)")

# Whole tokens only: a bare "mini" would match inside "gemini".
_SPEED = re.compile(r"(^|[-_.])(lite|mini|nano|tiny|small|xs|instant|lightning|haiku|"
                    r"e\d+b|[1-9]b|1[0-4]b)([-_.:]|$)")


def normalize(model: str) -> str:
    """'openrouter:nvidia/nemotron-3-super-120b-a12b:free' → 'nemotron-3-super-120b-a12b'."""
    m = (model or "").strip().lower()
    head, sep, rest = m.partition(":")
    if sep and head in ("nvidia", "mistral", "openrouter", "openai", "anthropic", "xai", "meta"):
        m = rest
    return m.removesuffix(":free").rsplit("/", 1)[-1]


def excluded(model: str) -> bool:
    """Non-chat models, and auto-updating aliases (their target is ranked itself)."""
    n = normalize(model)
    if NON_CHAT.search(n):
        return True
    return bool(_ALIAS.search(n)) and not _ALIAS_OK.search(n)


def lookup(model: str) -> "dict | None":
    """{'score','tier','basis'} for a known family, {'chat': False} for a known
    non-chat model, None when the table doesn't know it."""
    n = normalize(model)
    for pattern, score, tier, note in KNOWN:
        if re.search(pattern, n):
            if score < 0:
                return {"chat": False}
            return {"score": float(score), "tier": tier,
                    "basis": f"{note} ({SNAPSHOT})" if note.startswith("AA") else note}
    return None


def tier_for(model: str, score: float) -> str:
    """Tier for a model the table doesn't know: small/fast by name, else by score."""
    if _SPEED.search(normalize(model)):
        return "fast"
    return "flagship" if score >= FLAGSHIP_MIN else "mid"


def guess_score(model: str) -> float:
    """Conservative score for an unknown model when no LLM estimate exists: below
    every tracked current model of its class, so it is only a late fallback."""
    return 4.0 if _SPEED.search(normalize(model)) else 6.0


def anchors(limit: int = 40) -> str:
    """Known scores as reference points for the LLM that estimates unknown models."""
    rows = [(p.strip("^$").replace("\\", "").split("(")[0].rstrip("-"), s)
            for p, s, _t, note in KNOWN if s > 0 and note.startswith("AA")]
    rows.sort(key=lambda r: -r[1])
    step = max(1, len(rows) // limit)
    return "\n".join(f"- {name}: {score:g}" for name, score in rows[::step][:limit])
