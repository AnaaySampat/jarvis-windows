"""Structured, local-only timer operations using the existing reminder scheduler.

The timer ID is distinct from a request ID. A stable start token makes an uncertain
start safe to reconcile without creating another reminder. Cancellation matches
one ID exactly; general reminder substring matching is intentionally not used.
"""
import re
import time
import uuid

import reminders


def _public(item):
    return {"id": item["id"], "deadline": int(item["at"] * 1000),
            "seconds": item["seconds"], "label": item["label"],
            "state": item.get("state", "running"), "token": item["token"]}


def perform(action, data):
    with reminders._lock:
        items = reminders._all()
        own = [r for r in items if r.get("source") == "hands-free"]
        if action == "timer.list":
            return {"ok": True, "summary": "Timers updated.", "timers": [_public(r) for r in own]}
        if action == "timer.cancel":
            target = next((r for r in own if r["id"] == data.get("timer_id")), None)
            if target is None:
                return {"ok": False, "summary": "That timer is no longer active. Refresh your timers."}
            if target.get("state") == "finished" or target["at"] <= time.time():
                return {"ok": False, "summary": "That timer has already finished."}
            if not reminders._save([r for r in items if r is not target]):
                return {"ok": False, "summary": "Could not cancel the timer: saving failed."}
            return {"ok": True, "summary": "Timer cancelled.", "timers": [_public(r) for r in own if r is not target]}
        if action != "timer.start":
            return {"ok": False, "summary": "Unknown timer action."}
        seconds = data.get("seconds")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or not 5 <= seconds <= 86400 or int(seconds) != seconds:
            return {"ok": False, "summary": "Choose a timer duration between 5 seconds and 24 hours."}
        token = str(data.get("token") or "")
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", token):
            return {"ok": False, "summary": "The timer start token is missing or invalid."}
        previous = next((r for r in own if r.get("token") == token), None)
        if previous:
            return {"ok": True, "summary": "Timer already registered.", "timers": [_public(r) for r in own]}
        item = {"id": "activity_" + uuid.uuid4().hex, "at": time.time() + seconds,
                "text": "", "timer": True, "source": "hands-free", "state": "running",
                "seconds": int(seconds), "label": str(data.get("label") or "Activity")[:160], "token": token}
        # Keep every live timer and at most 50 old receipts.
        live = [r for r in items if r.get("source") != "hands-free" or r.get("state") != "finished"]
        receipts = [r for r in own if r.get("state") == "finished"][-49:]
        if not reminders._save(live + receipts + [item]):
            return {"ok": False, "summary": "Could not save the timer."}
        return {"ok": True, "summary": "Timer started.", "timers": [_public(r) for r in live + receipts + [item] if r.get("source") == "hands-free"]}
