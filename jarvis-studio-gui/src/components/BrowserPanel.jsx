// BrowserPanel.jsx — JARVIS companion side panel.
// Sits on the right edge of the screen while JARVIS drives its browser on the left.
// Shows the active browser task, live steps, and page screenshots.

import { useEffect, useRef, useMemo, useState } from "react";
import { useWebSocket } from "../hooks/useWebSocket";
import "./browser-panel.css";

const PANEL_W = 400;
// Collapsed ("minimized") footprint — a compact status bar pinned to the
// top-right corner so the panel can tuck out of the way without leaving screen.
const MIN_W = 250;
const MIN_H = 52;

function stepIcon(line) {
  const s = (line || "").toLowerCase();
  if (s.startsWith("plan:")) return "🧭";
  if (s.startsWith("✗") || s.includes("failed") || s.includes("couldn't")) return "✕";
  if (s.includes("click")) return "🖱";
  if (s.includes("type") || s.includes("typed")) return "⌨";
  if (s.includes("scroll")) return "↕";
  if (s.includes("search")) return "🔍";
  if (s.includes("navigat") || s.includes("open") || s.includes("goto")) return "🌐";
  if (s.includes("wait")) return "⏳";
  if (s.includes("look") || s.includes("screenshot")) return "📷";
  if (s.includes("note")) return "📝";
  if (s.includes("done")) return "✓";
  return "•";
}

const STATUS_PILL = {
  running: { label: "EXECUTING", cls: "bp-pill--run" },
  done: { label: "COMPLETE", cls: "bp-pill--ok" },
  failed: { label: "STOPPED", cls: "bp-pill--fail" },
  stopped: { label: "STOPPED", cls: "bp-pill--fail" },
  idle: { label: "STANDBY", cls: "bp-pill--idle" },
};

async function applyPanelWindow(visible, minimized) {
  try {
    const { getCurrentWindow, LogicalSize, LogicalPosition } =
      await import("@tauri-apps/api/window");
    const win = getCurrentWindow();
    if (!visible) {
      await win.hide();
      return;
    }
    const left = window.screen.availLeft || 0;
    const top = window.screen.availTop || 0;
    const screenW = window.screen.availWidth || window.screen.width;
    const screenH = window.screen.availHeight || window.screen.height;
    // Full = right-edge rail; minimized = small bar flush to the top-right corner.
    const w = minimized ? MIN_W : PANEL_W;
    const h = minimized ? MIN_H : screenH;
    const x = Math.round(left + screenW - w);
    await win.setSize(new LogicalSize(w, h));
    await win.setPosition(new LogicalPosition(x, top));
    await win.show();
    await win.setAlwaysOnTop(true);
  } catch (err) {
    console.warn("[BrowserPanel] applyPanelWindow failed:", err);
  }
}

export { PANEL_W };

export default function BrowserPanel() {
  const {
    browserState, agentTasks, status, stopSpeech, setBrowserOpen, wsSend, screen,
    stopControl, sendMessage,
  } = useWebSocket();

  const runningBrowser = useMemo(
    () => (agentTasks || []).find((t) => t.kind === "browser" && t.status === "running"),
    [agentTasks],
  );
  const recentBrowser = useMemo(() => {
    const browserTasks = (agentTasks || []).filter((t) => t.kind === "browser");
    return browserTasks.length ? browserTasks[browserTasks.length - 1] : null;
  }, [agentTasks]);
  const task = runningBrowser || (browserState.open ? recentBrowser : null);

  const visible = !!browserState.open
    || !!browserState.setup
    || !!(agentTasks || []).find((t) => t.kind === "browser" && t.status === "running");
  const [minimized, setMinimized] = useState(false);
  const [cmdText, setCmdText] = useState("");

  const handleCommandSubmit = (e) => {
    e.preventDefault();
    if (!cmdText.trim()) return;
    stopControl(); // Immediately halt the current action/autopilot
    wsSend({ type: "browser_control", data: { open: true } }); // Auto-approve
    sendMessage(cmdText); // Issue the new browser command
    setCmdText("");
  };
  const lastApplied = useRef("");
  const stepsRef = useRef(null);

  // Always reappear expanded: reset when the panel hides, and whenever a fresh
  // task starts running (so the user never misses new activity behind the bar).
  useEffect(() => {
    if (!visible) setMinimized(false);
  }, [visible]);
  useEffect(() => {
    if (runningBrowser?.id) setMinimized(false);
  }, [runningBrowser?.id]);

  useEffect(() => {
    const key = visible ? (minimized ? "min" : "full") : "hidden";
    if (key === lastApplied.current) return;
    lastApplied.current = key;
    applyPanelWindow(visible, minimized);
    if (wsSend) {
      wsSend({ type: "browser_panel", data: { open: visible, width: PANEL_W } });
    }
  }, [visible, minimized, wsSend]);

  const busy = status === "working" || status === "thinking" || !!runningBrowser;
  const steps = task?.items || [];
  const stepRows = steps.filter((it) => it.kind === "step").slice(-14);
  const lastShot = [...steps].reverse().find((it) => it.kind === "shot");

  const pill = runningBrowser
    ? STATUS_PILL.running
    : (status === "thinking" || status === "working")
      ? { label: status.toUpperCase(), cls: "bp-pill--run" }
      : STATUS_PILL[task?.status] || STATUS_PILL.idle;

  const themeStyle = useMemo(() => ({
    "--bp-ac": screen?.accent || "#00e5ff",
    "--bp-ac2": screen?.accent2 || "#6fe9ff",
  }), [screen?.accent, screen?.accent2]);

  useEffect(() => {
    const el = stepsRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [stepRows.length, runningBrowser, lastShot?.image]);

  if (minimized) {
    return (
      <div
        className={`bp-root bp-root--min${busy ? " bp-root--busy" : ""}`}
        style={themeStyle}
      >
        <div className="bp-bg" aria-hidden="true" />
        <div className="bp-minbar">
          <div className="bp-brand">
            <div className="bp-logo-ring">
              <span className="bp-logo" aria-hidden="true">◆</span>
            </div>
            <span className="bp-min-title">JARVIS</span>
          </div>
          <div className="bp-hdr-actions">
            <span className={`bp-pill ${pill.cls}`}>{pill.label}</span>
            {busy && (
              <button
                className="bp-icon-btn bp-icon-btn--stop"
                title="Stop"
                aria-label="Stop"
                onClick={stopSpeech}
              >
                ■
              </button>
            )}
            <button
              className="bp-icon-btn"
              title="Expand panel"
              aria-label="Expand panel"
              onClick={() => setMinimized(false)}
            >
              ⤢
            </button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div
      className={`bp-root${busy ? " bp-root--busy" : ""}`}
      style={themeStyle}
    >
      <div className="bp-bg" aria-hidden="true" />
      <div className="bp-grid" aria-hidden="true" />
      <div className="bp-scanline" aria-hidden="true" />
      <div className="bp-frame" aria-hidden="true">
        <span className="bp-corner tl" />
        <span className="bp-corner tr" />
        <span className="bp-corner bl" />
        <span className="bp-corner br" />
      </div>

      <header className="bp-hdr">
        <div className="bp-brand">
          <div className="bp-logo-ring">
            <span className="bp-logo" aria-hidden="true">◆</span>
          </div>
          <div className="bp-title-block">
            <span className="bp-title">JARVIS</span>
            <span className="bp-subtitle">Browser control</span>
          </div>
        </div>
        <div className="bp-hdr-actions">
          <span className={`bp-pill ${pill.cls}`}>{pill.label}</span>
          {busy && (
            <button
              className="bp-icon-btn bp-icon-btn--stop"
              title="Stop"
              aria-label="Stop"
              onClick={stopSpeech}
            >
              ■
            </button>
          )}
          <button
            className="bp-icon-btn"
            title="Minimize panel"
            aria-label="Minimize panel"
            onClick={() => setMinimized(true)}
          >
            —
          </button>
          <button
            className="bp-icon-btn"
            title="Close browser"
            aria-label="Close browser"
            onClick={() => setBrowserOpen(false)}
          >
            ✕
          </button>
        </div>
      </header>

      {browserState.setup && (
        <div className="bp-setup" role="status">
          <span className="bp-setup-spin" aria-hidden="true">◓</span>
          <span>{browserState.setup}</span>
        </div>
      )}

      <div className="bp-page">
        <div className="bp-page-label">Current mission</div>
        <div className="bp-goal">
          {task?.goal || browserState.title || "Ready when you are, sir."}
        </div>
        {(browserState.url || browserState.title) && (
          <div className="bp-url" title={browserState.url}>
            {browserState.title || browserState.url}
          </div>
        )}
      </div>

      {lastShot?.image && (
        <div className="bp-preview">
          <div className="bp-preview-frame">
            <span className="bp-preview-label">Live view</span>
            <img src={lastShot.image} alt="Current page" />
          </div>
        </div>
      )}

      <div className="bp-steps" ref={stepsRef} aria-live="polite">
        {stepRows.length > 0 && (
          <div className="bp-steps-label">Activity log</div>
        )}
        {steps.length === 0 && visible && (
          <div className="bp-step bp-step--muted">
            <span className="bp-step-ico">⏳</span>
            <span className="bp-step-txt">Watching the page…</span>
          </div>
        )}
        {stepRows.map((it, i) => (
          <div
            key={i}
            className={`bp-step${it.ok === false ? " bp-step--fail" : ""}`}
          >
            <span className="bp-step-ico">{stepIcon(it.line)}</span>
            <span className="bp-step-txt">{it.line}</span>
          </div>
        ))}
        {runningBrowser && (
          <div className="bp-step bp-step--live">
            <span className="bp-step-ico">⏳</span>
            <span className="bp-step-txt">Working…</span>
          </div>
        )}
        {!runningBrowser && status === "thinking" && (
          <div className="bp-step bp-step--live">
            <span className="bp-step-ico">⏳</span>
            <span className="bp-step-txt">Thinking…</span>
          </div>
        )}
        {task?.summary && task.status !== "running" && (
          <div className="bp-summary">{task.summary}</div>
        )}
      </div>

      <footer className="bp-foot">
        <form className="bp-cmd-form" onSubmit={handleCommandSubmit}>
          <input
            type="text"
            className="bp-cmd-input"
            placeholder="Give a new command or interrupt..."
            value={cmdText}
            onChange={(e) => setCmdText(e.target.value)}
          />
          <button type="submit" className="bp-cmd-submit" title="Send Command">
            ➤
          </button>
        </form>
        <div className="bp-foot-text">
          JARVIS pauses for your approval before anything destructive. This panel is
          read-only — what it did and saw.
        </div>
      </footer>
    </div>
  );
}
