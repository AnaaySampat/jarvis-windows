"""Smart routing: rank every model the user's keys can reach, and send each turn to
the weakest model that is still good enough.

Scores come from real benchmarks first (llm/model_catalog.py — the Artificial
Analysis Intelligence Index). An LLM with Google Search is asked ONLY about models
the catalog doesn't know, with the catalog's real values as anchors; its answers
are cached per model, so each unknown model costs one lookup, ever (the Re-rank
button asks again). Tiers come from rules, never from the LLM.

Because scoring is now deterministic, the ranking is simply recomputed whenever the
reachable models change — a key added OR removed, a provider's catalog changing.

Each ranked model carries:
    score   Artificial Analysis Intelligence Index (or an anchored estimate)
    tier    fast | mid | flagship — what kind of turn it is good enough for
    tools   whether it does native function calling (JARVIS's actions need it)
    source  benchmark | llm | guess
    basis   where the score came from, for Settings

Routing (``ladder``) is "good enough first": the turn's difficulty picks a tier;
that tier is tried best-first, then stronger tiers, and weaker ones last.
"""

from __future__ import annotations

import json
import os
import re
import time

import jarvis_paths

from . import model_catalog as catalog

TIERS = ("fast", "mid", "flagship")
_FILE = "model_ranking.json"
_BATCH = 30
# Estimates on another scale (e.g. LMArena Elo ~1200) are rejected whole.
_MAX_ESTIMATE = 70.0

_state: "dict | None" = None
_loaded = False
ranking_in_progress = False
rerank_requested = False  # the Re-rank button was pressed; discovery runs first
last_result = ""          # one line for Settings: how the last ranking went


def _path():
    return jarvis_paths.app_data_dir() / _FILE


def load() -> "dict | None":
    global _state, _loaded
    if not _loaded:
        _loaded = True
        try:
            data = json.loads(_path().read_text(encoding="utf-8"))
            _state = data if isinstance(data, dict) and data.get("models") else None
        except Exception:  # noqa: BLE001 — no/corrupt file = not ranked yet
            _state = None
    return _state


def _save() -> None:
    try:
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(_state, indent=1), encoding="utf-8")
        os.replace(tmp, p)
    except Exception as exc:  # noqa: BLE001
        print(f"[Ranker] couldn't save ranking: {exc}", flush=True)


def has_ranking() -> bool:
    return bool(load())


def estimates() -> dict:
    """Cached LLM estimates, keyed by normalized model name."""
    return dict((load() or {}).get("estimates") or {})


def no_tools() -> "set[str]":
    return set((load() or {}).get("no_tools") or [])


# ── Scoring ───────────────────────────────────────────────────────────────────

def unknown_models(models: "list[str]") -> "list[str]":
    """Chat models the benchmark catalog doesn't cover (normalized, de-duplicated)."""
    out: "list[str]" = []
    for m in models:
        if catalog.excluded(m) or catalog.lookup(m) is not None:
            continue
        n = catalog.normalize(m)
        if n not in out:
            out.append(n)
    return out


def compute(models: "list[str]", caps: "dict | None" = None,
            estimated: "dict | None" = None, rejected_tools: "set | None" = None) -> "list[dict]":
    """Rank ``models`` (ids as routed, provider-prefixed where needed), best first.

    Pure and deterministic: catalog score, else the cached LLM estimate, else a
    conservative guess that keeps an unscored model behind every scored one of its
    class. Tool support is on unless the provider published otherwise (``caps``)
    or the model was seen rejecting tool calls (``rejected_tools``) — an LLM's
    opinion is never used (measured: it wrongly denied Groq's gpt-oss)."""
    caps, estimated, rejected_tools = caps or {}, estimated or {}, rejected_tools or set()
    rows: "list[dict]" = []
    for m in dict.fromkeys(models):
        if catalog.excluded(m):
            continue
        known = catalog.lookup(m)
        if known is not None:
            if known.get("chat") is False:
                continue
            score, tier, basis, source = known["score"], known["tier"], known["basis"], "benchmark"
        else:
            est = estimated.get(catalog.normalize(m))
            if est and est.get("chat") is False:
                continue
            if est:
                score, basis, source = float(est["score"]), est.get("basis", ""), "llm"
            else:
                score, basis, source = catalog.guess_score(m), "no benchmark data yet", "guess"
            tier = catalog.tier_for(m, score)
        tools = bool(caps.get(m, {}).get("tools", True)) and m not in rejected_tools
        rows.append({"id": m, "score": round(score, 1), "tier": tier, "tools": tools,
                     "source": source, "basis": basis[:80]})
    rows.sort(key=lambda r: (-r["score"], r["id"]))
    return rows


def store(rows: "list[dict]", estimated: dict, estimated_by: str = "",
          estimate_failed_at: "float | None" = None) -> None:
    global _state, _loaded
    prev = load() or {}
    _loaded = True
    _state = {"ranked_at": time.time(), "models": rows, "estimates": estimated,
              "estimated_by": estimated_by or prev.get("estimated_by", ""),
              "estimate_failed_at": estimate_failed_at,
              "no_tools": sorted(no_tools()), "catalog": catalog.SNAPSHOT}
    _save()


# ── Routing ───────────────────────────────────────────────────────────────────

def ladder(tier: str, needs_tools: bool, available: "set[str]") -> "list[str]":
    """Model ids to try, in order, for a turn that needs ``tier``: that tier
    best-first, then each stronger tier, then (last resort) the weaker ones."""
    state = load()
    if not state:
        return []
    want = TIERS.index(tier) if tier in TIERS else 1

    def order(m: dict) -> tuple:
        t = TIERS.index(m["tier"])
        group = 0 if t == want else (t - want if t > want else 10 + (want - t))
        return (group, -m["score"])

    rows = [m for m in state["models"]
            if m["id"] in available and (m.get("tools", True) or not needs_tools)]
    return [m["id"] for m in sorted(rows, key=order)]


def info(model: str) -> "dict | None":
    for m in (load() or {}).get("models", []):
        if m["id"] == model:
            return m
    return None


def tools_ok(model: str) -> bool:
    m = info(model)
    return True if m is None else bool(m.get("tools", True))


def mark_no_tools(model: str) -> None:
    """A provider rejected tool calling for this model — remember it (it survives
    re-ranking) so later action turns route past it instead of re-paying the 400."""
    state = load()
    if not state or model in state.setdefault("no_tools", []):
        return
    state["no_tools"].append(model)
    m = info(model)
    if m:
        m["tools"] = False
    _save()
    print(f"[Ranker] {model} rejected tool calls — excluded from action turns", flush=True)


def summary() -> dict:
    """What the Settings panel shows."""
    state = load() or {}
    models = state.get("models") or []
    counts = {s: sum(1 for m in models if m.get("source") == s)
              for s in ("benchmark", "llm", "guess")}
    return {"in_progress": ranking_in_progress or rerank_requested, "last_result": last_result,
            "ranked_at": state.get("ranked_at"), "catalog": state.get("catalog", ""),
            "estimated_by": state.get("estimated_by", ""), "counts": counts,
            "models": models}


def reset_for_tests() -> None:
    global _state, _loaded
    _state, _loaded = None, True


# ── Estimating the models the catalog doesn't know ────────────────────────────

ESTIMATE_SYSTEM = (
    "You estimate how capable large language models are, for the model router of a "
    "voice assistant. Use Google Search to look up each model — its Artificial "
    "Analysis Intelligence Index if published, else its release date, size and "
    "published benchmarks — rather than memory. Answer with ONLY a JSON array."
)


def batches(ids: "list[str]", size: int = _BATCH) -> "list[list[str]]":
    return [ids[i:i + size] for i in range(0, len(ids), size)]


def estimate_prompt(ids: "list[str]", today: str) -> str:
    return "\n".join([
        f"Today is {today}. Estimate each model's Artificial Analysis Intelligence "
        "Index. For reference, these are REAL current values on that scale:",
        catalog.anchors(),
        "",
        "Models to estimate (copy each id exactly):",
        *(f"- {i}" for i in ids),
        "",
        'Return [{"id": "<id>", "score": <number on the scale above>, '
        '"basis": "<max 8 words: where the estimate came from>"}]. Use the published '
        "value when one exists; otherwise compare with the reference models of the "
        "same generation and size. A model that is not a general chat model (speech, "
        'image, embedding, OCR, safety classifier, translation-only) gets {"id": '
        '"<id>", "chat": false}.',
    ])


def _extract_json_array(text: str):
    t = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if fence:
        t = fence.group(1).strip()
    start, end = t.find("["), t.rfind("]")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(t[start:end + 1])
    except json.JSONDecodeError:
        return None


def parse_estimates(text: str, ids: "list[str]") -> dict:
    """{normalized id: {"score","basis"} | {"chat": False}} for the ids asked about.
    Anything else is dropped; a batch on another scale is rejected whole."""
    rows = _extract_json_array(text)
    if not isinstance(rows, list):
        return {}
    out: dict = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        n = catalog.normalize(str(row.get("id") or ""))
        if n not in ids or n in out:
            continue
        if row.get("chat") is False:
            out[n] = {"chat": False}
            continue
        try:
            score = float(row.get("score"))
        except (TypeError, ValueError):
            continue
        if score > _MAX_ESTIMATE:
            return {}
        out[n] = {"score": round(max(0.0, score), 1),
                  "basis": "est. " + str(row.get("basis") or "")[:70]}
    return out
