"""Checks for the route ladder in llm/groq_bridge.py.

Run: python test_route_ladder.py

test_quota.py covers the bench arithmetic in isolation. This drives the REAL
send_prompt against a fake Groq transport, because the behaviour that actually
matters to a free-tier user is emergent: a 429 must rotate to the next key, and
the NEXT turn must skip the route we just learned was dead. Neither is visible
from quota.py alone.
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
os.environ["JARVIS_CONFIG_DIR"] = tempfile.mkdtemp(prefix="jarvis-ladder-test-")

import llm.groq_bridge as gb  # noqa: E402
from llm import quota  # noqa: E402

calls: list = []
OK_BODY = {"choices": [{"message": {"content": "answer from the live route"}}]}


class FakeHeaders(dict):
    def get(self, key, default=None):
        return dict.get(self, key.lower(), default)


class FakeResp:
    def __init__(self, status, headers=None, body=None):
        self.status_code = status
        self.headers = FakeHeaders(headers or {})
        self._body = body if isinstance(body, dict) else {}
        self.text = str(body)

    def json(self):
        return self._body


class FakeClient:
    """Stands in for the shared httpx client; records every (model, key) tried."""

    def __init__(self, plan):
        self.plan = plan

    async def post(self, url, **kw):
        key = kw["headers"]["Authorization"].removeprefix("Bearer ")
        model = kw["json"]["model"]
        calls.append((model, key))
        result = self.plan(model, key)
        return result if isinstance(result, FakeResp) else FakeResp(200, {}, result)


def run(plan, text="what is the capital of France?") -> str:
    calls.clear()
    gb._client = lambda: FakeClient(plan)
    return asyncio.run(gb.send_prompt(text))


def keys_tried() -> list:
    return [k for _, k in calls]


def setup(groq_keys, gemini_keys=()):
    quota.reset()
    # A dev machine may have real Vertex ADC configured, in which case the ladder
    # would legitimately fall through to it and answer for real — correct, but not
    # what these checks are isolating.
    gb.vertex_auth.enabled = lambda: False
    gb._groq_keys = list(groq_keys)
    gb._gemini_keys = list(gemini_keys)
    gb._api_key = groq_keys[0] if groq_keys else ""
    gb._gemini_key = gemini_keys[0] if gemini_keys else ""
    gb._selected_model = "openai/gpt-oss-120b"   # pin Groq so the ladder stays Groq
    gb._history.clear()
    # send_prompt saves the conversation, and the storage root isn't covered by
    # JARVIS_CONFIG_DIR: every suite run replaced the user's REAL chat history
    # (~/Jarvis/memory/history.json) with "what is the capital of France?".
    gb._persist_history = lambda: None


def rate_limited(retry_after=None):
    return FakeResp(429, {"retry-after": retry_after} if retry_after else {},
                    "rate limit reached")


def test_429_rotates_to_the_next_key():
    setup(["key-A", "key-B", "key-C"])
    out = run(lambda m, k: OK_BODY if k == "key-B" else rate_limited("30"))
    assert out.startswith("answer"), out
    assert keys_tried() == ["key-A", "key-B"], calls


def test_next_turn_skips_the_benched_key():
    """The whole point. Before this, every turn re-tried the same dead route."""
    setup(["key-A", "key-B"])
    run(lambda m, k: OK_BODY if k == "key-B" else rate_limited())
    run(lambda m, k: OK_BODY if k == "key-B" else rate_limited())
    assert keys_tried() == ["key-B"], "key-A must not be retried while benched"


def test_zero_remaining_header_benches_before_the_next_429():
    """A 200 that says nothing is left must take the route out of rotation, so the
    next turn never spends a 429 rediscovering it."""
    setup(["key-A", "key-B"])
    run(lambda m, k: FakeResp(200, {"x-ratelimit-remaining-tokens": "0",
                                    "x-ratelimit-reset-tokens": "45s"}, OK_BODY))
    assert keys_tried() == ["key-A"]
    run(lambda m, k: OK_BODY)
    assert keys_tried() == ["key-B"], "key-A reported 0 remaining; it must be skipped"


def test_exhaustion_message_names_a_real_time():
    setup(["key-A"])
    out = run(lambda m, k: rate_limited("300"))
    assert "rate limit" not in out.lower(), f"never blame the rate limit at the user: {out}"
    assert "minutes" in out or "hour" in out, out


def test_model_level_404_skips_sibling_keys():
    """Every key fails a missing model identically, so it must cost ONE attempt,
    not one per key."""
    setup(["key-A", "key-B", "key-C"])
    run(lambda m, k: FakeResp(404, {}, "model not found")
        if m == "openai/gpt-oss-120b" else OK_BODY)
    tried_gone = [k for m, k in calls if m == "openai/gpt-oss-120b"]
    assert tried_gone == ["key-A"], tried_gone


def test_timeout_does_not_escalate_the_ladder():
    setup(["key-A"])
    route = quota.Route("groq", "openai/gpt-oss-120b", 0)
    for _ in range(4):
        gb._bench_route(route, 0, "ReadTimeout: ")
    assert quota.cooldown_remaining(route) <= 90, quota.cooldown_remaining(route)


def test_ladder_covers_models_and_keys():
    setup(["k1", "k2"], ["g1"])
    gb._selected_model = "auto"
    routes = gb.routes_for("hello")
    assert len({(r.provider, r.model, r.key_index) for r in routes}) == len(routes), \
        "the ladder must never list the same route twice"
    assert any(r.provider == "groq" and r.key_index == 1 for r in routes), \
        "a second Groq key must produce its own routes"
    assert any(r.provider == "gemini" for r in routes), \
        "a Groq turn must still be able to fall through to Gemini"


def test_paid_providers_get_their_own_wire_format():
    from llm import model_catalog
    assert gb.split_model("anthropic:claude-opus-5-5") == ("anthropic", "claude-opus-5-5")
    assert gb.split_model("openai/gpt-oss-120b") == ("", "openai/gpt-oss-120b"), \
        "Groq's bare openai/... ids must not be mistaken for the OpenAI provider"
    assert model_catalog.normalize("meta:Llama-4-Maverick-17B") == "llama-4-maverick-17b"
    url, _h, p = gb._openai_request(quota.Route("openai", "openai:gpt-5-mini", 0),
                                    [], 100, 0.3, stream=False)
    assert url.startswith("https://api.openai.com/") and p["model"] == "gpt-5-mini"
    assert p["max_completion_tokens"] == 100 and "temperature" not in p, \
        "gpt-5 rejects max_tokens and a non-default temperature"
    _u, _h, p = gb._openai_request(quota.Route("xai", "xai:grok-4", 0), [], 100, 0.3, False)
    assert p["max_tokens"] == 100 and p["temperature"] == 0.3


def test_no_keys_means_no_routes():
    setup([])
    assert gb.routes_for("hello") == []


TPM_429 = ("Rate limit reached for model on tokens per minute (TPM): Limit 8000, "
           "Used 7000, Requested 1500. Please try again in 0.5s.")


def test_minute_limit_waits_once_instead_of_giving_up():
    """Every route briefly out on a TPM ceiling must cost one short wait, not
    'I've used up the free quota' — and must never climb the day ladder."""
    setup(["key-A"])
    first_pass = len(gb.routes_for("what is the capital of France?"))
    out = run(lambda m, k: FakeResp(429, {}, TPM_429) if len(calls) <= first_pass
              else OK_BODY)
    assert out.startswith("answer"), out
    assert len(calls) == first_pass + 1, calls
    route = quota.Route("groq", "openai/gpt-oss-120b", 0)
    assert quota._hits.get(route) is None, "a minute window must not feed the ladder"


def test_retired_gemini_model_is_remapped_on_the_next_turn():
    setup(["key-A"])
    old = quota.Route("gemini", "gemini-retired-x", 0)
    body = ("This model models/gemini-retired-x is no longer available. "
            "Please update your code to use models/gemini-live-y")
    gb._bench_route(old, 404, body)
    assert quota.live_model("gemini-retired-x") == "gemini-live-y"
    assert "gemini-live-y" in gb._model_ladder(pinned="gemini-retired-x")
    assert "gemini-retired-x" not in gb._model_ladder(pinned="gemini-retired-x")


def test_tool_only_stream_reply_is_an_answer():
    """A function call with no spoken preamble used to count as empty: the next
    route was asked too (running the action twice) and the user heard 'quota spent'."""
    setup(["key-A", "key-B"])
    asked = []

    async def fake_stream(route, messages, max_tokens, temperature, text, use_tools, sink):
        asked.append(route.key_index)
        sink.append({"id": "call_0", "name": "open_app", "args": {"name": "notepad"}})
        return
        yield  # noqa: unreachable — makes this an async generator

    real, real_tools = gb._stream_route, gb.tools_enabled
    gb._stream_route = fake_stream
    gb.tools_enabled = lambda model: True
    try:
        sink: list = []

        async def drain():
            return [full async for _d, full in gb.stream_prompt("open notepad", tool_sink=sink)]
        yielded = asyncio.run(drain())
    finally:
        gb._stream_route, gb.tools_enabled = real, real_tools
    assert asked == [0], f"only one route may be asked: {asked}"
    assert len(sink) == 1, sink
    assert yielded == [], f"no quota message for a tool-only reply: {yielded}"


def test_thinking_budget_is_resolved_per_route():
    # Dynamic thinking stays dynamic where it's short, and is capped on 2.5 Flash/Pro.
    assert gb._route_thinking("gemini-3.5-flash", "click it", -1) == -1
    assert gb._route_thinking("gemini-2.5-flash-lite", "click it", -1) == -1
    assert gb._route_thinking("gemini-2.5-flash", "click it", -1) == gb._DYNAMIC_THINK_CAP_25
    assert gb._route_thinking("gemini-2.5-flash", "click it", 0) == 0
    # "auto" is sized for the model actually answering, not the first one tried.
    assert gb._route_thinking("gemini-3.1-flash-lite", "open the page", "auto") is None
    assert gb._route_thinking("gemini-3.5-flash", "open the page", "auto") == 0


def test_a_lane_order_is_tried_before_the_general_ladder():
    """The autopilot's fast lane must fall to its OTHER fast model before any slow
    one, and a text-led call keeps its image for a Gemini fallback."""
    setup(["k1"], ["g1"])
    ladder = gb._model_ladder("click it", pinned=("openai/gpt-oss-20b", "openai/gpt-oss-120b"))
    assert ladder[:2] == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"], ladder
    assert any(m.startswith("gemini") for m in ladder[2:]), ladder
    asked = []

    async def fake_complete(route, messages, *_a, **_k):
        asked.append((route.model, isinstance(messages[-1]["content"], list)))
        if route.provider == "groq":
            raise gb.RouteError("busy", status=429, detail="tokens per minute (TPM)")
        return "{}"
    real = gb._complete
    gb._complete = fake_complete
    try:
        out = asyncio.run(gb.quick_completion("s", "u", model="openai/gpt-oss-20b",
                                              fallbacks=("openai/gpt-oss-120b",),
                                              image_b64="data:image/jpeg;base64,AA"))
    finally:
        gb._complete = real
    assert out == "{}", out
    assert [m for m, _ in asked[:2]] == ["openai/gpt-oss-20b", "openai/gpt-oss-120b"], asked
    assert asked[2][0].startswith("gemini") and asked[2][1], "the Gemini fallback sees the image"
    assert not asked[0][1], "a Groq route gets the image flattened away"


def test_the_vision_lane_tries_quick_models_before_slow_ones():
    """Ranked smart isn't the same as fast today: a model seen taking 40s (or
    answering 503) drops behind quick ones, score order kept within a band."""
    setup(["k1"], ["g1"])
    gb._config.pop("autopilot_model", None)
    real = dict(quota._latency)
    try:
        quota._latency.clear()
        base = gb._lane_candidates("eyes")
        assert base, "no vision models without a ranking?"
        quota.note_latency(base[0], 40.0)
        reordered = gb._lane_candidates("eyes")
        assert reordered[-1] == base[0], reordered
        assert reordered[:-1] == base[1:], reordered
    finally:
        quota._latency.clear()
        quota._latency.update(real)


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
