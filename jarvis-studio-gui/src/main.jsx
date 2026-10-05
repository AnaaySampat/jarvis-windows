import React from "react";
import ReactDOM from "react-dom/client";

// Bundled HUD fonts (offline, served from 'self' so they satisfy the app CSP).
// Orbitron = display (--disp), Share Tech Mono = mono (--mono), Inter = body (--body).
import "@fontsource/orbitron/400.css";
import "@fontsource/orbitron/500.css";
import "@fontsource/orbitron/600.css";
import "@fontsource/orbitron/700.css";
import "@fontsource/orbitron/800.css";
import "@fontsource/orbitron/900.css";
import "@fontsource/inter/300.css";
import "@fontsource/inter/400.css";
import "@fontsource/inter/500.css";
import "@fontsource/inter/600.css";
import "@fontsource/inter/700.css";
import "@fontsource/share-tech-mono/400.css";

import App from "./App";
import Overlay from "./Overlay";
import BrowserPanel from "./components/BrowserPanel";
import ControlOverlay from "./components/ControlOverlay";
import "./index.css";

// Both windows load the same page. Detect which one we are by reading the
// Tauri-injected window label. Falls back to "main" in browser dev mode.
// In a plain browser you can force the floating overlay with ?overlay (handy for
// previewing/iterating on the pod without the Tauri runtime).
const params = new URLSearchParams(window.location.search);
const forceOverlay = params.has("overlay");
const forceBrowserPanel = params.has("browser-panel");
const forceControlOverlay = params.has("control-overlay");
const windowLabel = forceControlOverlay
  ? "control-overlay"
  : forceBrowserPanel
    ? "browser-panel"
    : forceOverlay
      ? "overlay"
      : (window.__TAURI_INTERNALS__?.metadata?.currentWindow?.label ?? "main");

function Root() {
  if (windowLabel === "overlay") return <Overlay />;
  if (windowLabel === "browser-panel") return <BrowserPanel />;
  if (windowLabel === "control-overlay") return <ControlOverlay />;
  return <App />;
}

ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
