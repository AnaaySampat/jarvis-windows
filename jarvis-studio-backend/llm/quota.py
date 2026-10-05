"""Free-tier quota memory: what just failed, and when it comes back.

Before this module JARVIS blind-fired at every provider. A 429 was answered with
one sleep-and-retry on the SAME model and the SAME key, then the user got "I've
hit the rate limit"; the next turn immediately re-picked the identical dead route
because nothing anywhere remembered it was dead.

Three ideas, ported from the freellmapi gateway (github.com/tashfeenahmed/freellmapi):

  1. Groq reports remaining quota on EVERY response via x-ratelimit-* headers.
     Reading them means we know a route is spent BEFORE we waste a call on it.
     (Gemini publishes no such headers, so its ceilings are learned from failures.)
  2. A failed route gets benched on an escalating ladder — 2m, 10m, 1h, then a
     day — so a genuinely exhausted daily quota stops consuming a fallback slot on
     every single turn. A success clears the ladder.
  3. Providers state their real limit in the error body ("Limit 30000, Requested
     33476"). Learning it lets the NEXT request pre-check instead of re-discovering.

State is keyed on a Route (provider, model, key index) and persisted to app data,
because a 24h bench that a restart forgets is not a bench.
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import NamedTuple, Optional

import jarvis_paths

MINUTE = 60.0
HOUR = 60 * MINUTE
DAY = 24 * HOUR


class Route(NamedTuple):
    """One concrete way to answer a turn. ``key_index`` is the position in the
    provider's key list; -1 means the route needs no key (Vertex ADC, Ollama)."""

    provider: str
    model: str
    key_index: int = 0


# ── Cooldowns ────────────────────────────────────────────────────────────────
# Escalate per route over a rolling 24h window so a spent daily quota quarantines
# for the rest of the day rather than looping through a 2-minute bench 20 times.
_COOLDOWN_LADDER = (2 * MINUTE, 10 * MINUTE, HOUR, DAY)

# A failure that carries NO quota information (timeout, 5xx, transport) gets this
# fixed short bench and never advances the ladder. Without that split, two slow
# local Ollama generations would escalate a perfectly healthy route to a day.
# Short, because the common case is Gemini's 503 "high demand", which passes in seconds.
_TRANSIENT_COOLDOWN = 20.0

# A PER-MINUTE ceiling (TPM/RPM) clears within its minute. Bench for exactly what
# the provider asked, clamped to this range, and never climb the day ladder for it:
# a Groq TPM 429 resets in ~8s, and laddering it (2m -> 10m -> 1h -> day) let one
# burst of autopilot steps park every route. Ported from the phone (2026-09-23).
_MINUTE_WINDOW_DEFAULT = 20.0
_MINUTE_WINDOW_MIN = 2.0
_MINUTE_WINDOW_MAX = 65.0

# A local Ollama endpoint has no quota to protect, and it is usually the user's
# ONLY offline route — benching it for minutes turns a slow generation into no
# assistant at all. A few seconds is all the ladder needs there.
_LOCAL_COOLDOWN = 5.0

_cooldowns: "dict[Route, float]" = {}       # route -> expiry (epoch seconds)
_hits: "dict[Route, list[float]]" = {}      # route -> recent bench timestamps
_headers: "dict[Route, tuple[float, float]]" = {}   # route -> (remaining, reset_at)
_learned: "dict[tuple[str, str], dict]" = {}        # (provider, model) -> {kind: limit}
_remaps: "dict[str, str]" = {}                      # retired model id -> replacement
# Seconds per call, per model (an EWMA). Kept with the benches so a model that was
# overloaded or slow last session stays at the back of the autopilot's vision lane
# after a restart, instead of costing a slow round trip to rediscover it.
_latency: "dict[str, float]" = {}

_dirty = False
_last_save = 0.0
_SAVE_INTERVAL = 10.0    # debounce: this is a hint file, not a ledger


def _path():
    return jarvis_paths.app_data_dir() / "quota.json"


def _now() -> float:
    return time.time()


# ── Persistence ──────────────────────────────────────────────────────────────
# JSON, not a database: one user, a handful of routes, and losing the file costs
# at most one wasted request per route. Tuple keys are flattened to strings.

def _route_key(r: Route) -> str:
    return f"{r.provider}\x1f{r.model}\x1f{r.key_index}"


def _parse_route(s: str) -> "Route | None":
    parts = s.split("\x1f")
    if len(parts) != 3:
        return None
    try:
        return Route(parts[0], parts[1], int(parts[2]))
    except ValueError:
        return None


def load() -> None:
    """Restore benches and learned limits. Safe to call more than once."""
    _cooldowns.clear()
    _hits.clear()
    _headers.clear()
    _learned.clear()
    _remaps.clear()
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return
    if not isinstance(data, dict):
        return
    now = _now()
    for raw, expiry in (data.get("cooldowns") or {}).items():
        route = _parse_route(raw)
        if route and isinstance(expiry, (int, float)) and expiry > now:
            _cooldowns[route] = float(expiry)
    for raw, stamps in (data.get("hits") or {}).items():
        route = _parse_route(raw)
        if not route or not isinstance(stamps, list):
            continue
        recent = [float(t) for t in stamps
                  if isinstance(t, (int, float)) and t > now - DAY]
        if recent:
            _hits[route] = recent
    for raw, limits in (data.get("learned") or {}).items():
        provider, _, model = raw.partition("\x1f")
        if model and isinstance(limits, dict):
            _learned[(provider, model)] = {k: v for k, v in limits.items()
                                           if isinstance(v, (int, float))}
    for old, new in (data.get("remaps") or {}).items():
        if isinstance(old, str) and isinstance(new, str) and old and new:
            _remaps[old] = new
    _latency.clear()
    for model, secs in (data.get("latency") or {}).items():
        if isinstance(model, str) and isinstance(secs, (int, float)):
            _latency[model] = float(secs)


def note_latency(model: str, seconds: float) -> None:
    """Fold one observed call duration into the model's average."""
    global _dirty
    prev = _latency.get(model)
    _latency[model] = round(seconds if prev is None else 0.6 * prev + 0.4 * seconds, 2)
    _dirty = True
    save()                                  # debounced


def latency(model: str) -> "float | None":
    return _latency.get(model)


def save(force: bool = False) -> None:
    """Write benches to disk, debounced. Header observations are deliberately NOT
    persisted: they describe a minute-scale window that is stale by the time the
    app restarts, and a stale 'remaining: 0' would strand a healthy route."""
    global _dirty, _last_save
    if not _dirty and not force:
        return
    now = _now()
    if not force and now - _last_save < _SAVE_INTERVAL:
        return
    payload = {
        "cooldowns": {_route_key(r): e for r, e in _cooldowns.items() if e > now},
        "hits": {_route_key(r): [t for t in s if t > now - DAY]
                 for r, s in _hits.items()},
        "learned": {f"{p}\x1f{m}": v for (p, m), v in _learned.items()},
        "remaps": dict(_remaps),
        "latency": dict(_latency),
    }
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
        _dirty = False
        _last_save = now
    except Exception as exc:  # noqa: BLE001
        print(f"[Quota] Couldn't write {path}: {exc}", flush=True)


# ── Provider headers ─────────────────────────────────────────────────────────

def _num(raw) -> "float | None":
    """A header value as a number. Groq sends durations like '7.66s' / '2m59.56s'
    in the reset headers and plain integers in the remaining ones."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total, matched = 0.0, False
    for value, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(ms|s|m|h|d)", text):
        seconds = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}[unit]
        total += float(value) * seconds
        matched = True
    return total if matched else None


def note_response(route: Route, status: int, headers) -> None:
    """Record what the provider just told us about this route's remaining quota.

    Called on EVERY response, 200 included — a 200 that reports 0 remaining is the
    whole point: it lets the next turn skip this route without spending a 429 to
    find out. ``headers`` is anything with a case-insensitive ``.get()``
    (httpx.Headers qualifies); providers that publish nothing are a silent no-op.
    """
    global _dirty
    try:
        get = headers.get
    except AttributeError:
        return

    remaining: "float | None" = None
    reset: "float | None" = None
    for metric in ("requests", "tokens"):
        left = _num(get(f"x-ratelimit-remaining-{metric}"))
        if left is None:
            continue
        after = _num(get(f"x-ratelimit-reset-{metric}")) or 0.0
        # Keep the TIGHTEST axis: whichever of requests/tokens runs out first is
        # the one that will actually 429 us.
        if remaining is None or left < remaining:
            remaining, reset = left, after

    if remaining is not None:
        _headers[route] = (remaining, _now() + (reset or 0.0))

    retry_after = _num(get("retry-after"))
    if status == 429 and retry_after:
        # A 429 always benches via bench(); this just makes sure the header path
        # agrees with it rather than leaving a stale 'plenty remaining' reading.
        _headers[route] = (0.0, _now() + retry_after)
        _dirty = True


# ── Learned limits ───────────────────────────────────────────────────────────
# Ordered day-before-minute and tokens-before-requests so "tokens per day" is not
# shadowed by the 'tpm' alternative and a body mentioning both lands on the more
# specific token ceiling.
_LIMIT_AXES = (
    ("tpd", re.compile(r"tokens?\s*per\s*day|\btpd\b", re.I)),
    ("tpm", re.compile(r"tokens?\s*per\s*min(?:ute)?|\btpm\b", re.I)),
    ("rpd", re.compile(r"requests?\s*per\s*day|\brpd\b", re.I)),
    ("rpm", re.compile(r"requests?\s*per\s*min(?:ute)?|\brpm\b", re.I)),
)
_LIMIT_NUM = re.compile(r"\blimit[:\s]+([\d,]+)", re.I)


def parse_limit(message: "str | None") -> "tuple[str, float] | None":
    """Pull a provider-stated ceiling out of an error body.

    Groq 413s read: "Request too large ... on tokens per minute (TPM): Limit
    30000, Requested 33476". Returns None unless BOTH a number and a confident
    axis are present — guessing the axis would record the wrong ceiling and
    mis-gate every later request, so we refuse to guess.
    """
    if not message:
        return None
    found = _LIMIT_NUM.search(message)
    if not found:
        return None
    try:
        limit = float(found.group(1).replace(",", ""))
    except ValueError:
        return None
    if limit <= 0:
        return None
    for kind, pattern in _LIMIT_AXES:
        if pattern.search(message):
            return kind, limit
    return None


def learn_limit(route: Route, message: "str | None") -> "tuple[str, float] | None":
    """Record a stated ceiling, but only when it makes us MORE conservative.
    Hitting a limit proves our previous belief was too high, never too low."""
    global _dirty
    parsed = parse_limit(message)
    if not parsed:
        return None
    kind, limit = parsed
    slot = _learned.setdefault((route.provider, route.model), {})
    if kind in slot and slot[kind] <= limit:
        return None
    slot[kind] = limit
    _dirty = True
    return parsed


def learned_limits(route: Route) -> dict:
    return dict(_learned.get((route.provider, route.model), {}))


# ── Error bodies ─────────────────────────────────────────────────────────────

def limit_window(message: "str | None") -> "str | None":
    """Which quota WINDOW a rate-limit failure belongs to: "minute", "day", or None
    when the body doesn't say. Reads Groq's "on tokens per minute (TPM)" / "per day"
    wording and Gemini's quotaId ("...PerMinutePerProject..." / "...PerDay...").
    Day is checked first: a body that names both is bound by the slower one."""
    text = (message or "").lower()
    if re.search(r"per\s*day|perday|\b(?:rpd|tpd)\b", text):
        return "day"
    if re.search(r"per\s*min(?:ute)?|perminute|\b(?:rpm|tpm)\b", text):
        return "minute"
    return None


_RETRY_PROSE = re.compile(
    r"(?:try again|retry) in ((?:\d+(?:\.\d+)?\s*(?:ms|s|m|h)\s*)+)", re.I)


def retry_after_in(message: "str | None") -> "float | None":
    """The wait a provider wrote into its error PROSE, in seconds. Groq: "Please try
    again in 7.66s." / "in 2m59.56s"; Gemini: "Please retry in 7.6s." Covers the
    responses that carry no Retry-After header."""
    found = _RETRY_PROSE.search(message or "")
    return _num(found.group(1).strip()) if found else None


_RETIRED = re.compile(r"no longer available.*?use\s+models/([a-z0-9.-]+)", re.I | re.S)


def retired_replacement(status: int, message: "str | None") -> "str | None":
    """The replacement Google names in its own retirement 404, or None.

    Google retires Gemini ids on a schedule and answers a retired one with, verbatim:
    "This model models/gemini-2.0-flash is no longer available. Please update your
    code to use models/gemini-3.6-flash". The error carries the fix, so read it
    instead of benching the route for a day until someone re-pins a constant.
    """
    if status != 404:
        return None
    found = _RETIRED.search(message or "")
    model = found.group(1).lower().rstrip(".,)") if found else ""
    return model if model.startswith(("gemini", "gemma")) else None


# ── Retired-model remap ──────────────────────────────────────────────────────
# Self-healing, NOT a substitute for keeping the pinned ids current: it engages
# only after one request has already 404'd. Persisted, so it costs one failed
# request total rather than one per launch.

_MAX_REMAPS = 24


def record_remap(old: str, new: str) -> None:
    """Remember that ``old`` is retired in favour of ``new``."""
    global _dirty
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new or _remaps.get(old) == new:
        return
    _remaps[old] = new
    while len(_remaps) > _MAX_REMAPS:           # dicts keep insertion order
        del _remaps[next(iter(_remaps))]
    _dirty = True
    save(force=True)


def live_model(model: str) -> str:
    """The id to actually call for ``model``, following recorded retirements.
    Cycle-safe: stops on a revisited id."""
    seen = {model}
    while _remaps.get(model) and _remaps[model] not in seen:
        model = _remaps[model]
        seen.add(model)
    return model


# ── Benching ─────────────────────────────────────────────────────────────────

def bench(route: Route, retry_after: "float | None" = None,
          quota_signal: bool = True, duration: "float | None" = None,
          window: "str | None" = None) -> float:
    """Take this route out of rotation and return the bench length in seconds.

    ``quota_signal`` must be False for failures that carry no quota information
    (timeouts, 5xx, transport errors). Those get the fixed short bench and do NOT
    advance the ladder — otherwise a slow local model escalates itself to a
    day-long quarantine for being slow.

    ``duration`` forces a specific bench, for failures where the ladder's reasoning
    doesn't apply (a rejected key, a model that no longer exists).

    ``window`` is the quota window the provider named (:func:`limit_window`).
    ``"minute"`` benches for the provider's own wait (2-65s) and never advances the
    ladder: that ceiling is gone before a 2-minute bench would even end.

    An explicit ``retry_after`` from the provider is honoured as a FLOOR: we never
    bench shorter than our own reasoning, but we do extend when the provider asks
    for longer than we would have.
    """
    global _dirty
    now = _now()

    if duration is None and quota_signal and window == "minute":
        duration = min(_MINUTE_WINDOW_MAX,
                       max(_MINUTE_WINDOW_MIN, retry_after or _MINUTE_WINDOW_DEFAULT))
        retry_after = None      # already applied, and clamped
    elif duration is not None:
        pass
    elif route.provider == "ollama":
        duration = _LOCAL_COOLDOWN
    elif not quota_signal:
        duration = _TRANSIENT_COOLDOWN
    else:
        recent = [t for t in _hits.get(route, []) if t > now - DAY]
        recent.append(now)
        _hits[route] = recent
        duration = _COOLDOWN_LADDER[min(len(recent) - 1, len(_COOLDOWN_LADDER) - 1)]

    if retry_after and retry_after > duration:
        duration = min(retry_after, DAY)

    _cooldowns[route] = now + duration
    _headers.pop(route, None)
    _dirty = True
    save()
    return duration


def on_success(route: Route) -> None:
    """A served request proves this route is alive: drop its bench and reset the
    ladder, so the next failure starts at 2 minutes instead of inheriting steps."""
    global _dirty
    # Both pops must run: `or` would short-circuit past the hit ladder as soon as
    # a cooldown was found, leaving the route to resume mid-ladder on its next
    # failure instead of restarting at two minutes.
    had_cooldown = _cooldowns.pop(route, None) is not None
    had_hits = _hits.pop(route, None) is not None
    # Header observations deliberately survive. note_response runs just before this
    # on every 200, and a success that reports 0 tokens remaining is precisely the
    # signal we want to keep — clearing it here would throw away the pre-flight
    # gate and send the next turn straight into the 429 this module exists to dodge.
    if had_cooldown or had_hits:
        _dirty = True
        save()


def usable(route: Route) -> bool:
    """False when we already know this route will fail."""
    now = _now()
    expiry = _cooldowns.get(route)
    if expiry is not None:
        if now < expiry:
            return False
        del _cooldowns[route]

    observed = _headers.get(route)
    if observed is not None:
        remaining, reset_at = observed
        if now >= reset_at:
            del _headers[route]
        elif remaining <= 0:
            return False
    return True


def cooldown_remaining(route: Route) -> float:
    """Seconds until this route is usable again; 0 when it already is."""
    return max(0.0, _cooldowns.get(route, 0.0) - _now())


def ready_in(route: Route) -> float:
    """Seconds until this route can be tried again: its bench, or a provider
    header that said nothing is left until a reset. 0 when it's usable now."""
    now = _now()
    held = max(0.0, _cooldowns.get(route, 0.0) - now)
    observed = _headers.get(route)
    if observed is not None and observed[0] <= 0:
        held = max(held, observed[1] - now)
    return held


def soonest_reset() -> "Optional[float]":
    """Seconds until the FIRST benched route returns, or None when nothing is
    benched. Feeds the user-facing exhaustion message so it names a real time."""
    now = _now()
    live = [e for e in _cooldowns.values() if e > now]
    live += [r for _, r in _headers.values() if r > now]
    return min(live) - now if live else None


def format_eta(seconds: "float | None") -> str:
    """A spoken-language ETA. JARVIS talks, so '4 minutes' beats '243s'."""
    if seconds is None or seconds <= 0:
        return "shortly"
    if seconds < 90:
        return "under a minute"
    if seconds < HOUR:
        return f"about {round(seconds / MINUTE)} minutes"
    if seconds < DAY:
        hours = round(seconds / HOUR)
        return "about an hour" if hours <= 1 else f"about {hours} hours"
    return "tomorrow"


def exhausted_message(what: str = "every model I can reach") -> str:
    """The only rate-limit sentence the user should ever see — and only once every
    route is genuinely spent, with a real time rather than 'give me a moment'."""
    return (f"I've used up the free quota on {what}, sir. "
            f"It should free up {format_eta(soonest_reset())}.")


def snapshot() -> dict:
    """Diagnostics for the Settings panel / logs. Read-only."""
    now = _now()
    return {
        "benched": [
            {"provider": r.provider, "model": r.model, "key": r.key_index,
             "seconds_left": round(e - now)}
            for r, e in sorted(_cooldowns.items(), key=lambda kv: kv[1])
            if e > now
        ],
        "remaining": {
            f"{r.provider}/{r.model}#{r.key_index}": rem
            for r, (rem, reset) in _headers.items() if reset > now
        },
        "learned": {f"{p}/{m}": v for (p, m), v in _learned.items()},
        "remaps": dict(_remaps),
    }


def clear() -> None:
    """Drop every bench, on disk too — the escape hatch.

    An escalated bench can park a route for a day off one bad window. Called when
    the user saves their API keys, which is exactly the moment they've fixed the
    cause (corrected a rejected key, added a fresh one) and should not have to wait
    out a quarantine that no longer applies. Learned limits survive: those are
    facts about the model, not about the key.
    """
    global _dirty
    _cooldowns.clear()
    _hits.clear()
    _headers.clear()
    _dirty = True
    save(force=True)


def reset() -> None:
    """Test seam: forget everything in memory (leaves the file alone)."""
    global _dirty
    _cooldowns.clear()
    _hits.clear()
    _headers.clear()
    _learned.clear()
    _remaps.clear()
    _latency.clear()
    _dirty = False
