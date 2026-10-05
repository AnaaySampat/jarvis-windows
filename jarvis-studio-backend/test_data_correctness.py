"""Agenda, playbook and memory-write correctness — twins of bugs the Android
port found in its 2026-09-22 review (time strings sorted as text, weekday-keyed
items recurring forever, substring deletes, "noted" after a failed write)."""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_store
import playbooks
from actions import skills


class AgendaTests(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "schedule.json"
        patcher = mock.patch.object(skills, "_SCHEDULE_PATH", self.path)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_times_sort_by_clock_not_text(self):
        for t in ("13:00", "9:00", "7pm", "noon", "8:30 am", ""):
            skills.schedule({"do": "add", "day": "monday", "time": t, "task": t or "blank"})
        order = [i["time"] for i in json.loads(self.path.read_text())["monday"]]
        self.assertEqual(order, ["8:30 am", "9:00", "noon", "13:00", "7pm", ""])

    def test_items_are_dated_and_pruned_after_their_day(self):
        skills.schedule({"do": "add", "day": "today", "time": "09:00", "task": "dentist"})
        today = skills._normalise_day("today")
        data = json.loads(self.path.read_text())
        self.assertEqual(data[today][0]["date"], dt.date.today().isoformat())
        data[today][0]["date"] = (dt.date.today() - dt.timedelta(days=7)).isoformat()
        data[today].append({"time": "10:00", "task": "legacy undated"})
        self.path.write_text(json.dumps(data))
        self.assertEqual([i["task"] for i in skills._load_schedule()[today]],
                         ["legacy undated"])

    def test_weekday_date_is_the_next_occurrence(self):
        date = dt.date.fromisoformat(skills._date_for_day("friday"))
        self.assertEqual(date.weekday(), 4)
        self.assertLess((date - dt.date.today()).days, 7)

    def test_edit_by_match_does_not_rename_to_the_match_text(self):
        skills.schedule({"do": "add", "day": "monday", "time": "09:00", "task": "Gym session"})
        # The native tool sends the match text as both "task" and "match".
        ok, _ = skills.schedule({"do": "edit", "day": "monday", "task": "gym",
                                 "match": "gym", "new_time": "07:00"})
        self.assertTrue(ok)
        item = json.loads(self.path.read_text())["monday"][0]
        self.assertEqual((item["time"], item["task"]), ("07:00", "Gym session"))


class PlaybookTests(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp())
        patcher = mock.patch.object(memory_store, "_mem_dir", lambda: root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_ambiguous_forget_refuses_and_lists(self):
        playbooks.add_playbook("gym", "go")
        playbooks.add_playbook("gym playlist", "play")
        ok, msg = playbooks.remove_playbook("gy")
        self.assertFalse(ok)
        self.assertIn("gym playlist", msg)
        self.assertEqual(len(playbooks._user_list()), 2)

    def test_exact_name_wins_over_substring(self):
        playbooks.add_playbook("gym", "go")
        playbooks.add_playbook("gym playlist", "play")
        ok, _ = playbooks.remove_playbook("gym")
        self.assertTrue(ok)
        self.assertEqual([p["name"] for p in playbooks._user_list()], ["gym playlist"])

    def test_failed_write_is_not_reported_as_success(self):
        with mock.patch.object(memory_store, "_write", return_value=False):
            self.assertFalse(playbooks.add_playbook("x", "y")[0])
            self.assertFalse(memory_store.add_fact("likes tea")[0])

    def test_memory_view_forget_is_exact(self):
        # Forget from the Memory view drops exactly one item by id — never every
        # fact/playbook that merely contains the same words.
        mock.patch.object(memory_store, "_facts_cache", None).start()
        self.addCleanup(mock.patch.stopall)
        memory_store.add_fact("likes tea")
        memory_store.add_fact("likes tea with honey")
        fid = memory_store.all_facts()[0]["id"]
        self.assertTrue(memory_store.delete_fact(fid))
        self.assertFalse(memory_store.delete_fact(fid))
        self.assertEqual([f["text"] for f in memory_store.all_facts()], ["likes tea with honey"])
        memory_store.write_json(playbooks._AUTO_FILE, [
            {"id": "auto1781368437602", "name": "auto: a"}, {"id": "auto1781368437999", "name": "auto: a b"}])
        self.assertTrue(playbooks.remove_playbook("auto1781368437602")[0])
        self.assertEqual([p["name"] for p in playbooks.learned()], ["auto: a b"])


if __name__ == "__main__":
    unittest.main()
