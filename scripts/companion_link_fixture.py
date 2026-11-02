"""Loopback-only synthetic server for check-companion-link.mjs.

Uses the production handshake, registry, signatures, allowlist, subscriptions,
memory merge and phone-command correlation. Only the network identity gate is
substituted: no Tailscale daemon or physical devices are required. Never starts
the assistant, reads user data, or binds a non-loopback interface.
"""

import asyncio
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "jarvis-studio-backend"))

import memory_store
import main
import memory_sync
import phone_link
import storage
import websockets
from autonomy.device_identity import DeviceIdentityRegistry
from server import websocket_server as ws


async def run(directory):
    registry = DeviceIdentityRegistry(Path(directory) / "identity")
    ws.configure_device_identity(registry)
    main._device_registry = registry
    main._task_passkey = main.TaskPasskeyStore(registry.directory)
    main._task_passkey.set("test-only-companion-passkey")
    main._interrupt = asyncio.Event()
    # Only image capture is synthetic; the production task/approval handlers run.
    main.skills.capture_screen_b64 = lambda: "data:image/png;base64,c3ludGhldGlj"
    challenge = registry.create_pairing_challenge()
    terminal = {}
    submissions = {}
    dropped = set()

    async def hello(data):
        await ws.send_to_current("protocol.ready", {"version": 2})

    async def listing(data):
        await ws.send_to_current("task.snapshot", {"tasks": []})

    async def echo(data):
        await ws.send_to_current("probe.echoed", data)

    async def memory(data):
        reply, _ = memory_sync.handle(data)
        await ws.send_to_current("memory.synced", reply)

    async def submit(data):
        task = data["task_id"]
        if main.companion_screenshot.requested(data.get("goal")):
            await main.handle_task_submit(data)
            return
        submissions[task] = submissions.get(task, 0) + 1
        terminal[task] = {
            "task_id": task, "state": "succeeded", "summary": "Synthetic transport task completed.",
            "proof": {"passed": True, "evidence_ids": ["synthetic-transport-receipt"]},
        }
        await ws.send_to_current("task.accepted", {"task_id": task, "state": "running"})

    async def subscribe(data):
        task = data["task_id"]
        if task == main._remote_task_id or task in main._remote_finished:
            await main.handle_task_subscribe(data)
            return
        ws.subscribe_task(task)
        # Drop the first authenticated subscription before completion. The real
        # Android client must authenticate again and resume the same task.
        if task not in dropped:
            dropped.add(task)
            await ws.current_sender().close(code=1012, reason="synthetic restart")
            return
        await ws.emit_task_event(task, "task.event", {"kind": "terminal", "data": terminal[task]}, seq=1)

    async def status(data):
        task = data["task_id"]
        if task == main._remote_task_id or task in main._remote_finished:
            await main.handle_task_status(data)
            return
        if task in dropped:
            await ws.send_to_current("task.status", terminal[task])

    async def command(data):
        result = await phone_link.command("synthetic phone command", timeout=3)
        await ws.send_to_current("probe.phone_result", {"ok": result["ok"]})

    async def phone_result(data):
        phone_link.on_result(ws.current_authenticated_device(), data)

    async def privacy(data):
        await ws.emit("config", {"private": "synthetic desktop-only data"})
        await ws.send_to_current("probe.privacy_done", {"submissions": submissions})

    async def revoke(data):
        device = ws.current_authenticated_device()
        registry.revoke(device)
        await ws.disconnect_authenticated_device(device)

    handlers = {
        "protocol.hello": hello, "task.list": listing, "probe.echo": echo,
        "memory.sync": memory, "task.submit": submit, "task.subscribe": subscribe,
        "task.status": status, "probe.phone": command, "phone.result": phone_result,
        "probe.privacy": privacy, "probe.revoke": revoke,
        "approval.response": main.handle_approval_response,
    }
    for kind, handler in handlers.items():
        ws.register_handler(kind, handler)
    ws.set_remote_allowed(handlers)
    phone_link.configure(asyncio.get_running_loop(), ws.send_to_device, ws.connected_device_ids, lambda: True)
    storage.DEFAULT_ROOT = Path(directory) / "storage"
    memory_store._mem_dir = lambda: Path(directory)
    # Only this explicitly loopback-bound fixture treats its local client as a
    # remote phone. Production network authorization is tested independently.
    with patch.object(ws, "_is_loopback", return_value=False), patch.object(
        ws, "_remote_transport_allowed", side_effect=lambda sock: ws._peer_host(sock) == "127.0.0.1"
    ):
        async with websockets.serve(
            ws._handler, "127.0.0.1", 0,
            subprotocols=["aura-v3"], select_subprotocol=ws._select_subprotocol,
        ) as server:
            port = server.sockets[0].getsockname()[1]
            print("COMPANION_PROBE " + json.dumps({"port": port, "pairing": challenge}), flush=True)
            await asyncio.Future()


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="jarvis-companion-probe-", dir=sys.argv[1] if len(sys.argv) > 1 else None) as directory:
        try:
            asyncio.run(run(directory))
        except KeyboardInterrupt:
            pass
