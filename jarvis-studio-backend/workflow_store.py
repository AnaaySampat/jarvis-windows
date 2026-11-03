"""Durable coordinator checkpoints. Interrupted work is suspended, never replayed.

The journal contains task inputs/results, not credentials or provider messages.
SQLite commits each intent BEFORE execution and each outcome BEFORE handover.
"""
from __future__ import annotations

import contextvars
import json
import sqlite3
import time
import uuid
import hashlib
from pathlib import Path
from contextlib import contextmanager

import jarvis_paths

current = contextvars.ContextVar("agent_workflow", default=None)


class Journal:
    def __init__(self, path: Path | None = None):
        self.path = path or jarvis_paths.app_data_dir() / "workflows.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS workflows (id TEXT PRIMARY KEY, state TEXT NOT NULL, updated REAL NOT NULL, payload TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, run: dict):
        metrics = run.setdefault("metrics", {"started_at_ms": int(time.time() * 1000), "model_decisions": 0})
        if run["state"] != "running":
            metrics.setdefault("ended_at_ms", int(time.time() * 1000))
        metrics["elapsed_ms"] = max(0, metrics.get("ended_at_ms", int(time.time() * 1000)) - metrics["started_at_ms"])
        metrics["tool_calls"] = len(run.get("operations", []))
        metrics["unverified_results"] = sum(r.get("status") == "unverified" for r in run.get("results", []))
        metrics["provider_cost"] = None  # providers do not expose consistent usage here
        with self.connect() as db:
            db.execute("INSERT INTO workflows VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET state=excluded.state, updated=excluded.updated, payload=excluded.payload",
                       (run["id"], run["state"], time.time(), json.dumps(run, ensure_ascii=False)))

    def begin(self, objective: str, steps: list[dict]) -> dict:
        run = {"version": 1, "id": uuid.uuid4().hex, "state": "running", "objective": objective,
               "plan": steps, "results": [], "pending": None, "questions": []}
        self.save(run)
        return run

    def load(self, workflow_id: str) -> dict | None:
        with self.connect() as db:
            row = db.execute("SELECT payload FROM workflows WHERE id=?", (workflow_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def recover(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM workflows WHERE state='running'").fetchall()
            recovered = [json.loads(row[0]) for row in rows]
            for run in recovered:
                run["state"] = "suspended"
                run["reason"] = "Execution was interrupted. Re-observe and reconcile pending work before a new attempt; do not replay it."
                db.execute("UPDATE workflows SET state='suspended', updated=?, payload=? WHERE id=?",
                           (time.time(), json.dumps(run, ensure_ascii=False), run["id"]))
        return recovered

    def read_result(self, workflow_id: str, result_id: str) -> dict | None:
        run = self.load(workflow_id)
        return next((r for r in (run or {}).get("results", []) if r.get("result_id") == result_id), None)

    def remote(self, task_id: str, device_id: str) -> dict | None:
        with self.connect() as db:
            rows = db.execute("SELECT payload FROM workflows ORDER BY updated DESC").fetchall()
        return next((run for (payload,) in rows if
                     (run := json.loads(payload)).get("remote_task_id") == task_id and
                     run.get("device_id") == device_id), None)


def active_result(result_id: str) -> dict | None:
    active = current.get()
    return active[0].read_result(active[1]["id"], result_id) if active else None


def model_decision():
    active = current.get()
    if active:
        metrics = active[1].setdefault("metrics", {})
        metrics["model_decisions"] = metrics.get("model_decisions", 0) + 1


def execute(fn, source: str, command: dict):
    """Record uncertain side effects separately from step/model progress."""
    active = current.get()
    if not active:
        return fn(command)
    journal, run = active
    operations = run.setdefault("operations", [])
    if any(op["state"] in {"pending", "unknown"} for op in operations):
        raise RuntimeError("An earlier action has an unknown outcome; inspect before retrying.")
    op = {"id": uuid.uuid4().hex, "source": source, "state": "pending",
          "request_sha256": hashlib.sha256(json.dumps(command, sort_keys=True, default=str).encode()).hexdigest()}
    operations.append(op)
    journal.save(run)
    try:
        outcome = fn(command)
    except BaseException:
        op["state"] = "unknown"
        journal.save(run)
        raise
    ok = outcome.get("ok") if isinstance(outcome, dict) else outcome[0]
    op["state"] = "confirmed" if ok else "not_confirmed"
    journal.save(run)
    return outcome
