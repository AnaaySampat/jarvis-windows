"""OpenClaw-style heartbeat (heartbeat.py): silent unless the checklist needs the user,
no call for an empty checklist, active hours, no repeats."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import heartbeat


class HeartbeatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        for p in (mock.patch.object(heartbeat, "path", lambda: root / "HEARTBEAT.md"),
                  mock.patch.object(heartbeat, "_settings_path", lambda: root / "hb.json")):
            p.start()
            self.addCleanup(p.stop)
        heartbeat._alerts.clear()
        heartbeat._last_run = 0.0
        self.calls = []

    def _model(self, reply):
        async def complete(system, user):
            self.calls.append(user)
            return reply
        return complete

    async def test_the_template_alone_costs_no_call(self):
        heartbeat.ensure_file()
        self.assertEqual(heartbeat.checklist(), "")
        self.assertEqual(await heartbeat.check("Time: 10:00", "", self._model("x")), "")
        self.assertEqual(self.calls, [])

    async def test_ok_is_silent_and_an_alert_is_said_once(self):
        heartbeat.path().write_text("- Tell me 10 minutes before a meeting.\n", encoding="utf-8")
        self.assertEqual(await heartbeat.check("Agenda: 10:30 standup", "", self._model(
            "HEARTBEAT_OK")), "")
        alert = "Your standup starts in ten minutes, sir."
        self.assertEqual(await heartbeat.check("x", "", self._model(alert), now=1000.0), alert)
        self.assertIn("Tell me 10 minutes before a meeting.", self.calls[-1])
        self.assertEqual(await heartbeat.check("x", "", self._model(alert), now=2000.0), "")
        self.assertIn("standup starts in ten minutes", self.calls[-1])   # under ALREADY SAID
        # Three hours on, it may be said again.
        self.assertEqual(await heartbeat.check("x", "", self._model(alert), now=1000.0 + 3 * 3600 + 1),
                         alert)

    def test_due_only_when_on_inside_hours_and_after_the_interval(self):
        at = lambda h, m=0: dt.datetime(2026, 10, 19, h, m).timestamp()  # noqa: E731
        self.assertFalse(heartbeat.due(at(12)))                          # off by default
        heartbeat.save_settings({"enabled": True, "every_min": 30, "start": "08:00", "end": "22:00"})
        self.assertTrue(heartbeat.due(at(12)))
        self.assertFalse(heartbeat.due(at(23)))
        heartbeat._last_run = at(12)
        self.assertFalse(heartbeat.due(at(12, 20)))
        self.assertTrue(heartbeat.due(at(12, 31)))
        heartbeat.save_settings({"start": "22:00", "end": "02:00"})        # past midnight
        self.assertTrue(heartbeat.due(at(23)))
        self.assertFalse(heartbeat.due(at(13)))

    def test_settings_are_clamped(self):
        s = heartbeat.save_settings({"every_min": 1, "start": "25:00"})
        self.assertEqual((s["every_min"], s["start"]), (10, "08:00"))


if __name__ == "__main__":
    unittest.main()
