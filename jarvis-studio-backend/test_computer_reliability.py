"""Native control must verify its target and never silently retarget input."""

from contextlib import nullcontext
import itertools
import ctypes
import sys
import types
import unittest
from unittest import mock

from actions import computer as c
from test_computer_targeting import FakeControl, walk_of


class NativeControlTests(unittest.TestCase):
    def setUp(self):
        self.saved = (c._observed_hwnd, c._ui_cache, c._ui_cache_hwnd)
        c._clear_observation()
        self.addCleanup(self.restore)

    def restore(self):
        c._observed_hwnd, c._ui_cache, c._ui_cache_hwnd = self.saved

    def guard_context(self, hwnd):
        self.enterContext(mock.patch.object(c, "control_available", return_value=True))
        self.enterContext(mock.patch.object(c, "is_armed", return_value=True))
        self.enterContext(mock.patch.object(c, "foreground_app", return_value={
            "hwnd": hwnd, "title": "Document - Notepad", "app": "notepad", "on_jarvis": False}))

    def auto_context(self, focused=None):
        auto = types.SimpleNamespace(UIAutomationInitializerInThread=lambda **kw: nullcontext(),
                                     GetFocusedControl=lambda: focused)
        self.enterContext(mock.patch.dict(sys.modules, {"uiautomation": auto}))
        self.enterContext(mock.patch.object(c, "_guard", return_value=None))
        self.enterContext(mock.patch.object(c, "_foreground_hwnd", return_value=42))
        self.enterContext(mock.patch.object(c.time, "sleep"))
        return auto

    def test_keyboard_and_mouse_guard_rejects_focus_theft(self):
        self.guard_context(99)
        c._observed_hwnd = 42
        self.assertIn("focused window has changed", c._guard())
        self.assertIsNone(c._guard(check_window=False), "a new observation must remain possible")

    def test_unstable_observation_blocks_input_until_fresh_read(self):
        self.guard_context(99)
        with mock.patch.object(c, "_foreground_hwnd", return_value=99):
            self.assertFalse(c._pin_window(42))
            self.assertIn("focused window has changed", c._guard())
            self.assertTrue(c._pin_window(99))
            self.assertIsNone(c._guard())

    def test_arm_and_disarm_drop_old_targets_and_release_held_input(self):
        with mock.patch.object(c, "control_available", return_value=True), \
             mock.patch.object(c, "_emit_state"), \
             mock.patch.object(c, "release_inputs") as release:
            c._observed_hwnd, c._ui_cache_hwnd = 42, 42
            self.assertTrue(c.arm()[0])
            self.assertEqual(c._observed_hwnd, 0)
            c._observed_hwnd = 99
            self.assertTrue(c.disarm()[0])
            self.assertEqual(c._observed_hwnd, 0)
            release.assert_called_once()

    def test_tint_is_an_own_window_but_documents_mentioning_jarvis_are_not(self):
        self.assertTrue(c._is_jarvis_own_window("JARVIS Control Tint"))
        self.assertFalse(c._is_jarvis_own_window("JARVIS Control Tint notes - Notepad"))
        self.assertFalse(c._is_jarvis_own_window("JARVIS", "notepad"))
        self.assertTrue(c._is_jarvis_own_window("", "jarvis-studio-gui"))

    def test_listing_keeps_a_document_named_exactly_jarvis(self):
        wins = [types.SimpleNamespace(title="JARVIS", _hWnd=42),
                types.SimpleNamespace(title="J.A.R.V.I.S", _hWnd=99)]
        with mock.patch.dict(sys.modules, {"pygetwindow": types.SimpleNamespace(getAllWindows=lambda: wins)}), \
             mock.patch.object(c, "_process_of", side_effect=lambda hwnd: "notepad" if hwnd == 42 else "jarvis-studio-gui"):
            self.assertEqual(c.list_windows(), ["JARVIS"])

    def test_replaced_same_name_control_does_not_inherit_old_number(self):
        c._ui_cache = [("rid:1.2", "Save", "ButtonControl", 0)]
        c._ui_cache_hwnd = 42
        with mock.patch.object(c, "_foreground_hwnd", return_value=42), \
             mock.patch.object(c, "_walk_controls", walk_of(FakeControl("Save", runtime_id=[9, 9]))):
            ctrl, message = c._find_control("1")
        self.assertIsNone(ctrl)
        self.assertIn("isn't in this window", message)

    def test_duplicate_exact_and_partial_names_require_a_number(self):
        for names in (("Save", "Save"), ("Save as PDF", "Save as text")):
            with self.subTest(names=names), mock.patch.object(c, "_walk_controls", walk_of(
                    *(FakeControl(name, runtime_id=[7, i]) for i, name in enumerate(names)))):
                ctrl, message = c._find_control("save")
                self.assertIsNone(ctrl)
                self.assertIn("more than one", message)

    def test_focus_activation_failure_is_not_success(self):
        win = types.SimpleNamespace(title="Untitled - Notepad", _hWnd=101,
                                    isMinimized=False, activate=mock.Mock())
        with mock.patch.dict(sys.modules, {"pygetwindow": types.SimpleNamespace(getAllWindows=lambda: [win])}), \
             mock.patch.object(c, "control_available", return_value=True), \
             mock.patch.object(c, "is_armed", return_value=True), \
             mock.patch.object(c, "foreground_app", return_value={"hwnd": 99, "title": "Other", "app": "other"}), \
             mock.patch.object(c, "_foreground_hwnd", return_value=99), \
             mock.patch.object(c, "_process_of", return_value="notepad"), \
             mock.patch.object(c, "_force_foreground", return_value=False), \
             mock.patch.object(c.time, "sleep"):
            ok, message = c.focus_window("notepad")
        self.assertFalse(ok)
        self.assertIn("No input was sent", message)

    def test_word_never_focuses_a_wordle_browser_tab(self):
        win = types.SimpleNamespace(title="Wordle - Google Chrome", _hWnd=101)
        with mock.patch.dict(sys.modules, {"pygetwindow": types.SimpleNamespace(getAllWindows=lambda: [win])}), \
             mock.patch.object(c, "control_available", return_value=True), \
             mock.patch.object(c, "is_armed", return_value=True), \
             mock.patch.object(c, "foreground_app", return_value={"hwnd": 99, "title": "Other", "app": "other"}), \
             mock.patch.object(c, "_process_of", return_value="chrome"), \
             mock.patch.object(c, "_force_foreground") as activate:
            self.assertFalse(c.focus_window("word")[0])
        activate.assert_not_called()

    def test_multiple_app_documents_do_not_pick_first(self):
        wins = [types.SimpleNamespace(title=f"Document {n} - Notepad", _hWnd=n) for n in (1, 2)]
        with mock.patch.dict(sys.modules, {"pygetwindow": types.SimpleNamespace(getAllWindows=lambda: wins)}), \
             mock.patch.object(c, "control_available", return_value=True), \
             mock.patch.object(c, "is_armed", return_value=True), \
             mock.patch.object(c, "foreground_app", return_value={"hwnd": 99, "title": "Other", "app": "other"}), \
             mock.patch.object(c, "_process_of", return_value="notepad"), \
             mock.patch.object(c, "_force_foreground") as activate:
            ok, message = c.focus_window("notepad")
        self.assertFalse(ok)
        self.assertIn("More than one", message)
        activate.assert_not_called()

    def test_app_launch_wait_never_accepts_unrelated_new_window(self):
        times = itertools.count(0, 0.5)
        with mock.patch.object(c, "list_windows", return_value=["Unrelated chat"]), \
             mock.patch.object(c, "app_windows", return_value=[]), \
             mock.patch.object(c.time, "monotonic", side_effect=lambda: next(times)), \
             mock.patch.object(c.time, "sleep"):
            self.assertEqual(c.wait_for_window("notepad", before=[], timeout=1), (False, ""))

    def test_long_type_verification_checks_entire_text_not_prefix(self):
        text = "A" * 60 + " expected ending"
        with mock.patch.object(c, "_doc_text", return_value="A" * 60 + " WRONG ending"):
            self.assertFalse(c._value_matches(object(), text))
        with mock.patch.object(c, "_doc_text", return_value="prefix " + text + " suffix"):
            self.assertTrue(c._value_matches(object(), text))
            self.assertFalse(c._value_matches(object(), text, exact=True))

    def test_missing_named_type_target_never_falls_back_to_focus(self):
        self.auto_context(FakeControl("Other field", "EditControl", runtime_id=[1, 1]))
        with mock.patch.object(c, "_find_control", return_value=(None, "Missing field")), \
             mock.patch.object(c, "_paste_text") as paste, mock.patch.object(c, "_send_text") as send:
            self.assertEqual(c.type_text("hello", target="Missing field"), (False, "Missing field"))
        paste.assert_not_called()
        send.assert_not_called()

    def test_type_target_that_did_not_gain_focus_receives_no_input(self):
        target = FakeControl("Target", "EditControl", runtime_id=[1, 1])
        target.SetFocus = mock.Mock(side_effect=RuntimeError("focus denied"))
        self.auto_context(FakeControl("Other", "EditControl", runtime_id=[1, 2]))
        with mock.patch.object(c, "_find_control", return_value=(target, "Target")), \
             mock.patch.object(c, "_paste_text") as paste:
            ok, message = c.type_text("hello", target="Target")
        self.assertFalse(ok)
        self.assertIn("couldn't focus", message)
        paste.assert_not_called()

    def test_unverified_text_is_never_submitted_with_enter(self):
        target = FakeControl("Editor", "EditControl", runtime_id=[1, 1])
        self.auto_context(target)
        with mock.patch.object(c, "_paste_text", return_value=True), \
             mock.patch.object(c, "_await_value", return_value=False), \
             mock.patch.object(c, "_send_chord") as chord:
            ok, message = c.type_text("complete message\n")
        self.assertFalse(ok)
        self.assertIn("No Enter was sent", message)
        chord.assert_not_called()

    def test_partial_sendinput_failure_is_not_retried_with_another_backend(self):
        target = FakeControl("Editor", "EditControl", runtime_id=[1, 1])
        self.auto_context(target)
        with mock.patch.object(c, "_paste_text", return_value=False), \
             mock.patch.object(c, "_send_text", side_effect=RuntimeError("partly delivered")), \
             mock.patch.dict(sys.modules, {"pyautogui": types.SimpleNamespace(write=mock.Mock())}):
            ok, message = c.type_text("hello")
        self.assertFalse(ok)
        self.assertIn("partly delivered", message)

    def test_native_partial_delivery_raises_instead_of_safe_to_retry_false(self):
        with mock.patch.object(c, "_guard", return_value=None), \
             mock.patch.object(c, "_get_sendinput", return_value=lambda events: False):
            with self.assertRaisesRegex(RuntimeError, "partly delivered"):
                c._send_text("hello")

    def test_partial_chord_releases_modifiers_without_retrying_key_down(self):
        send = mock.Mock(side_effect=[False, True])
        with mock.patch.object(c, "_get_sendinput", return_value=send):
            self.assertFalse(c._send_chord([c._VK_CONTROL, c._VK_A]))
        self.assertEqual(len(send.call_args_list), 2)
        self.assertTrue(all(event[2] == c._KEYEVENTF_KEYUP for event in send.call_args_list[1].args[0]))

    @unittest.skipUnless(sys.platform == "win32", "Win32 HWND signatures")
    def test_window_entry_points_preserve_full_handle_width(self):
        api = c._window_api()
        self.assertEqual(ctypes.sizeof(api.GetForegroundWindow.restype), ctypes.sizeof(ctypes.c_void_p))
        self.assertEqual(ctypes.sizeof(api.SetActiveWindow.restype), ctypes.sizeof(ctypes.c_void_p))

    def test_scroll_uses_notches_in_both_directions(self):
        scroll = mock.Mock()
        with mock.patch.object(c, "_guard", return_value=None), \
             mock.patch.dict(sys.modules, {"pyautogui": types.SimpleNamespace(scroll=scroll)}):
            for amount, expected in ((600, -6), (-600, 6), (3, -3), (-3, 3), (150, -1), (-150, 1)):
                with self.subTest(amount=amount):
                    self.assertTrue(c.scroll_amount(amount)[0])
                    self.assertEqual(scroll.call_args.args, (expected,))


if __name__ == "__main__":
    unittest.main()
