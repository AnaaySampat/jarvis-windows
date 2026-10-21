"""The home screen's look: built-in styles by voice, the look settings, saved and
imported presets, JARVIS designing a theme, and dragged card arrangements."""

import unittest
from unittest import mock

from actions import _screen_patch
from llm import groq_bridge as gb


class ScreenStyleTest(unittest.TestCase):
    def test_ironman_words(self):
        for said in ("iron man", "the ironman one", "arc reactor", "classic hud"):
            self.assertEqual(_screen_patch("set_style", {"value": said})[0], {"style": "ironman"}, said)

    def test_modern_words(self):
        for said in ("modern", "the orb one", "back to normal"):
            self.assertEqual(_screen_patch("set_style", {"value": said})[0], {"style": "modern"}, said)

    def test_unknown_asks(self):
        patch, msg = _screen_patch("set_style", {"value": "banana"})
        self.assertIsNone(patch)
        self.assertIn("Iron Man", msg)


class ScreenLookTest(unittest.TestCase):
    def setUp(self):
        saved = mock.patch.object(gb, "_save_config")
        saved.start()
        self.addCleanup(saved.stop)
        self._old = gb._config.get("screen")
        gb._config["screen"] = {}
        self.addCleanup(lambda: gb._config.__setitem__("screen", self._old))

    def test_style_applies_the_builtin_preset_and_keeps_the_colour(self):
        gb.set_screen({"accent": "#ff0000"})
        scr = gb.set_screen({"style": "ironman"})
        self.assertEqual((scr["cards"], scr["core"], scr["gauges"], scr["accent"]),
                         ("hud", "reactor", "rings", "#ff0000"))
        self.assertEqual(gb.set_screen({"style": "modern"})["core"], "orb")

    def test_an_old_ironman_config_upgrades(self):
        gb._config["screen"] = {"style": "ironman", "background": "minimal"}
        scr = gb.get_screen()
        self.assertEqual((scr["font"], scr["corners"], scr["background"]), ("hud", 3, "minimal"))
        self.assertNotIn("style", scr)

    def test_bad_values_never_reach_the_css(self):
        scr = gb.set_screen({"accent": "red;}body{x", "font": "comic", "corners": 999, "glow": "nan",
                             "background": "stars", "image": 'data:image/png;base64,AA"),url(x'})
        self.assertEqual((scr["accent"], scr["font"], scr["corners"], scr["glow"], scr["background"], scr["image"]),
                         ("#00e5ff", "clean", 24, 40, "grid", ""))

    def test_one_colour_gets_a_lighter_second(self):
        scr = gb.set_screen({"accent": "#00FF00"})
        self.assertEqual((scr["accent"], scr["rgb"]), ("#00ff00", [0, 255, 0]))
        self.assertEqual(scr["accent2"], "#6bff6b")

    def test_save_apply_delete_a_preset(self):
        gb.set_screen({"accent": "#112233", "corners": 0, "image": "data:image/png;base64,iVBORw0KGgo="})
        gb.set_screen({"save_preset": "  Night   ops "})
        gb.set_screen({"style": "modern", "accent": "#00e5ff", "image": ""})
        scr = gb.set_screen({"preset": "night ops"})
        self.assertEqual((scr["accent"], scr["corners"], scr["image"][:10]), ("#112233", 0, "data:image"))
        self.assertEqual([p["name"] for p in scr["presets"]], ["Night ops"])
        self.assertEqual(gb.set_screen({"reset": True})["presets"][0]["name"], "Night ops")
        self.assertEqual(gb.set_screen({"delete_preset": "NIGHT OPS"})["presets"], [])

    def test_import_saves_and_applies_only_the_look(self):
        scr = gb.set_screen({"import_preset": {"name": "Sonar", "accent": "#00ff88", "cards": "hud",
                                               "panels": {"weather": False}, "evil": 1}})
        self.assertEqual((scr["accent"], scr["cards"], scr["panels"]["weather"]), ("#00ff88", "hud", True))
        self.assertNotIn("evil", scr["presets"][0])

    def test_a_dragged_order_keeps_every_card_once(self):
        scr = gb.set_screen({"order": {"left": ["weather", "weather", "bogus", "system"], "right": ["power"]}})
        self.assertEqual(scr["order"]["left"], ["weather", "system", "agenda"])
        self.assertEqual(sorted(scr["order"]["left"] + scr["order"]["right"]),
                         sorted(gb.DEFAULT_SCREEN["panels"]))

    def test_jarvis_designs_and_saves_a_theme(self):
        patch, msg = _screen_patch("design", {"accent": "#ff2200", "accent2": "#ffaa00", "font": "hud",
                                              "corners": "0", "glow": 90, "cards": "hud", "core": "reactor",
                                              "gauges": "rings", "background": "minimal", "save_as": "Mark 42"})
        self.assertEqual(patch, {"accent": "#ff2200", "accent2": "#ffaa00", "font": "hud", "corners": 0,
                                 "glow": 90, "cards": "hud", "core": "reactor", "gauges": "rings",
                                 "background": "minimal", "save_preset": "Mark 42"})
        self.assertIn("Mark 42", msg)
        gb.set_screen(patch)
        self.assertEqual(_screen_patch("apply_preset", {"value": "mark 42"})[0], {"preset": "mark 42"})
        self.assertIsNone(_screen_patch("apply_preset", {"value": "nope"})[0])


if __name__ == "__main__":
    unittest.main()
