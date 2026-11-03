"""Pinned ecosystem contracts. Existing signed v2 execution remains unchanged."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

CONTRACT_DIRECTORY = Path(__file__).resolve().parent.parent / "contracts" / "ecosystem-v1"


def verify_contract_pin():
    manifest = json.loads((CONTRACT_DIRECTORY / "manifest.json").read_text(encoding="utf-8"))
    bundle = json.loads((CONTRACT_DIRECTORY / "bundle.json").read_text(encoding="utf-8"))
    if manifest["version"] != bundle["version"]:
        raise ValueError("Contract version differs from pin")
    for name, digest in manifest["sha256"].items():
        if hashlib.sha256((CONTRACT_DIRECTORY / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f"Contract pin mismatch: {name}")
    return bundle


def task_display_state(status):
    if status in {"accepted", "queued"}: return "queued"
    if status in {"succeeded", "done", "completed"}: return "completed"
    if status in {"partial", "partially_completed"}: return "partial"
    if status == "failed": return "failed"
    if status in {"cancelled", "stopped"}: return "cancelled"
    if status in {"needs_you", "suspended", "recovering", "blocked", "unverified", "waiting_for_unlock", "waiting_for_approval"}: return "needs_you"
    return "running" if status in {"planning", "policy_check", "executing", "verifying", "running", "cancelling"} else "needs_you"


def workflow_summary(run, updated_at_ms=0):
    results = run.get("results") or []
    completed = [r.get("summary", "") for r in results if r.get("ok") is True]
    remaining = [r.get("summary", "") for r in results if r.get("ok") is not True]
    uncertain = bool(run.get("pending")) or any(op.get("state") in {"pending", "unknown"} for op in run.get("operations", []))
    state = task_display_state(run.get("state"))
    if uncertain:
        state = "needs_you"
        remaining.append("Check recorded actions with the executing device before another attempt.")
    elif state == "needs_you" and completed and remaining:
        state = "partial"
    return {"contract_version": "1.0.0", "task_id": run["id"], "status": state,
            "executing_device": "windows", "completed": completed, "remaining": remaining,
            "updated_at_ms": max(0, int(updated_at_ms)),
            "recovery": "review" if state in {"needs_you", "partial"} else "cancel" if state == "running" else "none"}


def peer_info():
    # A packaged backend may not include the GUI metadata. Missing is unknown.
    try:
        version = json.loads((Path(__file__).resolve().parent.parent / "jarvis-studio-gui" / "package.json").read_text(encoding="utf-8"))["version"]
    except (OSError, KeyError, ValueError):
        version = ""
    return {"app_version": version, "ecosystem_contracts": ["1.0.0"], "portable_workflows": False}
