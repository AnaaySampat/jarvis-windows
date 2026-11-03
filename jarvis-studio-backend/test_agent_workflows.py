"""Deterministic workflow evaluations: no live models, browser or desktop required."""
import asyncio
import tempfile
import unittest
from pathlib import Path

import agent_results as ar
import agents
import workflow_store as ws


class WorkflowEvaluations(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = ws.Journal(Path(self.temp.name) / "workflows.sqlite3")

    def test_long_research_handover_retains_last_source_and_exact_destination(self):
        raw = ar.result({"ok": True, "summary": "Comparison", "findings": ["x" * 1600, "Final source: https://vendor.example/price?sku=123"],
                         "evidence": [ar.receipt("read", "Source was read")]})
        run = self.journal.begin("Research and save to D:/Reports/laptops.txt", [])
        run["results"].append(raw)
        self.journal.save(run)
        context = agents.task_context(run["objective"], agents.Step("computer", "Save comparison"),
                                     [agents.StepResult("research", "Compare", True, raw["summary"], raw["findings"], raw)])
        self.assertIn("D:/Reports/laptops.txt", context)
        token = ws.current.set((self.journal, run))
        try:
            self.assertIn("sku=123", ws.active_result(raw["result_id"])["findings"][-1])
        finally:
            ws.current.reset(token)

    def test_a_polite_reply_without_actions_is_unverified(self):
        self.assertEqual(ar.from_actions("Done, I've saved it!", [], "Save a file")["status"], "unverified")
        outcome = ar.result({"ok": True, "summary": "Saved."}, "Save", require_evidence=True)
        self.assertFalse(outcome["ok"])

    def test_fast_browser_success_retains_its_receipt_through_the_contract(self):
        from test_autopilot_loop import run_loop
        raw, _ = run_loop(self, goal="open example.com")
        outcome = ar.result(raw, "Open example.com", require_evidence=True)
        self.assertTrue(outcome["ok"])
        self.assertEqual(outcome["evidence"][0]["kind"], "tool_result")

    def test_uncertain_native_work_retains_status_and_evidence_in_a_specialist_result(self):
        proof = ar.receipt("tool_result", "Clicked Save")
        native = ar.result({"status": "unverified", "summary": "Save ran; completion needs checking.",
                            "evidence": [proof], "completed": ["Opened report"], "remaining": ["Confirm saved"]})
        outcome = ar.from_actions(native["summary"], [native], "Save the report")
        self.assertEqual(outcome["status"], "unverified")
        self.assertEqual(outcome["evidence"], [proof])
        self.assertEqual(outcome["completed"], ["Opened report"])
        self.assertEqual(outcome["remaining"], ["Confirm saved"])

    def test_checker_outage_never_promotes_a_tool_receipt_to_task_success(self):
        async def unavailable(*args):
            raise TimeoutError("provider unavailable")
        raw = ar.from_actions("Done", [{"ok": True, "type": "open_app", "message": "Opened Notepad"}], "Open and save")
        checked = asyncio.run(ar.check_completion("Open and save", raw, unavailable))
        self.assertEqual(checked["status"], "unverified")

    def test_native_action_is_not_repeated_when_its_completion_checker_is_unavailable(self):
        from test_autopilot_loop import run_loop
        for predicted in (False, True):
            click = '{"do":"click","target":1,"done":"Saved the report"}' if predicted else '{"do":"click","target":1}'
            outcome, surfaces = run_loop(self, replies=[click, '{"do":"done","summary":"Saved the report"}'],
                                         verdicts=[""], goal="Save the report")
            self.assertEqual(outcome["status"], "unverified")
            self.assertEqual(len(surfaces["browser"].commands), 1)

    def test_interruption_keeps_results_and_pending_work_without_replay(self):
        run = self.journal.begin("Research then save", [{"agent": "research"}, {"agent": "computer"}])
        run["results"] = [ar.result({"ok": True, "findings": ["Price 4210"]})]
        run["pending"] = {"step": 1, "state": "intent_recorded"}
        self.journal.save(run)
        restarted = ws.Journal(self.journal.path)
        recovered = restarted.recover()
        self.assertEqual(recovered[0]["state"], "suspended")
        self.assertEqual(recovered[0]["results"][0]["findings"], ["Price 4210"])
        self.assertEqual(recovered[0]["pending"]["step"], 1)
        self.assertEqual(restarted.recover(), [])

    def test_lost_ack_after_effect_blocks_a_duplicate_action(self):
        run = self.journal.begin("Send one message", [])
        token = ws.current.set((self.journal, run))
        sent = []
        def send(_):
            sent.append("sent")
            raise ConnectionError("ack lost after send")
        try:
            with self.assertRaises(ConnectionError):
                ws.execute(send, "message", {"text": "Hello"})
            with self.assertRaises(RuntimeError):
                ws.execute(send, "message", {"text": "Hello"})
            self.assertEqual(sent, ["sent"])
            self.assertEqual(self.journal.load(run["id"])["operations"][0]["state"], "unknown")
        finally:
            ws.current.reset(token)

    def test_consultation_can_read_the_agenda_but_cannot_add_an_event(self):
        self.assertEqual(agents.consult_target("scheduling", "computer"), "scheduling")
        self.assertIsNone(agents.consult_target("scheduling", "scheduling"))
        self.assertTrue(ar.read_only_spec({"type": "schedule", "do": "get"}))
        self.assertFalse(ar.read_only_spec({"type": "schedule", "do": "add"}))
        from actions import run_specs
        token = ar.readonly.set(True)
        try:
            outcome = run_specs([{"type": "schedule", "do": "add", "task": "Injected event"}])[0]
            self.assertEqual(outcome["status"], "blocked")
        finally:
            ar.readonly.reset(token)


if __name__ == "__main__":
    unittest.main()
