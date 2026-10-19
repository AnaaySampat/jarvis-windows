"""One memory across phone and PC (memory_sync.py): facts and named routines merge
newest-wins, deletions travel as tombstones, and a timed routine fires only on the
device it belongs to."""
import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import memory_store
import memory_sync as ms
import routines


class SyncTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = mock.patch.object(memory_store, "_mem_dir", lambda: Path(d.name))
        p.start()
        self.addCleanup(p.stop)
        memory_store._facts_cache = None
        self.addCleanup(setattr, memory_store, "_facts_cache", None)

    def texts(self):
        return [f["text"] for f in memory_store.all_facts()]

    def test_a_first_sync_merges_both_ways_without_duplicates(self):
        memory_store.add_fact("My sister is Priya")
        reply, changed = ms.handle({"since": 0, "facts": [
            {"key": "my sister is priya.", "text": "My sister is Priya.", "ts": 5},
            {"key": "i like dosa", "text": "I like dosa", "ts": 6}]})
        self.assertTrue(changed)
        self.assertEqual(self.texts(), ["My sister is Priya", "I like dosa"])
        self.assertEqual({f["text"] for f in reply["facts"]}, {"My sister is Priya", "I like dosa"})
        self.assertGreater(reply["now"], 0)

    def test_deleting_on_one_device_deletes_on_the_other_and_stays_deleted(self):
        memory_store.add_fact("I drive a Honda")
        learned = ms.changes(0)["facts"][0]["ts"]
        ms.apply({"facts": [{"key": "i drive a honda", "deleted": True, "ts": learned + 10}]})
        self.assertEqual(self.texts(), [])
        # The phone, not yet synced, still offers the older copy: it stays out.
        ms.apply({"facts": [{"key": "i drive a honda", "text": "I drive a Honda", "ts": learned}]})
        self.assertEqual(self.texts(), [])
        # Forgetting here leaves a tombstone the phone is sent.
        memory_store.add_fact("I have a dog")
        memory_store.forget_fact("dog")
        gone = [f for f in ms.changes(0)["facts"] if f.get("deleted")]
        self.assertIn("i have a dog", [f["key"] for f in gone])
        # …and saying it again brings it back.
        memory_store.add_fact("I have a dog")
        self.assertNotIn("i have a dog", [f["key"] for f in ms.changes(0)["facts"] if f.get("deleted")])

    def test_only_changes_after_the_cursor_are_sent(self):
        memory_store.add_fact("old fact", at=1000.0)
        cursor = ms.now_ms()
        memory_store.add_fact("new fact")
        self.assertEqual([f["text"] for f in ms.changes(cursor)["facts"]], ["new fact"])

    def test_routines_newest_wins_and_keep_their_home_device(self):
        routines.save_routine("Morning brief", "weather and agenda", "07:00", "weekdays")
        mine = ms.changes(0)["routines"][0]
        self.assertEqual((mine["origin"], mine["days"]), ("pc", "mon,tue,wed,thu,fri"))
        # An older edit from the phone loses; a newer one wins and keeps the PC as home.
        ms.apply({"routines": [{"key": "morning brief", "name": "Morning brief", "prompt": "old",
                                "time": "07:00", "days": "daily", "origin": "phone", "ts": 5}]})
        self.assertEqual(routines.all_routines()[0]["prompt"], "weather and agenda")
        ms.apply({"routines": [{"key": "morning brief", "name": "Morning brief", "prompt": "new",
                                "time": "07:30", "days": "mon,wed", "origin": "pc",
                                "ts": mine["ts"] + 1}]})
        r = routines.all_routines()[0]
        self.assertEqual((r["prompt"], r["time"], r["days"], r["origin"]),
                         ("new", "07:30", ["mon", "wed"], "pc"))

    def test_a_phone_routine_is_listed_here_but_fired_by_the_phone(self):
        ms.apply({"routines": [{"key": "wake up", "name": "Wake up", "prompt": "news",
                                "time": "08:00", "days": "daily", "origin": "phone", "ts": 9}]})
        self.assertEqual(routines.items()[0]["when"], "Every day at 08:00 (on your phone)")
        self.assertEqual(routines.due(dt.datetime(2026, 10, 19, 8, 0)), [])
        ms.apply({"routines": [{"key": "wake up", "deleted": True, "ts": 10}]})
        self.assertEqual(routines.all_routines(), [])

    def test_a_home_routine_belongs_to_the_phone_and_round_trips(self):
        ok, msg = routines.save_routine("Welcome", "turn on the TV", "when I get home")
        self.assertTrue(ok, msg)
        r = routines.all_routines()[0]
        self.assertEqual((r["time"], r["trigger"], r["origin"]), ("", "home", "phone"))
        self.assertEqual(routines.when(r), "When you get home (on your phone)")
        sent = ms.changes(0)["routines"][0]
        self.assertEqual((sent["trigger"], sent["origin"]), ("home", "phone"))
        ms.apply({"routines": [{**sent, "prompt": "TV and lights", "ts": sent["ts"] + 1}]})
        self.assertEqual(routines.all_routines()[0].get("trigger"), "home")

    def test_bad_entries_are_ignored(self):
        self.assertFalse(ms.apply({"facts": ["x", {"text": "", "ts": 5}, {"key": "a", "ts": "zz"}],
                                   "routines": [{"name": "x"}]}))


if __name__ == "__main__":
    unittest.main()
