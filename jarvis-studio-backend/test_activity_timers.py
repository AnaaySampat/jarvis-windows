import copy
import unittest
from unittest.mock import patch

import activity_timers
import reminders


class ActivityTimerTests(unittest.TestCase):
    def setUp(self):
        self.items = []
        self.load = patch.object(reminders, "_all", side_effect=lambda: copy.deepcopy(self.items))
        self.save = patch.object(reminders, "_save", side_effect=self.persist)
        self.load.start()
        self.writer = self.save.start()
        self.clock = patch("time.time", return_value=1000)
        self.clock.start()
        self.addCleanup(self.load.stop)
        self.addCleanup(self.save.stop)
        self.addCleanup(self.clock.stop)

    def persist(self, items):
        self.items = copy.deepcopy(items)
        return True

    def start(self, token="start_one", seconds=60, label="Step 2: Simmer"):
        return activity_timers.perform("timer.start", dict(token=token, seconds=seconds, label=label))

    def test_start_is_persisted_and_idempotent_across_request_ids(self):
        first = self.start()
        second = self.start()
        self.assertTrue(first["ok"])
        self.assertEqual(len(self.items), 1)
        self.assertEqual(first["timers"], second["timers"])
        self.assertEqual(first["timers"][0]["deadline"], 1060000)
        self.assertEqual(activity_timers.perform("timer.list", {})["timers"], first["timers"])

    def test_cancel_matches_only_one_exact_activity_id(self):
        first = self.start()
        self.start("start_two")
        self.items.append(dict(id="rem-other", at=2000, text="Step 2: Simmer", timer=False))
        response = activity_timers.perform("timer.cancel", {"timer_id": first["timers"][0]["id"]})
        self.assertTrue(response["ok"])
        self.assertEqual(len(self.items), 2)
        self.assertTrue(any(r["id"] == "rem-other" for r in self.items))
        self.assertFalse(activity_timers.perform("timer.cancel", {"timer_id": "Step 2"})["ok"])

    def test_expiry_keeps_a_receipt_and_fires_once(self):
        self.start()
        fired = reminders.due(1061)
        self.assertEqual(len(fired), 1)
        self.assertIn("1 minute timer finished for Step 2", reminders.spoken_for(fired[0]))
        self.assertEqual(reminders.due(1062), [])
        self.assertEqual(activity_timers.perform("timer.list", {})["timers"][0]["state"], "finished")
        self.assertEqual(reminders.list_text(), "You have no reminders set, sir.")

    def test_storage_failure_never_reports_a_start_or_cancel(self):
        self.writer.side_effect = lambda _items: False
        self.assertFalse(self.start()["ok"])
        self.assertEqual(self.items, [])
        self.writer.side_effect = self.persist
        timer_id = self.start()["timers"][0]["id"]
        self.writer.side_effect = lambda _items: False
        self.assertFalse(activity_timers.perform("timer.cancel", {"timer_id": timer_id})["ok"])
        self.assertEqual(len(self.items), 1)

    def test_invalid_duration_and_tokens_are_rejected(self):
        for value in [True, "60", None, 4, 86401, float("inf"), float("nan"), 5.5]:
            self.assertFalse(self.start(seconds=value)["ok"])
        for token in ["", "../timer", "a" * 101]:
            self.assertFalse(self.start(token=token)["ok"])
        self.assertEqual(self.items, [])
