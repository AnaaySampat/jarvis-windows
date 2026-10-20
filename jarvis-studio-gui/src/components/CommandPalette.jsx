// CommandPalette.jsx — Ctrl+K: one box that reaches every menu, setting page and
// quick action, and otherwise sends what you typed to JARVIS as a message. The
// command list is built by App (it owns the state the commands change).

import { useState, useEffect, useRef, useMemo } from "react";
import { Icon } from "../hud/icons";
import { rankCommands } from "../utils/commandSearch";

export default function CommandPalette({ commands, onClose, onAsk }) {
  const [query, setQuery] = useState("");
  const [active, setActive] = useState(0);
  const listRef = useRef(null);
  const q = query.trim();

  const results = useMemo(() => {
    const ranked = rankCommands(commands, q);
    return q ? [...ranked, { id: "__ask", label: `Ask JARVIS: “${q}”`, group: "Chat", icon: "send" }] : ranked;
  }, [commands, q]);

  // Keep the highlighted row on screen while arrowing through a long list.
  useEffect(() => {
    listRef.current?.querySelector(`[data-i="${active}"]`)?.scrollIntoView({ block: "nearest" });
  }, [active]);

  const run = (c) => {
    if (!c) return;
    onClose();
    if (c.id === "__ask") onAsk(q);
    else c.run();
  };

  const onKey = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setActive((a) => Math.min(results.length - 1, a + 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActive((a) => Math.max(0, a - 1)); }
    else if (e.key === "Home") { e.preventDefault(); setActive(0); }
    else if (e.key === "End") { e.preventDefault(); setActive(results.length - 1); }
    else if (e.key === "Enter") { e.preventDefault(); run(results[active]); }
    else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); onClose(); }
  };

  // Group headers only for the unfiltered list; a search is one ranked list.
  let lastGroup = null;
  return (
    <div className="cmdk-overlay" onMouseDown={onClose}>
      <div className="cmdk" role="dialog" aria-modal="true" aria-label="Command palette"
           onMouseDown={(e) => e.stopPropagation()}>
        <div className="cmdk-input">
          <Icon name="search" size={17} />
          <input autoFocus value={query} placeholder="Search menus, settings and actions — or ask JARVIS…"
                 aria-label="Command" aria-controls="cmdk-list" spellCheck={false}
                 aria-activedescendant={results[active] ? `cmdk-${results[active].id}` : undefined}
                 onChange={(e) => { setQuery(e.target.value); setActive(0); }} onKeyDown={onKey} />
          <kbd>Esc</kbd>
        </div>
        <div className="cmdk-list" id="cmdk-list" role="listbox" ref={listRef}>
          {results.length === 0 && <div className="cmdk-empty">Type to search, or to ask JARVIS anything.</div>}
          {results.map((c, i) => {
            const header = !q && c.group !== lastGroup ? c.group : null;
            lastGroup = c.group;
            return (
              <div key={c.id}>
                {header && <div className="cmdk-group">{header}</div>}
                <div id={`cmdk-${c.id}`} data-i={i} role="option" aria-selected={i === active}
                     className={`cmdk-item${i === active ? " is-on" : ""}${c.id === "__ask" ? " cmdk-item--ask" : ""}`}
                     onMouseMove={() => i !== active && setActive(i)} onClick={() => run(c)}>
                  <span className="cmdk-ico"><Icon name={c.icon || "chevRight"} size={16} /></span>
                  <span className="cmdk-label">{c.label}</span>
                  {c.state && <span className="cmdk-state">{c.state}</span>}
                  {q && c.id !== "__ask" && <span className="cmdk-meta">{c.group}</span>}
                  {c.keys && <span className="cmdk-keys">{c.keys.map((k) => <kbd key={k}>{k}</kbd>)}</span>}
                </div>
              </div>
            );
          })}
        </div>
        <div className="cmdk-foot">
          <span><kbd>↑</kbd><kbd>↓</kbd> move</span>
          <span><kbd>Enter</kbd> run</span>
          <span><kbd>Ctrl</kbd>+<kbd>K</kbd> anywhere</span>
        </div>
      </div>
    </div>
  );
}
