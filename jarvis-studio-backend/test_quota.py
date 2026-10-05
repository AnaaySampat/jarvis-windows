"""Checks for llm/quota.py — the free-tier route memory.

Run: python test_quota.py   (or: python -m pytest test_quota.py)

The logic worth guarding is the bench decision. Getting it wrong is expensive in
both directions: too eager and a healthy route is stranded for a day, too lax and
we re-hammer a spent key on every turn — which is the bug this module exists to
fix. So the cases below pin the ladder, the Retry-After floor, and the
quota-signal gate that keeps a slow local model out of the escalation path.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Point app data at a scratch dir BEFORE quota resolves its file path.
_TMP = tempfile.mkdtemp(prefix="jarvis-quota-test-")
import os
os.environ["JARVIS_CONFIG_DIR"] = _TMP

from llm import quota  # noqa: E402

R = quota.Route("groq", "openai/gpt-oss-120b", 0)
R2 = quota.Route("groq", "openai/gpt-oss-120b", 1)
LOCAL = quota.Route("ollama", "ollama:llama3.1", 0)


class FakeHeaders:
    """Minimal stand-in for httpx.Headers — only .get() is used."""

    def __init__(self, data):
        self._data = {k.lower(): v for k, v in data.items()}

    def get(self, name, default=None):
        return self._data.get(name.lower(), default)


def test_ladder_escalates_then_caps():
    quota.reset()
    steps = [quota.bench(R) for _ in range(5)]
    assert steps[0] == 2 * quota.MINUTE, steps
    assert steps[1] == 10 * quota.MINUTE, steps
    assert steps[2] == quota.HOUR, steps
    assert steps[3] == quota.DAY, steps
    assert steps[4] == quota.DAY, "ladder must cap, not keep growing"


def test_success_resets_the_ladder():
    quota.reset()
    quota.bench(R)
    quota.bench(R)
    quota.on_success(R)
    assert quota.usable(R), "a served request must clear the bench"
    assert quota.bench(R) == 2 * quota.MINUTE, "ladder must restart after a success"


def test_transient_failure_never_escalates():
    """A timeout says nothing about quota. Without this gate two slow local
    generations would quarantine a perfectly healthy route for a day."""
    quota.reset()
    for _ in range(5):
        held = quota.bench(R, quota_signal=False)
        assert held == quota._TRANSIENT_COOLDOWN, held


def test_retry_after_is_a_floor_not_a_ceiling():
    quota.reset()
    # Longer than our own guess → honour the provider.
    assert quota.bench(R, retry_after=600.0) == 600.0
    quota.reset()
    # Shorter than our own guess → keep ours; the provider is only a lower bound.
    assert quota.bench(R, retry_after=5.0) == 2 * quota.MINUTE
    quota.reset()
    # Never longer than a day, however wild the header.
    assert quota.bench(R, retry_after=99 * quota.DAY) == quota.DAY


def test_local_endpoint_gets_a_token_bench():
    """A local model has no quota to protect and is usually the only offline
    route — benching it for minutes turns 'slow' into 'no assistant'."""
    quota.reset()
    for _ in range(4):
        assert quota.bench(LOCAL) == 5.0


def test_bench_is_per_key_not_per_model():
    quota.reset()
    quota.bench(R)
    assert not quota.usable(R)
    assert quota.usable(R2), "a second key must survive the first one's bench"


def test_explicit_duration_overrides_the_ladder():
    quota.reset()
    assert quota.bench(R, duration=quota.DAY, quota_signal=False) == quota.DAY


def test_headers_bench_before_a_429_happens():
    """The whole point: a 200 that reports 0 remaining must take the route out of
    rotation, so the NEXT turn never spends a 429 rediscovering it."""
    quota.reset()
    quota.note_response(R, 200, FakeHeaders({
        "x-ratelimit-remaining-requests": "50",
        "x-ratelimit-remaining-tokens": "0",
        "x-ratelimit-reset-tokens": "7.66s",
    }))
    assert not quota.usable(R), "0 tokens remaining must bench the route"


def test_success_does_not_erase_a_zero_remaining_reading():
    """note_response runs just before on_success on every 200. A success proves the
    route answered, NOT that quota is left — if the same response said 0 remaining,
    that reading has to survive or the next turn walks into the 429 anyway."""
    quota.reset()
    quota.note_response(R, 200, FakeHeaders({
        "x-ratelimit-remaining-tokens": "0",
        "x-ratelimit-reset-tokens": "30s",
    }))
    quota.on_success(R)
    assert not quota.usable(R)


def test_headers_keep_the_tightest_axis():
    quota.reset()
    quota.note_response(R, 200, FakeHeaders({
        "x-ratelimit-remaining-requests": "0",
        "x-ratelimit-remaining-tokens": "9000",
        "x-ratelimit-reset-requests": "2m30s",
    }))
    assert not quota.usable(R), "requests exhausted must bench even if tokens remain"


def test_healthy_headers_leave_the_route_usable():
    quota.reset()
    quota.note_response(R, 200, FakeHeaders({
        "x-ratelimit-remaining-requests": "900",
        "x-ratelimit-remaining-tokens": "12000",
        "x-ratelimit-reset-tokens": "3s",
    }))
    assert quota.usable(R)


def test_header_duration_parsing():
    assert quota._num("7.66s") == 7.66
    assert quota._num("2m59.56s") == 179.56
    assert quota._num("1h30m") == 5400.0
    assert quota._num("60") == 60.0
    assert quota._num("") is None
    assert quota._num(None) is None


def test_learn_limit_reads_the_provider_ceiling():
    quota.reset()
    msg = ("Request too large for model `openai/gpt-oss-120b` on tokens per "
           "minute (TPM): Limit 30000, Requested 33476")
    assert quota.learn_limit(R, msg) == ("tpm", 30000.0)
    assert quota.learned_limits(R) == {"tpm": 30000.0}


def test_learn_limit_only_ever_lowers():
    """Hitting a ceiling proves our belief was too HIGH. Raising it on a later,
    looser message would re-open the exact gap that caused the 429."""
    quota.reset()
    quota.learn_limit(R, "tokens per minute (TPM): Limit 30000")
    quota.learn_limit(R, "tokens per minute (TPM): Limit 90000")
    assert quota.learned_limits(R) == {"tpm": 30000.0}
    quota.learn_limit(R, "tokens per minute (TPM): Limit 6000")
    assert quota.learned_limits(R) == {"tpm": 6000.0}


def test_learn_limit_refuses_to_guess_the_axis():
    quota.reset()
    assert quota.learn_limit(R, "Limit 30000, Requested 33476") is None, \
        "a number with no stated axis must not be recorded against a column"
    assert quota.learn_limit(R, "tokens per minute (TPM): no number here") is None
    assert quota.learned_limits(R) == {}


def test_day_axis_wins_over_minute():
    assert quota.parse_limit("requests per day (RPD): Limit 1000")[0] == "rpd"
    assert quota.parse_limit("tokens per day: Limit 500000")[0] == "tpd"


def test_soonest_reset_and_eta_wording():
    quota.reset()
    assert quota.soonest_reset() is None
    quota.bench(R)                       # 2 minutes
    quota.bench(R2, retry_after=3600.0)  # an hour
    soonest = quota.soonest_reset()
    assert 110 < soonest <= 120, soonest
    assert quota.format_eta(soonest) == "about 2 minutes"
    assert "2 minutes" in quota.exhausted_message()


def test_clear_unbenches_everything():
    quota.reset()
    quota.bench(R)
    quota.bench(R2)
    quota.learn_limit(R, "tokens per minute (TPM): Limit 30000")
    quota.clear()
    assert quota.usable(R) and quota.usable(R2)
    assert quota.learned_limits(R) == {"tpm": 30000.0}, \
        "learned limits are facts about the model, not the key — they survive"


def test_benches_survive_a_restart():
    """A 24h daily-exhaustion bench that a restart forgets is not a bench."""
    quota.reset()
    quota.bench(R, duration=quota.DAY)
    quota.save(force=True)
    quota.reset()
    assert quota.usable(R), "sanity: in-memory state really was dropped"
    quota.load()
    assert not quota.usable(R), "the bench must come back from disk"
    assert quota.cooldown_remaining(R) > quota.HOUR


def test_expired_benches_are_not_restored():
    quota.reset()
    quota.bench(R, duration=1.0)
    quota._cooldowns[R] = quota._now() - 5     # pretend it lapsed
    quota.save(force=True)
    quota.reset()
    quota.load()
    assert quota.usable(R)


GROQ_TPM = ("Rate limit reached for model `openai/gpt-oss-120b` on tokens per minute "
            "(TPM): Limit 8000, Used 7000, Requested 1500. Please try again in 7.66s.")
GEMINI_DAY = ('"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier" '
              '"retryDelay": "40s"')


def test_limit_window_reads_both_providers():
    assert quota.limit_window(GROQ_TPM) == "minute"
    assert quota.limit_window('"quotaId": "GenerateRequestsPerMinutePerProject"') == "minute"
    assert quota.limit_window(GEMINI_DAY) == "day"
    assert quota.limit_window("tokens per day (TPD): Limit 500000") == "day"
    assert quota.limit_window("rate limit reached") is None


def test_retry_prose_is_parsed():
    assert quota.retry_after_in(GROQ_TPM) == 7.66
    assert quota.retry_after_in("Please retry in 2m59.56s.") == 179.56
    assert quota.retry_after_in("rate limit reached") is None


def test_minute_window_benches_for_seconds_and_never_climbs():
    """The phone's 2026-09-23 bug: TPM 429s that reset in ~8s climbed the day ladder."""
    quota.reset()
    for _ in range(6):
        assert quota.bench(R, retry_after=7.66, window="minute") == 7.66
    assert quota.bench(R, window="minute") == quota._MINUTE_WINDOW_DEFAULT
    assert quota.bench(R, retry_after=0.1, window="minute") == quota._MINUTE_WINDOW_MIN
    assert quota.bench(R, retry_after=3600, window="minute") == quota._MINUTE_WINDOW_MAX
    # ...while a day window still starts the normal ladder.
    assert quota.bench(R2, window="day") == 2 * quota.MINUTE


def test_ready_in_covers_bench_and_zero_remaining_header():
    quota.reset()
    assert quota.ready_in(R) == 0
    quota.bench(R, retry_after=8, window="minute")
    assert 7 < quota.ready_in(R) <= 8
    quota.note_response(R2, 200, FakeHeaders({"x-ratelimit-remaining-tokens": "0",
                                              "x-ratelimit-reset-tokens": "5s"}))
    assert 4 < quota.ready_in(R2) <= 5


def test_retired_replacement_is_read_from_googles_404():
    body = ('{"error": {"code": 404, "message": "This model models/gemini-2.0-flash is '
            'no longer available. Please update your code to use models/gemini-3.6-flash."}}')
    assert quota.retired_replacement(404, body) == "gemini-3.6-flash"
    assert quota.retired_replacement(429, body) is None
    assert quota.retired_replacement(404, "model not found") is None
    assert quota.retired_replacement(404, body.replace("gemini-3.6", "evil-3.6")) is None


def test_remap_follows_chains_survives_cycles_and_restarts():
    quota.reset()
    quota.record_remap("gemini-a", "gemini-b")
    quota.record_remap("gemini-b", "gemini-c")
    assert quota.live_model("gemini-a") == "gemini-c"
    assert quota.live_model("gemini-z") == "gemini-z"
    quota.record_remap("gemini-c", "gemini-a")      # a cycle must not hang
    assert quota.live_model("gemini-a") in {"gemini-a", "gemini-b", "gemini-c"}
    quota.reset()
    quota.record_remap("gemini-old", "gemini-new")
    quota.reset()
    quota.load()
    assert quota.live_model("gemini-old") == "gemini-new", "remap must persist"


def _run():
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  ok   {name}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  ERR  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


def load_tests(loader, tests, pattern):
    """Let `python -m unittest discover` (and CI) run these plain functions too."""
    import unittest
    return unittest.TestSuite(unittest.FunctionTestCase(f) for n, f in sorted(globals().items())
                              if n.startswith("test_") and callable(f))


if __name__ == "__main__":
    sys.exit(_run())
