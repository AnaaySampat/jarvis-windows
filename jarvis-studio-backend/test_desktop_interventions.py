"""Control changes invalidate in-flight decisions before they reach Windows."""

import unittest
from unittest import mock

import autopilot as ap
from test_autopilot_loop import FakeSurface, run_loop


class InterventionTests(unittest.TestCase):
    def test_correction_during_model_call_discards_the_old_action(self):
        pending = []

        def decided(number):
            if number == 1:
                pending.append("Type the corrected text instead.")

        result, surfaces = run_loop(
            self, kind="computer", goal="type the requested text",
            replies=['{"do":"type","text":"wrong"}',
                     '{"do":"type","text":"corrected"}',
                     '{"do":"done","summary":"Typed the corrected text."}'],
            on_decision=decided, corrections=lambda: pending.pop(0) if pending else "")
        self.assertTrue(result["ok"], result)
        self.assertEqual([c["text"] for c in surfaces["computer"].commands], ["corrected"])
        self.assertTrue(any("correction:" in line for line in result["steps"]))

    def test_correction_during_a_chain_stops_the_remaining_commands(self):
        pending = []
        surface = FakeSurface()
        execute = surface.execute

        def run(command):
            outcome = execute(command)
            if command.get("text") == "first":
                pending.append("Stop that sequence; type corrected instead.")
            return outcome

        surface.execute = run
        result, _ = run_loop(
            self, kind="computer", goal="type the requested sequence", computer=surface,
            replies=['[{"do":"type","text":"first"},{"do":"type","text":"stale"}]',
                     '{"do":"type","text":"corrected"}',
                     '{"do":"done","summary":"Typed the corrected sequence."}'],
            corrections=lambda: pending.pop(0) if pending else "")
        self.assertTrue(result["ok"], result)
        self.assertEqual([c["text"] for c in surface.commands], ["first", "corrected"])

    def test_stop_during_uia_settle_snapshot_prevents_a_queued_click(self):
        state = {"stopped": False}

        def snapshot():
            state["stopped"] = True
            return "snapshot"

        result, surfaces = run_loop(
            self, kind="computer", goal="click the requested control",
            replies=['{"do":"click","target":"1"}'],
            interrupted=lambda: state["stopped"], focus_signature=snapshot)
        self.assertTrue(result.get("stopped"), result)
        self.assertEqual(surfaces["computer"].commands, [])

    def test_focus_change_during_uia_snapshot_requires_a_fresh_decision(self):
        state = {"hwnd": 1}

        def snapshot():
            state["hwnd"] = 2
            return "snapshot"

        result, surfaces = run_loop(
            self, kind="computer", goal="click the requested control",
            replies=['{"do":"click","target":"1"}', '{"do":"click","target":"2"}',
                     '{"do":"done","summary":"Clicked the requested control."}'],
            foreground=lambda: dict(state), focus_signature=snapshot)
        self.assertTrue(result["ok"], result)
        self.assertEqual([c["target"] for c in surfaces["computer"].commands], ["2"])

    def test_fast_launch_waits_for_resume(self):
        state = {"paused": True}
        events = []
        surface = FakeSurface()
        original = surface.execute

        def execute(command):
            self.assertFalse(state["paused"], "No fast or leading launch may run while paused")
            return original(command)

        async def progress(line):
            events.append(line)
            if "paused" in line:
                state["paused"] = False

        surface.execute = execute
        result, _ = run_loop(
            self, kind="computer", goal="open Notepad", computer=surface,
            paused=lambda: state["paused"], on_step=progress,
            replies=['{"do":"launch","app":"Notepad"}',
                     '{"do":"done","summary":"Opened Notepad."}'])
        self.assertTrue(result["ok"], result)
        self.assertTrue(any("paused" in event for event in events))


class LaunchTruthTests(unittest.TestCase):
    def test_a_document_named_jarvis_is_not_confused_with_the_assistants_window(self):
        focus = mock.Mock(return_value=(True, "Focused the document"))
        for title in ("JARVIS QA 42 - Notepad", "JARVIS"):
            with self.subTest(title=title), \
                 mock.patch.object(ap.computer, "foreground_app", return_value={"hwnd": 1, "title": "Other"}), \
                 mock.patch.object(ap.computer, "is_foreground", return_value=False), \
                 mock.patch.object(ap.computer, "focus_window", focus):
                focus.reset_mock()
                ok, _, _ = ap._exec_desktop({"do": "focus", "target": title})
                self.assertTrue(ok)
                focus.assert_called_once_with(title)

    def test_an_existing_window_that_cannot_be_focused_is_not_reported_ready(self):
        with mock.patch.object(ap.computer, "app_windows", return_value=["Untitled - Notepad"]), \
             mock.patch.object(ap.computer, "focus_window", return_value=(False, "Windows denied focus")):
            ok, message, extras = ap._exec_desktop({"do": "launch", "app": "notepad"})
        self.assertFalse(ok)
        self.assertIn("denied", message)
        self.assertTrue(extras.get("noop"))

    def test_multiple_documents_are_not_silently_reduced_to_the_first_title(self):
        focus = mock.Mock(return_value=(False, "Choose a Notepad window"))
        with mock.patch.object(ap.computer, "app_windows", return_value=["First - Notepad", "Second - Notepad"]), \
             mock.patch.object(ap.computer, "focus_window", focus):
            ok, _, _ = ap._exec_desktop({"do": "launch", "app": "notepad"})
        self.assertFalse(ok)
        focus.assert_called_once_with("notepad")


if __name__ == "__main__":
    unittest.main()
