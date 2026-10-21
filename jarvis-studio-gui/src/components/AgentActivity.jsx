// AgentActivity.jsx — a dockable panel showing what JARVIS's autopilot actually
// did: every step it took and the page screenshots it saw, newest task first,
// click a thumbnail to enlarge. This is AUDITABILITY, not control: JARVIS still
// asks before anything destructive (see the footer). Fed by the agent_* websocket
// events accumulated in useWebSocket (`agentTasks`).

import { useState, useEffect } from "react";
import { Icon } from "../hud/icons";
import { SOURCE_LABEL } from "../utils/taskSource";

// A rough verb icon for a step line ("Opened YouTube", "clicked (1,2)", …).
function stepIcon(line) {
  const s = (line || "").toLowerCase();
  if (s.startsWith("plan:")) return "route";
  if (s.startsWith("✗") || s.includes("failed") || s.includes("couldn't")) return "close";
  if (s.includes("click")) return "cursor";
  if (s.includes("type") || s.includes("typed")) return "keyboard";
  if (s.includes("scroll")) return "scroll";
  if (s.includes("search")) return "search";
  if (s.includes("navigat") || s.includes("open") || s.includes("goto") || s.includes("went")) return "globe";
  if (s.includes("wait")) return "hourglass";
  if (s.includes("back")) return "undo";
  if (s.includes("look") || s.includes("screenshot") || s.includes("see")) return "camera";
  if (s.includes("note")) return "note";
  if (s.includes("done")) return "check";
  return "chevRight";
}

const STATUS = {
  running: { label: "Running", cls: "tag--busy" },
  done:    { label: "Done", cls: "tag--ok" },
  failed:  { label: "Stopped", cls: "tag--err" },
  stopped: { label: "Stopped", cls: "tag--err" },
};
const KIND_ICON = { computer: "cursor", tv: "tv" };

export default function AgentActivity({ tasks, onClose, onClear }) {
  const [zoom, setZoom] = useState(null); // enlarged screenshot src, or null
  const ordered = [...(tasks || [])].reverse(); // newest task first
  const running = ordered.filter((t) => t.status === "running").length;

  // Esc closes the enlarged screenshot first (before App closes the whole panel).
  useEffect(() => {
    if (!zoom) return undefined;
    const onKey = (e) => { if (e.key === "Escape") { e.preventDefault(); setZoom(null); } };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [zoom]);

  return (
    <div className="settings-overlay" onClick={onClose}>
      <div className="settings-panel activity-panel" role="dialog" aria-modal="true" aria-label="Agent activity"
           onClick={(e) => e.stopPropagation()}>
        <div className="activity-hd">
          <div>
            <h2>Agent activity</h2>
            <p>
              {ordered.length === 0 ? "Nothing yet"
                : `${ordered.length} task${ordered.length === 1 ? "" : "s"}${running ? ` · ${running} running` : ""}`}
            </p>
          </div>
          <div className="activity-hdr-ctrls">
            {ordered.length > 0 && (
              <button className="btn btn--ghost btn--sm" onClick={onClear}>Clear</button>
            )}
            <button className="x-btn" onClick={onClose} aria-label="Close"><Icon name="close" size={16} /></button>
          </div>
        </div>

        {ordered.length === 0 ? (
          <div className="activity-empty">
            <Icon name="activity" size={28} />
            <div>
              When JARVIS drives a browser or desktop task, every step it takes — and the
              screenshots it sees of the page — will appear here.
            </div>
          </div>
        ) : (
          <div className="activity-list">
            {ordered.map((t, n) => {
              const st = STATUS[t.status] || STATUS.running;
              const items = t.items || [];
              const steps = items.filter((it) => it.kind !== "shot").length;
              const shots = items.length - steps;
              return (
                <details key={t.id} className="activity-task" open={n === 0 || t.status === "running" || undefined}>
                  <summary className="activity-task-hd">
                    <span className="activity-task-ico"><Icon name={KIND_ICON[t.kind] || "globe"} size={16} /></span>
                    <span className="activity-task-goal">{t.goal}</span>
                    {SOURCE_LABEL[t.source] && <span className="tag tag--off">{SOURCE_LABEL[t.source]}</span>}
                    {t.agent && <span className="tag tag--busy tag--plain">{t.agent}</span>}
                    <span className="activity-task-n">
                      {steps} step{steps === 1 ? "" : "s"}{shots ? ` · ${shots} shot${shots === 1 ? "" : "s"}` : ""}
                    </span>
                    <span className={`tag ${st.cls}`}>{st.label}</span>
                    <Icon name="chevDown" size={15} className="activity-chev" />
                  </summary>
                  {t.summary && t.status !== "running" && (
                    <div className="activity-summary">{t.summary}</div>
                  )}
                  <div className="activity-timeline">
                    {items.map((it, i) =>
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
                          <span className="activity-step-ico"><Icon name={it.ok === false ? "close" : stepIcon(it.line)} size={14} /></span>
                          <span className="activity-step-txt">{it.line}</span>
                        </div>
                      )
                    )}
                    {t.status === "running" && (
                      <div className="activity-step activity-step--live">
                        <span className="activity-step-ico"><Icon name="hourglass" size={14} /></span>
                        <span className="activity-step-txt">working…</span>
                      </div>
                    )}
                  </div>
                </details>
              );
            })}
          </div>
        )}

        <div className="activity-foot">
          <Icon name="lock" size={13} />
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
            aria-label="Close screenshot"
            onClick={(e) => { e.stopPropagation(); setZoom(null); }}
          >
            <Icon name="close" size={18} />
          </button>
        </div>
      )}
    </div>
  );
}
