"""chrome_relay: the tab model Playwright sees through the JARVIS extension, and who may
connect. A fake extension stands in for chrome.*; the end-to-end run with a real
Chromium and the extension loaded is in the commit notes, not here (no browser in CI)."""

import asyncio
import unittest

from actions import chrome_relay


class FakeExtension:
    def __init__(self):
        self.calls, self.tabs = [], 6

    async def __call__(self, method, params):
        self.calls.append((method, params))
        if method == "chrome.tabs.create":
            self.tabs += 1
            return {"id": self.tabs, "url": params[0]["url"]}
        if method == "chrome.debugger.sendCommand" and params[1] == "Target.getTargetInfo":
            return {"targetInfo": {"targetId": f"T{params[0]['tabId']}", "type": "page"}}
        return {"ok": method}


class ModelTests(unittest.TestCase):
    def setUp(self):
        self.ext, self.out = FakeExtension(), []
        self.model = chrome_relay.Model(self.ext, self.out.append)

    def run_(self, coro):
        return asyncio.run(coro)

    def test_a_new_tab_is_opened_attached_and_announced_before_the_reply(self):
        res = self.run_(self.model.command("Target.createTarget", {"url": "about:blank"}, None))
        self.assertEqual(res, {"targetId": "T7"})
        self.assertEqual([c[0] for c in self.ext.calls], ["chrome.tabs.create", "chrome.debugger.attach",
                                                          "chrome.debugger.sendCommand"])
        self.assertEqual(self.out[0]["method"], "Target.attachedToTarget")
        self.assertEqual(self.out[0]["params"]["sessionId"], "jarvis-tab-1")

    def test_commands_go_to_their_tab_and_child_sessions_keep_their_id(self):
        self.run_(self.model.command("Target.createTarget", {}, None))
        self.run_(self.model.command("Page.navigate", {"url": "x"}, "jarvis-tab-1"))
        self.assertEqual(self.ext.calls[-1], ("chrome.debugger.sendCommand",
                                              [{"tabId": 7}, "Page.navigate", {"url": "x"}]))
        self.model.on_event("chrome.debugger.onEvent", [{"tabId": 7}, "Target.attachedToTarget",
                                                        {"sessionId": "CHILD"}])
        self.run_(self.model.command("Runtime.enable", {}, "CHILD"))
        self.assertEqual(self.ext.calls[-1][1][0], {"tabId": 7, "sessionId": "CHILD"})
        self.model.on_event("chrome.debugger.onEvent", [{"tabId": 7}, "Page.loadEventFired", {}])
        self.assertEqual(self.out[-1], {"sessionId": "jarvis-tab-1", "method": "Page.loadEventFired",
                                        "params": {}})

    def test_events_from_tabs_it_doesnt_hold_go_nowhere(self):
        self.model.on_event("chrome.debugger.onEvent", [{"tabId": 99}, "Page.loadEventFired", {}])
        self.assertEqual(self.out, [])
        with self.assertRaises(RuntimeError):
            self.run_(self.model.command("Page.navigate", {}, "jarvis-tab-9"))

    def test_a_popup_is_attached_once_auto_attach_is_on_and_a_closed_tab_detaches(self):
        async def go():
            await self.model.command("Target.setAutoAttach", {"autoAttach": True}, None)
            self.model.on_event("chrome.tabs.onCreated", [{"id": 42, "openerTabId": 7}])
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        self.run_(go())
        self.assertIn(42, self.model.tabs)
        self.model.on_event("chrome.tabs.onRemoved", [42])
        self.assertEqual(self.out[-1]["method"], "Target.detachedFromTarget")
        self.assertNotIn(42, self.model.tabs)


class Request:
    def __init__(self, path, origin=""):
        self.path, self.headers = path, ({"Origin": origin} if origin else {})


class Conn:
    def respond(self, status, text):
        return status


class WhoMayConnectTests(unittest.TestCase):
    check = staticmethod(chrome_relay.Relay.check)

    def test_only_the_extension_and_jarvis_itself_get_in(self):
        self.assertIsNone(self.check(Conn(), Request("/extension", chrome_relay.ORIGIN)))
        self.assertIsNone(self.check(Conn(), Request(chrome_relay.CDP_PATH)))
        self.assertEqual(self.check(Conn(), Request("/extension", "https://evil.example")), 403)
        self.assertEqual(self.check(Conn(), Request("/extension", "chrome-extension://other")), 403)
        self.assertEqual(self.check(Conn(), Request(chrome_relay.CDP_PATH, "https://evil.example")), 403)
        self.assertEqual(self.check(Conn(), Request("/cdp/guess")), 404)


class OpenTheirChromeTests(unittest.TestCase):
    def test_chrome_opens_on_the_extensions_profile_not_the_picker(self):
        import json, os, tempfile
        from unittest import mock
        from actions import browser
        with tempfile.TemporaryDirectory() as tmp:
            data = os.path.join(tmp, "Google", "Chrome", "User Data")
            for name, ids in (("Default", ["other"]), ("Profile 3", [chrome_relay.EXTENSION_ID])):
                os.makedirs(os.path.join(data, name))
                with open(os.path.join(data, name, "Secure Preferences"), "w") as f:
                    json.dump({"extensions": {"settings": {i: {} for i in ids}}}, f)
            linked = []
            with mock.patch.dict(os.environ, {"LOCALAPPDATA": tmp}), \
                    mock.patch.object(chrome_relay, "start"), \
                    mock.patch.object(chrome_relay, "connected", lambda: bool(linked)), \
                    mock.patch.object(chrome_relay, "ever_connected", lambda: True), \
                    mock.patch.object(browser, "_chrome_running", lambda: False), \
                    mock.patch.object(browser, "_find_chrome_exe", lambda: "chrome.exe"), \
                    mock.patch.object(browser.subprocess, "Popen",
                                      lambda args, **_: linked.append(args)):
                self.assertEqual(browser.ensure_personal_available(), (True, ""))
        self.assertIn("--profile-directory=Profile 3", linked[0])


class OnlyJarvisOwnBrowserReachesTheHudTests(unittest.TestCase):
    def test_a_headless_or_personal_page_is_the_autopilots_not_the_huds(self):
        from unittest import mock
        from actions import browser

        class Page:
            url = "https://example.com/"
            def is_closed(self):
                return False
        hud = []
        with mock.patch.object(browser, "_state_cb", hud.append), \
                mock.patch.object(browser, "_last_state", {"open": False, "url": ""}):
            for mode in ("headless", "personal"):
                w = browser._BrowserWorker.__new__(browser._BrowserWorker)
                w._mode, w._ctx, w._page, w._last_title, w._state = mode, object(), Page(), "", {}
                w.push_state()
                with mock.patch.object(browser, "_active_worker", lambda: w):
                    self.assertEqual(browser.target_state()["url"], "https://example.com/")
            self.assertEqual(hud, [])                 # no side panel over nothing
            self.assertFalse(browser.is_open())       # the consent gate isn't fooled


if __name__ == "__main__":
    unittest.main()
