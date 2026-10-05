"""Repetition guards key on what an action LANDED on, not what it asked for.

The regression this locks down was observed in a real run: goal "scroll down",
nine consecutive successful clicks on "Next video", none of the guards firing.
Element numbers are re-stamped on every observation, so the model asked for a
different number each step and every number-keyed guard saw nine distinct
commands. _resolved_sig reads the executor's own "Clicked “…”." message instead.
"""

import unittest

import autopilot as ap


class ResolvedSigTests(unittest.TestCase):
    def test_same_control_under_different_numbers_has_one_signature(self):
        # What actually happened: click 7, click 12, click 4 — all "Next video".
        sigs = {
            ap._resolved_sig({"do": "click", "target": n}, "click",
                             "Clicked “Next video”.")
            for n in ("7", "12", "4")
        }
        self.assertEqual(len(sigs), 1, "number changes must not change identity")
        self.assertEqual(sigs.pop(), "click|next video")

    def test_different_controls_keep_different_signatures(self):
        a = ap._resolved_sig({"do": "click", "target": "3"}, "click",
                             "Clicked “Next video”.")
        b = ap._resolved_sig({"do": "click", "target": "3"}, "click",
                             "Clicked “Subscribe”.")
        self.assertNotEqual(a, b)

    def test_label_whitespace_is_normalized(self):
        self.assertEqual(
            ap._resolved_sig({"do": "click", "target": "1"}, "click",
                             "Clicked “Next  \n video”."),
            "click|next video")

    def test_falls_back_to_the_command_when_nothing_was_named(self):
        # A command the executor doesn't report a label for still gets a usable
        # signature — the guards must never go blind just because parsing missed.
        self.assertEqual(
            ap._resolved_sig({"do": "type", "text": "hello"}, "type", "Typed “hello”."),
            "type|hello")

    def test_coordinate_clicks_are_keyed_by_position(self):
        self.assertEqual(
            ap._resolved_sig({"do": "click_xy", "x": 500, "y": 240}, "click_xy",
                             "Clicked at (500,240) on screen — verify it landed."),
            "click_xy|500,240")


class SameTargetStreakTests(unittest.TestCase):
    """The loop aborts on the 3rd identical landing (_SAME_TARGET_ABORT)."""

    @staticmethod
    def _streak(labels):
        """Replay the loop's counter over a sequence of resolved labels.
        Returns the step index that would abort, or None."""
        last, repeats = "", 0
        for i, label in enumerate(labels):
            sig = ap._resolved_sig({"do": "click", "target": str(i)}, "click",
                                   f"Clicked “{label}”.")
            repeats = repeats + 1 if sig == last else 0
            last = sig
            if repeats >= ap._SAME_TARGET_ABORT - 1:
                return i
        return None

    def test_the_observed_nine_click_run_aborts_early(self):
        self.assertEqual(self._streak(["Next video"] * 9), 2)

    def test_two_identical_hits_are_allowed(self):
        self.assertIsNone(self._streak(["Send", "Send"]))

    def test_alternating_targets_do_not_trip_the_streak(self):
        self.assertIsNone(self._streak(["A", "B", "A", "B", "A"]))


class ResolvedCycleTests(unittest.TestCase):
    """…but an A↔B oscillation is caught by the resolved cycle detector."""

    def test_alternating_targets_are_caught_as_a_cycle(self):
        history = []
        tripped = None
        for i, label in enumerate(["A", "B", "A", "B", "A", "B"]):
            sig = ap._resolved_sig({"do": "click", "target": str(i)}, "click",
                                   f"Clicked “{label}”.")
            if ap._cycle_detected(history, sig):
                tripped = i
                break
        self.assertIsNotNone(tripped, "A-B-A-B must be detected")

    def test_genuine_progress_is_not_a_cycle(self):
        history = []
        for i, label in enumerate(["Search", "First result", "Play", "Fullscreen"]):
            sig = ap._resolved_sig({"do": "click", "target": str(i)}, "click",
                                   f"Clicked “{label}”.")
            self.assertFalse(ap._cycle_detected(history, sig), label)


class StopAndSafetyTests(unittest.TestCase):
    def test_stop_during_a_model_call_returns_the_sentinel(self):
        """CancelledError is a BaseException; it used to escape run_task."""
        import asyncio

        async def slow():
            await asyncio.sleep(10)

        out = asyncio.run(ap._await_or_stop(slow(), lambda: True))
        self.assertIs(out, ap._STOPPED)

    def test_hotkey_blocklist_sees_aliases_and_order(self):
        for combo in ("win+r", "windows+r", "cmd+r", "r+win", "Meta + L",
                      "ctrl+alt+del", "ctrl+shift+esc", "win+x"):
            self.assertIn(ap._hotkey(combo), ap._BLOCKED_HOTKEYS, combo)
        self.assertNotIn(ap._hotkey("ctrl+s"), ap._BLOCKED_HOTKEYS)

    def test_incomplete_gate_matches_words_not_substrings(self):
        for real in ("Mumbai should be sunny tomorrow.",
                     "Your order is ready and waiting for pickup.",
                     "You will wait about 20 minutes at the counter."):
            self.assertFalse(ap._summary_looks_incomplete(real), real)
        for narration in ("I should click the button again.", "The page is blank."):
            self.assertTrue(ap._summary_looks_incomplete(narration), narration)


if __name__ == "__main__":
    unittest.main()
