"""Web search grounds on Gemini 2.5 Flash / Flash-Lite across every Gemini key (Search
grounding isn't on the free tier for 3.x), and a Tavily key answers only when
grounding can't — the phone's 2026-09-27 and 2026-10-11 fixes."""

import unittest
from unittest import mock

from llm import gemini_bridge, quota
from llm import groq_bridge as gb


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.calls = []
        self.secrets = {}
        patches = [
            mock.patch.object(gb, "_keys_for", lambda p: ["k1", "k2"] if p == "gemini" else []),
            mock.patch.object(gb, "_gemini_key", "k1"),
            mock.patch.object(gb, "_history_for_prompt", lambda **k: []),
            mock.patch.object(gb.app_secrets, "get", lambda n, d="": self.secrets.get(n, d)),
            mock.patch.object(gb.app_secrets, "get_list",
                              lambda n: [self.secrets[n]] if self.secrets.get(n) else []),
            mock.patch.object(quota, "usable", lambda r: (r.model, r.key_index) != (
                "gemini-2.5-flash", 0)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def _grounded(self, answers):
        async def fake(msgs, model, key, **kw):
            self.calls.append((model, key, kw.get("thinking_budget")))
            out = answers.pop(0)
            if isinstance(out, Exception):
                raise out
            return out, [{"title": "t", "uri": "u"}], ["q"]
        return mock.patch.object(gemini_bridge, "gemini_grounded", fake)

    async def test_grounds_on_2_5_across_keys_skipping_spent_routes(self):
        with self._grounded([gemini_bridge.GeminiError("quota", status=429), "Paris."]):
            ok, text, sources = await gb.web_answer("capital of France")
        self.assertTrue(ok)
        self.assertEqual(text, "Paris.")
        # flash#0 is benched: flash#1 first (429), then flash-lite#0.
        self.assertEqual([(m, k) for m, k, _ in self.calls],
                         [("gemini-2.5-flash", "k2"), ("gemini-2.5-flash-lite", "k1")])
        self.assertEqual(self.calls[0][2], 0)          # thinking off on 2.5 Flash

    async def test_tavily_answers_only_when_grounding_cannot(self):
        self.secrets["tavily_api_key"] = "tvly-1"
        posted = []

        class FakeClient:
            async def post(self, url, **kw):
                posted.append((url, kw["headers"]["Authorization"]))
                return _Resp(200, {"answer": "It's 31°C.", "results": [
                    {"title": "Weather", "url": "https://w.example", "content": "hot"}]})

        with self._grounded(["The answer."]), mock.patch.object(gb, "_client", FakeClient):
            ok, text, _ = await gb.web_answer("weather in Mumbai")
        self.assertEqual((ok, text, posted), (True, "The answer.", []))

        errs = [gemini_bridge.GeminiError("x", status=429)] * 3
        with self._grounded(list(errs)), mock.patch.object(gb, "_client", FakeClient):
            ok, text, sources = await gb.web_answer("weather in Mumbai")
        self.assertTrue(ok)
        self.assertEqual(text, "It's 31°C.")
        self.assertEqual(posted, [("https://api.tavily.com/search", "Bearer tvly-1")])
        self.assertEqual(sources, [{"title": "Weather", "uri": "https://w.example"}])

    async def test_both_failing_says_so(self):
        self.secrets["tavily_api_key"] = "tvly-1"

        class Down:
            async def post(self, url, **kw):
                return _Resp(401, {})

        with self._grounded([gemini_bridge.GeminiError("x", status=429)] * 3), \
             mock.patch.object(gb, "_client", Down):
            ok, text, _ = await gb.web_answer("anything")
        self.assertFalse(ok)
        self.assertIn("Tavily backup failed too (HTTP 401)", text)


if __name__ == "__main__":
    unittest.main()
