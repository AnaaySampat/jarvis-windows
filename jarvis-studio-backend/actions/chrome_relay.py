"""The user's own Chrome, for the browser agent, through the JARVIS extension
(chrome-extension/ next to this backend).

Chrome 136+ ignores --remote-debugging-port on the user's normal profile, so reaching
their signed-in Chrome takes an extension: it holds chrome.debugger on the tabs JARVIS
opens and passes DevTools commands through this relay, which looks to Playwright like an
ordinary CDP endpoint (browser.py's "personal" worker uses connect_over_cdp). It follows
Playwright MCP's extension relay (cdpRelay.ts / browserModel.ts in microsoft/playwright).

Only tabs JARVIS created — and popups they open — are ever attached; the extension
refuses anything else too, so the user's own tabs are never seen.

Two endpoints on 127.0.0.1:PORT, on a thread of their own:
  /extension      the extension; only from its own origin (chrome-extension://ID)
  /cdp/<random>   Playwright; the path is new every start and never leaves this process,
                  and a request with a web page's Origin is refused
"""

from __future__ import annotations

import asyncio
import itertools
import json
import secrets
import sys
import threading
import time
from pathlib import Path

PORT = 8766
EXTENSION_ID = "cdgjabnhlkhcinlhlkaamohebijdoofm"     # from the manifest's "key"
ORIGIN = f"chrome-extension://{EXTENSION_ID}"
CDP_PATH = f"/cdp/{secrets.token_urlsafe(18)}"
PING_S = 20                 # keeps the extension's service worker awake
CALL_TIMEOUT_S = 30


def extension_dir() -> Path:
    """The folder to "Load unpacked" in chrome://extensions (bundled with the app)."""
    base = getattr(sys, "_MEIPASS", None) or Path(__file__).resolve().parent.parent
    return Path(base) / "chrome-extension"


class Model:
    """The tabs JARVIS drives, as Playwright sees them (browserModel.ts). `call(method,
    params)` asks the extension; `emit(message)` tells Playwright."""

    def __init__(self, call, emit):
        self.call, self.emit = call, emit
        self.tabs: "dict[int, dict]" = {}     # tabId → {session, info, children}
        self.known: "dict[int, dict]" = {}    # popups announced before auto-attach
        self.auto = False
        self._n = itertools.count(1)

    async def attach(self, tab_id: int) -> dict:
        if tab_id in self.tabs:
            return self.tabs[tab_id]
        await self.call("chrome.debugger.attach", [{"tabId": tab_id}, "1.3"])
        got = await self.call("chrome.debugger.sendCommand",
                              [{"tabId": tab_id}, "Target.getTargetInfo"])
        info = (got or {}).get("targetInfo") or {}
        s = {"session": f"jarvis-tab-{next(self._n)}", "info": info, "children": set()}
        self.tabs[tab_id] = s
        self.emit({"method": "Target.attachedToTarget",
                   "params": {"sessionId": s["session"], "targetInfo": {**info, "attached": True},
                              "waitingForDebugger": False}})
        return s

    def detach(self, tab_id) -> None:
        s = self.tabs.pop(tab_id, None)
        if s:
            self.emit({"method": "Target.detachedFromTarget",
                       "params": {"sessionId": s["session"], "targetId": s["info"].get("targetId")}})

    def _by(self, pred) -> "tuple[int, dict] | tuple[None, None]":
        return next(((t, s) for t, s in self.tabs.items() if pred(s)), (None, None))

    # ── from the extension ──

    def on_event(self, method: str, params: list) -> None:
        if method == "chrome.debugger.onEvent" and len(params) >= 2:
            source, cdp_method = params[0] or {}, params[1]
            cdp_params = params[2] if len(params) > 2 else {}
            s = self.tabs.get(source.get("tabId"))
            if not s:
                return
            child = (cdp_params or {}).get("sessionId")
            if cdp_method == "Target.attachedToTarget" and child:
                s["children"].add(child)
            elif cdp_method == "Target.detachedFromTarget" and child:
                s["children"].discard(child)
            self.emit({"sessionId": source.get("sessionId") or s["session"],
                       "method": cdp_method, "params": cdp_params})
        elif method == "chrome.debugger.onDetach" and params:
            self.detach((params[0] or {}).get("tabId"))
        elif method == "chrome.tabs.onCreated" and params:
            tab = params[0] or {}
            if tab.get("id") is not None:
                self.known[tab["id"]] = tab
                if self.auto:
                    asyncio.ensure_future(self._try_attach(tab["id"]))
        elif method == "chrome.tabs.onRemoved" and params:
            self.known.pop(params[0], None)
            self.detach(params[0])

    async def _try_attach(self, tab_id: int) -> None:
        try:
            await self.attach(tab_id)
        except Exception as exc:  # noqa: BLE001 — a popup we can't hold is just not driven
            print(f"[Chrome] couldn't attach tab {tab_id}: {exc}", flush=True)

    # ── from Playwright ──

    async def command(self, method: str, params: dict, session: "str | None"):
        if method == "Browser.getVersion":
            return {"protocolVersion": "1.3", "product": "Chrome/JARVIS-extension",
                    "revision": "", "userAgent": "JARVIS", "jsVersion": ""}
        if method == "Browser.setDownloadBehavior":
            return {}
        if method == "Target.setAutoAttach" and not session:
            self.auto = True
            for tab_id in list(self.known):
                await self._try_attach(tab_id)
            return {}
        if method == "Target.createTarget":
            tab = await self.call("chrome.tabs.create", [{"url": params.get("url") or "about:blank"}])
            if not isinstance(tab, dict) or tab.get("id") is None:
                raise RuntimeError("Chrome didn't open a tab")
            self.known[tab["id"]] = tab
            return {"targetId": (await self.attach(tab["id"]))["info"].get("targetId")}
        if method == "Target.closeTarget":
            tab_id, _ = self._by(lambda s: s["info"].get("targetId") == params.get("targetId"))
            if tab_id is None:
                return {"success": False}
            await self.call("chrome.tabs.remove", [tab_id])
            return {"success": True}
        if method == "Target.getTargetInfo":
            _, s = self._by(lambda s: s["session"] == session)
            return {"targetInfo": s["info"]} if s else {}
        if not session:                          # browser-level: through any held tab
            if not self.tabs:
                raise RuntimeError(f"No JARVIS tab to send {method} through")
            return await self.call("chrome.debugger.sendCommand",
                                   [{"tabId": next(iter(self.tabs))}, method, params])
        tab_id, _ = self._by(lambda s: s["session"] == session)
        target = {"tabId": tab_id}
        if tab_id is None:                       # a child session (an iframe, a worker)
            tab_id, _ = self._by(lambda s: session in s["children"])
            target = {"tabId": tab_id, "sessionId": session}
        if tab_id is None:
            raise RuntimeError(f"No tab for session {session}")
        return await self.call("chrome.debugger.sendCommand", [target, method, params])


class Relay:
    def __init__(self):
        self.loop: "asyncio.AbstractEventLoop | None" = None
        self.ext = None                  # the extension's connection (newest wins)
        self.ext_since = 0.0
        self.cdp = None                  # Playwright's connection (one at a time)
        self.model: "Model | None" = None
        self.error = ""
        self._ids = itertools.count(1)
        self._pending: "dict[int, asyncio.Future]" = {}

    async def call(self, method: str, params: list):
        ws = self.ext
        if ws is None:
            raise RuntimeError("The JARVIS extension isn't connected")
        i = next(self._ids)
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        try:
            await ws.send(json.dumps({"id": i, "method": method, "params": params}))
            return await asyncio.wait_for(fut, CALL_TIMEOUT_S)
        finally:
            self._pending.pop(i, None)

    @staticmethod
    def check(connection, request):
        """Refuse what isn't the extension or JARVIS's own Playwright."""
        origin = request.headers.get("Origin", "")
        if request.path == "/extension":
            return None if origin == ORIGIN else connection.respond(403, "JARVIS extension only\n")
        if request.path == CDP_PATH:
            return None if not origin else connection.respond(403, "Not from a web page\n")
        return connection.respond(404, "")

    async def handler(self, ws) -> None:
        if ws.request.path == "/extension":
            await self._extension(ws)
        else:
            await self._playwright(ws)

    async def _extension(self, ws) -> None:
        old, self.ext, self.ext_since = self.ext, ws, time.time()
        if old is not None:
            await old.close(1000, "Another Chrome connected")
        if self.cdp is not None:
            await self.cdp.close(1011, "Chrome changed")
        print("[Chrome] the JARVIS extension connected", flush=True)
        try:
            _seen_marker().touch()
        except OSError:
            pass

        async def ping() -> None:
            while True:
                await asyncio.sleep(PING_S)
                await ws.send('{"method":"ping"}')
        pinger = asyncio.create_task(ping())
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except ValueError:
                    continue
                fut = self._pending.get(msg.get("id")) if msg.get("id") else None
                if fut is not None and not fut.done():
                    if msg.get("error"):
                        fut.set_exception(RuntimeError(str(msg["error"])))
                    else:
                        fut.set_result(msg.get("result"))
                elif msg.get("method") and self.model is not None:
                    self.model.on_event(msg["method"], msg.get("params") or [])
        except Exception:  # noqa: BLE001 — a dropped connection ends like a closed one
            pass
        finally:
            pinger.cancel()
            if self.ext is ws:
                self.ext = None
                print("[Chrome] the JARVIS extension disconnected", flush=True)
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(RuntimeError("The JARVIS extension disconnected"))
                if self.cdp is not None:
                    await self.cdp.close(1011, "The JARVIS extension disconnected")

    async def _playwright(self, ws) -> None:
        if self.ext is None:
            await ws.close(1011, "The JARVIS extension isn't connected")
            return
        if self.cdp is not None:
            await ws.close(1013, "Another task is using Chrome")
            return
        out: "asyncio.Queue" = asyncio.Queue()       # in order: responses and events

        async def writer() -> None:
            while True:
                await ws.send(json.dumps(await out.get()))
        self.cdp, model = ws, Model(self.call, out.put_nowait)
        self.model = model
        sender = asyncio.create_task(writer())

        async def run(msg: dict) -> None:
            reply = {"id": msg.get("id"), **({"sessionId": msg["sessionId"]}
                                             if msg.get("sessionId") else {})}
            try:
                reply["result"] = await model.command(msg.get("method", ""), msg.get("params") or {},
                                                      msg.get("sessionId")) or {}
            except Exception as exc:  # noqa: BLE001 — Playwright gets it as a CDP error
                reply["error"] = {"code": -32000, "message": str(exc)}
            out.put_nowait(reply)
        try:
            async for raw in ws:
                try:
                    asyncio.ensure_future(run(json.loads(raw)))
                except ValueError:
                    continue
        except Exception:  # noqa: BLE001
            pass
        finally:
            if self.cdp is ws:
                self.cdp, self.model = None, None
            sender.cancel()
            for tab_id in list(model.tabs):          # drop the "debugging" bar; tabs stay
                try:
                    await self.call("chrome.debugger.detach", [{"tabId": tab_id}])
                except Exception:  # noqa: BLE001
                    pass

    async def serve(self) -> None:
        from websockets.asyncio.server import serve
        async with serve(self.handler, "127.0.0.1", PORT, process_request=self.check,
                         max_size=None, ping_interval=None):
            await asyncio.Future()


_relay = Relay()
_started = threading.Lock()


def start() -> None:
    """Listen for the extension (idempotent; its own thread and loop)."""
    if not _started.acquire(blocking=False):
        return

    def main() -> None:
        loop = asyncio.new_event_loop()
        _relay.loop = loop
        try:
            loop.run_until_complete(_relay.serve())
        except Exception as exc:  # noqa: BLE001 — e.g. the port is taken
            _relay.error = f"Couldn't listen on 127.0.0.1:{PORT} ({exc})"
            print(f"[Chrome] {_relay.error}", flush=True)
    threading.Thread(target=main, name="jarvis-chrome-relay", daemon=True).start()


def connected() -> bool:
    return _relay.ext is not None


def _seen_marker() -> Path:
    import jarvis_paths
    return jarvis_paths.app_data_dir() / "chrome_extension.seen"


def ever_connected() -> bool:
    """The extension has connected on this PC before (so it's installed somewhere)."""
    return connected() or _seen_marker().exists()


def cdp_url() -> str:
    return f"ws://127.0.0.1:{PORT}{CDP_PATH}"


def state() -> dict:
    """For Settings ▸ Connectors."""
    return {"connected": connected(), "installed": ever_connected(), "error": _relay.error,
            "id": EXTENSION_ID, "folder": str(extension_dir())}
