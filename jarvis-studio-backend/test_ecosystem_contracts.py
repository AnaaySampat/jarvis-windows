"""Shared golden fixtures and real SQLite interruption integration; no live models."""
import json
import tempfile
import unittest
from pathlib import Path
import jsonschema
import ecosystem_contracts as ec
import workflow_store

class EcosystemContractTests(unittest.TestCase):
    def test_pinned_golden_schemas(self):
        bundle = ec.verify_contract_pin()
        for fixture in json.loads((ec.CONTRACT_DIRECTORY / "golden.json").read_text(encoding="utf-8")):
            with self.subTest(schema=fixture["schema"], valid=fixture["valid"]):
                validator = jsonschema.Draft7Validator(bundle["schemas"][fixture["schema"]])
                self.assertEqual(validator.is_valid(fixture["value"]), fixture["valid"])

    def test_android_and_windows_share_task_states(self):
        for fixture in json.loads((ec.CONTRACT_DIRECTORY / "task-states.json").read_text(encoding="utf-8")):
            self.assertEqual(ec.task_display_state(fixture["status"]), fixture["expected"])

    def test_recovered_sqlite_action_is_needs_you_and_is_never_replayed(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = workflow_store.Journal(Path(temporary) / "workflows.sqlite3")
            run = journal.begin("Synthetic interrupted write", [])
            run["results"] = [{"ok": True, "summary": "Read source"}]
            run["pending"] = {"id": "opaque-pending", "state": "intent_recorded"}
            journal.save(run)
            recovered = workflow_store.Journal(journal.path).recover()
            summary = ec.workflow_summary(recovered[0])
            self.assertEqual(summary["status"], "needs_you")
            self.assertEqual(summary["completed"], ["Read source"])
            self.assertEqual(summary["recovery"], "review")
            self.assertEqual(journal.recover(), [])
            jsonschema.validate(summary, ec.verify_contract_pin()["schemas"]["task_summary"])

    def test_partial_portable_work_retains_completed_results(self):
        summary = ec.workflow_summary({"id": "opaque", "state": "suspended", "results": [
            {"ok": True, "summary": "Research complete"}, {"ok": False, "summary": "Device unavailable"}]})
        self.assertEqual(summary["status"], "partial")
        self.assertEqual(summary["remaining"], ["Device unavailable"])
