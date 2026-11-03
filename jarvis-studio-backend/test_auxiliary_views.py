"""Lazily opened views hydrate current prompts without loading chat history."""
import asyncio
import unittest
from unittest import mock

import main
from server import websocket_server as server


class Socket:
    def __init__(self, view="main", peer="127.0.0.1"):
        self.path = "/?view=" + view
        self.remote_address = (peer, 1000)
        self.request_headers = {}
        self.send = mock.AsyncMock()


class AuxiliaryViewTests(unittest.IsolatedAsyncioTestCase):
    async def test_hidden_views_do_not_receive_unneeded_broadcasts(self):
        hud, overlay, control = Socket(), Socket("overlay"), Socket("control-overlay")
        remote = Socket("overlay", "100.64.0.2")
        with mock.patch.object(server, "_clients", {hud, overlay, control, remote}), \
             mock.patch.object(server, "_taps", []):
            await server.emit("telemetry", {"cpu": 50})
            hud.send.assert_awaited_once()
            overlay.send.assert_not_awaited()
            control.send.assert_not_awaited()
            remote.send.assert_not_awaited()
            await server.emit("permission_request", {"id": "approval"})
            overlay.send.assert_awaited_once()

    async def test_new_permission_window_replays_live_prompt_and_status(self):
        prompt = {"id": "approval", "kind": "computer", "description": "Open an application"}
        future = asyncio.get_running_loop().create_future()
        sent = mock.AsyncMock()
        with mock.patch.object(main, "send_to", sent), \
             mock.patch.object(main, "_pending_perms", {"approval": future}), \
             mock.patch.object(main, "_pending_permission_prompts", {"approval": prompt}), \
             mock.patch.object(main, "_active_agent_tasks", {}), \
             mock.patch.object(main, "is_current_sender_remote", return_value=False), \
             mock.patch.object(main.activity.tracker, "status", "thinking"), \
             mock.patch.object(main, "get_sysinfo", side_effect=AssertionError("Full config not needed")), \
             mock.patch.object(main, "_history_for_display", side_effect=AssertionError("Chat not needed")), \
             mock.patch.object(main, "_track_task", side_effect=AssertionError("No startup greeting")):
            client = Socket("overlay")
            await main.push_sysinfo(client)
            data = {call.args[1]: call.args[2] for call in sent.await_args_list}
            self.assertEqual(data["permission_request"], prompt)
            self.assertEqual(data["status"], "thinking")
            self.assertNotIn("history", data)
            future.set_result(False)
            sent.reset_mock()
            await main.push_sysinfo(client)
            data = {call.args[1]: call.args[2] for call in sent.await_args_list}
            self.assertIsNone(data["permission_request"])


if __name__ == "__main__":
    unittest.main()
