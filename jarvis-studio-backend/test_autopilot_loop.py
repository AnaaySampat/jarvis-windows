"""End-to-end tests for the operator loop with a scripted model and fake surfaces.

No browser, no UIA, no network — `quick_completion`, the observers and the
executors are all stubbed, so the loop's control flow (fast path, handoff,
consent, the repetition guards) is exercised deterministically.

This is the harness the guard work needs: the guards are the part of the loop
that is impossible to check by reading, because every one of them is a counter
interacting with eight others.
"""

import asyncio
import unittest

import autopilot as ap


class FakeSurface:
    """Records commands and replays scripted (ok, msg, extras) results."""

    def __init__(self, results=None, default=(True, "Did it.", None)):
        self.results = list(results or [])
        self.default = default
        self.commands = []
        self.observations = 0

    def observe(self):
        self.observations += 1
        # Vary the observation so the stale-observation guard stays quiet unless
        # a test wants it; identical text is what that guard keys on.
        return f"Page: test — step {self.observations}\n1. Something [button]"

    def execute(self, cmd):
        self.commands.append(cmd)
        ok, msg, extras = (self.results.pop(0) if self.results else self.default)
        return ok, msg, dict(extras or {})


def run_loop(test, *, kind="browser", goal="do the thing", replies=(),
             browser=None, computer=None, consent=("browser", "computer"),
             foreground=None, verdicts=(), lanes=None, calls=None, scores=None,
             **kwargs):
    """Drive ap.run_task with everything external stubbed. Returns (result, surfaces)."""
    browser = browser or FakeSurface()
    computer = computer or FakeSurface()
    step_replies = list(replies)
    verify_replies = list(verdicts)
    step_calls = {"n": 0, "verify": 0}

    async def quick_completion(system, user, **_kw):
        if calls is not None:               # tests that check which models were asked
            calls.append((system, _kw))
        if system is ap._PLAN_SYSTEM:
            return ""                       # skip planning — not under test here
        if system is ap._VERIFY_SYSTEM:
            step_calls["verify"] += 1       # completion checker: scripted, else agrees
            return verify_replies.pop(0) if verify_replies else '{"verdict":"pass"}'
        step_calls["n"] += 1
        return step_replies.pop(0) if step_replies else '{"do":"fail","reason":"script exhausted"}'

    patches = {
        "quick_completion": quick_completion,
        "_OBSERVE": {"browser": browser.observe, "computer": computer.observe},
        "_EXEC": {"browser": browser.execute, "computer": computer.execute},
        # `consent` may be a set of granted surfaces, or a callable for tests
        # that need it to change mid-task (an expiring window).
        "_consent_ok": consent if callable(consent) else (lambda k: k in consent),
        "_common_folders_note": lambda: "",
        "autopilot_vision_enabled": lambda: False,
        "autopilot_model_is_vision": lambda: False,
        "autopilot_thinking_budget": lambda: None,
        # The real lanes read this PC's keys, ranking and quota state.
        "autopilot_lane_models": lanes or (lambda lane: []),
        "autopilot_lane_wait": lambda lane: 0.0,
        "model_score": scores or (lambda model: 0.0),
    }
    saved = {name: getattr(ap, name) for name in patches}
    for name, value in patches.items():
        setattr(ap, name, value)
    # The real foreground window would make desktop runs depend on this PC's desktop.
    real_fg, real_sig, real_settle = (ap.computer.foreground_app,
                                      ap.computer.focus_signature, ap._settle)
    ap.computer.foreground_app = foreground or (lambda: {"hwnd": 1, "title": "App"})
    ap.computer.focus_signature = lambda: "sig"      # no real UIA, no settle waits
    ap._settle = lambda *_a: None
    try:
        result = asyncio.run(ap.run_task(kind, goal, **kwargs))
    finally:
        for name, value in saved.items():
            setattr(ap, name, value)
        ap.computer.foreground_app = real_fg
        ap.computer.focus_signature, ap._settle = real_sig, real_settle
    return result, {"browser": browser, "computer": computer, "steps": step_calls["n"],
                    "verify": step_calls["verify"]}


class FastPathTests(unittest.TestCase):
    def test_scroll_down_runs_one_action_and_never_calls_the_model(self):
        result, s = run_loop(self, goal="scroll down",
                             browser=FakeSurface([(True, "Scrolled down.", None)]))
        self.assertTrue(result["ok"])
        self.assertEqual(s["steps"], 0, "the step model must not be consulted")
        self.assertEqual(len(s["browser"].commands), 1)
        self.assertEqual(s["browser"].commands[0]["do"], "scroll")
        self.assertEqual(result["summary"], "Scrolled down, sir.")

    def test_a_compound_goal_is_not_fast_pathed(self):
        _result, s = run_loop(
            self, goal="scroll down and click the first result",
            replies=['{"do":"done","summary":"Done, sir."}'])
        self.assertGreater(s["steps"], 0, "must go through the operator loop")

    def test_a_failed_fast_path_falls_through_to_the_loop(self):
        # "go back" with nothing to go back to, then the loop handles it properly.
        result, s = run_loop(
            self, goal="go back",
            browser=FakeSurface([(False, "There's no page to go back to.", None),
                                 (True, "Went back, sir.", None)]),
            replies=['{"do":"back"}', '{"do":"done","summary":"Went back, sir."}'])
        self.assertTrue(result["ok"])
        self.assertGreater(s["steps"], 0)

    def test_a_desktop_open_fast_paths_to_a_launch(self):
        result, s = run_loop(self, kind="computer", goal="open Notepad",
                             computer=FakeSurface([(True, "Opened Notepad.", None)]))
        self.assertTrue(result["ok"])
        self.assertEqual(s["computer"].commands[0], {"do": "launch", "app": "Notepad"})

    def test_a_leading_web_address_is_opened_without_a_model_call(self):
        result, s = run_loop(
            self, goal="go to news.ycombinator.com and tell me the title of the top story",
            replies=['{"do":"done","summary":"The top story is about Qwen, sir."}'])
        self.assertTrue(result["ok"])
        self.assertEqual(s["browser"].commands[0],
                         {"do": "open", "url": "news.ycombinator.com"})
        self.assertEqual(s["steps"], 1, "only the answer needs the operator")

    def test_the_browser_lead_only_takes_real_addresses_not_site_searches(self):
        lead = ap._leading_launch
        self.assertEqual(lead("browser", "Open github.com/trending, then tell me the top repo"),
                         {"do": "open", "url": "github.com/trending"})
        # The operator searches the site directly; its home page would be a wasted load.
        self.assertIsNone(lead("browser", "go to youtube.com and play lofi hip hop"))
        self.assertIsNone(lead("browser", "go to amazon.com and search for a keyboard"))
        # A bare name would become a Google search for the name.
        self.assertIsNone(lead("browser", "open amazon and tell me today's deals"))
        self.assertIsNone(lead("browser", "tell me the top story on news.ycombinator.com"))

    def test_a_browser_open_of_a_bare_app_name_is_not_fast_pathed(self):
        # This is the misroute the router exists to stop; the fast path must not
        # re-open the hole by navigating to an invented URL.
        _result, s = run_loop(self, goal="open Notepad",
                              replies=['{"do":"handoff","to":"computer"}',
                                       '{"do":"done","summary":"Opened Notepad, sir."}'])
        self.assertGreater(s["steps"], 0)


class HandoffTests(unittest.TestCase):
    def test_a_browser_task_can_hand_itself_to_the_desktop(self):
        result, s = run_loop(
            self, kind="browser", goal="write a note in Notepad",
            replies=['{"do":"handoff","to":"computer"}',
                     '{"do":"launch","app":"Notepad"}',
                     '{"do":"done","summary":"Typed the note, sir."}'])
        self.assertTrue(result["ok"])
        self.assertEqual(len(s["computer"].commands), 1, "work moved to the desktop")
        self.assertEqual(s["computer"].commands[0]["do"], "launch")
        self.assertEqual(len(s["browser"].commands), 0)

    def test_a_desktop_task_can_hand_itself_to_the_browser(self):
        result, s = run_loop(
            self, kind="computer", goal="edit the design",
            replies=['{"do":"handoff","to":"browser"}',
                     '{"do":"click","target":"1"}',
                     '{"do":"done","summary":"Edited it, sir."}'])
        self.assertTrue(result["ok"])
        self.assertEqual(len(s["browser"].commands), 1)

    def test_handing_off_to_an_unapproved_surface_stops_honestly(self):
        result, s = run_loop(
            self, kind="browser", goal="write a note in Notepad",
            consent=("browser",),                     # computer control NOT armed
            replies=['{"do":"handoff","to":"computer"}'])
        self.assertFalse(result["ok"])
        self.assertIn("approve", result["summary"].lower())
        self.assertEqual(len(s["computer"].commands), 0, "nothing may be actuated")

    def test_only_one_handoff_per_task(self):
        result, _s = run_loop(
            self, kind="browser", goal="do the thing",
            replies=['{"do":"handoff","to":"computer"}',
                     '{"do":"handoff","to":"browser"}',   # refused
                     '{"do":"done","summary":"Finished, sir."}'])
        self.assertTrue(result["ok"])
        self.assertTrue(any("already switched once" in s for s in result["steps"]))

    def test_handing_off_to_the_surface_already_in_use_is_a_no_op(self):
        result, s = run_loop(
            self, kind="browser", goal="do the thing",
            replies=['{"do":"handoff","to":"browser"}',
                     '{"do":"done","summary":"Finished, sir."}'])
        self.assertTrue(result["ok"])
        self.assertTrue(any("already on the browser" in s for s in result["steps"]))
        self.assertEqual(len(s["computer"].commands), 0)


class RepetitionGuardTests(unittest.TestCase):
    def test_the_observed_next_video_run_is_stopped(self):
        # The real failure: goal "scroll down", nine successful clicks on the
        # same control, each under a DIFFERENT element number so every
        # number-keyed guard stayed silent.
        clicks = [f'{{"do":"click","target":"{n}"}}' for n in range(3, 15)]
        result, s = run_loop(
            self, goal="find the lecture video",     # not fast-pathable
            replies=clicks,
            browser=FakeSurface(default=(True, "Clicked “Next video”.", None)))
        self.assertFalse(result["ok"])
        self.assertLess(len(s["browser"].commands), 5,
                        "must stop within a few repeats, not nine")

    def test_genuinely_different_clicks_are_not_stopped(self):
        labels = ["Search", "First result", "Play", "Fullscreen"]
        result, s = run_loop(
            self, goal="watch the lecture video",
            replies=[f'{{"do":"click","target":"{i}"}}' for i in range(len(labels))]
                    + ['{"do":"done","summary":"Playing it now, sir."}'],
            browser=FakeSurface([(True, f"Clicked “{lb}”.", None) for lb in labels]))
        self.assertTrue(result["ok"])
        self.assertEqual(len(s["browser"].commands), len(labels))


class TruthfulnessTests(unittest.TestCase):
    def test_a_done_summary_that_reads_as_incomplete_is_not_a_success(self):
        result, _s = run_loop(
            self, goal="check the price",
            replies=['{"do":"click","target":"1"}',
                     '{"do":"done","summary":"Let me wait for the page to load."}',
                     '{"do":"done","summary":"Still loading, one moment."}'])
        self.assertFalse(result["ok"])

    def test_findings_survive_an_abort(self):
        result, _s = run_loop(
            self, goal="compare and note the price of three keyboards",
            replies=['{"do":"note","text":"Keychron K2 is $89"}']
                    + [f'{{"do":"click","target":"{n}"}}' for n in range(9)],
            browser=FakeSurface(default=(True, "Clicked “Next”.", None)))
        self.assertFalse(result["ok"])
        self.assertIn("Keychron K2 is $89", result.get("findings", []))


class PredictedFinishTests(unittest.TestCase):
    """An action that finishes the goal may carry "done"; the completion
    check confirms it on the next observation instead of an operator call."""

    def test_a_confirmed_prediction_ends_without_another_operator_call(self):
        result, s = run_loop(
            self, goal="open the first video",
            replies=['{"do":"click","target":"3","done":"Playing it now, sir."}'])
        self.assertTrue(result["ok"])
        self.assertEqual(result["summary"], "Playing it now, sir.")
        self.assertEqual(s["steps"], 1, "no operator call just to look and say done")
        self.assertEqual(s["verify"], 1)

    def test_a_refuted_prediction_hands_back_to_the_operator_for_good(self):
        result, s = run_loop(
            self, goal="open the first video",
            verdicts=['{"verdict":"fail","reason":"an ad opened instead"}'],
            replies=['{"do":"click","target":"3","done":"Playing it now, sir."}',
                     '{"do":"click","target":"4","done":"Playing it now, sir."}',
                     '{"do":"done","summary":"Playing it now, sir."}'])
        self.assertTrue(result["ok"])
        self.assertEqual(s["steps"], 3, "after one miss, predictions are not trusted")
        self.assertTrue(any("NOT confirmed" in st for st in result["steps"]))

    def test_a_failed_action_never_finishes_the_task(self):
        result, s = run_loop(
            self, goal="open the first video",
            browser=FakeSurface([(False, "Element 3 isn't on this page any more.", None)]),
            replies=['{"do":"click","target":"3","done":"Playing it now, sir."}',
                     '{"do":"fail","reason":"The video did not open, sir."}'])
        self.assertFalse(result["ok"])
        self.assertEqual(s["verify"], 0)


if __name__ == "__main__":
    unittest.main()


class ReconsentTests(unittest.TestCase):
    """A bounded consent window that expires mid-task asks for itself back
    rather than binning everything the task has already done."""

    def _run(self, regrant, replies):
        # Consent lapses once the first action has been executed.
        state = {"granted": True, "asked": 0}
        surface = FakeSurface()
        original = surface.execute

        def execute(cmd):
            result = original(cmd)
            if not state.get("expired"):    # the window closes once, mid-task
                state["expired"] = True
                state["granted"] = False
            return result

        surface.execute = execute

        async def reconsent(_kind, _goal):
            state["asked"] += 1
            state["granted"] = regrant
            return regrant

        result, s = run_loop(self, goal="do the whole job", replies=replies,
                             browser=surface, consent=lambda _k: state["granted"],
                             reconsent=reconsent)
        return result, s, state

    def test_a_regranted_window_lets_the_task_finish(self):
        result, s, state = self._run(True, [
            '{"do":"click","target":"1"}',
            '{"do":"click","target":"2"}',
            '{"do":"done","summary":"All finished, sir."}'])
        self.assertEqual(state["asked"], 1, "asked exactly once")
        self.assertTrue(result["ok"])
        self.assertTrue(any("re-approved" in line for line in result["steps"]))
        self.assertEqual(len(s["browser"].commands), 2, "the work carried on")

    def test_a_refused_window_stops_the_task(self):
        result, s, state = self._run(False, [
            '{"do":"click","target":"1"}',
            '{"do":"click","target":"2"}'])
        self.assertEqual(state["asked"], 1)
        self.assertFalse(result["ok"])
        self.assertIn("approve it again", result["summary"])
        self.assertEqual(len(s["browser"].commands), 1, "nothing ran after the refusal")

    def test_without_a_hook_an_expired_window_still_stops_cleanly(self):
        state = {"granted": True}
        surface = FakeSurface()
        original = surface.execute

        def execute(cmd):
            result = original(cmd)
            state["granted"] = False
            return result

        surface.execute = execute
        result, _s = run_loop(self, goal="do the whole job",
                              replies=['{"do":"click","target":"1"}',
                                       '{"do":"click","target":"2"}'],
                              browser=surface, consent=lambda _k: state["granted"])
        self.assertFalse(result["ok"])
        self.assertIn("expired mid-task", result["summary"])


class StaleDecisionTests(unittest.TestCase):
    """A decision takes seconds; nothing may fire against a state that moved."""

    def test_a_window_change_during_the_decision_holds_the_action(self):
        hwnds = iter([1, 2, 2, 2, 2, 2])          # observe → 1, then the user switched
        result, s = run_loop(
            self, kind="computer", goal="type hello in the editor",
            foreground=lambda: {"hwnd": next(hwnds, 2)},
            replies=['{"do":"type","text":"hello"}', '{"do":"type","text":"hello"}',
                     '{"do":"done","summary":"Typed hello."}'])
        self.assertEqual(len(s["computer"].commands), 1, "held once, then ran once")
        self.assertTrue(any("HELD" in line for line in result["steps"]), result["steps"])

    def test_a_pause_during_the_decision_holds_the_action(self):
        calls = {"n": 0}

        def paused():
            calls["n"] += 1
            return calls["n"] == 2                 # true only at the pre-action check
        result, s = run_loop(
            self, goal="click the button", paused=paused,
            replies=['{"do":"click","target":"1"}', '{"do":"click","target":"1"}',
                     '{"do":"done","summary":"Clicked it."}'])
        self.assertEqual(len(s["browser"].commands), 1)
        self.assertTrue(result["ok"], result)

    def test_steps_that_never_act_stop_the_task(self):
        # Varied, so no repetition guard fires first: a skipped look, a handoff to
        # the surface it's already on, a duplicate note, over and over.
        idle = ['{"do":"see"}', '{"do":"handoff","to":"browser"}',
                '{"do":"note","text":"price is $5"}']
        result, s = run_loop(self, goal="find the price",
                             replies=['{"do":"note","text":"price is $5"}'] + idle * 5)
        self.assertFalse(result["ok"])
        self.assertIn("price is $5", result["summary"])   # partial results kept
        self.assertEqual(s["steps"], 1 + ap.ProgressMonitor.IDLE_ABORT)
        self.assertEqual(s["browser"].commands, [])


class RateLimitPacingTests(unittest.TestCase):
    """No model answering is a rate limit, not confusion: wait, then carry on."""

    def _run(self, replies, wait_s):
        real = ap.route_wait_s
        ap.route_wait_s = lambda model="": wait_s
        try:
            return run_loop(self, goal="click the button", replies=replies)
        finally:
            ap.route_wait_s = real

    def test_a_brief_limit_is_waited_out_and_the_task_continues(self):
        result, s = self._run(["", '{"do":"click","target":"1"}',
                               '{"do":"done","summary":"Clicked it."}'], 0.1)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("rate limited" in line for line in result["steps"]))

    def test_a_lasting_limit_is_reported_as_quota_not_confusion(self):
        result, _ = self._run([""], 0.0)
        self.assertFalse(result["ok"])
        self.assertIn("quota", result["summary"])
        self.assertNotIn("work out the next step", result["summary"])


class SystemPromptTests(unittest.TestCase):
    """Situational rules ride along only when the goal can actually hit them."""

    def test_a_plain_browser_goal_gets_only_the_core(self):
        p = ap._system_for("browser", "open youtube and play lofi")
        self.assertNotIn("LONG PAGES", p)
        self.assertNotIn("DATE/TIME BOOKING", p)

    def test_a_research_goal_gets_the_long_page_rules(self):
        p = ap._system_for("browser", "research and note five facts about apollo 11")
        self.assertIn("LONG PAGES", p)

    def test_a_booking_goal_gets_the_calendar_rules(self):
        self.assertIn("DATE/TIME BOOKING",
                      ap._system_for("browser", "book a hotel for friday"))

    def test_a_plain_desktop_goal_gets_only_the_core(self):
        p = ap._system_for("computer", "open notepad and type a list")
        self.assertNotIn("NO-CONTROL apps", p)

    def test_a_java_app_goal_gets_the_coordinate_fallback_rules(self):
        self.assertIn("NO-CONTROL apps",
                      ap._system_for("computer", "open BlueJ and compile the class"))

    def test_the_canvas_rule_is_not_duplicated_in_the_base_prompt(self):
        # It's detected from the live page and injected per-step instead.
        self.assertNotIn("DESIGN-CANVAS EDITORS", ap._SYSTEM["browser"])

    def test_every_assembled_prompt_is_smaller_than_the_old_monolith(self):
        for kind, goal in (("browser", "open youtube"), ("computer", "open notepad")):
            self.assertLess(len(ap._system_for(kind, goal)), 3_200)


class ControlsSurface(FakeSurface):
    """A desktop window with a real-looking control list (three named controls),
    so the lane logic treats it as a window the fast text model can work in."""

    def observe(self):
        self.observations += 1
        return (f"Focused window: Untitled - Notepad (notepad) step {self.observations}\n"
                "1. Text editor [text area] (empty) @405,410\n"
                "2. File [menu item] @54,132\n3. Edit [menu item] @93,132")


class ChainTests(unittest.TestCase):
    """One decision may carry a chain of commands (save-as + path + Enter)."""

    def test_a_chain_runs_every_link_from_one_decision(self):
        comp = ControlsSurface()
        result, s = run_loop(
            self, kind="computer", goal="save the note as a.txt", computer=comp,
            replies=['[{"do":"press","keys":"ctrl+shift+s"},'
                     '{"do":"type","text":"C:\\\\x\\\\a.txt\\n","done":"Saved it, sir."}]'])
        self.assertTrue(result["ok"], result)
        self.assertEqual([c["do"] for c in comp.commands], ["press", "type"])
        self.assertEqual(s["steps"], 1, "both links came from ONE model call")

    def test_a_failed_link_stops_the_chain(self):
        comp = ControlsSurface([(False, "Key press failed.", None)])
        run_loop(self, kind="computer", goal="save it", computer=comp,
                 replies=['[{"do":"press","keys":"ctrl+s"},{"do":"type","text":"x"}]'])
        self.assertEqual(comp.commands[0]["do"], "press")
        self.assertNotIn({"do": "type", "text": "x"}, comp.commands,
                         "nothing after a failure may run blind")

    def test_a_link_that_needs_a_look_stops_the_chain(self):
        comp = ControlsSurface()
        result, _ = run_loop(self, kind="computer", goal="look it up", computer=comp,
                             replies=['[{"do":"press","keys":"ctrl+f"},{"do":"read"}]'])
        self.assertEqual([c["do"] for c in comp.commands][:1], ["press"])
        self.assertTrue(any("chain stopped" in ln for ln in result["steps"]))

    def test_browser_replies_never_chain(self):
        b = FakeSurface()
        run_loop(self, goal="search it", browser=b,
                 replies=['[{"do":"click","target":"3"},{"do":"click","target":"4"}]'])
        self.assertEqual(b.commands[0], {"do": "click", "target": "3"})
        self.assertNotIn({"do": "click", "target": "4"}, b.commands[:1])

    def test_parse_commands(self):
        p = ap._parse_commands
        self.assertEqual(p('{"do":"click","target":"2"}'), [{"do": "click", "target": "2"}])
        self.assertEqual(len(p("[" + ",".join(['{"do":"press","keys":"tab"}'] * 9) + "]")),
                         ap._MAX_CHAIN)
        # A trailing done becomes the last action's predicted finish.
        self.assertEqual(p('[{"do":"type","text":"hi"},{"do":"done","summary":"Typed."}]'),
                         [{"do": "type", "text": "hi", "done": "Typed."}])
        self.assertEqual(p('Sure: {"do":"wait"}'), [{"do": "wait"}])
        self.assertEqual(p("no json here"), [])


class LaneTests(unittest.TestCase):
    """Routine desktop steps run on the fast text lane; trouble or a window with
    no usable controls brings in the vision lane with the screenshot."""

    LANES = {"fast": ["fast-a", "fast-b"], "eyes": ["eyes-a"]}

    def _run(self, comp, replies, goal="type hello in notepad", vision=True, scores=None):
        calls = []
        saved = {n: getattr(ap, n) for n in ("autopilot_vision_enabled",
                                              "autopilot_model_is_vision",
                                              "_capture_desktop_b64")}
        ap._capture_desktop_b64 = lambda: "IMG"
        try:
            # run_loop pins vision off; re-enable it under the patch it applies.
            real_run = ap.run_task

            async def run_task(*a, **k):
                ap.autopilot_vision_enabled = lambda: vision
                ap.autopilot_model_is_vision = lambda: vision
                return await real_run(*a, **k)
            ap.run_task = run_task
            result, _ = run_loop(self, kind="computer", goal=goal, computer=comp,
                                 replies=replies, calls=calls, scores=scores,
                                 lanes=lambda lane: list(self.LANES[lane]))
        finally:
            ap.run_task = real_run
            for n, v in saved.items():
                setattr(ap, n, v)
        return result, [kw for system, kw in calls
                        if system not in (ap._PLAN_SYSTEM, ap._VERIFY_SYSTEM)]

    def test_a_routine_step_goes_to_the_fast_lane_without_a_picture(self):
        _r, steps = self._run(ControlsSurface(), ['{"do":"done","summary":"Done, sir."}'])
        self.assertEqual(steps[0]["model"], "fast-a")
        self.assertEqual(steps[0]["fallbacks"], ("fast-b",))
        self.assertEqual(steps[0]["image_b64"], "")

    def test_a_window_without_controls_goes_to_the_eyes_lane(self):
        _r, steps = self._run(FakeSurface(), ['{"do":"done","summary":"Done, sir."}'])
        self.assertEqual(steps[0]["model"], "eyes-a")
        self.assertEqual(steps[0]["image_b64"], "IMG")

    def test_two_failures_escalate_to_the_eyes_lane(self):
        comp = ControlsSurface([(False, "nope", None), (False, "nope", None)])
        _r, steps = self._run(comp, ['{"do":"click","target":"2"}',
                                     '{"do":"click","target":"3"}',
                                     '{"do":"done","summary":"Done, sir."}'])
        self.assertEqual([s["model"] for s in steps[:3]], ["fast-a", "fast-a", "eyes-a"])

    def test_trouble_never_hands_a_step_to_a_weaker_vision_model(self):
        comp = ControlsSurface([(False, "nope", None), (False, "nope", None)])
        _r, steps = self._run(comp, ['{"do":"click","target":"2"}',
                                     '{"do":"click","target":"3"}',
                                     '{"do":"done","summary":"Done, sir."}'],
                              scores=lambda m: {"fast-a": 26.0, "eyes-a": 16.0}.get(m, 0.0))
        self.assertEqual([s["model"] for s in steps[:3]], ["fast-a"] * 3)

    def test_without_vision_the_fast_lane_carries_everything(self):
        _r, steps = self._run(FakeSurface(), ['{"do":"done","summary":"Done, sir."}'],
                              vision=False)
        self.assertEqual(steps[0]["model"], "fast-a")


class DesktopHelperTests(unittest.TestCase):
    def test_settings_pages_open_without_a_model(self):
        sp = ap._settings_page
        self.assertEqual(sp("Open the Bluetooth settings page")[0], "ms-settings:bluetooth")
        self.assertEqual(sp("open display settings")[0], "ms-settings:display")
        self.assertEqual(sp("go to wifi settings")[0], "ms-settings:network-wifi")
        self.assertEqual(sp("open settings for night light")[0], "ms-settings:nightlight")
        self.assertEqual(sp("open the windows update settings")[0], "ms-settings:windowsupdate")
        self.assertEqual(sp("turn on bluetooth"), ("", ""))     # a change, not a page
        self.assertEqual(sp("open settings"), ("", ""))         # the app itself
        self.assertEqual(ap._fast_path("computer", "open dark mode settings")["app"],
                         "ms-settings:colors")
        # The same table serves a chat-level "open display settings" launch.
        su = ap.app_launcher.settings_uri
        self.assertEqual(su("display settings"), "ms-settings:display")
        self.assertEqual(su("the Bluetooth settings page"), "ms-settings:bluetooth")
        self.assertEqual(su("settings"), "")
        self.assertEqual(su("notepad"), "")

    def test_app_hints_match_by_process_or_uwp_title(self):
        self.assertIn("ctrl+shift+s", ap._app_hint({"app": "notepad", "title": "x"}))
        self.assertIn("Display is", ap._app_hint({"app": "applicationframehost",
                                                  "title": "Calculator"}))
        self.assertEqual(ap._app_hint({"app": "applicationframehost", "title": "Photos"}), "")
        self.assertEqual(ap._app_hint({"app": "chrome", "title": "Calculator - Google"}), "")

    def test_window_hints_name_the_window_a_launch_opens(self):
        self.assertEqual(ap._window_hint("ms-settings:bluetooth"), "Settings")
        self.assertEqual(ap._window_hint("notepad"), "notepad")
        self.assertEqual(ap._window_hint(r"C:\Users\x\report.docx"), "report")

    def test_a_window_needs_eyes_without_controls_or_for_a_visual_goal(self):
        listing = "1. A [button]\n2. B [button]\n3. C [button]"
        self.assertFalse(ap._needs_eyes("type hello", listing))
        self.assertTrue(ap._needs_eyes("type hello", "1. A [button]"))
        self.assertTrue(ap._needs_eyes("what colour is the icon", listing))
        # Title-bar buttons are every window's; an Electron app listed only those.
        chrome = ("1. Minimize [button]\n2. Maximize [button]\n3. Restore [button]\n"
                  "4. Close [button]\n5. Agents [text area]")
        self.assertTrue(ap._needs_eyes("type hello", chrome))

    def test_every_key_press_has_its_own_signature(self):
        # All presses once shared the signature "press", so ANY three in a row
        # (ctrl+n, esc, ctrl+shift+p) aborted as "hitting the same thing".
        sig = ap._action_sig
        self.assertNotEqual(sig({"do": "press", "keys": "ctrl+n"}, "press"),
                            sig({"do": "press", "keys": "esc"}, "press"))


class RedoKeyTests(unittest.TestCase):
    def test_a_submit_is_not_a_redo_of_the_same_text(self):
        k = ap._redo_key
        self.assertNotEqual(k({"do": "type", "text": "C:\\a.txt"}, "type"),
                            k({"do": "type", "text": "C:\\a.txt\n"}, "type"))
        self.assertEqual(k({"do": "type", "text": " hi "}, "type"),
                         k({"do": "type", "text": "hi"}, "type"))

    def test_the_step_log_keeps_a_long_paths_enter(self):
        """Cut at 60 chars, the \\n vanished: the model pressed Enter again, into
        the editor, after the Save dialog had already closed."""
        line = ap._describe({"do": "type", "text": "C:\\Users\\" + "x" * 80 + "\\a.txt\n"})
        self.assertTrue(line.endswith("+ Enter"), line)
        self.assertNotIn("\n", line)

    def test_a_file_dialog_gets_its_own_hint_over_the_apps(self):
        self.assertIn("File name:", ap._app_hint({"app": "notepad", "title": "Save as"}))


class DiscardGuardTests(unittest.TestCase):
    """The model may not throw the user's work away on its own say-so."""

    def test_dont_save_is_blocked_unless_the_goal_asks(self):
        real = ap.computer._ui_cache
        ap.computer._ui_cache = [("", "Save", "ButtonControl", 0),
                                 ("", "Don't Save", "ButtonControl", 0)]
        try:
            self.assertEqual(ap._discard_click({"do": "click", "target": "2"}, "click",
                                               "close Word"), "Don't Save")
            self.assertEqual(ap._discard_click({"do": "click", "target": "1"}, "click",
                                               "close Word"), "")
            self.assertEqual(ap._discard_click({"do": "click", "target": "Don't save"},
                                               "click", "close Word without saving"), "")
            for button in ("Empty Recycle Bin", "Uninstall", "Reset this PC"):
                self.assertEqual(ap._discard_click({"do": "click", "target": button},
                                                   "click", "open the recycle bin"), button)
            self.assertEqual(ap._discard_click({"do": "click", "target": "Empty Recycle Bin"},
                                               "click", "empty the recycle bin"), "")
        finally:
            ap.computer._ui_cache = real

    def test_a_blocked_discard_never_reaches_the_app(self):
        comp = ControlsSurface()
        real = ap.computer._ui_cache
        ap.computer._ui_cache = [("", "Don't Save", "ButtonControl", 0)]
        try:
            result, _ = run_loop(self, kind="computer", goal="close Word", computer=comp,
                                 replies=['{"do":"click","target":"1"}',
                                          '{"do":"fail","reason":"Needs your call, sir."}'])
        finally:
            ap.computer._ui_cache = real
        self.assertEqual(comp.commands, [])
        self.assertTrue(any("BLOCKED" in s for s in result["steps"]))