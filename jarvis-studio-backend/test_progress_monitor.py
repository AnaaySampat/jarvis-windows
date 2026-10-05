"""Each operator guard, on its own.

These used to be nine counters interleaved through a 600-line loop body, which is
why a broken one (repetition keyed on element numbers) went unnoticed. Now every
threshold has a test that names what it protects against.
"""

import unittest

from autopilot import ProgressMonitor


def clicked(label):
    """An executor result for a successful click on `label`."""
    return True, f"Clicked “{label}”.", {}


class StaleObservationTests(unittest.TestCase):
    """A successful action that changes nothing on screen did nothing."""

    def setUp(self):
        self.m = ProgressMonitor("do the thing")

    def _act(self):
        self.m.after({"do": "click", "target": "1"}, "click", *clicked("Next"))

    def test_an_unchanged_observation_after_a_real_action_steers_then_aborts(self):
        self.m.observed("screen A")
        verdicts = []
        for _ in range(4):
            self._act()
            verdicts.append(self.m.observed("screen A").action)
        self.assertEqual(verdicts[0], "steer")
        self.assertEqual(verdicts[-1], "abort")

    def test_a_changing_screen_never_trips_it(self):
        for i in range(8):
            self._act()
            self.assertEqual(self.m.observed(f"screen {i}").action, "continue")

    def test_a_repeat_screen_after_a_non_actuation_is_not_stale(self):
        # 'note' and 'ask' change nothing by design; they must not look stuck.
        self.m.observed("screen A")
        self.m.last_actuation_ok = False
        self.assertEqual(self.m.observed("screen A").action, "continue")

    def test_the_streak_resets_when_the_screen_moves_again(self):
        self.m.observed("A")
        self._act()
        self.assertEqual(self.m.observed("A").action, "steer")
        self._act()
        self.m.observed("B")
        self.assertEqual(self.m.stale_streak, 0)


class RepeatGuardTests(unittest.TestCase):
    def test_the_same_command_three_times_aborts(self):
        m = ProgressMonitor("do the thing")
        cmd = {"do": "click", "target": "5"}
        self.assertEqual(m.before(cmd, "click", []).action, "continue")
        self.assertEqual(m.before(cmd, "click", []).action, "continue")
        self.assertEqual(m.before(cmd, "click", []).action, "abort")

    def test_alternating_commands_do_not_trip_the_consecutive_counter(self):
        # They DO trip the cycle guard (A-B-A-B is exactly what it's for) — this
        # is only about the consecutive counter never accumulating.
        m = ProgressMonitor("do the thing")
        for i in range(4):
            m.before({"do": "click", "target": str(i % 2)}, "click", [])
            self.assertEqual(m.repeats, 0)

    def test_varied_commands_run_freely(self):
        m = ProgressMonitor("do the thing")
        for i in range(6):
            v = m.before({"do": "click", "target": str(i)}, "click", [])
            self.assertEqual(v.action, "continue")


class RedoGuardTests(unittest.TestCase):
    def test_redoing_a_succeeded_command_is_skipped_once_then_allowed(self):
        m = ProgressMonitor("do the thing")
        cmd = {"do": "type", "text": "hello", "target": "3"}
        m.before(cmd, "type", [])
        m.after(cmd, "type", True, "Typed “hello”.", {})
        # Second time: refused with a corrective note.
        v = m.before(cmd, "type", [])
        self.assertEqual(v.action, "steer")
        self.assertIn("REDO BLOCKED", v.note)
        # Third time, after doing something else in between: the operator
        # insisted, so let it through — a page can genuinely regress. (Back to
        # back with no intervening command the consecutive-repeat guard stops it
        # first, which is also correct; these two guards overlap on purpose.)
        m.before({"do": "click", "target": "9"}, "click", [])
        self.assertEqual(m.before(cmd, "type", []).action, "continue")

    def test_a_failed_command_is_not_recorded_as_done(self):
        m = ProgressMonitor("do the thing")
        cmd = {"do": "open", "url": "example.com"}
        m.before(cmd, "open", [])
        m.after(cmd, "open", False, "Couldn't reach it.", {})
        m.last_key = ""                       # ignore the consecutive-repeat guard
        self.assertEqual(m.before(cmd, "open", []).action, "continue")

    def test_a_noop_is_not_recorded_as_done(self):
        m = ProgressMonitor("do the thing")
        cmd = {"do": "open", "url": "example.com"}
        m.before(cmd, "open", [])
        m.after(cmd, "open", True, "already on that page", {"noop": True})
        m.last_key = ""
        self.assertEqual(m.before(cmd, "open", []).action, "continue")


class ScrollGuardTests(unittest.TestCase):
    def test_repeated_scrolling_nudges_once_then_aborts(self):
        m = ProgressMonitor("watch the video")
        actions = []
        for _ in range(8):
            v = m.before({"do": "scroll", "amount": 600}, "scroll", [])
            actions.append(v.action)
            m.last_key = ""                   # isolate from the repeat guard
        self.assertIn("steer", actions)
        self.assertEqual(actions[-1], "abort")

    def test_one_scroll_between_other_actions_is_fine(self):
        m = ProgressMonitor("watch the video")
        for _ in range(5):
            m.before({"do": "scroll", "amount": 600}, "scroll", [])
            m.before({"do": "click", "target": "1"}, "click", [])
        self.assertEqual(m.scroll_streak, 0)

    def test_a_research_task_is_redirected_rather_than_killed(self):
        m = ProgressMonitor("research and note five facts about the moon landing")
        cmd = {"do": "scroll", "amount": 600}
        m.before(cmd, "scroll", [])
        m.before(cmd, "scroll", [])
        v = m.before(cmd, "scroll", ["one fact"])
        self.assertEqual(v.action, "steer", "a research task keeps its findings")
        self.assertIn("BLOCKED", v.step_line)

    def test_a_research_task_with_enough_facts_is_told_to_finish(self):
        m = ProgressMonitor("note three facts about the moon landing")
        cmd = {"do": "scroll", "amount": 600}
        m.before(cmd, "scroll", [])
        m.before(cmd, "scroll", [])
        v = m.before(cmd, "scroll", ["a", "b", "c", "d"])
        self.assertEqual(v.action, "steer")
        self.assertIn("done", v.note)


class ResolvedIdentityTests(unittest.TestCase):
    def test_the_same_control_under_changing_numbers_aborts(self):
        m = ProgressMonitor("find the video")
        actions = []
        for n in range(3, 9):
            cmd = {"do": "click", "target": str(n)}
            m.before(cmd, "click", [])
            actions.append(m.after(cmd, "click", *clicked("Next video")).action)
            m.last_key = ""                   # the command differs each time anyway
        self.assertEqual(actions[2], "abort", "third identical landing stops it")

    def test_distinct_controls_are_left_alone(self):
        m = ProgressMonitor("watch the video")
        for i, label in enumerate(["Search", "Result", "Play", "Fullscreen"]):
            cmd = {"do": "click", "target": str(i)}
            m.before(cmd, "click", [])
            self.assertEqual(m.after(cmd, "click", *clicked(label)).action, "continue")

    def test_an_a_b_oscillation_is_caught(self):
        m = ProgressMonitor("book it")
        actions = []
        for i, label in enumerate(["BOOK NOW", "Home"] * 3):
            cmd = {"do": "click", "target": str(i)}
            m.before(cmd, "click", [])
            actions.append(m.after(cmd, "click", *clicked(label)).action)
            m.last_key = ""
        self.assertIn("abort", actions)


class FailAndRelaunchTests(unittest.TestCase):
    def test_four_failures_in_a_row_abort(self):
        m = ProgressMonitor("do the thing")
        cmd = {"do": "click", "target": "1"}
        actions = [m.after(cmd, "click", False, "nope", {}).action for _ in range(4)]
        self.assertEqual(actions[-1], "abort")

    def test_a_success_resets_the_fail_streak(self):
        m = ProgressMonitor("do the thing")
        cmd = {"do": "click", "target": "1"}
        for _ in range(3):
            m.after(cmd, "click", False, "nope", {})
        m.after({"do": "click", "target": "2"}, "click", *clicked("OK"))
        self.assertEqual(m.fail_streak, 0)

    def test_relaunching_an_already_open_app_stops_after_a_few_steers(self):
        m = ProgressMonitor("open notepad")
        cmd = {"do": "launch", "app": "Notepad"}
        extras = {"launched": True, "window": "Notepad", "already_open": True}
        actions = [m.after(cmd, "launch", True, "already open", dict(extras))
                   for _ in range(3)]
        self.assertEqual(actions[-1].action, "abort")
        self.assertIn("re-opening it", actions[-1].reason)

    def test_a_launch_clears_the_counters_so_real_work_can_start(self):
        m = ProgressMonitor("open notepad and type")
        cmd = {"do": "click", "target": "1"}
        for n in range(2):
            m.before({"do": "click", "target": str(n)}, "click", [])
            m.after(cmd, "click", *clicked("Same"))
        m.after({"do": "launch", "app": "Notepad"}, "launch", True, "Opened Notepad.",
                {"launched": True, "window": "Notepad"})
        self.assertEqual(m.rsig_repeats, 0)
        self.assertEqual(m.sig_history, [])
        self.assertEqual(m.repeats, 0)

    def test_a_reset_keeps_the_done_keys_unless_asked_for_a_full_one(self):
        m = ProgressMonitor("do the thing")
        cmd = {"do": "open", "url": "example.com"}
        m.before(cmd, "open", [])
        m.after(cmd, "open", True, "Opened it.", {})
        m.reset()
        self.assertTrue(m.done_keys, "a surface-local reset keeps what's finished")
        m.reset(full=True)
        self.assertFalse(m.done_keys, "a handoff starts over completely")


class ProgressingTests(unittest.TestCase):
    def test_a_healthy_task_is_progressing(self):
        self.assertTrue(ProgressMonitor("do the thing").progressing())

    def test_a_failing_task_is_not(self):
        m = ProgressMonitor("do the thing")
        m.after({"do": "click", "target": "1"}, "click", False, "nope", {})
        self.assertFalse(m.progressing())

    def test_a_stuck_screen_is_not(self):
        m = ProgressMonitor("do the thing")
        m.observed("A")
        for _ in range(2):
            m.after({"do": "click", "target": "1"}, "click", *clicked("X"))
            m.observed("A")
        self.assertFalse(m.progressing())


if __name__ == "__main__":
    unittest.main()
