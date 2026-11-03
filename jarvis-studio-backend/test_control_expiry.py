"""A clock-expired control lease clears the client indicator without new input."""

import asyncio
import unittest
from unittest import mock
import main


class ControlExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def test_telemetry_broadcasts_automatic_disarm(self):
        events = []
        states = iter([True, False])
        current = {"armed": True}
        ticks = 0

        def armed():
            current["armed"] = next(states)
            return current["armed"]

        async def emit(event, data):
            events.append((event, data))

        async def sleep(_delay):
            nonlocal ticks
            ticks += 1
            if ticks == 2:
                raise asyncio.CancelledError()

        with mock.patch.object(main.telemetry, "get_fast_stats", return_value={}), \
             mock.patch.object(main.computer, "is_armed", armed), \
             mock.patch.object(main.computer, "state", side_effect=lambda: {**current, "available": True}), \
             mock.patch.object(main, "emit", emit), \
             mock.patch.object(main, "maybe_alert", mock.AsyncMock()), \
             mock.patch.object(main, "set_live_context"), \
             mock.patch.object(main, "build_live_context", return_value="test"), \
             mock.patch.object(main, "_user_busy", True), \
             mock.patch.object(main.asyncio, "sleep", sleep):
            with self.assertRaises(asyncio.CancelledError):
                await main.telemetry_loop()

        control_events = [data for event, data in events if event == "control_state"]
        self.assertEqual(len(control_events), 1)
        self.assertFalse(control_events[0]["armed"])


if __name__ == "__main__":
    unittest.main()
