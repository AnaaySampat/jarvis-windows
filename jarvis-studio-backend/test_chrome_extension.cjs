const { test } = require("node:test");
const assert = require("node:assert/strict");
const vm = require("node:vm");
const fs = require("node:fs");
const path = require("node:path");

function extension() {
  const listeners = {};
  const event = (name) => ({ addListener(fn) { listeners[name] = fn; } });
  const timers = new Map();
  const sockets = [];
  class Socket {
    static OPEN = 1;
    static CONNECTING = 0;
    constructor() { this.readyState = 0; sockets.push(this); }
    send() {}
  }
  const chrome = {
    action: { setBadgeText() {}, setBadgeBackgroundColor() {}, setTitle() {}, onClicked: event("click") },
    debugger: { onEvent: event("debug"), onDetach: event("detach"), detach: async () => {} },
    tabs: { onCreated: event("created"), onRemoved: event("removed") },
    alarms: { create() {}, onAlarm: event("alarm") },
    runtime: { id: "jarvis", onStartup: event("startup"), onInstalled: event("installed"),
      onMessage: event("message") },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "chrome-extension/background.js"), "utf8"), {
    chrome, WebSocket: Socket,
    setTimeout(fn, delay) { const id = {}; timers.set(id, { fn, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  });
  return { sockets, timers, listeners };
}

test("a disconnected awake worker retries after one second, before Chrome's alarm", () => {
  const x = extension();
  x.sockets[0].readyState = 3;
  x.sockets[0].onclose();
  const retry = [...x.timers.values()][0];
  assert.equal(retry.delay, 1000);
  retry.fn();
  assert.equal(x.sockets.length, 2);
});

test("the bootstrap page starts a connection immediately and cancels the pending retry", () => {
  const x = extension();
  x.sockets[0].readyState = 3;
  x.sockets[0].onclose();
  let answer;
  x.listeners.message({ type: "jarvis-connect" }, { id: "jarvis" }, (r) => { answer = r; });
  assert.equal(x.sockets.length, 2);
  assert.equal(x.timers.size, 0);
  assert.equal(answer.connected, false);
  x.sockets[1].readyState = 1;
  x.sockets[1].onopen();
  x.listeners.message({ type: "jarvis-connect" }, { id: "jarvis" }, (r) => { answer = r; });
  assert.equal(answer.connected, true);
  assert.equal(x.sockets.length, 2);
});

test("a stale socket closing cannot tear down the replacement connection", () => {
  const x = extension();
  const old = x.sockets[0];
  old.readyState = 3;
  old.onclose();
  [...x.timers.values()][0].fn();
  old.onclose();
  assert.equal(x.timers.size, 0);
});

test("another profile taking over does not cause a reconnect fight", () => {
  const x = extension();
  x.sockets[0].readyState = 3;
  x.sockets[0].onclose({ code: 1000, reason: "Another Chrome connected" });
  x.listeners.alarm();
  assert.equal(x.timers.size, 0);
  assert.equal(x.sockets.length, 1);
  x.listeners.message({ type: "jarvis-connect" }, { id: "jarvis" }, () => {});
  assert.equal(x.sockets.length, 2);
});

test("an older running extension shows the one-time update instruction without disabling it", () => {
  let reloaded = false;
  const status = { textContent: "" };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "chrome-extension/connect.js"), "utf8"), {
    chrome: { runtime: { getManifest: () => ({ version: "1.0.0" }), reload() { reloaded = true; } } },
    document: { getElementById: () => status },
  });
  assert.equal(reloaded, false);
  assert.match(status.textContent, /Restart Chrome once/);
});

test("a connected bootstrap closes only its own tab", async () => {
  const removed = [];
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "chrome-extension/connect.js"), "utf8"), {
    chrome: {
      runtime: { getManifest: () => ({ version: "1.1.0" }), getURL: (p) => `chrome-extension://jarvis/${p}`, sendMessage: async () => ({ connected: true }) },
      tabs: { getCurrent: async () => ({ id: 7, windowId: 1 }),
        query: async () => [{ id: 7, windowId: 1 }, { id: 8, windowId: 1 }],
        onCreated: { addListener() {}, removeListener() {} },
        remove: async (id) => { removed.push(id); } },
    },
    Date, setTimeout,
  });
  await new Promise(setImmediate);
  assert.deepEqual(removed, [7]);
});

test("the bootstrap keeps a cold Chrome alive until its working tab opens", async () => {
  const removed = [];
  let created;
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, "chrome-extension/connect.js"), "utf8"), {
    chrome: {
      runtime: { getManifest: () => ({ version: "1.1.0" }), getURL: (p) => `chrome-extension://jarvis/${p}`, sendMessage: async () => ({ connected: true }) },
      tabs: { getCurrent: async () => ({ id: 7, windowId: 1 }),
        query: async () => [{ id: 7, windowId: 1 }],
        onCreated: { addListener(fn) { created = fn; }, removeListener() {} },
        remove: async (id) => { removed.push(id); } },
    },
    document: { getElementById: () => ({ textContent: "" }) }, Date, setTimeout,
  });
  await new Promise(setImmediate);
  assert.deepEqual(removed, []);
  await created({ id: 9, windowId: 2 }); // another window must not close this one's last tab
  assert.deepEqual(removed, []);
  await created({ id: 10, windowId: 1, pendingUrl: "chrome-extension://jarvis/connect.html" });
  assert.deepEqual(removed, []); // simultaneous wake tabs must not close each other
  await created({ id: 8, windowId: 1 });
  assert.deepEqual(removed, [7]);
});
