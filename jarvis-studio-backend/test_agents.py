"""Agent mode (agents.py + main._run_agent_plan): which agent gets a request, how a
plan is read, what agents hand each other, and the coordinator end to end with a fake
operator."""

import asyncio
import unittest
from unittest import mock

import agents

INSTALLED = {"notepad": "Notepad", "excel": "Excel", "spotify": "Spotify"}


def installed(name):
    return INSTALLED.get(name.lower(), "")


class RouteTests(unittest.TestCase):
    def route(self, text):
        return agents.route(text, installed)

    def test_questions_and_one_step_tools_stay_with_the_chat_agent(self):
        for text in ("what's the capital of France?", "tell me a joke",
                     "set a timer for 10 minutes", "what's the weather tomorrow",
                     "play some jazz on spotify", "open spotify",
                     "set an alarm on my phone for 7", "check my email",
                     "make the theme red", "search for the best pizza near me"):
            self.assertEqual(self.route(text), "chat", text)

    def test_one_surface_goes_straight_to_its_agent(self):
        self.assertEqual(self.route("play lofi hip hop on youtube"), "browser")
        self.assertEqual(self.route("open amazon.in and add AA batteries to my cart"), "browser")
        self.assertEqual(self.route("order a pizza online"), "browser")
        self.assertEqual(self.route("open notepad and write a shopping list"), "computer")
        self.assertEqual(self.route("rename the files on my desktop to lowercase"), "computer")

    def test_the_web_and_the_desktop_together_need_a_plan(self):
        self.assertEqual(self.route(
            "find the cheapest flight to Goa on Google Flights and write it in Notepad"), "multi")
        self.assertEqual(self.route(
            "look up today's top story on reddit and save it to a text file on my desktop"),
            "multi")

    def test_an_action_without_a_surface_is_for_the_model(self):
        self.assertEqual(self.route("find me a good laptop under 60k and compare two"), "unsure")

    def test_research_and_going_through_files_have_their_own_agents(self):
        self.assertEqual(self.route("research the best budget phones under 20k"), "research")
        self.assertEqual(self.route("give me a report on solid state batteries"), "research")
        self.assertEqual(self.route("summarize all the files in my downloads folder"), "files")
        self.assertEqual(self.route("summarize my downloads and post it on my blog online"),
                         "multi")


class PickTests(unittest.TestCase):
    def test_an_at_mention_or_a_spoken_name_picks_the_agent(self):
        self.assertEqual(agents.mention("@files summarize my downloads"),
                         ("files", "summarize my downloads"))
        self.assertEqual(agents.mention("open github @desktop"), ("computer", "open github"))
        self.assertEqual(agents.mention("tell the browser agent to open github"),
                         ("browser", "open github"))
        self.assertEqual(agents.mention("Research agent, best budget phones"),
                         ("research", "best budget phones"))
        self.assertEqual(agents.mention("@auto what's the time"), ("auto", "what's the time"))

    def test_addresses_strangers_and_bare_names_pick_nothing(self):
        for text in ("email it to me at someone@gmail.com", "@nobody do it", "@files",
                     "call my travel agent", "the agent said no"):
            self.assertEqual(agents.mention(text), ("", text), text)

    def test_a_picked_agent_gets_the_whole_request(self):
        long = "summarize " + "x" * 900
        plan = agents.picked_plan("files", long)
        self.assertEqual((plan.how, plan.steps[0].agent, plan.steps[0].task),
                         ("picked", "files", long))
        self.assertIsNone(agents.picked_plan("robot", "do it"))
        self.assertIsNone(agents.picked_plan("files", "  "))


class HandOffTests(unittest.TestCase):
    done = [agents.StepResult("files", "summarize my downloads", True, "3 files: a, b, c.")]

    def test_the_rest_goes_to_the_agent_the_coordinator_names(self):
        nxt = agents.parse_next('{"next":{"agent":"web","task":"post the summary on my blog"}}',
                                self.done)
        self.assertEqual((nxt.agent, nxt.task), ("browser", "post the summary on my blog"))

    def test_done_junk_and_repeats_end_it(self):
        for raw in ('{"next": null}', "all done!", '{"next":{"agent":"robot","task":"x"}}',
                    '{"next":{"agent":"files","task":"Summarize my downloads"}}'):
            self.assertIsNone(agents.parse_next(raw, self.done), raw)

    def test_only_a_request_with_more_to_it_is_looked_at_again(self):
        self.assertTrue(agents.may_continue("summarize my downloads and email it to me", self.done))
        self.assertFalse(agents.may_continue("summarize my downloads", self.done))
        self.assertFalse(agents.may_continue("when was it completed and how tall is it", self.done))
        self.assertTrue(agents.may_continue("find the cheapest flight then write it in notepad",
                                            self.done))
        failed = [agents.StepResult("files", "x", False, "no")]
        self.assertFalse(agents.may_continue("open it and save it", failed))


class PlanTests(unittest.TestCase):
    def test_a_good_plan_is_read_and_capped(self):
        raw = ('Sure! {"steps": [{"agent": "browser", "task": "Find the price"},'
               ' {"agent": "desktop", "task": "Write it in Notepad"},'
               ' {"agent": "robot", "task": "x"}, {"agent": "chat", "task": ""}]}')
        plan = agents.parse_plan(raw)
        self.assertEqual([(s.agent, s.task) for s in plan.steps],
                         [("browser", "Find the price"), ("computer", "Write it in Notepad")])
        many = '{"steps": [' + ",".join('{"agent":"browser","task":"t%d"}' % i
                                        for i in range(9)) + "]}"
        self.assertEqual(len(agents.parse_plan(many).steps), agents.MAX_STEPS)

    def test_chat_only_or_junk_leaves_it_to_the_chat_agent(self):
        self.assertIsNone(agents.parse_plan('{"steps":[{"agent":"chat","task":"answer"}]}'))
        self.assertIsNone(agents.parse_plan("I think the browser should do it."))
        self.assertIsNone(agents.parse_plan('{"steps": "browser"}'))
        self.assertIsNone(agents.parse_plan(""))

    def test_the_model_is_asked_only_when_the_words_dont_say(self):
        calls = []

        async def complete(system, user):
            calls.append(user)
            return '{"steps":[{"agent":"browser","task":"compare two laptops"}]}'
        plan = asyncio.run(agents.decide("open notepad", complete, installed))
        self.assertEqual((plan.how, plan.steps[0].agent), ("words", "computer"))
        self.assertIsNone(asyncio.run(agents.decide("hi there", complete, installed)))
        self.assertEqual(calls, [])
        plan = asyncio.run(agents.decide("find me a good laptop and compare two", complete,
                                         installed))
        self.assertEqual((plan.how, len(calls)), ("model", 1))

        async def broken(system, user):
            raise RuntimeError("all routes benched")
        self.assertIsNone(asyncio.run(agents.decide("find me a laptop", broken, installed)))


class TalkTests(unittest.TestCase):
    def test_the_next_agent_hears_what_the_last_one_found(self):
        text = agents.handover([agents.StepResult(
            "browser", "find the cheapest flight", True, "IndiGo, ₹4,210 on Friday.",
            ["6E 5321 dep 06:10", "₹4,210"])])
        self.assertIn("Browser agent did “find the cheapest flight” → done: IndiGo", text)
        self.assertIn("· 6E 5321 dep 06:10", text)
        self.assertEqual(agents.handover([]), "")
        self.assertIn("browser agent can find anything on the web", agents.ask_note("computer"))

    def test_a_question_goes_to_the_other_agent_or_the_user(self):
        self.assertEqual(agents.consult_target("browser", "computer"), "browser")
        self.assertEqual(agents.consult_target("Web.", "computer"), "browser")
        self.assertIsNone(agents.consult_target("browser", "browser"))   # not itself
        self.assertIsNone(agents.consult_target("user", "computer"))
        self.assertIsNone(agents.consult_target("", "computer"))

    def test_the_outcome_says_where_it_stopped(self):
        plan = agents.Plan([agents.Step("browser", "find it"), agents.Step("computer", "write it")])
        ok = agents.StepResult("browser", "find it", True, "Found: ₹4,210.")
        bubble, say = agents.outcome(plan, [ok, agents.StepResult(
            "computer", "write it", True, "Written in Notepad, sir.")])
        self.assertEqual(say, "Written in Notepad, sir.")
        bubble, say = agents.outcome(plan, [agents.StepResult(
            "browser", "find it", False, "The site was down.")])
        self.assertEqual(say, "I stopped at step 1 of 2: The site was down.")
        self.assertIn("– Desktop agent: not started — write it", bubble)
        self.assertIn("🧭 2 agents on this:", agents.plan_text(plan))


class CoordinatorTests(unittest.IsolatedAsyncioTestCase):
    """main._run_agent_plan with the operator, consent and model faked."""

    async def asyncSetUp(self):
        import main
        self.main = main
        self.hud, self.runs, self.asked = [], [], []
        main._autopilot_owner = ""
        main._interrupt = asyncio.Event()
        main._acted_and_failed.clear()

        async def emit(event, data):
            self.hud.append((event, data))

        async def permission(kind, desc, timeout=120.0):
            self.asked.append(kind)
            return True
        self.outcomes = {}

        async def run_task(kind, goal, **kw):
            self.runs.append((kind, goal, kw.get("hint", "")))
            fn = self.outcomes.get(kind)
            return await fn(goal, kw) if fn else {"ok": True, "summary": "Done.", "findings": []}

        self.consult_answer = "user"

        async def quick(system, user, **kw):
            return self.consult_answer

        for name, value in (("emit", emit), ("request_permission", permission),
                            ("quick_completion", quick), ("_tts", None),
                            ("consented", lambda kind: kind in self.consented),
                            ("grant", lambda kind: self.consented.add(kind))):
            p = mock.patch.object(main, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(main.autopilot, "run_task", run_task)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch.object(main, "remember_turn_outcome", lambda *a: None)
        p.start()
        self.addCleanup(p.stop)
        self.consented = set()
        p = mock.patch("actions.browser.is_approved", lambda: "browser" in self.consented)
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("actions.computer.is_armed", lambda: "computer" in self.consented)
        p.start()
        self.addCleanup(p.stop)

    def plan(self, *steps):
        return agents.Plan([agents.Step(a, t) for a, t in steps])

    async def test_the_second_agent_works_from_what_the_first_found(self):
        async def browser(goal, kw):
            return {"ok": True, "summary": "IndiGo is cheapest at ₹4,210.",
                    "findings": ["6E 5321, Fri 06:10, ₹4,210"]}
        self.outcomes["browser"] = browser
        await self.main._run_agent_plan("flight to Goa into notepad", self.plan(
            ("browser", "find the cheapest flight to Goa on Friday"),
            ("computer", "open Notepad and write the flight the browser agent found")))
        self.assertEqual([r[0] for r in self.runs], ["browser", "computer"])
        hint = self.runs[1][2]
        self.assertIn("Browser agent did “find the cheapest flight to Goa on Friday” → done", hint)
        self.assertIn("6E 5321, Fri 06:10, ₹4,210", hint)
        self.assertEqual(self.asked, ["browser", "computer"])        # each surface's consent
        said = [d for e, d in self.hud if e == "response"]
        self.assertTrue(said[0].startswith("🧭 2 agents on this:"))
        self.assertIn("✓ Desktop agent", said[-1])
        labels = [d.get("agent") for e, d in self.hud if e == "agent_task"]
        self.assertEqual(labels, ["Browser agent · step 1 of 2", "Desktop agent · step 2 of 2"])
        [msg] = [d for e, d in self.hud if e == "agent_message"]     # the chat sees the hand-over
        self.assertEqual((msg["from"], msg["to"], msg["kind"]), ("browser", "computer", "handover"))
        self.assertEqual(msg["items"], ["6E 5321, Fri 06:10, ₹4,210"])
        self.assertTrue(msg["line"].startswith("🌐 Browser agent → 🖱 Desktop agent: IndiGo"))

    async def test_a_failed_step_stops_the_plan(self):
        async def browser(goal, kw):
            return {"ok": False, "summary": "Google Flights didn't load.", "findings": []}
        self.outcomes["browser"] = browser
        await self.main._run_agent_plan("x", self.plan(("browser", "find it"),
                                                       ("computer", "write it in Notepad")))
        self.assertEqual([r[0] for r in self.runs], ["browser"])
        self.assertIn("– Desktop agent: not started", [d for e, d in self.hud
                                                       if e == "response"][-1])

    async def test_a_refused_step_starts_nothing(self):
        async def deny(kind, desc, timeout=120.0):
            return False
        with mock.patch.object(self.main, "request_permission", deny):
            await self.main._run_agent_plan("x", self.plan(("computer", "tidy my desktop")))
        self.assertEqual(self.runs, [])

    async def test_an_agent_asks_the_other_agent_before_the_user(self):
        self.consented.update({"browser", "computer"})
        got = {}

        async def computer(goal, kw):
            got["answer"] = await kw["ask"]("What is the price of the cheapest flight?")
            return {"ok": True, "summary": "Written.", "findings": []}

        async def browser(goal, kw):
            return {"ok": True, "summary": "₹4,210 on IndiGo.", "findings": []}
        self.outcomes.update(computer=computer, browser=browser)
        self.consult_answer = "browser"
        with mock.patch.object(self.main, "request_clarification",
                               side_effect=AssertionError("the user was asked")):
            await self.main._run_agent_plan("x", self.plan(("computer", "write the price")))
        self.assertEqual(got["answer"], "₹4,210 on IndiGo.")
        self.assertEqual(self.runs[1][0], "browser")
        self.assertTrue(self.runs[1][1].startswith("Find this out and report it"))
        talk = [(d["from"], d["to"], d["kind"], d["text"]) for e, d in self.hud
                if e == "agent_message"]
        self.assertEqual(talk, [
            ("computer", "browser", "ask", "What is the price of the cheapest flight?"),
            ("browser", "computer", "answer", "₹4,210 on IndiGo.")])

    async def test_a_choice_only_the_user_can_make_still_goes_to_the_user(self):
        self.consented.add("browser")

        async def browser(goal, kw):
            return {"ok": True, "summary": f"Booked {await kw['ask']('Window or aisle?')}.",
                    "findings": []}
        self.outcomes["browser"] = browser
        self.consult_answer = "user"

        async def clarify(question, timeout=90.0):
            return "aisle"
        with mock.patch.object(self.main, "request_clarification", clarify):
            await self.main._run_agent_plan("x", self.plan(("browser", "book a seat")))
        self.assertEqual(len(self.runs), 1)

    async def test_a_turn_agent_hands_the_rest_to_the_browser_agent(self):
        turns = []

        async def turn_step(step, task_tier, context):
            turns.append((step.agent, step.task))
            return "Three files: a.pdf (rent), b.docx (CV), c.png (a receipt)."
        self.consult_answer = '{"next":{"agent":"browser","task":"post the summary on my blog"}}'
        with mock.patch.object(self.main, "_agent_turn_step", turn_step):
            await self.main._run_agent_plan(
                "summarize my downloads and post it on my blog",
                self.plan(("files", "summarize my downloads")))
        self.assertEqual(turns, [("files", "summarize my downloads")])
        self.assertEqual([r[:2] for r in self.runs], [("browser", "post the summary on my blog")])
        self.assertIn("Files agent did “summarize my downloads” → done: Three files", self.runs[0][2])
        [msg] = [d for e, d in self.hud if e == "agent_message"]
        self.assertEqual((msg["from"], msg["to"], msg["kind"], msg["task"]),
                         ("files", "browser", "next", "post the summary on my blog"))

    async def test_research_runs_headless_on_the_browsers_consent(self):
        targets = []

        async def research(goal, kw):
            from actions import browser
            targets.append(browser.get_target())
            return {"ok": True, "summary": "Pixel 8a is the pick.", "findings": ["a (gsmarena)"]}
        self.outcomes["browser"] = research
        await self.main._run_agent_plan("x", self.plan(("research", "best budget phone")))
        self.assertEqual(self.asked, ["browser"])
        self.assertEqual(targets, ["headless"])
        self.assertTrue(self.runs[0][1].startswith("Research this and report back: best budget"))
        from actions import browser
        self.assertNotEqual(browser.get_target(), "headless")

    async def test_agent_mode_off_or_a_plain_question_never_plans(self):
        async def never(*a, **k):
            raise AssertionError("no model call for a plain question")
        with mock.patch.object(self.main, "quick_completion", never):
            self.assertIsNone(await self.main._agent_plan_for("what's the capital of France?"))



class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_agent_mode_hands_the_turn_to_the_coordinator_and_simple_mode_doesnt(self):
        import main
        main._interrupt = asyncio.Event()
        ran, streamed = [], []

        async def plan_for(text):
            return agents.Plan([agents.Step("computer", text)])

        async def run_plan(text, plan, **kw):
            ran.append((text, plan.steps[0].agent))

        async def stream(text, **kw):
            streamed.append(text)
            if False:
                yield "", ""

        async def emit(event, data):
            pass
        with mock.patch.object(main, "_agent_plan_for", plan_for), \
             mock.patch.object(main, "_run_agent_plan", run_plan), \
             mock.patch.object(main, "stream_prompt", stream), \
             mock.patch.object(main, "emit", emit), \
             mock.patch.object(main, "_tts", None):
            with mock.patch.object(main, "get_agent_mode", lambda: True):
                await main._pipeline_turn("open notepad and write a list", True, "")
            self.assertEqual(ran, [("open notepad and write a list", "computer")])
            self.assertEqual(streamed, [])
            with mock.patch.object(main, "get_agent_mode", lambda: False), \
                 mock.patch.object(main, "_agent_plan_for",
                                   side_effect=AssertionError("planned in simple mode")):
                await main._pipeline_turn("open notepad", True, "")
            self.assertEqual(streamed, ["open notepad"])


if __name__ == "__main__":
    unittest.main()

    async def test_a_picked_agent_or_a_mention_skips_the_coordinator(self):
        import main
        main._interrupt = asyncio.Event()
        ran = []

        async def run_plan(text, plan, **kw):
            ran.append((text, plan.steps[0].agent, plan.how))

        async def emit(event, data):
            pass
        never = mock.AsyncMock(side_effect=AssertionError("the coordinator was asked"))
        with mock.patch.object(main, "_agent_plan_for", never), \
             mock.patch.object(main, "_run_agent_plan", run_plan), \
             mock.patch.object(main, "emit", emit), \
             mock.patch.object(main, "_tts", None):
            with mock.patch.object(main, "get_agent_mode", lambda: False):
                await main._pipeline_turn("@research solid state batteries", True, "")
            with mock.patch.object(main, "get_agent_mode", lambda: True), \
                 mock.patch.object(main, "get_active_agent", lambda: "files"):
                await main._pipeline_turn("what's in my downloads", True, "")
        self.assertEqual(ran, [("solid state batteries", "research", "picked"),
                               ("what's in my downloads", "files", "picked")])

    async def test_a_whatsapp_command_ignores_the_hud_pick(self):
        import main
        main._interrupt = asyncio.Event()
        asked = []

        async def plan_for(text):
            asked.append(text)
            return None

        async def chat(text, task_tier, **kw):
            pass

        async def emit(event, data):
            pass
        with mock.patch.object(main, "_agent_plan_for", plan_for), \
             mock.patch.object(main, "_chat_turn", chat), \
             mock.patch.object(main, "emit", emit), \
             mock.patch.object(main, "_wa_chat", object()), \
             mock.patch.object(main, "get_agent_mode", lambda: True), \
             mock.patch.object(main, "get_active_agent", lambda: "files"):
            await main._pipeline_turn("what's the time", True, "")
        self.assertEqual(asked, ["what's the time"])        # Auto, not the Files agent


class ForcedToolsTests(unittest.TestCase):
    def test_an_agents_groups_stay_loaded_for_its_turn(self):
        from llm import live_tools
        live_tools.force(("files",))
        try:
            self.assertIn("files", live_tools.begin_turn("hello"))
        finally:
            live_tools.force(())
        self.assertNotIn("files", live_tools.begin_turn("hello again"))
