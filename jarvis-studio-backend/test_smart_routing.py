"""Smart routing: benchmark-scored ranking (LLM only for unknown models, cached),
recomputed whenever the reachable models change, and a good-enough-first ladder
with cross-provider fallbacks."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from llm import groq_bridge as gb
from llm import model_catalog as mc
from llm import model_ranker as mr
from llm import quota

MODELS = [
    {"id": "gemini-3.5-flash", "score": 88, "tier": "flagship", "tools": True},
    {"id": "openai/gpt-oss-120b", "score": 80, "tier": "flagship", "tools": True},
    {"id": "mistral:mistral-small-latest", "score": 62, "tier": "mid", "tools": True},
    {"id": "nvidia:openai/gpt-oss-20b", "score": 58, "tier": "mid", "tools": False},
    {"id": "openrouter:qwen/qwen3.8-27b:free", "score": 55, "tier": "mid", "tools": True},
    {"id": "gemini-2.5-flash-lite", "score": 40, "tier": "fast", "tools": True},
]


def _isolate(test):
    d = Path(tempfile.mkdtemp())
    p = mock.patch.object(mr, "_path", lambda: d / "ranking.json")
    p.start()
    test.addCleanup(p.stop)
    mr.reset_for_tests()
    test.addCleanup(mr.reset_for_tests)


class CatalogTests(unittest.TestCase):
    def test_benchmark_scores_match_the_published_index(self):
        # The exact values the LLM-only ranking got badly wrong.
        self.assertEqual(mc.lookup("openai/gpt-oss-120b")["score"], 10)
        self.assertGreater(mc.lookup("gemini-3.8-flash")["score"],
                           mc.lookup("gemini-3.1-pro-preview")["score"])
        self.assertEqual(mc.lookup("nvidia:nvidia/nemotron-3-super-120b-a12b")["score"], 13)
        # Same weights on every provider → same score.
        self.assertEqual(mc.lookup("nvidia:google/gemma-4-31b-it"),
                         mc.lookup("openrouter:google/gemma-4-31b-it:free"))

    def test_specific_patterns_win_over_general_ones(self):
        self.assertEqual(mc.lookup("gemini-3.5-flash-lite")["tier"], "fast")
        self.assertEqual(mc.lookup("gemini-3.5-flash")["tier"], "mid")
        self.assertIsNone(mc.lookup("nvidia:mistralai/mistral-nemotron"))  # not Mistral-Nemo
        self.assertEqual(mc.lookup("nvidia:nv-mistralai/mistral-nemo-12b-instruct")["score"], 3)

    def test_non_chat_models_and_aliases_are_excluded(self):
        for m in ("gemini-3.8-flash-tts", "lyria-3.5", "nano-banana-pro-preview",
                  "nvidia:nvidia/nv-embedqa-mistral-7b-v2", "whisper-large-v3",
                  "openai/gpt-oss-safeguard-20b", "gemini-flash-latest"):
            self.assertTrue(mc.excluded(m) or mc.lookup(m) == {"chat": False}, m)
        self.assertFalse(mc.excluded("mistral:mistral-small-latest"))   # Mistral's real ids

    def test_speed_class_is_whole_token(self):
        self.assertEqual(mc.tier_for("gemini-omni-1.1-flash", 20), "mid")   # "gemini" ⊅ "mini"
        self.assertEqual(mc.tier_for("openrouter:nex-agi/nex-n2.5-mini:free", 40), "fast")
        self.assertEqual(mc.tier_for("x/unknown-7b-instruct", 40), "fast")
        self.assertEqual(mc.tier_for("x/unknown-big", 40), "flagship")


class RankerTests(unittest.TestCase):
    def setUp(self):
        _isolate(self)

    def test_compute_scores_known_estimated_and_unknown_models(self):
        models = ["gemini-3.8-flash", "openai/gpt-oss-120b", "nvidia:moonshotai/kimi-k2.6",
                  "gemini-omni-1.1-flash", "whisper-large-v3", "lyria-3.5"]
        est = {"kimi-k2.6": {"score": 30, "basis": "est. AA-like"}}
        rows = mr.compute(models, {"openai/gpt-oss-120b": {"tools": False}}, est,
                          {"gemini-3.8-flash"})
        by = {r["id"]: r for r in rows}
        self.assertEqual(list(by), ["gemini-3.8-flash", "nvidia:moonshotai/kimi-k2.6",
                                    "openai/gpt-oss-120b", "gemini-omni-1.1-flash"])
        self.assertEqual(by["gemini-3.8-flash"]["source"], "benchmark")
        self.assertEqual(by["nvidia:moonshotai/kimi-k2.6"]["source"], "llm")
        self.assertEqual(by["gemini-omni-1.1-flash"]["source"], "guess")
        self.assertFalse(by["openai/gpt-oss-120b"]["tools"])                 # provider fact
        self.assertFalse(by["gemini-3.8-flash"]["tools"])                    # observed 400

    def test_unknown_models_are_normalized_once(self):
        self.assertEqual(mr.unknown_models([
            "nvidia:poolside/laguna-xs-2.1", "openrouter:poolside/laguna-xs-2.1:free",
            "gemini-3.8-flash", "lyria-3.5"]), ["laguna-xs-2.1"])

    def test_estimates_are_validated(self):
        text = """```json
[{"id": "laguna-xs-2.1", "score": 14, "basis": "AA 14"},
 {"id": "made-up", "score": 50},
 {"id": "dots-3-note-preview", "chat": false},
 {"id": "laguna-xs-2.1", "score": 99}]
```"""
        got = mr.parse_estimates(text, ["laguna-xs-2.1", "dots-3-note-preview"])
        self.assertEqual(got, {"laguna-xs-2.1": {"score": 14.0, "basis": "est. AA 14"},
                               "dots-3-note-preview": {"chat": False}})
        # Off-scale (LMArena Elo) → the whole batch is rejected.
        self.assertEqual(mr.parse_estimates('[{"id": "laguna-xs-2.1", "score": 1250}]',
                                            ["laguna-xs-2.1"]), {})
        self.assertEqual(mr.parse_estimates("no json", ["laguna-xs-2.1"]), {})

    def test_ladder_is_good_enough_first(self):
        mr.store(MODELS, {})
        avail = {m["id"] for m in MODELS}
        self.assertEqual(mr.ladder("mid", True, avail), [
            "mistral:mistral-small-latest", "openrouter:qwen/qwen3.8-27b:free",  # mid, no-tools out
            "gemini-3.5-flash", "openai/gpt-oss-120b",                           # then step up
            "gemini-2.5-flash-lite"])                                            # weaker last
        self.assertEqual(mr.ladder("fast", False, avail)[:2],
                         ["gemini-2.5-flash-lite", "mistral:mistral-small-latest"])
        self.assertEqual(mr.ladder("flagship", True, {"openai/gpt-oss-120b"}),
                         ["openai/gpt-oss-120b"])                                # no key = skipped

    def test_tool_rejection_survives_reranking(self):
        mr.store([dict(m) for m in MODELS], {})
        mr.mark_no_tools("mistral:mistral-small-latest")
        mr.reset_for_tests()
        mr._loaded = False                                                       # reload from disk
        self.assertFalse(mr.tools_ok("mistral:mistral-small-latest"))
        rows = mr.compute(["mistral:mistral-small-latest"], {}, {}, mr.no_tools())
        mr.store(rows, {})
        self.assertFalse(mr.tools_ok("mistral:mistral-small-latest"))
        self.assertTrue(mr.tools_ok("unranked-model"))


class RoutingWireTests(unittest.TestCase):
    def test_prefixed_ids_route_to_their_provider(self):
        self.assertEqual(gb._provider_for("nvidia:openai/gpt-oss-20b"), "nvidia")
        self.assertEqual(gb._provider_for("openai/gpt-oss-20b"), "groq")
        self.assertEqual(gb._provider_for("openrouter:google/gemma-4-31b-it:free"), "openrouter")
        self.assertEqual(gb.split_model("openrouter:qwen/x:free"), ("openrouter", "qwen/x:free"))

    def test_payload_per_provider(self):
        with mock.patch.object(gb, "_credential", return_value="k"):
            url, _, body = gb._openai_request(
                quota.Route("mistral", "mistral:mistral-small-latest", 0), [], 100, 0.2, False)
            self.assertEqual(url, "https://api.mistral.ai/v1/chat/completions")
            self.assertEqual(body["model"], "mistral-small-latest")
            self.assertEqual(body["max_tokens"], 100)
            self.assertNotIn("max_completion_tokens", body)
            _, _, body = gb._openai_request(
                quota.Route("nvidia", "nvidia:openai/gpt-oss-20b", 0), [], 100, 0.2, False)
            self.assertNotIn("reasoning_effort", body)                          # Groq-only field
            _, _, body = gb._openai_request(
                quota.Route("groq", "openai/gpt-oss-20b", 0), [], 100, 0.2, False)
            self.assertEqual((body["max_completion_tokens"], body["reasoning_effort"]),
                             (100, gb._REASONING_EFFORT))


class LadderIntegrationTests(unittest.TestCase):
    def setUp(self):
        _isolate(self)
        patches = [
            mock.patch.object(gb, "_selected_model", "auto"),
            mock.patch.object(gb, "_groq_keys", ["g"]),
            mock.patch.object(gb, "_gemini_keys", ["k"]),
            mock.patch.object(gb, "_extra_keys", {"nvidia": [], "mistral": ["m"],
                                                 "openrouter": []}),
            mock.patch.object(gb.model_discovery, "available_model_ids",
                              lambda: [m["id"] for m in MODELS]),
            mock.patch.object(gb.model_discovery, "first_ollama", lambda: ""),
            mock.patch.object(gb, "provider_mode", lambda: "gemini"),
            mock.patch.object(quota, "usable", lambda r: True),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        mr.store(MODELS, {})

    def test_everyday_command_goes_to_mid_then_steps_up(self):
        text = "open spotify and play some lofi"
        self.assertEqual(gb.resolve_model(text), "mistral:mistral-small-latest")
        self.assertEqual(gb._model_ladder(text), [
            "mistral:mistral-small-latest",        # openrouter/nvidia have no key → skipped
            "gemini-3.5-flash", "openai/gpt-oss-120b", "gemini-2.5-flash-lite"])

    def test_benched_head_falls_through_to_next(self):
        with mock.patch.object(quota, "usable", lambda r: r.provider != "mistral"):
            self.assertEqual(gb.resolve_model("open spotify and play some lofi"),
                             "gemini-3.5-flash")

    def test_pinned_model_is_left_alone(self):
        with mock.patch.object(gb, "_selected_model", "openai/gpt-oss-120b"):
            self.assertEqual(gb.resolve_model("hi"), "openai/gpt-oss-120b")


class RankRunTests(unittest.IsolatedAsyncioTestCase):
    """rank_models(): benchmarks first, LLM once per unknown model, recompute on
    every change of reachable models."""

    def setUp(self):
        _isolate(self)
        self.cat = {"gemini": ["gemini-3.8-flash", "gemini-3.5-flash"],
                    "mistral": ["mistral:mistral-small-latest"],
                    "nvidia": ["nvidia:moonshotai/kimi-k2.6"]}
        self.keys = {"groq": [], "nvidia": ["n"], "mistral": ["m"], "openrouter": []}
        patches = [
            mock.patch.object(gb.model_discovery, "catalog", lambda: self.cat),
            mock.patch.object(gb.model_discovery, "caps", lambda: {}),
            mock.patch.object(gb, "_gemini_keys", ["k"]),
            mock.patch.object(gb, "_gemini_key", "k"),
            mock.patch.object(gb, "_extra_keys", self.keys),
            mock.patch.object(gb, "provider_mode", lambda: "gemini"),
            mock.patch.object(gb.vertex_auth, "enabled", lambda: False),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.calls = 0

    async def ask(self, prompt):
        self.calls += 1
        return '[{"id": "kimi-k2.6", "score": 31, "basis": "AA 31"}]', "gemini-3.5-flash"

    async def test_llm_is_asked_once_per_unknown_model(self):
        with mock.patch.object(gb, "_ask_ranker", self.ask):
            self.assertTrue(await gb.rank_models())
            self.assertTrue(await gb.rank_models())            # cached: no second call
        self.assertEqual(self.calls, 1)
        rows = {r["id"]: r for r in mr.load()["models"]}
        self.assertEqual(rows["nvidia:moonshotai/kimi-k2.6"]["score"], 31)
        self.assertEqual(rows["gemini-3.8-flash"]["source"], "benchmark")
        self.assertIn("3 from benchmarks, 1 estimated", mr.last_result)

    async def test_known_models_are_usable_before_the_lookup_finishes(self):
        seen = []

        async def on_update():
            seen.append([r["id"] for r in mr.load()["models"]])
        with mock.patch.object(gb, "_ask_ranker", self.ask):
            await gb.rank_models(on_update=on_update)
        self.assertEqual(len(seen), 1)                          # stored before the LLM call
        self.assertIn("gemini-3.8-flash", seen[0])
        kimi = [r for r in mr.load()["models"] if "kimi" in r["id"]][0]
        self.assertEqual(kimi["source"], "llm")                 # then filled in

    async def test_removing_a_key_drops_its_models_without_the_llm(self):
        with mock.patch.object(gb, "_ask_ranker", self.ask):
            await gb.rank_models()
            self.keys["nvidia"] = []                             # key removed
            await gb.rank_models()
        self.assertEqual(self.calls, 1)
        ids = [r["id"] for r in mr.load()["models"]]
        self.assertNotIn("nvidia:moonshotai/kimi-k2.6", ids)
        self.assertEqual(ids[0], "gemini-3.8-flash")

    async def test_failed_lookup_places_unknowns_conservatively_and_backs_off(self):
        async def down(prompt):
            self.calls += 1
            return "", ""
        with mock.patch.object(gb, "_ask_ranker", down):
            self.assertTrue(await gb.rank_models())
            await gb.rank_models()                              # within back-off: no retry
            self.assertEqual(self.calls, 1)
            await gb.rank_models(force=True)                    # Re-rank retries
            self.assertEqual(self.calls, 2)
        kimi = [r for r in mr.load()["models"] if "kimi" in r["id"]][0]
        self.assertEqual((kimi["source"], kimi["score"]), ("guess", mc.guess_score(kimi["id"])))
        self.assertLess(time.time() - mr.load()["estimate_failed_at"], 60)
        self.assertIn("couldn't be estimated", mr.last_result)


if __name__ == "__main__":
    unittest.main()
