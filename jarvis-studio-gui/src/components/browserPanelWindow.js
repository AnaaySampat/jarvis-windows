// Position/show the browser-panel Tauri window beside JARVIS's Chromium browser.
// Called from the main HUD when browser_state.open changes (belt-and-suspenders:
// the panel window also watches the same state on its own websocket).

import { syncAuxiliaryWindow } from "./auxiliaryWindows";

export const PANEL_W = 400;

export async function syncBrowserPanel(open, wsSend) {
  try {
    const { LogicalSize, LogicalPosition } = await import("@tauri-apps/api/window");
    await syncAuxiliaryWindow("browser-panel", open, async (panel) => {
      const left = window.screen.availLeft || 0;
      const top = window.screen.availTop || 0;
      const w = PANEL_W;
      const h = window.screen.availHeight || window.screen.height;
      const x = Math.round(left + (window.screen.availWidth || window.screen.width) - w);
      await panel.setSize(new LogicalSize(w, h));
      await panel.setPosition(new LogicalPosition(x, top));
      await panel.setAlwaysOnTop(true);
      if (wsSend) {
        wsSend({ type: "browser_panel", data: { open: true, width: PANEL_W } });
      }
    });
  } catch (err) {
    console.warn("[BrowserPanel] sync failed:", err);
  }
}
