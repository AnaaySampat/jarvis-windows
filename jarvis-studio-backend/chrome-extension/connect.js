// A short-lived bootstrap tab wakes the extension without touching the user's tabs.
// An unpacked extension may still have its old worker after an app upgrade.
if (chrome.runtime.getManifest().version !== "1.1.0") {
  document.getElementById("status").textContent =
    "The JARVIS extension was updated. Restart Chrome once to load the update, then retry your task.";
} else {
  const deadline = Date.now() + 10000;
  async function connect() {
    try {
      const status = await chrome.runtime.sendMessage({ type: "jarvis-connect" });
      if (status?.connected) {
        const tab = await chrome.tabs.getCurrent();
        if (tab?.id) {
          // On a cold Chrome start this may be its only tab. Closing it now
          // exits Chrome before JARVIS can attach and open its working tab.
          let closed = false;
          const bootstrap = chrome.runtime.getURL("connect.html");
          const isWorkingTab = (other) => other.id !== tab.id && other.windowId === tab.windowId &&
            !(other.pendingUrl || other.url || "").startsWith(bootstrap);
          async function closeWhenAnotherOpens(other) {
            if (closed || !isWorkingTab(other)) return;
            closed = true;
            chrome.tabs.onCreated.removeListener(closeWhenAnotherOpens);
            await chrome.tabs.remove(tab.id);
          }
          chrome.tabs.onCreated.addListener(closeWhenAnotherOpens);
          const tabs = await chrome.tabs.query({ windowId: tab.windowId });
          const other = tabs.find(isWorkingTab);
          if (other) await closeWhenAnotherOpens(other);
          else document.getElementById("status").textContent = "Connected to JARVIS. Opening your task…";
        }
        return;
      }
    } catch { /* the worker may be starting; retry within this tab's deadline */ }
    if (Date.now() < deadline) setTimeout(connect, 200);
    else document.getElementById("status").textContent =
      "JARVIS could not connect. Check that the Windows app is running, then retry your request.";
  }
  connect();
}
