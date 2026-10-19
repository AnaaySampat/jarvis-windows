"""Replay tests: recorded (or live-observed) model replies played back through the REAL
pipeline — _run_pipeline, the tool groups, to_spec, the claim recheck and the dispatcher's
plumbing — with only the model's streaming and the actions' effects scripted. A change
that would have mishandled what a model actually said fails here. Fixtures live in
replay_fixtures.json; record new ones from real use with turn_recorder.py
(JARVIS_RECORD_TURNS=1). A fixture whose model calls a tool the palette no longer has
fails too: a real model would be calling something that's gone."""

import asyncio
import json
import re
import unittest
from pathlib import Path
from unittest import mock

import main
from llm import groq_bridge as gb
from llm import live_tools as lt
from llm import quota

FIXTURES = json.loads((Path(__file__).parent / "replay_fixtures.json").read_text(encoding="utf-8"))
_REAL = {"load_tools", "read_skill"}           # plumbing runs for real


class ReplayTests(unittest.IsolatedAsyncioTestCase):
    async def replay(self, f):
        script = [step.get("toolCalls") or step.get("text", "") for step in f["model"]]
        results = list(f.get("results") or [])
        palettes, specs, said, shown = [], [], [], []

        async def fake_stream_route(route, messages, max_tokens, temperature, text,
                                    use_tools, tool_sink):
            palettes.append({d["name"] for d in lt.turn_declarations()})
            step = script.pop(0) if script else "(the fixture ran out of model replies)"
            if isinstance(step, str):
                yield step, step
            else:
                tool_sink.extend({"id": f"c{len(palettes)}_{i}", **c} for i, c in enumerate(step))
            return

        async def fake_walk(text="", pinned="", task_tier="", skip=None, slow_last=False):
            yield quota.Route("gemini", "gemini-3.5-flash", 0)

        async def fake_emit(event, data=None, **kw):
            if event == "response_end" and isinstance(data, dict):
                said.append(data.get("text") or "")
                shown.extend(a.get("message", "") for a in data.get("actions") or [])
            elif event == "action" and isinstance(data, list):
                shown.extend(a.get("message", "") for a in data if isinstance(a, dict))

        real_run_specs = main.run_specs

        def spy_run_specs(batch):
            out = []
            for sp in batch:
                specs.append(sp)
                if sp.get("type") in _REAL:
                    out.append(real_run_specs([sp])[0])
                else:
                    r = results.pop(0) if results else {"ok": True, "message": "Done."}
                    out.append({"type": sp.get("type"), **r})
            return out

        lt._recent.clear()
        lt._used.clear()
        main._pipeline_lock = asyncio.Lock()
        main._interrupt = None
        main._acted_and_failed.clear()
        tv = bool(f.get("context", {}).get("tvPaired"))
        with mock.patch.object(lt, "_tv_paired", lambda: tv):
            names = {d["name"] for d in lt.function_declarations()} | {"load_tools"}
        for step in f["model"]:
            for call in step.get("toolCalls") or []:
                self.assertIn(call["name"], names, f"{call['name']} no longer exists")
        with mock.patch.object(gb, "_stream_route", fake_stream_route), \
             mock.patch.object(gb, "_walk_routes", fake_walk), \
             mock.patch.object(gb, "_key_for", lambda m: "k"), \
             mock.patch.object(gb, "tools_enabled", lambda m: True), \
             mock.patch.object(gb, "_persist_history", lambda: None), \
             mock.patch.object(lt, "_tv_paired", lambda: tv), \
             mock.patch.object(main, "emit", fake_emit), \
             mock.patch.object(main, "run_specs", spy_run_specs), \
             mock.patch.object(main, "_tts", None), \
             mock.patch.object(main, "emit_schedule", mock.AsyncMock()), \
             mock.patch.object(main, "_log_commands", mock.AsyncMock()), \
             mock.patch.object(main, "_remember_outcomes", lambda r: None):
            await main._run_pipeline(f["user"])
        return script, palettes, specs, said, shown

    async def test_fixtures(self):
        for f in FIXTURES:
            with self.subTest(f["name"]):
                left, palettes, specs, said, shown = await self.replay(f)
                want = f["expect"]
                self.assertEqual(left, [], "every scripted model reply was asked for")
                self.assertEqual(len(specs), len(want["specs"]), [s.get("type") for s in specs])
                for got, exp in zip(specs, want["specs"]):
                    self.assertEqual({k: got.get(k) for k in exp}, exp)
                if want.get("said"):                  # the model's own words
                    self.assertTrue(any(re.search(want["said"], s, re.I) for s in said), said)
                if want.get("shown"):                 # an action's result, as shown/spoken
                    self.assertTrue(any(re.search(want["shown"], s, re.I) for s in shown), shown)
                for name in want.get("offered", []):
                    self.assertIn(name, palettes[0])
                for name in want.get("notOffered", []):
                    self.assertNotIn(name, palettes[0])


class RecorderTests(unittest.TestCase):
    def test_a_recorded_turn_is_a_fixture_skeleton_and_off_means_off(self):
        import tempfile
        import turn_recorder as tr
        with tempfile.TemporaryDirectory() as d, \
             mock.patch.object(tr, "path", lambda: Path(d) / "turns.jsonl"):
            with mock.patch.dict("os.environ", {"JARVIS_RECORD_TURNS": "1"}):
                tr.begin("turn on the tv")
                tr.step([{"id": "1", "name": "tv_control", "args": {"action": "power_on"}}], "")
                tr.step([], "The TV is on.")
                tr.end()
            tr.begin("not recorded")
            tr.step([], "x")
            tr.end()
            [line] = (Path(d) / "turns.jsonl").read_text(encoding="utf-8").splitlines()
        turn = json.loads(line)
        self.assertEqual(turn["user"], "turn on the tv")
        self.assertEqual(turn["model"], [
            {"toolCalls": [{"name": "tv_control", "args": {"action": "power_on"}}]},
            {"text": "The TV is on."}])


if __name__ == "__main__":
    unittest.main()
