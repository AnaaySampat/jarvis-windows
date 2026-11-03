"""Translation uses the selected provider, including keyless local models."""

import unittest
from unittest import mock

from llm import groq_bridge as gb, quota


class TranslationTests(unittest.IsolatedAsyncioTestCase):
    async def test_openai_translation_reaches_openai_with_unprefixed_model(self):
        posted = []

        class Response:
            status_code = 200
            headers = {}

            def json(self):
                return {"choices": [{"message": {"content": "Bonjour"}}]}

        class Client:
            async def post(self, url, **kwargs):
                posted.append((url, kwargs))
                return Response()

        async def routes(*args, **kwargs):
            yield quota.Route("openai", "openai:gpt-4.1", 0)

        with mock.patch.object(gb, "resolve_model", return_value="openai:gpt-4.1"), \
             mock.patch.object(gb, "_walk_routes", routes), \
             mock.patch.object(gb, "_credential", return_value="test-key"), \
             mock.patch.object(gb, "_client", return_value=Client()), \
             mock.patch.object(quota, "note_response"), \
             mock.patch.object(quota, "on_success"):
            result = await gb.translate_text("Hello", "en", "fr")

        self.assertEqual(result, "Bonjour")
        self.assertEqual(posted[0][0], "https://api.openai.com/v1/chat/completions")
        self.assertEqual(posted[0][1]["json"]["model"], "gpt-4.1")
        self.assertIn("French", posted[0][1]["json"]["messages"][0]["content"])

    async def test_offline_translation_does_not_require_a_cloud_key(self):
        local = mock.AsyncMock(return_value="Hola")

        async def routes(*args, **kwargs):
            yield quota.Route("ollama", "ollama:local-model", -1)

        with mock.patch.object(gb, "resolve_model", return_value="ollama:local-model"), \
             mock.patch.object(gb, "_walk_routes", routes), \
             mock.patch.object(gb, "_ollama_chat", local), \
             mock.patch.object(gb, "_key_for", return_value=""), \
             mock.patch.object(quota, "on_success"):
            result = await gb.translate_text("Hello", "en", "es")

        self.assertEqual(result, "Hola")
        self.assertEqual(local.await_args.args[1], "local-model")

    async def test_empty_input_does_not_call_a_model(self):
        complete = mock.AsyncMock()
        with mock.patch.object(gb, "quick_completion", complete):
            self.assertEqual(await gb.translate_text("  "), "")
        complete.assert_not_awaited()

    async def test_unavailable_provider_is_reported_as_failure(self):
        with mock.patch.object(gb, "quick_completion", mock.AsyncMock(return_value="")):
            self.assertIn("translation failed", await gb.translate_text("Hello"))


if __name__ == "__main__":
    unittest.main()
