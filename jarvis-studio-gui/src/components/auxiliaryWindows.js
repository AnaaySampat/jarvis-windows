// Serialize create/show/destroy for each window so rapid state changes cannot
// resurrect a panel after its task has ended. Only the main controller calls this.
const queues = new Map();

export function syncAuxiliaryWindow(label, active, configure) {
  const next = (queues.get(label) || Promise.resolve()).catch(() => {}).then(async () => {
    if (!window.__TAURI_INTERNALS__) return;
    const { invoke } = await import("@tauri-apps/api/core");
    await invoke("set_aux_window", { label, active });
    if (!active) return;
    const { WebviewWindow } = await import("@tauri-apps/api/webviewWindow");
    const win = await WebviewWindow.getByLabel(label);
    if (!win) throw new Error(`Could not create ${label}`);
    if (configure) await configure(win);
    await win.show();
    await win.emit("jarvis-window-visibility", true);
  });
  queues.set(label, next);
  return next.catch((error) => console.warn(`[JARVIS] ${label}:`, error));
}

export function voiceOverlayVisible(activeApp, overlay, permission) {
  if (permission && overlay?.enabled !== false) return true;
  if (!overlay?.enabled || !activeApp || activeApp.on_jarvis) return false;
  const stem = (s) => String(s || "").toLowerCase().replace(/\.exe$/, "").trim();
  const apps = (overlay.apps || []).map(stem).filter(Boolean);
  const match = !!stem(activeApp.app) && apps.includes(stem(activeApp.app));
  if (overlay.mode === "only") return apps.length > 0 && match;
  return overlay.mode === "except" ? !match : true;
}
