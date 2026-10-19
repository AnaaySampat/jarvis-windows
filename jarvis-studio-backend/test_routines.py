"""Routines run by name, at a time, or both (routines.py) — ported from the phone."""
import datetime as dt
import unittest
from unittest import mock

import routines
from actions import run_specs
from llm import live_tools as lt


class RoutineTests(unittest.TestCase):
    def setUp(self):
        self.store = {}
        for p in (mock.patch.object(routines.memory_store, "read_json",
                                    lambda name, default: self.store.get(name, default)),
                  mock.patch.object(routines.memory_store, "write_json",
                                    lambda name, data: self.store.__setitem__(name, data) or True)):
            p.start()
            self.addCleanup(p.stop)

    def test_a_named_routine_needs_no_time_and_names_are_unique(self):
        ok, msg = routines.add_routine("", "open Netflix on the TV, volume 30", name="Movie night")
        self.assertTrue(ok, msg)
        self.assertIn("Say its name", msg)
        ok, msg = routines.add_routine("", "something else", name="movie NIGHT")
        self.assertFalse(ok)
        ok, msg = routines.add_routine("", "no name, no time")
        self.assertFalse(ok)
        self.assertEqual(routines.items()[0]["when"], "When you ask")

    def test_an_untimed_routine_never_fires_by_itself(self):
        routines.add_routine("", "steps", name="movie night")
        routines.add_routine("08:00", "give me a morning brief", "weekdays", name="brief")
        monday_8 = dt.datetime(2026, 10, 19, 8, 0)
        self.assertEqual([r["name"] for r in routines.due(monday_8)], ["brief"])
        self.assertEqual(routines.items()[1]["when"], "Weekdays at 08:00")

    def test_edit_keeps_its_place_and_frees_the_old_name(self):
        routines.add_routine("", "a", name="first")
        routines.add_routine("", "b", name="second")
        rid = routines.items()[0]["id"]
        ok, _ = routines.save_routine("renamed", "a2", "", "daily", rid)
        self.assertTrue(ok)
        self.assertEqual([i["name"] for i in routines.items()], ["renamed", "second"])
        self.assertTrue(routines.add_routine("", "c", name="first")[0])

    def test_running_by_name_hands_the_steps_to_the_model(self):
        routines.add_routine("", "turn the HUD theme red and open Spotify", name="focus")
        lt.begin_turn("focus")
        [res] = run_specs([lt.to_spec("manage_routine", {"action": "run", "name": "focus"})])
        self.assertTrue(res["ok"])
        self.assertIn("Run my routine “focus”", res["feed_to_model"]["content"])
        self.assertIn("open Spotify", res["feed_to_model"]["content"])
        # The steps' own tools are loaded for the follow-up step.
        self.assertIn("customize_screen", {d["name"] for d in lt.turn_declarations()})
        [res] = run_specs([lt.to_spec("manage_routine", {"action": "run", "name": "nope"})])
        self.assertFalse(res["ok"])

    def test_saying_a_name_is_noticed_and_old_timed_routines_run_as_before(self):
        routines.add_routine("", "steps", name="movie night")
        self.assertTrue(routines.mentioned("Movie night, please"))
        self.assertFalse(routines.mentioned("what's a good movie tonight"))
        self.assertIn("“movie night”", routines.prompt_hint())
        self.store["routines.json"].append({"id": "r1", "time": "07:00", "prompt": "brief me",
                                            "days": "daily", "enabled": True, "last": ""})
        legacy, _ = routines.find("r1")
        self.assertEqual(routines.run_turn(legacy), "brief me")


if __name__ == "__main__":
    unittest.main()
