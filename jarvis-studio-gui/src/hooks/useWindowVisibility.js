import { useEffect, useState } from "react";

// WebView2 may keep document.visibilityState="visible" after a native hide.
// Native window events cover tray hiding/minimization without a polling timer.
export function useWindowVisibility() {
  const [visible, setVisible] = useState(() => !document.hidden);
  useEffect(() => {
    let disposed = false, unlisten, nativeVisible = true;
    const apply = (value) => {
      if (disposed) return;
      document.documentElement.dataset.windowVisible = String(value);
      setVisible(value);
    };
    const onDocument = () => apply(nativeVisible && !document.hidden);
    document.addEventListener("visibilitychange", onDocument);
    if (window.__TAURI_INTERNALS__) {
      (async () => {
        try {
          const { getCurrentWindow } = await import("@tauri-apps/api/window");
          const win = getCurrentWindow();
          unlisten = await win.listen("jarvis-window-visibility", ({ payload }) => {
            nativeVisible = !!payload;
            onDocument();
          });
          if (disposed) { unlisten(); return; }
          nativeVisible = (await win.isVisible()) && !(await win.isMinimized());
          onDocument();
        } catch { /* A browser preview still uses document visibility. */ }
      })();
    }
    return () => {
      disposed = true;
      document.removeEventListener("visibilitychange", onDocument);
      unlisten?.();
    };
  }, []);
  return visible;
}
