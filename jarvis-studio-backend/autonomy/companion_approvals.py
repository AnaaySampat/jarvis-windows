"""Task-bound approval replies from the authenticated phone that requested work."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import hashlib
import json
import secrets
import time

_current = contextvars.ContextVar("companion_approval_session", default=None)
_pending = {}


def current():
    return _current.get()


@contextlib.contextmanager
def task_scope(task_id, device_id, emit, ask):
    session = Session(task_id, device_id, emit, ask)
    token = _current.set(session)
    try:
        yield session
    finally:
        session.closed = True
        cancel_task(task_id, device_id)
        _current.reset(token)


class Session:
    def __init__(self, task_id, device_id, emit, ask):
        self.task_id, self.device_id, self.emit, self.ask = task_id, device_id, emit, ask
        self.closed = False

    async def request(self, kind, description, timeout=120.0):
        if self.closed:
            return False
        approval_id = "approval-" + secrets.token_hex(16)
        step_id = "step-" + secrets.token_hex(16)
        material = {"task_id": self.task_id, "kind": kind, "description": description}
        digest = hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()
        challenge = {
            "task_id": self.task_id, "approval_id": approval_id, "step_id": step_id,
            "action_digest": digest, "expires_at": int(time.time() + timeout) + 1,
            "kind": kind, "description": description,
        }
        future = asyncio.get_running_loop().create_future()
        _pending[approval_id] = (self.device_id, challenge, future)
        approved = False
        try:
            await self.emit("task.event", {
                "task_id": self.task_id, "kind": "approval.challenge", "payload": challenge,
            })
            approved = await asyncio.wait_for(future, timeout) is True
            return approved
        except asyncio.TimeoutError:
            return False
        finally:
            _pending.pop(approval_id, None)
            await self.emit("task.event", {
                "task_id": self.task_id, "kind": "approval.resolved",
                "payload": {**challenge, "approved": approved},
            })


def respond(device_id, data):
    if not isinstance(data, dict):
        return False
    entry = _pending.get(str(data.get("approval_id") or ""))
    if entry is None:
        return False
    owner, challenge, future = entry
    if (owner != device_id or future.done() or time.time() >= challenge["expires_at"]
            or type(data.get("approved")) is not bool
            or any(data.get(key) != challenge[key] for key in (
                "task_id", "approval_id", "step_id", "action_digest", "expires_at"))):
        return False
    future.set_result(data["approved"])
    return True


def cancel_task(task_id, device_id):
    for owner, challenge, future in tuple(_pending.values()):
        if owner == device_id and challenge["task_id"] == task_id and not future.done():
            future.set_result(False)


def pending_for_task(task_id, device_id):
    return [dict(challenge) for owner, challenge, future in tuple(_pending.values())
            if owner == device_id and challenge["task_id"] == task_id
            and not future.done() and time.time() < challenge["expires_at"]]
