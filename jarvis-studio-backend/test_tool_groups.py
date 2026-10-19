"""A chat turn advertises a small palette; the rest loads on demand (live_tools groups),
and the user's skill files are listed by name and read on demand (skill_files)."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from llm import live_tools as lt
from actions import run_specs
import skill_files


def _names(decls):
    return {d["name"] for d in decls}


class ToolGroupTests(unittest.TestCase):
    def setUp(self):
        lt._recent.clear()
        lt._used.clear()

    def test_every_grouped_tool_is_a_real_tool(self):
        declared = _names(lt.function_declarations(places_ok=True, web_ok=True))
        for group, (_desc, tools) in lt.GROUPS.items():
            for t in tools:
                self.assertIn(t, declared, f"{group} lists {t}, which isn't declared")

    def test_every_declared_tool_lowers_to_a_spec(self):
        for d in lt.function_declarations(places_ok=True, web_ok=True):
            self.assertIn(d["name"], lt._SPEC, f"{d['name']} has no spec builder")

    def test_a_plain_turn_sends_well_under_half_the_palette(self):
        lt.begin_turn("tell me a joke")
        turn = lt.turn_declarations()
        self.assertIn("load_tools", _names(turn))
        self.assertIn("open_application", _names(turn))
        self.assertNotIn("customize_screen", _names(turn))
        full = len(json.dumps(lt.function_declarations()))
        self.assertLess(len(json.dumps(turn)), full / 2)

    def test_the_request_words_preload_their_group(self):
        lt.begin_turn("how many PDFs are in my downloads folder?")
        self.assertIn("list_directory", _names(lt.turn_declarations()))
        lt.begin_turn("make the HUD red")
        self.assertIn("customize_screen", _names(lt.turn_declarations()))

    def test_load_tools_adds_a_group_for_the_rest_of_the_turn(self):
        lt.begin_turn("hmm")
        self.assertNotIn("manage_schedule", _names(lt.turn_declarations()))
        spec = lt.to_spec("load_tools", {"groups": ["planner"]})
        [res] = run_specs([spec])
        self.assertTrue(res["ok"])
        self.assertTrue(res["internal"])          # never a chat card or speech
        self.assertEqual(res["speak"], "")
        self.assertIn("manage_schedule", res["feed_to_model"]["content"])
        self.assertIn("manage_schedule", _names(lt.turn_declarations()))

    def test_load_tools_takes_a_bare_name_and_refuses_an_unknown_group(self):
        self.assertEqual(lt.to_spec("load_tools", {"groups": "files, hud"})["groups"],
                         ["files", "hud"])
        [res] = run_specs([lt.to_spec("load_tools", {"groups": ["nope"]})])
        self.assertFalse(res["ok"])
        self.assertIn("files", res["feed_to_model"]["content"])

    def test_a_group_used_stays_for_two_more_turns(self):
        lt.begin_turn("read the file notes.txt")
        lt.to_spec("read_file", {"path": "notes.txt"})
        lt.begin_turn("and the other one")
        self.assertIn("read_file", _names(lt.turn_declarations()))
        lt.begin_turn("ok")
        self.assertIn("read_file", _names(lt.turn_declarations()))
        lt.begin_turn("thanks")
        self.assertNotIn("read_file", _names(lt.turn_declarations()))

    def test_the_openai_shape_of_load_tools_is_valid_json_schema(self):
        lt.begin_turn("hi there")
        [tool] = [t for t in lt.openai_tools(lt.turn_declarations())
                  if t["function"]["name"] == "load_tools"]
        groups = tool["function"]["parameters"]["properties"]["groups"]
        self.assertEqual(groups["type"], "array")
        self.assertEqual(groups["items"]["type"], "string")

    def test_tv_use_is_remembered_for_the_tv_brief(self):
        lt.begin_turn("play stranger things on the tv")
        lt.to_spec("tv_control", {"action": "play"})
        lt.begin_turn("ok")
        self.assertTrue(lt.recently_used("tv"))
        lt.begin_turn("thanks")
        lt.begin_turn("weather?")
        self.assertFalse(lt.recently_used("tv"))


class SkillFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        patcher = mock.patch.object(skill_files, "skills_dir", lambda: self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(setattr, skill_files, "_cache", (0.0, []))
        skill_files._cache = (0.0, [])

    def _write(self, rel, text):
        p = self.dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        skill_files._cache = (0.0, [])

    def test_no_skills_means_no_tool_and_no_prompt(self):
        self.assertEqual(skill_files.prompt_block(), "")
        self.assertNotIn("read_skill", _names(lt.function_declarations()))

    def test_openclaw_and_plain_markdown_skills_load(self):
        self._write("expense-report/SKILL.md",
                    "---\nname: expense report\ndescription: File my monthly expenses\n"
                    "metadata: {\"openclaw\": {}}\n---\n1. Open the portal.\n")
        self._write("plant care.md", "# Water the ferns on Sundays\nUse 200 ml each.\n")
        self._write("notes.txt", "not a skill")
        names = {g["name"]: g for g in skill_files.catalog()}
        self.assertEqual(set(names), {"expense report", "plant care"})
        self.assertEqual(names["expense report"]["description"], "File my monthly expenses")
        self.assertEqual(names["plant care"]["description"], "Water the ferns on Sundays")
        block = skill_files.prompt_block()
        self.assertIn("- expense report: File my monthly expenses", block)
        self.assertIn("read_skill", _names(lt.function_declarations()))

    def test_read_skill_by_name_and_by_unique_part(self):
        self._write("expense-report/SKILL.md", "---\nname: expense report\n---\nStep one.\n")
        ok, text = skill_files.read("Expense Report")
        self.assertTrue(ok)
        self.assertIn("Step one.", text)
        self.assertTrue(skill_files.read("expense")[0])
        ok, text = skill_files.read("taxes")
        self.assertFalse(ok)
        self.assertIn("expense report", text)
        [res] = run_specs([lt.to_spec("read_skill", {"name": "expense report"})])
        self.assertTrue(res["ok"] and res["internal"])
        self.assertIn("Step one.", res["feed_to_model"]["content"])

    def test_a_skill_the_user_names_loads_with_the_turn(self):
        self._write("expense-report/SKILL.md", "---\nname: expense report\n---\nStep one.\n")
        self._write("tv.md", "Short-named skill.\n")
        self.assertIn("Step one.", skill_files.mentioned_block("do my expense report please"))
        self.assertEqual(skill_files.mentioned_block("turn on the tv"), "")
        self.assertEqual(skill_files.mentioned_block("what's the weather"), "")


if __name__ == "__main__":
    unittest.main()
