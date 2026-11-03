"""Reconnect restores only operators that are actually alive in this process."""

import asyncio
import contextlib
import unittest
from unittest import mock

import main


class ControlReconnectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.saved_tasks = main._active_agent_tasks
        main._active_agent_tasks = {}
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(setattr, main, "_active_agent_tasks", self.saved_tasks)
        self.events = []

        async def send(_client, event, data):
            self.events.append((event, data))

        patches = [
            mock.patch.object(main, "emit", mock.AsyncMock()),
            mock.patch.object(main, "send_to", send),
            mock.patch.object(main, "get_sysinfo", return_value={}),
            mock.patch.object(main.skills, "is_mute_toggled", return_value=False),
            mock.patch.object(main.recorder, "active_recordings", return_value={}),
            mock.patch.object(main.browser, "state", return_value={}),
            mock.patch.object(main.computer, "state", return_value={"armed": True}),
            mock.patch.object(main, "get_screen", return_value="home"),
            mock.patch.object(main, "get_overlay", return_value=False),
            mock.patch.object(main, "get_always_on", return_value=False),
            mock.patch.object(main, "_history_for_display", return_value=[]),
            mock.patch.object(main.memory_store, "list_conversations", return_value=[]),
            mock.patch.object(main, "has_llm_credentials", return_value=False),
            mock.patch.object(main, "is_current_sender_remote", return_value=False),
            mock.patch.object(main, "_workflow_recovered", []),
            mock.patch.object(main, "_setup_snapshot", None),
            mock.patch.object(main, "_control_paused", False),
        ]
        for attr in ("_last_active_app", "_last_telemetry", "_last_netinfo", "_last_weather", "_last_schedule"):
            patches.append(mock.patch.object(main, attr, None))
        for patch in patches:
            self.stack.enter_context(patch)

    def replayed(self):
        return [data for event, data in self.events if event == "agent_task"]

    async def test_reconnect_restores_live_public_fields_and_actual_pause(self):
        data = {"id": "active", "kind": "computer", "goal": "synthetic task",
                "source": "phone", "agent": "Desktop", "image": "not replayed",
                "history": ["not replayed"]}
        await main._emit_agent_task(data)
        main._control_paused = True
        await main.push_sysinfo(object())
        self.assertEqual(self.replayed(), [{key: data[key] for key in ("id", "kind", "goal", "source", "agent")}])
        control = next(data for event, data in self.events if event == "control_state")
        self.assertTrue(control["armed"])
        self.assertTrue(control["paused"])

    async def test_terminal_tasks_are_not_replayed(self):
        await main._emit_agent_task({"id": "finished", "kind": "computer", "goal": "synthetic task"})
        await main._emit_agent_task_end({"id": "finished", "ok": True})
        await main.push_sysinfo(object())
        self.assertEqual(self.replayed(), [])
        self.assertFalse(main._active_agent_tasks)

    async def test_completed_owner_without_terminal_event_is_not_replayed(self):
        async def operator():
            await main._emit_agent_task({"id": "owner-done", "kind": "computer", "goal": "synthetic task"})

        await asyncio.create_task(operator())
        await main.push_sysinfo(object())
        self.assertEqual(self.replayed(), [])
        self.assertFalse(main._active_agent_tasks)

    async def test_cancelled_owner_is_not_replayed(self):
        started = asyncio.Event()

        async def operator():
            await main._emit_agent_task({"id": "cancelled", "kind": "computer", "goal": "synthetic task"})
            started.set()
            await asyncio.Future()

        owner = asyncio.create_task(operator())
        await started.wait()
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        await main.push_sysinfo(object())
        self.assertEqual(self.replayed(), [])
        self.assertFalse(main._active_agent_tasks)

    async def test_remote_connection_does_not_receive_local_task_snapshot(self):
        await main._emit_agent_task({"id": "private", "kind": "computer", "goal": "synthetic task"})
        with mock.patch.object(main, "is_current_sender_remote", return_value=True):
            await main.push_sysinfo(object())
        self.assertEqual(self.replayed(), [])

    async def test_task_finished_during_initial_push_is_not_revived(self):
        await main._emit_agent_task({"id": "ending", "kind": "computer", "goal": "synthetic task"})

        async def send(_client, event, data):
            self.events.append((event, data))
            if event == "sysinfo":
                await main._emit_agent_task_end({"id": "ending", "ok": True})

        with mock.patch.object(main, "send_to", send):
            await main.push_sysinfo(object())
        self.assertEqual(self.replayed(), [])

    async def test_suspended_journal_is_not_replayed_as_active(self):
        with mock.patch.object(main, "_workflow_recovered", [{"id": "historical", "state": "suspended", "objective": "synthetic old task"}]):
            await main.push_sysinfo(object())
        self.assertEqual(self.replayed(), [])

    async def test_local_cancellation_at_first_progress_emit_clears_live_task(self):
        events = []

        async def emit(event, _data):
            events.append(event)
            if event == "stream_delta":
                raise asyncio.CancelledError()

        with mock.patch.object(main, "emit", emit):
            with self.assertRaises(asyncio.CancelledError):
                await main._run_autopilot_task_owned({}, "computer", "synthetic task", speak=False)
        self.assertIn("agent_task", events)
        self.assertFalse(main._active_agent_tasks)

    async def test_remote_cancellation_before_operator_body_clears_live_task(self):
        async def body(task_id, kind, goal, _device_name):
            await main._emit_agent_task({"id": task_id, "kind": kind, "goal": goal, "source": "phone"})
            raise asyncio.CancelledError()

        journal = mock.Mock()
        journal.begin.return_value = {"state": "running"}
        with mock.patch.object(main, "_get_workflow_journal", return_value=journal), \
             mock.patch.object(main, "_run_remote_task_owned", body), \
             mock.patch.object(main, "current_authenticated_device", return_value="synthetic-device"):
            with self.assertRaises(asyncio.CancelledError):
                await main._run_remote_task("remote-cancelled", "computer", "synthetic task", "synthetic")
        self.assertFalse(main._active_agent_tasks)


if __name__ == "__main__":
    unittest.main()
