"""MCP servers as tools (mcp_client.py): config, schemas Gemini accepts, the approval
rule, the tool group, and — when the `mcp` package is installed — a real stdio server
called end to end through the one dispatcher."""
import asyncio
import json
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import mcp_client as mc
from llm import live_tools as lt


class ConfigTests(unittest.TestCase):
    def setUp(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = mock.patch.object(mc, "CONFIG_PATH", Path(d.name) / "mcp.json")
        p.start()
        self.addCleanup(p.stop)

    def test_claude_desktops_shape_loads_and_bad_entries_are_skipped(self):
        mc.CONFIG_PATH.write_text(json.dumps({"mcpServers": {
            "files": {"command": "npx", "args": ["-y", "x"]},
            "notes": {"url": "https://example.com/mcp"},
            "bad name!": {"command": "x"},
            "empty": {}}}), encoding="utf-8")
        self.assertEqual(sorted(mc.load_config()), ["files", "notes"])

    def test_cleaning_what_settings_sends(self):
        self.assertEqual(mc.clean_server("gh", {"command": "npx", "args": ["a"], "env": {"T": "1"}}),
                         ({"command": "npx", "args": ["a"], "env": {"T": "1"}}, ""))
        cfg, _ = mc.clean_server("n", {"url": "https://x/mcp", "headers": {"A": "b"}, "trusted": True})
        self.assertEqual(cfg, {"url": "https://x/mcp", "headers": {"A": "b"}, "trusted": True})
        self.assertIsNone(mc.clean_server("n", {"url": "ftp://x"})[0])
        self.assertIsNone(mc.clean_server("has space", {"command": "x"})[0])
        self.assertIsNone(mc.clean_server("n", {})[0])

    def test_state_never_shows_secret_values(self):
        mc.save_config({"gh": {"command": "npx", "env": {"GITHUB_TOKEN": "s3cret"}}})
        st = json.dumps(mc.state())
        self.assertIn("GITHUB_TOKEN", st)
        self.assertNotIn("s3cret", st)


class SchemaTests(unittest.TestCase):
    def test_json_schema_to_what_gemini_accepts(self):
        s = mc.to_schema({"type": "object", "additionalProperties": False, "$schema": "x",
                          "properties": {
                              "q": {"type": "string", "description": "query", "default": "a"},
                              "n": {"type": ["integer", "null"]},
                              "mode": {"enum": ["a", "b"], "type": "string"},
                              "tags": {"type": "array", "items": {"type": "string"}},
                              "opt": {"anyOf": [{"type": "null"}, {"type": "number"}]},
                              "extra": {"type": "object"}},
                          "required": ["q", "missing"]})
        self.assertEqual(s["type"], "OBJECT")
        self.assertEqual(s["required"], ["q"])
        p = s["properties"]
        self.assertEqual(p["q"], {"type": "STRING", "description": "query"})
        self.assertEqual(p["n"], {"type": "INTEGER"})
        self.assertEqual(p["mode"]["enum"], ["a", "b"])
        self.assertEqual(p["tags"], {"type": "ARRAY", "items": {"type": "STRING"}})
        self.assertEqual(p["opt"], {"type": "NUMBER"})
        self.assertTrue(p["extra"]["_json"])          # free-form object → a JSON string

    def test_names_are_safe_and_short(self):
        self.assertEqual(mc.function_name("gh", "create.issue"), "mcp__gh__create_issue")
        self.assertLessEqual(len(mc.function_name("server", "x" * 100)), 64)


def fake_server(name="gh", trusted=False):
    srv = mc.Server(name, {"command": "x", **({"trusted": True} if trusted else {})})
    srv.status, srv.session = "ready", object()
    srv.tools = [
        {"name": "list_issues", "description": "List issues", "read_only": True, "json_args": [],
         "schema": {"type": "OBJECT", "properties": {"repo": {"type": "STRING"}}}},
        {"name": "create_issue", "description": "Create an issue", "read_only": False,
         "json_args": ["labels"],
         "schema": {"type": "OBJECT", "properties": {
             "title": {"type": "STRING"},
             "labels": {"type": "STRING", "_json": True}}}},
    ]
    return srv


class PaletteTests(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(mc._servers, {"gh": fake_server()}, clear=True)
        p.start()
        self.addCleanup(p.stop)
        lt._recent.clear()               # no earlier turn's tools carried over
        lt._used.clear()

    def test_tools_are_declared_without_internal_marks(self):
        decls = {d["name"]: d for d in mc.declarations()}
        self.assertIn("mcp__gh__create_issue", decls)
        self.assertNotIn("_json", json.dumps(decls))
        self.assertIn("[gh — MCP]", decls["mcp__gh__list_issues"]["description"])

    def test_a_group_loaded_by_the_servers_name_or_load_tools(self):
        lt.begin_turn("what's the weather")
        names = [d["name"] for d in lt.turn_declarations()]
        self.assertNotIn("mcp__gh__list_issues", names)
        load = next(d for d in lt.turn_declarations() if d["name"] == "load_tools")
        self.assertIn("mcp = Tools from the user's MCP servers: gh (list_issues, create_issue)",
                      load["description"])
        lt.begin_turn("open an issue on GH for the crash")
        self.assertIn("mcp__gh__create_issue", [d["name"] for d in lt.turn_declarations()])

    def test_a_call_lowers_to_one_mcp_spec(self):
        self.assertEqual(lt.to_spec("mcp__gh__list_issues", {"repo": "a/b"}),
                         {"type": "mcp", "tool": "mcp__gh__list_issues", "args": {"repo": "a/b"}})

    def test_only_changes_ask_and_trust_is_the_users(self):
        from actions import needs_permission
        self.assertEqual(needs_permission({"type": "mcp", "tool": "mcp__gh__list_issues"})[0], False)
        gated, kind, desc = needs_permission({"type": "mcp", "tool": "mcp__gh__create_issue",
                                              "args": {"title": "Crash"}})
        self.assertEqual((gated, kind), (True, "mcp"))
        self.assertIn("create_issue", desc)
        self.assertIn("Crash", desc)
        mc._servers["gh"] = fake_server(trusted=True)
        self.assertFalse(needs_permission({"type": "mcp", "tool": "mcp__gh__create_issue"})[0])

    def test_a_refusal_is_spoken_without_the_arguments(self):
        import main
        self.assertEqual(main._spoken_refusal('let the “gh” MCP server run create_issue {"title": "Crash"}'),
                         "Understood, sir — I won't let the “gh” MCP server run create_issue.")


class RunTests(unittest.TestCase):
    """mc.run on an executor thread, the call on a running loop (as in the app)."""

    def setUp(self):
        self.loop = asyncio.new_event_loop()
        t = threading.Thread(target=self.loop.run_forever, daemon=True)
        t.start()
        self.addCleanup(lambda: (self.loop.call_soon_threadsafe(self.loop.stop), t.join(2)))
        p = mock.patch.object(mc, "_loop", self.loop)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        srv = fake_server()

        async def call_tool(name, arguments):
            self.calls.append((name, arguments))
            if name == "list_issues":
                return NS(content=[NS(type="text", text="#1 Crash on start")], is_error=False)
            return NS(content=[NS(type="text", text="no permission")], isError=True)
        srv.session = NS(call_tool=call_tool)
        p = mock.patch.dict(mc._servers, {"gh": srv}, clear=True)
        p.start()
        self.addCleanup(p.stop)

    def test_the_answer_is_labelled_data_and_errors_are_failures(self):
        r = mc.run({"type": "mcp", "tool": "mcp__gh__list_issues", "args": {"repo": "a/b"}})
        self.assertTrue(r["ok"])
        self.assertIn("not instructions", r["message"])
        self.assertIn("#1 Crash on start", r["message"])
        # The model gets the answer and words it; the raw label isn't read aloud.
        self.assertEqual(r["speak"], "")
        self.assertIn("#1 Crash on start", r["feed_to_model"]["content"])
        self.assertIn("not instructions", r["feed_to_model"]["content"])
        r = mc.run({"type": "mcp", "tool": "mcp__gh__create_issue",
                    "args": {"title": "x", "labels": '["bug"]'}})
        self.assertFalse(r["ok"])
        self.assertEqual(self.calls[-1], ("create_issue", {"title": "x", "labels": ["bug"]}))

    def test_an_unknown_tool_is_not_called(self):
        r = mc.run({"type": "mcp", "tool": "mcp__gh__nope"})
        self.assertFalse(r["ok"])
        self.assertEqual(self.calls, [])


try:
    import mcp  # noqa: F401
    HAVE_MCP = True
except ImportError:
    HAVE_MCP = False

SERVER = textwrap.dedent('''
    try:
        from mcp.server.mcpserver import MCPServer as App       # mcp 2.x
    except ImportError:
        from mcp.server.fastmcp import FastMCP as App           # mcp 1.x
    app = App("demo")

    @app.tool()
    def add(a: int, b: int) -> str:
        """Add two numbers."""
        return str(a + b)

    app.run()
''')


@unittest.skipUnless(HAVE_MCP, "needs the mcp package")
class RealServerTests(unittest.TestCase):
    def test_a_stdio_server_end_to_end_through_the_dispatcher(self):
        from actions import run_specs
        with tempfile.TemporaryDirectory() as d:
            script = Path(d) / "server.py"
            script.write_text(SERVER, encoding="utf-8")
            cfg = Path(d) / "mcp.json"
            cfg.write_text(json.dumps({"mcpServers": {
                "demo": {"command": sys.executable, "args": [str(script)]}}}), encoding="utf-8")
            loop = asyncio.new_event_loop()
            t = threading.Thread(target=loop.run_forever, daemon=True)
            t.start()
            try:
                with mock.patch.object(mc, "CONFIG_PATH", cfg), \
                     mock.patch.dict(mc._servers, {}, clear=True):
                    loop.call_soon_threadsafe(mc.start, loop)
                    for _ in range(150):
                        if mc.server_names():
                            break
                        threading.Event().wait(0.1)
                    self.assertEqual(mc.server_names(), ["demo"], mc.state())
                    spec = lt.to_spec("mcp__demo__add", {"a": 2, "b": 3})
                    [r] = run_specs([spec])
                    if r.get("needs_permission"):          # FastMCP sets no read-only hint
                        r = mc.run(spec)
                    self.assertTrue(r["ok"], r)
                    self.assertIn("5", r["message"])
                    asyncio.run_coroutine_threadsafe(mc.stop_all(), loop).result(10)
            finally:
                loop.call_soon_threadsafe(loop.stop)
                t.join(5)


if __name__ == "__main__":
    unittest.main()
