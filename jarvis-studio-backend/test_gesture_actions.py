import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import gesture_actions as gestures


class GestureActionTests(unittest.TestCase):
    def test_unknown_actions_never_reach_an_executor(self):
        for action in ("approve", "shutdown", "pc.type", "shell", "media.toggle; shutdown"):
            self.assertFalse(gestures.execute(action, {})[0])

    def test_presentation_uses_the_existing_armed_control_primitive(self):
        with patch("actions.computer.press_keys", return_value=(False, "Arm control first")) as press:
            self.assertEqual(gestures.execute("pc.next", {}), (False, "Arm control first"))
            press.assert_called_once_with("pagedown")

    def test_media_vocabulary_contains_fixed_virtual_keys_only(self):
        with patch("actions.app_launcher._media_key", return_value=(True, "Sent")) as key:
            for action, expected in gestures.MEDIA_KEYS.items():
                self.assertTrue(gestures.execute(action, {})[0])
                self.assertEqual(key.call_args.args[0], expected)

    def test_timer_bounds_reject_invalid_or_unbounded_values(self):
        for seconds in (-1, 0, 4, 86401, "x", None, float("inf")):
            self.assertFalse(gestures.execute("timer.start", {"seconds": seconds})[0])

    def test_timer_uses_the_persisted_reminder_scheduler(self):
        with patch("reminders.add_reminder", return_value=(True, "Set")) as add:
            self.assertTrue(gestures.execute("timer.start", {"seconds": 300})[0])
            add.assert_called_once_with("in 300 seconds", "Gesture timer", is_timer=True)

    def test_tv_navigation_is_limited_to_the_saved_tv(self):
        with patch("actions.tv.run", return_value={"ok": True, "message": "Sent"}) as tv:
            self.assertTrue(gestures.execute("tv.previous", {"host": "attacker"})[0])
            tv.assert_called_once_with({"type": "tv", "do": "key", "target": "previous"})


class GestureTransportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        gestures._results.clear()
        self.jobs = []
        self.runtime = SimpleNamespace(
            current_client_id=lambda: "client1", is_current_sender_remote=lambda: False,
            send_to_current=AsyncMock(), emit=AsyncMock(), _remote_input_lock=asyncio.Lock(),
            _remote_control_error=Mock(return_value=""), _pipeline_lock=asyncio.Lock(),
            _tts=SimpleNamespace(speak=AsyncMock()), skills=SimpleNamespace(is_muted=lambda: False),
            _track_task=lambda coro, _name: self.jobs.append(asyncio.create_task(coro)),
        )

    async def asyncTearDown(self):
        if self.jobs:
            await asyncio.gather(*self.jobs, return_exceptions=True)

    async def submit(self, action="pc.next", request_id="gesture_test", **data):
        await gestures.submit({"request_id": request_id, "action": action, **data}, self.runtime)

    async def finish(self):
        await asyncio.gather(*self.jobs)

    async def test_remote_expired_lease_blocks_every_side_effect(self):
        self.runtime.is_current_sender_remote = lambda: True
        self.runtime._remote_control_error.return_value = "Expired lease"
        with patch.object(gestures, "execute") as execute:
            await self.submit()
            await self.finish()
            execute.assert_not_called()
            self.assertFalse(self.runtime.send_to_current.call_args.args[1]["ok"])

    async def test_remote_valid_lease_is_checked_before_execution(self):
        self.runtime.is_current_sender_remote = lambda: True
        with patch.object(gestures, "execute", return_value=(True, "Sent")) as execute:
            await self.submit(lease_id="lease", seq=12)
            await self.finish()
            self.runtime._remote_control_error.assert_called_once()
            self.assertEqual(execute.call_count, 1)
            self.assertTrue(self.runtime.send_to_current.call_args.args[1]["ok"])

    async def test_remote_cannot_request_arbitrary_speech_or_local_timers(self):
        self.runtime.is_current_sender_remote = lambda: True
        with patch.object(gestures, "execute") as execute:
            await self.submit("read_aloud", text="Not allowed")
            await self.submit("timer.start", request_id="gesture_other", seconds=10)
            execute.assert_not_called()
            self.assertEqual(len(self.jobs), 0)
            self.assertEqual(self.runtime.send_to_current.await_count, 2)

    async def test_replayed_request_never_repeats_a_toggle(self):
        with patch.object(gestures, "execute", return_value=(True, "Sent")) as execute:
            await self.submit("media.toggle")
            await self.submit("media.toggle")
            await self.finish()
            await self.submit("media.toggle")
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(self.runtime.send_to_current.await_count, 2)

    async def test_unknown_or_malformed_requests_are_not_scheduled(self):
        await self.submit("approval.response")
        await self.submit("pc.next", request_id="../bad")
        self.assertEqual(len(self.jobs), 0)

    async def test_read_aloud_runs_as_a_background_job_so_stop_can_arrive(self):
        release = asyncio.Event()
        self.runtime._tts.speak = AsyncMock(side_effect=lambda _text: release.wait())
        # Use an async side effect so the mocked TTS awaits the actual event.
        async def speak(_text):
            await release.wait()
        self.runtime._tts.speak = AsyncMock(side_effect=speak)
        await self.submit("read_aloud", text="A page")
        await asyncio.sleep(0)
        self.assertFalse(self.jobs[0].done())
        release.set()
        await self.finish()
        self.runtime._tts.speak.assert_awaited_once_with("A page")
        self.assertTrue(self.runtime.send_to_current.call_args.args[1]["ok"])

    async def test_reading_does_not_steal_an_active_pipeline(self):
        await self.runtime._pipeline_lock.acquire()
        try:
            await self.submit("read_aloud", text="A page")
            await self.finish()
            self.runtime._tts.speak.assert_not_awaited()
            self.assertFalse(self.runtime.send_to_current.call_args.args[1]["ok"])
        finally:
            self.runtime._pipeline_lock.release()
