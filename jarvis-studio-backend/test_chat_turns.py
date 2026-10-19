"""The chat loop's honesty and latency rules, ported from the phone (2026-09-26 to
2026-10-19): follow-up steps know what was asked, the step cap ends in an answer, a
task that acted and failed isn't re-run, a model's 400 moves on to the next model,
slow models go to the back, and a bare "OK." without a tool call counts as a claim."""

import asyncio
import unittest
from unittest import mock

import main
from llm import groq_bridge as gb
from llm import live_tools as lt
from llm import quota


class AgenticLoopTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._interrupt = None
        main._acted_and_failed.clear()
        lt._recent.clear()
        lt._used.clear()

    async def _loop(self, script, feeds, user_text="how many PDFs are in Downloads?"):
        """Run _agentic_loop with a scripted model: each entry is the list of tool calls
        that step makes ([] = it answers). Returns (prompts, final kwargs)."""
        prompts, calls = [], []

        async def fake_stream(prompt, **kw):
            prompts.append(prompt)
            calls.append(kw)
            step = script.pop(0) if script else []
            if kw.get("tool_sink") is not None:
                kw["tool_sink"].extend(step)
            return "" if step else "There are 3 PDFs, sir."

        async def fake_surface(results, _text):
            return [{"name": "result", "content": "more"}]

        with mock.patch.object(main, "_stream_and_speak", fake_stream), \
             mock.patch.object(main, "_surface_results", fake_surface), \
             mock.patch.object(main, "_tts", None):
            await main._agentic_loop(feeds, user_text)
        return prompts, calls

    async def test_follow_up_steps_see_the_request(self):
        prompts, _ = await self._loop([[]], [{"name": "listing", "content": "a.pdf"}])
        self.assertIn('My request was: "how many PDFs are in Downloads?"', prompts[0])

    async def test_the_step_cap_ends_in_an_answer_with_no_tools(self):
        call = {"name": "get_time", "args": {}}
        script = [[call] for _ in range(main._MAX_AGENTIC_STEPS)]
        prompts, calls = await self._loop(script, [{"name": "x", "content": "y"}])
        self.assertEqual(len(prompts), main._MAX_AGENTIC_STEPS + 1)
        self.assertIn("Take NO further action", prompts[-1])
        self.assertIsNone(calls[-1]["tool_sink"])
        self.assertTrue(calls[-1]["speak"])
        # If even that call says nothing, the results' own lines are said instead.
        self.assertIn("Here's where I got to", calls[-1]["empty_reply"])


class NoRepeatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._interrupt = None
        main._autopilot_owner = ""
        main._acted_and_failed.clear()

    async def test_a_task_that_acted_and_failed_is_not_run_again_this_request(self):
        runs = []

        async def fake_run(kind, goal, **kw):
            runs.append(goal)
            return {"ok": False, "steps": ["clicked Send"], "summary": "Couldn't confirm."}

        async def fake_emit(*a, **k):
            return None

        with mock.patch.object(main.autopilot, "run_task", fake_run), \
             mock.patch.object(main, "emit", fake_emit), \
             mock.patch.object(main, "_tts", None), \
             mock.patch.object(main.playbooks, "autopilot_hint", lambda g: ""), \
             mock.patch.object(main, "note_outcome", lambda t: None):
            await main._run_autopilot_task({"type": "computer_task", "goal": "send it"})
            again = await main._run_autopilot_task(
                {"type": "computer_task", "goal": "send the message again"})
            self.assertEqual(runs, ["send it"])
            self.assertIn("won't run it again", again)
            # A different kind is still allowed, and the next request starts clean.
            await main._run_autopilot_task({"type": "browser_task", "goal": "look it up"})
            self.assertEqual(len(runs), 2)


class RouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        gb._chat_latency.clear()

    def test_slow_models_go_to_the_back_in_quality_order(self):
        a, b, c = (quota.Route("gemini", m, 0) for m in ("best", "mid", "low"))
        gb.note_chat_latency(a, 14.0, now=1000.0)
        self.assertEqual(gb.fast_first([a, b, c], now=1001.0), [b, c, a])
        # Forgotten after an hour.
        self.assertEqual(gb.fast_first([a, b, c], now=1000.0 + 3601), [a, b, c])

    def test_only_a_real_no_tools_400_marks_a_model(self):
        self.assertTrue(gb._rejects_tools("tool calling is not supported for this model"))
        self.assertTrue(gb._rejects_tools("This model does not support function calling"))
        self.assertFalse(gb._rejects_tools(
            '{"error":{"code":"tool_use_failed","message":"Failed to call a function"}}'))
        self.assertFalse(gb._rejects_tools("Invalid schema for function 'x'"))

    async def test_a_400_moves_to_the_next_model_without_a_bench(self):
        routes = [quota.Route("groq", "bad", 0), quota.Route("groq", "bad", 1),
                  quota.Route("groq", "good", 0)]
        tried, benched = [], []

        async def fake_walk(text="", pinned="", task_tier="", skip=None):
            for r in routes:
                if r.model not in skip:
                    yield r

        async def fake_route(route, *a, **k):
            tried.append(route)
            if route.model == "bad":
                raise gb.RouteError("groq HTTP 400", status=400, detail="tool_use_failed")
            yield "Hello", "Hello"

        with mock.patch.object(gb, "_walk_routes", fake_walk), \
             mock.patch.object(gb, "_stream_route", fake_route), \
             mock.patch.object(gb, "_bench_route", lambda *a, **k: benched.append(a)), \
             mock.patch.object(gb, "_key_for", lambda m: "k"), \
             mock.patch.object(gb, "resolve_model", lambda *a, **k: "bad"), \
             mock.patch.object(gb, "_system_messages", lambda t: []):
            out = [full async for _d, full in gb.stream_prompt("hi", record_history=False)]
        self.assertEqual(out[-1], "Hello")
        self.assertEqual([r.model for r in tried], ["bad", "good"])   # one key, not two
        self.assertEqual(benched, [])


class WakeClipTests(unittest.IsolatedAsyncioTestCase):
    async def _cmd(self, heard, speech=True):
        async def fake_transcribe(_clip):
            return heard
        with mock.patch.object(main, "transcribe_clip", fake_transcribe):
            return await main._command_from_clip(object(), speech)

    async def test_one_breath_command_keeps_its_first_words(self):
        self.assertEqual(await self._cmd("Hey Jarvis, open Spotify and play jazz"),
                         "open Spotify and play jazz")

    async def test_conversation_about_jarvis_is_ignored(self):
        self.assertEqual(await self._cmd("Jarvis was the best part of that film"), "")

    async def test_a_pause_after_the_name_falls_back_to_listening(self):
        self.assertIsNone(await self._cmd("Hey Jarvis."))
        self.assertIsNone(await self._cmd("Hey Jarvis. Thank you."))

    async def test_nothing_said_is_not_transcribed(self):
        self.assertEqual(await self._cmd("should not be read", speech=False), "")


class ClaimTests(unittest.TestCase):
    def test_bare_ok_is_a_claim_but_a_question_is_not(self):
        self.assertTrue(main._claimed_done_without_acting("increase the volume", "OK.", []))
        self.assertFalse(main._claimed_done_without_acting(
            "set a reminder", "OK — for what time?", []))
        self.assertTrue(main._claimed_done_without_acting(
            "reply to Sam saying I'm late", "I've sent the reply.", []))
        self.assertTrue(main._claimed_done_without_acting("resume", "Resumed.", []))

    def test_plumbing_alone_is_not_acting(self):
        self.assertTrue(main._claimed_done_without_acting(
            "open notepad", "Opened Notepad.", [{"type": "load_tools", "internal": True}]))


if __name__ == "__main__":
    unittest.main()
