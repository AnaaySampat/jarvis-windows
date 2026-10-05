"""Capability map stays small and still names the real powers."""
import unittest

from llm import groq_bridge as gb
import autopilot as ap


_MUST_KNOW = (
    "web_search", "browser_task", "computer_task", "see_screen",
    "generate_image", "weather", "playbook", "remember",
)


class PromptDietTests(unittest.TestCase):
    def test_index_names_the_palette(self):
        for word in _MUST_KNOW:
            self.assertIn(word, gb.CAPABILITY_INDEX)

    def test_cores_stay_small(self):
        self.assertLess(len(gb.SYSTEM_CORE), 2_400)
        self.assertLess(len(gb.SYSTEM_CORE_TOOLS), 2_200)
        self.assertLess(len(gb.ACTION_CATALOG), 4_500)

    def test_tag_catalog_has_json_shapes(self):
        for t in ("open_app", "browser_task", "computer_task", "ui", "schedule"):
            self.assertIn(f'"type":"{t}"', gb.ACTION_CATALOG)

    def test_q_and_a_still_sees_capabilities(self):
        self.assertIn("YOU CAN", gb.SYSTEM_CORE)
        self.assertIn("YOU CAN", gb.SYSTEM_CORE_TOOLS)

    def test_operator_prompts_list_commands(self):
        self.assertIn('"do":"search"', ap._BROWSER_SYSTEM)
        self.assertIn('"do":"click_xy"', ap._DESKTOP_SYSTEM)
        self.assertIn("CAN DO", ap._BROWSER_SYSTEM)
        self.assertIn("CAN DO", ap._DESKTOP_SYSTEM)


if __name__ == "__main__":
    unittest.main()
