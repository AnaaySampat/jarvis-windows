// Customize.jsx — edit the home-screen HUD (accent, background, density, which
// panels show, and their order). Sends partial screen-config patches; the backend
// persists them and broadcasts the full new config back, so JARVIS and the user
// edit the same live state. Mirrors the named themes in actions/__init__.py.

import { Icon } from "../hud/icons";
import { Switch, ConfirmButton } from "./controls";

const THEMES = {
  cyan:   ["#00e5ff", "#6fe9ff", [0, 229, 255]],
  blue:   ["#3b82f6", "#93c5fd", [59, 130, 246]],
  amber:  ["#ffb648", "#ffd591", [255, 182, 72]],
  gold:   ["#ffd24a", "#ffe89a", [255, 210, 74]],
  red:    ["#ff4d57", "#ff9197", [255, 77, 87]],
  green:  ["#22e39a", "#8bf0cb", [34, 227, 154]],
  purple: ["#a872ff", "#cdaaff", [168, 114, 255]],
  magenta:["#ff5cf0", "#ffa9f6", [255, 92, 240]],
  pink:   ["#ff77c8", "#ffb3e0", [255, 119, 200]],
  orange: ["#ff8a3d", "#ffbb85", [255, 138, 61]],
  white:  ["#e8f4ff", "#ffffff", [232, 244, 255]],
};

const BACKGROUNDS = [
  { id: "grid", label: "Grid" },
  { id: "solid", label: "Glow" },
  { id: "aurora", label: "Aurora" },
  { id: "minimal", label: "Minimal" },
];
const DENSITIES = [
  { id: "normal", label: "Comfortable" },
  { id: "compact", label: "Compact" },
];
const CORES = [
  { id: "orb", label: "Orb" },
  { id: "reactor", label: "Arc reactor" },
];
const PANEL_LABELS = {
  system: "System", power: "Power", agenda: "Agenda",
  weather: "Weather", network: "Network", terminal: "Terminal",
};

export default function Customize({ screen, onClose, onPatch }) {
  const s = screen || {};
  const panels = s.panels || {};
  const order = s.order || { left: ["system", "power", "agenda"], right: ["weather", "network", "terminal"] };
  const accent = (s.accent || "#00e5ff").toLowerCase();
  const background = s.background || "grid";
  const density = s.density || "normal";
  const core = s.core || "orb";
  const move = (panel, direction) => onPatch({ move: { panel, direction } });

  return (
    <div className="settings-overlay" onClick={onClose}>
      <div className="settings-panel cz" role="dialog" aria-modal="true" aria-label="Customize the home screen"
           onClick={(e) => e.stopPropagation()}>
        <div className="cz-hd">
          <div>
            <h2>Customize</h2>
            <p>Changes apply live. You can also just tell JARVIS — “make the screen amber”, “hide the weather panel”.</p>
          </div>
          <button className="x-btn" onClick={onClose} aria-label="Close"><Icon name="close" size={16} /></button>
        </div>

        <div className="cz-body">
          <section className="cz-sec">
            <h3>Accent colour</h3>
            <div className="cz-swatches" role="radiogroup" aria-label="Accent colour">
              {Object.entries(THEMES).map(([name, [a, a2, rgb]]) => {
                const on = accent === a.toLowerCase();
                return (
                  <button key={name} role="radio" aria-checked={on} aria-label={name} title={name}
                          className={`cz-swatch ${on ? "cz-swatch--on" : ""}`} style={{ "--sw": a }}
                          onClick={() => onPatch({ accent: a, accent2: a2, rgb })}>
                    {on && <Icon name="check" size={14} stroke={2.4} />}
                  </button>
                );
              })}
            </div>
          </section>

          <div className="cz-two">
            <section className="cz-sec">
              <h3>Background</h3>
              <div className="cz-bgs" role="radiogroup" aria-label="Background">
                {BACKGROUNDS.map((bg) => (
                  <button key={bg.id} role="radio" aria-checked={background === bg.id}
                          className={`cz-bg ${background === bg.id ? "is-on" : ""}`}
                          onClick={() => onPatch({ background: bg.id })}>
                    <span className={`cz-bg-prev cz-bg-prev--${bg.id}`} aria-hidden />
                    <span className="cz-bg-l">{bg.label}</span>
                  </button>
                ))}
              </div>
            </section>
            <section className="cz-sec">
              <h3>Density</h3>
              <div className="seg" role="radiogroup" aria-label="Density">
                {DENSITIES.map((d) => (
                  <button key={d.id} role="radio" aria-checked={density === d.id}
                          className={`seg-btn${density === d.id ? " is-on" : ""}`}
                          onClick={() => onPatch({ density: d.id })}>
                    {d.label}
                  </button>
                ))}
              </div>
            </section>
          </div>

          <section className="cz-sec">
            <h3>Core</h3>
            <div className="seg" role="radiogroup" aria-label="Core">
              {CORES.map((o) => (
                <button key={o.id} role="radio" aria-checked={core === o.id}
                        className={`seg-btn${core === o.id ? " is-on" : ""}`}
                        onClick={() => onPatch({ core: o.id })}>
                  {o.label}
                </button>
              ))}
            </div>
          </section>

          <section className="cz-sec">
            <h3>Panels</h3>
            <p className="cz-note">Show or hide each panel, put them in order, or move one to the other side of the core.</p>
            <div className="cz-rails">
              {["left", "right"].map((rail) => {
                const arr = order[rail] || [];
                return (
                  <div key={rail} className="cz-railcol">
                    <div className="cz-rail-hd">{rail === "left" ? "Left of the core" : "Right of the core"}</div>
                    {arr.length === 0 && <div className="cz-rail-empty">No panels here.</div>}
                    {arr.map((key, i) => {
                      const visible = panels[key] !== false;
                      const name = PANEL_LABELS[key] || key;
                      return (
                        <div key={key} className={`cz-panel-row ${visible ? "" : "is-hidden"}`}>
                          <Switch checked={visible} label={`Show ${name}`}
                                  onChange={(on) => onPatch({ panels: { [key]: on } })} />
                          <span className="cz-panel-name">{name}</span>
                          <span className="cz-panel-ctrls">
                            <button disabled={i === 0} onClick={() => move(key, "up")}
                                    aria-label={`Move ${name} up`} title="Move up">
                              <Icon name="chevUp" size={14} stroke={2} />
                            </button>
                            <button disabled={i === arr.length - 1} onClick={() => move(key, "down")}
                                    aria-label={`Move ${name} down`} title="Move down">
                              <Icon name="chevDown" size={14} stroke={2} />
                            </button>
                            <button onClick={() => move(key, rail === "left" ? "right" : "left")}
                                    aria-label={`Move ${name} to the ${rail === "left" ? "right" : "left"}`}
                                    title={`Move to the ${rail === "left" ? "right" : "left"}`}>
                              <Icon name={rail === "left" ? "arrowRight" : "arrowLeft"} size={14} stroke={2} />
                            </button>
                          </span>
                        </div>
                      );
                    })}
                  </div>
                );
              })}
            </div>
          </section>
        </div>

        <div className="cz-foot">
          <ConfirmButton confirmLabel="Reset everything?" onConfirm={() => onPatch({ reset: true })}>
            Reset to defaults
          </ConfirmButton>
          <button className="btn btn--primary" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}
