// JARVIS in Chrome: lets JARVIS's browser agent work in tabs it opens here, where the
// user is signed in. Chrome 136+ ignores --remote-debugging-port on a normal profile, so
// this is the way in. It connects to JARVIS on this PC and does only what its relay
// (jarvis-studio-backend/actions/chrome_relay.py) asks: open a tab, attach the debugger
// to it, pass DevTools commands and events through, close it. The user's own tabs are
// never attached — only tabs JARVIS opened, and popups those tabs open. Cancelling the
// "started debugging" bar takes control back at once.

const RELAY = "ws://127.0.0.1:8766/extension";
const ALLOWED = new Set(["chrome.debugger.attach", "chrome.debugger.detach",
  "chrome.debugger.sendCommand", "chrome.tabs.create", "chrome.tabs.remove"]);

let ws = null;
const ours = new Set();       // tabs JARVIS opened (and their popups)
const attached = new Set();   // of those, the ones whose debugger JARVIS holds
let groupId = null;

function badge(on) {
  chrome.action.setBadgeText({ text: on ? "ON" : "" });
  chrome.action.setBadgeBackgroundColor({ color: "#3ddc97" });
  chrome.action.setTitle({ title: on ? "JARVIS is connected" : "JARVIS isn't running" });
}

function send(msg) {
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  let sock;
  try { sock = new WebSocket(RELAY); } catch { return; }
  ws = sock;
  sock.onopen = () => badge(true);
  sock.onclose = () => {
    if (ws === sock) ws = null;
    badge(false);
    for (const tabId of attached) chrome.debugger.detach({ tabId }).catch(() => {});
    attached.clear();
  };
  sock.onerror = () => {};    // JARVIS isn't running: the alarm tries again
  sock.onmessage = (e) => { onMessage(e.data); };
}

async function onMessage(data) {
  let msg;
  try { msg = JSON.parse(data); } catch { return; }
  if (msg.method === "ping") return;          // keeps this worker awake while connected
  const reply = { id: msg.id };
  try { reply.result = (await run(msg.method, msg.params || [])) ?? {}; }
  catch (err) { reply.error = String(err?.message || err); }
  send(reply);
}

async function run(method, args) {
  if (!ALLOWED.has(method)) throw new Error(`Not allowed: ${method}`);
  const target = args[0] || {};
  switch (method) {
    case "chrome.tabs.create": {
      const tab = await chrome.tabs.create({ url: target.url || "about:blank", active: true });
      ours.add(tab.id);
      group(tab.id);
      return tab;
    }
    case "chrome.tabs.remove":
      if (!ours.has(target)) throw new Error("Not a JARVIS tab");
      return chrome.tabs.remove(target);
    case "chrome.debugger.attach":
      if (!ours.has(target.tabId)) throw new Error("Not a JARVIS tab");
      await chrome.debugger.attach({ tabId: target.tabId }, args[1] || "1.3");
      attached.add(target.tabId);
      return {};
    case "chrome.debugger.detach":
      attached.delete(target.tabId);
      return chrome.debugger.detach({ tabId: target.tabId }).catch(() => {});
    default:                                   // chrome.debugger.sendCommand
      if (!attached.has(target.tabId)) throw new Error("Not a JARVIS tab");
      return chrome.debugger.sendCommand(target, args[1], args[2] || {});
  }
}

// Every JARVIS tab sits in one "JARVIS" tab group, so it's plain which ones it drives.
async function group(tabId) {
  try {
    if (groupId !== null) { await chrome.tabs.group({ groupId, tabIds: tabId }); return; }
  } catch { groupId = null; }
  try {
    groupId = await chrome.tabs.group({ tabIds: tabId });
    await chrome.tabGroups.update(groupId, { title: "JARVIS", color: "cyan" });
  } catch { /* a window that can't hold groups (a popup): leave the tab as it is */ }
}

chrome.debugger.onEvent.addListener((source, method, params) => {
  if (attached.has(source.tabId)) send({ method: "chrome.debugger.onEvent", params: [source, method, params] });
});
chrome.debugger.onDetach.addListener((source, reason) => {
  if (attached.delete(source.tabId)) send({ method: "chrome.debugger.onDetach", params: [source, reason] });
});
chrome.tabs.onCreated.addListener((tab) => {
  if (!ours.has(tab.openerTabId)) return;      // a popup from one of JARVIS's tabs only
  ours.add(tab.id);
  send({ method: "chrome.tabs.onCreated", params: [tab] });
});
chrome.tabs.onRemoved.addListener((tabId) => {
  if (ours.delete(tabId)) send({ method: "chrome.tabs.onRemoved", params: [tabId] });
});

chrome.alarms.create("jarvis-reconnect", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(connect);
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);
connect();
