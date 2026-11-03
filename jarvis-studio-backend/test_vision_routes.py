"""Images keep their payload while unavailable vision routes fall back."""

import unittest
from unittest import mock

from llm import gemini_bridge, groq_bridge as gb, quota


class VisionRouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.routes = []

        async def walk(*args, **kwargs):
            for route in self.routes:
                yield route

        for patch in (
            mock.patch.object(gb, "provider_mode", return_value="gemini"),
            mock.patch.object(gb, "resolve_model", return_value="gemini-selected"),
            mock.patch.object(gb, "_walk_routes", walk),
            mock.patch.object(gb, "_credential", side_effect=lambda route: f"key-{route.key_index}"),
            mock.patch.object(gb, "_bench_route"),
            mock.patch.object(quota, "on_success"),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    async def test_gemini_quota_failure_tries_next_vision_model(self):
        self.routes = [quota.Route("gemini", "gemini-selected", 0),
                       quota.Route("gemini", "gemini-2.5-flash", 0)]
        vision = mock.AsyncMock(side_effect=[gemini_bridge.GeminiError("quota", status=429), "12345"])
        with mock.patch.object(gemini_bridge, "gemini_vision", vision):
            self.assertEqual(await gb.vision_query("data:image/png;base64,fixture", "Read the number"), "12345")
        self.assertEqual(vision.await_count, 2)
        self.assertTrue(all(call.args[0] == "data:image/png;base64,fixture" for call in vision.await_args_list))
        gb._bench_route.assert_called_once()

    async def test_all_configured_keys_can_be_tried(self):
        self.routes = [quota.Route("gemini", "gemini-2.5-flash", 0),
                       quota.Route("gemini", "gemini-2.5-flash", 1)]
        vision = mock.AsyncMock(side_effect=[gemini_bridge.GeminiError("unavailable", status=503), "Hello"])
        with mock.patch.object(gemini_bridge, "gemini_vision", vision):
            self.assertEqual(await gb.vision_query("image", "Read it"), "Hello")
        self.assertEqual([call.args[3] for call in vision.await_args_list], ["key-0", "key-1"])

    async def test_text_chat_selection_does_not_hide_configured_vision_service(self):
        self.routes = [quota.Route("openai", "openai:text-model", 0),
                       quota.Route("gemini", "gemini-2.5-flash", 0)]
        vision = mock.AsyncMock(return_value="Hello")
        with mock.patch.object(gb, "resolve_model", return_value="openai:text-model"), \
             mock.patch.object(gemini_bridge, "gemini_vision", vision):
            self.assertEqual(await gb.vision_query("image", "Read it"), "Hello")
        vision.assert_awaited_once()

    async def test_groq_vision_fallback_retains_image_input(self):
        self.routes = [quota.Route("gemini", "gemini-selected", 0),
                       quota.Route("groq", gb.VISION_MODEL, 0)]
        vision = mock.AsyncMock(side_effect=gemini_bridge.GeminiError("quota", status=429))
        complete = mock.AsyncMock(return_value="Hello")
        with mock.patch.object(gemini_bridge, "gemini_vision", vision), \
             mock.patch.object(gb, "_complete", complete):
            self.assertEqual(await gb.vision_query("image", "Read it"), "Hello")
        content = complete.await_args.args[1][0]["content"]
        self.assertEqual(content[1], {"type": "image_url", "image_url": {"url": "image"}})

    async def test_text_only_routes_never_answer_without_the_image(self):
        self.routes = [quota.Route("groq", "openai/gpt-oss-20b", 0),
                       quota.Route("openai", "openai:text-model", 0)]
        complete = mock.AsyncMock(return_value="Invented description")
        with mock.patch.object(gb, "_complete", complete):
            self.assertEqual(await gb.vision_query("image", "Read it"), "")
        complete.assert_not_awaited()

    async def test_offline_mode_does_not_send_images_to_cloud(self):
        vision = mock.AsyncMock()
        with mock.patch.object(gb, "provider_mode", return_value="offline"), \
             mock.patch.object(gemini_bridge, "gemini_vision", vision):
            self.assertEqual(await gb.vision_query("image", "Read it"), "")
        vision.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
