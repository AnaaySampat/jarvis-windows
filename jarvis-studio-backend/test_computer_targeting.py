"""Click-by-number resolves a control by STABLE identity, not by walk position.

The failure this locks down is invisible at runtime: positional (name, type,
ordinal) matching silently repoints as soon as the tree shifts, so "click 7"
lands on whatever moved into 7th place and reports a perfectly successful click
on the wrong control. Matching the RuntimeId / AutomationId recorded when the
list was taken makes that impossible.

Pure logic — the UIA walk is stubbed, so this runs anywhere.
"""

import unittest

from actions import computer as c


class FakeControl:
    """The slice of a uiautomation Control that _find_control/_selector_for use."""

    def __init__(self, name, ctype="ButtonControl", runtime_id=None, automation_id=""):
        self.Name = name
        self.ControlTypeName = ctype
        self._rid = runtime_id
        self.AutomationId = automation_id

    def GetRuntimeId(self):
        if self._rid is None:
            raise RuntimeError("this control exposes no RuntimeId")
        return self._rid


def walk_of(*controls):
    """Stand in for _walk_controls(), which yields (control, kind, name)."""
    def _walk():
        for ctrl in controls:
            yield ctrl, "button", ctrl.Name
    return _walk


class TargetingTests(unittest.TestCase):
    def setUp(self):
        self._real_walk = c._walk_controls
        self._real_fg = c._foreground_hwnd
        c._foreground_hwnd = lambda: 42            # matches the cache below
        c._ui_cache_hwnd = 42

    def tearDown(self):
        c._walk_controls = self._real_walk
        c._foreground_hwnd = self._real_fg
        c._ui_cache, c._ui_cache_hwnd = [], 0

    def test_selector_survives_controls_being_inserted_above_it(self):
        # Listed: [Save, Cancel]; the user picks 2 (Cancel).
        c._ui_cache = [
            ("rid:1.2", "Save", "ButtonControl", 0),
            ("rid:1.3", "Cancel", "ButtonControl", 0),
        ]
        # …then a toolbar appears and everything shifts down by one.
        c._walk_controls = walk_of(
            FakeControl("Reload", runtime_id=[1, 9]),
            FakeControl("Save", runtime_id=[1, 2]),
            FakeControl("Cancel", runtime_id=[1, 3]),
        )
        ctrl, label = c._find_control("2")
        self.assertIsNotNone(ctrl)
        self.assertEqual(ctrl.Name, "Cancel", "must follow identity, not position")
        self.assertIn("Cancel", label)

    def test_duplicate_names_resolve_to_the_one_that_was_listed(self):
        # Three buttons all named "Remove"; the user picked the third.
        c._ui_cache = [
            ("rid:2.1", "Remove", "ButtonControl", 0),
            ("rid:2.2", "Remove", "ButtonControl", 1),
            ("rid:2.3", "Remove", "ButtonControl", 2),
        ]
        controls = [FakeControl("Remove", runtime_id=[2, n]) for n in (1, 2, 3)]
        c._walk_controls = walk_of(*controls)
        ctrl, _ = c._find_control("3")
        self.assertIs(ctrl, controls[2])

    def test_a_vanished_control_fails_loudly_instead_of_hitting_a_neighbour(self):
        c._ui_cache = [("rid:3.1", "Delete account", "ButtonControl", 0)]
        c._walk_controls = walk_of(FakeControl("Save", runtime_id=[3, 9]))
        ctrl, msg = c._find_control("1")
        self.assertIsNone(ctrl)
        self.assertIn("isn't in this window any more", msg)

    def test_automation_id_is_used_when_there_is_no_runtime_id(self):
        c._ui_cache = [("aid:btnSend|ButtonControl", "Send", "ButtonControl", 0)]
        target = FakeControl("Send", automation_id="btnSend")
        c._walk_controls = walk_of(FakeControl("Draft", automation_id="btnDraft"), target)
        ctrl, _ = c._find_control("1")
        self.assertIs(ctrl, target)

    def test_ambiguous_automation_id_refuses_to_guess(self):
        c._ui_cache = [("aid:row|ButtonControl", "Row", "ButtonControl", 0)]
        c._walk_controls = walk_of(
            FakeControl("Row", automation_id="row"),
            FakeControl("Row", automation_id="row"),
        )
        ctrl, msg = c._find_control("1")
        self.assertIsNone(ctrl)
        self.assertIn("more than one control", msg)

    def test_falls_back_to_position_when_no_stable_id_exists(self):
        # Neither id available → the old (name, type, ordinal) behaviour.
        c._ui_cache = [
            ("", "Play", "ButtonControl", 0),
            ("", "Play", "ButtonControl", 1),
        ]
        controls = [FakeControl("Play"), FakeControl("Play")]
        c._walk_controls = walk_of(*controls)
        ctrl, _ = c._find_control("2")
        self.assertIs(ctrl, controls[1])

    def test_a_changed_foreground_window_refuses_numbered_clicks(self):
        c._ui_cache = [("rid:4.1", "OK", "ButtonControl", 0)]
        c._foreground_hwnd = lambda: 99            # user switched apps
        c._walk_controls = walk_of(FakeControl("OK", runtime_id=[4, 1]))
        ctrl, msg = c._find_control("1")
        self.assertIsNone(ctrl)
        self.assertIn("focused window has changed", msg)

    def test_matching_by_visible_name_still_works(self):
        c._ui_cache = []
        exact = FakeControl("Save")
        c._walk_controls = walk_of(FakeControl("Save as…"), exact)
        ctrl, _ = c._find_control("save")
        self.assertIs(ctrl, exact, "an exact name beats a substring match")


class AppWindowMatchTests(unittest.TestCase):
    """The owning process decides which window IS an app — a title substring made a
    'Wordle' browser tab count as Word, and the operator then typed into Chrome."""

    def test_matches(self):
        m = c.app_window_matches
        self.assertTrue(m("word", "Document1 - Word", "winword"))          # launcher alias
        self.assertTrue(m("notepad", "Untitled - Notepad", "notepad"))
        self.assertTrue(m("bluej", "BlueJ:  java", "javaw"))               # title fallback
        self.assertTrue(m("calculator", "Calculator", "applicationframehost"))  # UWP host
        self.assertTrue(m("settings", "Settings", "systemsettings"))
        self.assertTrue(m("chrome", "Wordle - Google Chrome", "chrome"))

    def test_non_matches(self):
        m = c.app_window_matches
        self.assertFalse(m("word", "Wordle - NYT - Google Chrome", "chrome"))  # a tab
        self.assertFalse(m("notepad", "online notepad - Google Chrome", "chrome"))
        self.assertFalse(m("notepad", "new 1 - Notepad++", "notepad++"))     # other app
        self.assertFalse(m("word", "WordPad", "wordpad"))
        self.assertFalse(m("notepad", "new 1 - Notepad++", ""))              # no process


class SelectorTests(unittest.TestCase):
    def test_runtime_id_is_preferred(self):
        ctrl = FakeControl("X", runtime_id=[7, 1, 2], automation_id="ignored")
        self.assertEqual(c._selector_for(ctrl), "rid:7.1.2")

    def test_automation_id_is_the_fallback(self):
        self.assertEqual(
            c._selector_for(FakeControl("X", automation_id="btnX")),
            "aid:btnX|ButtonControl")

    def test_no_stable_id_yields_an_empty_selector(self):
        self.assertEqual(c._selector_for(FakeControl("X")), "")


class ListingTrimTests(unittest.TestCase):
    """A long file list must not crowd a dialog's own buttons out of the listing."""

    def test_rows_are_capped_and_buttons_kept_in_order(self):
        rows = [(None, f"file{i}", "ListItemControl", "list item", None, 0) for i in range(50)]
        tail = [(None, "Save", "ButtonControl", "button", None, 0),
                (None, "File name:", "ComboBoxControl", "dropdown", None, 0)]
        head = [(None, "Organize", "ButtonControl", "button", None, 0)]
        kept, dropped = c._prioritize(head + rows + tail)
        names = [r[1] for r in kept]
        self.assertIn("Save", names)
        self.assertEqual(names[0], "Organize")
        self.assertEqual(sum(1 for r in kept if r[3] == "list item"), c._MAX_ROWS_SHOWN)
        self.assertEqual(dropped, 50 - c._MAX_ROWS_SHOWN)

    def test_a_short_listing_is_untouched(self):
        ctrls = [(None, "A", "ButtonControl", "button", None, 0)] * 5
        self.assertEqual(c._prioritize(ctrls), (ctrls, 0))


if __name__ == "__main__":
    unittest.main()
