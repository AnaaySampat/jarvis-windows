// AgentActivity.jsx — a dockable panel showing what JARVIS's autopilot actually
// did: every step it took and the page screenshots it saw, newest task first,
// click a thumbnail to enlarge. This is AUDITABILITY, not control: JARVIS still
// asks before anything destructive (see the footer). Fed by the agent_* websocket
// events accumulated in useWebSocket (`agentTasks`).

import { useState } from "react";

// A rough verb icon for a step line ("Opened YouTube", "clicked (1,2)", …).
function stepIcon(line) {
  const s = (line || "").toLowerCase();
  if (s.startsWith("plan:")) return "🧭";
  if (s.startsWith("✗") || s.includes("failed") || s.includes("couldn't")) return "✕";
  if (s.includes("click")) return "🖱";
  if (s.includes("type") || s.includes("typed")) return "⌨";
  if (s.includes("scroll")) return "↕";
  if (s.includes("search")) return "🔍";
  if (s.includes("navigat") || s.includes("open") || s.includes("goto") || s.includes("went")) return "🌐";
  if (s.includes("wait")) return "⏳";
  if (s.includes("back")) return "↩";
  if (s.includes("look") || s.includes("screenshot") || s.includes("see")) return "📷";
  if (s.includes("note")) return "📝";
  if (s.includes("done")) return "✓";
  return "•";
}

const STATUS = {
  running: { label: "running…", cls: "activity-pill--run" },
  done:    { label: "done ✓", cls: "activity-pill--ok" },
  failed:  { label: "stopped ✕", cls: "activity-pill--fail" },
  stopped: { label: "stopped", cls: "activity-pill--fail" },
};

export default function AgentActivity({ tasks, onClose, onClear }) {
  const [zoom, setZoom] = useState(null); // enlarged screenshot src, or null
  const ordered = [...(tasks || [])].reverse(); // newest task first

  return (
    <div className="settings-overlay" onClick={onClose}>
      <div className="settings-panel activity-panel" onClick={(e) => e.stopPropagation()}>
        <div className="settings-hdr">
          <h2>Agent Activity</h2>
          <div className="activity-hdr-ctrls">
            {ordered.length > 0 && (
              <button className="activity-clear" onClick={onClear}>Clear</button>
            )}
            <button className="settings-x" onClick={onClose}>✕</button>
          </div>
        </div>

        {ordered.length === 0 ? (
          <div className="activity-empty">
            No activity yet. When JARVIS drives a browser or desktop task, every step it
            takes — and the screenshots it sees of the page — will appear here.
          </div>
        ) : (
          <div className="activity-list">
            {ordered.map((t) => {
              const st = STATUS[t.status] || STATUS.running;
              return (
                <div key={t.id} className="activity-task">
                  <div className="activity-task-hd">
                    <span className="activity-task-ico">{t.kind === "computer" ? "🖱" : "🌐"}</span>
                    <span className="activity-task-goal">{t.goal}</span>
                    <span className={`activity-pill ${st.cls}`}>{st.label}</span>
                  </div>
                  {t.summary && t.status !== "running" && (
                    <div className="activity-summary">{t.summary}</div>
                  )}
                  <div className="activity-timeline">
                    {(t.items || []).map((it, i) =>
                      it.kind === "shot" ? (
                        <img
                          key={i}
                          className="activity-shot"
                          src={it.image}
                          alt="page screenshot"
                          loading="lazy"
                          onClick={() => setZoom(it.image)}
                        />
                      ) : (
                        <div
                          key={i}
                          className={`activity-step${it.ok === false ? " activity-step--fail" : ""}`}
                        >
                          <span className="activity-step-ico">{stepIcon(it.line)}</span>
                          <span className="activity-step-txt">{it.line}</span>
                        </div>
                      )
                    )}
                    {t.status === "running" && (
                      <div className="activity-step activity-step--live">
                        <span className="activity-step-ico">⏳</span>
                        <span className="activity-step-txt">working…</span>
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}

        <div className="activity-foot">
          JARVIS still pauses for your approval before anything destructive — these are
          read-only records of what it did and saw, never a way to bypass that.
        </div>
      </div>

      {zoom && (
        <div
          className="activity-lightbox"
          onClick={(e) => { e.stopPropagation(); setZoom(null); }}
        >
          <img src={zoom} alt="screenshot" onClick={(e) => e.stopPropagation()} />
          <button
            className="activity-lightbox-x"
            onClick={(e) => { e.stopPropagation(); setZoom(null); }}
          >
            ✕
          </button>
        </div>
      )}
    </div>
  );
}
