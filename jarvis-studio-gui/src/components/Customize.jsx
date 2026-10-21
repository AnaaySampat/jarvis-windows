// Customize.jsx — edit the home screen: its look (a theme preset, then colours, font,
// cards, centrepiece, gauges, corners, glow, background) and its layout (density,
// which cards show, and where — drag them). Sends partial screen-config patches; the
// backend checks and persists them and broadcasts the full new config back, so JARVIS
// and the user edit the same live state. Mirrors the named themes in actions/__init__.py.

import { useState, useEffect, useRef } from "react";
import { Icon } from "../hud/icons";
import { Switch, ConfirmButton, AddField } from "./controls";
import { dragCard, useCardDrop } from "../hud/widgets";

const THEMES = {
  cyan:   ["#00e5ff", "#6fe9ff"],
  blue:   ["#3b82f6", "#93c5fd"],
  amber:  ["#ffb648", "#ffd591"],
  gold:   ["#ffd24a", "#ffe89a"],
  red:    ["#ff4d57", "#ff9197"],
  green:  ["#22e39a", "#8bf0cb"],
  purple: ["#a872ff", "#cdaaff"],
  magenta:["#ff5cf0", "#ffa9f6"],
  pink:   ["#ff77c8", "#ffb3e0"],
  orange: ["#ff8a3d", "#ffbb85"],
  white:  ["#e8f4ff", "#ffffff"],
};

// The built-in presets (llm/groq_bridge.py SCREEN_PRESETS): they keep your colours.
const BUILT_IN = [
  { id: "modern", label: "Modern", look: { font: "clean", corners: 14, glow: 40, cards: "clean", core: "orb", gauges: "bars" } },
  { id: "ironman", label: "Iron Man", look: { font: "hud", corners: 3, glow: 100, cards: "hud", core: "reactor", gauges: "rings" } },
];
const LOOK_KEYS = ["accent", "accent2", "background", "image", "font", "corners", "glow", "cards", "core", "gauges"];

const CHOICES = [
  { key: "font", label: "Font", options: [["clean", "Clean"], ["hud", "HUD"]] },
  { key: "cards", label: "Cards", options: [["clean", "Clean"], ["hud", "HUD"]] },
  { key: "core", label: "Centrepiece", options: [["orb", "Orb"], ["reactor", "Arc reactor"]] },
  { key: "gauges", label: "Gauges", options: [["bars", "Bars"], ["rings", "Rings"]] },
];
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
const PANEL_LABELS = {
  system: "System", power: "Power", agenda: "Agenda",
  weather: "Weather", network: "Network", terminal: "Terminal",
};
const ARROWS = { ArrowUp: "up", ArrowDown: "down", ArrowLeft: "left", ArrowRight: "right" };

function Seg({ label, value, options, onPick }) {
  return (
    <div className="seg" role="radiogroup" aria-label={label}>
      {options.map(([id, text]) => (
        <button key={id} role="radio" aria-checked={value === id}
                className={`seg-btn${value === id ? " is-on" : ""}`} onClick={() => onPick(id)}>
          {text}
        </button>
      ))}
    </div>
  );
}

/* Sliders and colour pickers fire on every move: show the value at once, send it
   at most every 120 ms, and drop the local copy once the backend's echo is in. */
function useLivePatch(screen, onPatch) {
  const [draft, setDraft] = useState({});
  const pending = useRef(null);
  useEffect(() => { if (!pending.current) setDraft({}); }, [screen]);
  const set = (patch) => {
    setDraft((d) => ({ ...d, ...patch }));
    if (pending.current) { Object.assign(pending.current, patch); return; }
    pending.current = { ...patch };
    setTimeout(() => { const p = pending.current; pending.current = null; onPatch(p); }, 120);
  };
  return [{ ...(screen || {}), ...draft }, set];
}

/* A picture for the background, shrunk to ≤1920px JPEG so it stays well under
   the backend's 4 MB limit and the WebSocket's frame size. */
async function shrinkImage(file) {
  const bmp = await createImageBitmap(file);
  const k = Math.min(1, 1920 / Math.max(bmp.width, bmp.height));
  const c = document.createElement("canvas");
  c.width = Math.round(bmp.width * k);
  c.height = Math.round(bmp.height * k);
  c.getContext("2d").drawImage(bmp, 0, 0, c.width, c.height);
  return c.toDataURL("image/jpeg", 0.85);
}

function saveThemeFile(preset) {
  const url = URL.createObjectURL(new Blob([JSON.stringify({ jarvis_theme: 1, ...preset }, null, 2)],
                                           { type: "application/json" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = `${preset.name.replace(/[^\w -]+/g, "").trim() || "theme"}.jarvis-theme.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export default function Customize({ screen, onClose, onPatch }) {
  const [s, live] = useLivePatch(screen, onPatch);
  const [error, setError] = useState("");
  const picture = useRef(null);
  const themeFile = useRef(null);
  const panels = s.panels || {};
  const order = s.order || { left: ["system", "power", "agenda"], right: ["weather", "network", "terminal"] };
  const accent = (s.accent || "#00e5ff").toLowerCase();
  const background = s.background || "grid";
  const saved = s.presets || [];
  const [drop, dropZone] = useCardDrop(order, (o) => onPatch({ order: o }));
  const isOn = (look) => Object.keys(look).every((k) => k === "name" || k === "rgb" || s[k] === look[k]);

  const choosePicture = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      onPatch({ background: "image", image: await shrinkImage(file) });
      setError("");
    } catch {
      setError("That picture couldn't be opened — try a JPEG or PNG.");
    }
  };
  const importTheme = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    try {
      const t = JSON.parse(await file.text());
      if (!t || typeof t !== "object" || !LOOK_KEYS.some((k) => k in t)) throw new Error("not a theme");
      onPatch({ import_preset: { ...t, name: t.name || file.name.replace(/(\.jarvis-theme)?\.json$/i, "") } });
      setError("");
    } catch {
      setError("That file isn't a JARVIS theme.");
    }
  };

  return (
    <div className="settings-overlay" onClick={onClose}>
      <div className="settings-panel cz" role="dialog" aria-modal="true" aria-label="Customize the home screen"
           onClick={(e) => e.stopPropagation()}>
        <div className="cz-hd">
          <div>
            <h2>Customize</h2>
            <p>Changes apply live. You can also just tell JARVIS — “make the screen amber”, or “make my home screen look like a submarine sonar”.</p>
          </div>
          <button className="x-btn" onClick={onClose} aria-label="Close"><Icon name="close" size={16} /></button>
        </div>

        <div className="cz-body">
          <section className="cz-sec">
            <h3>Theme</h3>
            <p className="cz-note">Start from one, change anything below, then save it as your own. Saved themes go to a file to share.</p>
            <div className="cz-presets">
              {BUILT_IN.map((p) => (
                <button key={p.id} className={`cz-preset${isOn(p.look) ? " is-on" : ""}`}
                        onClick={() => onPatch({ style: p.id })}>
                  {p.label}
                </button>
              ))}
              {saved.map((p) => (
                <span key={p.name} className={`cz-preset cz-preset--mine${isOn(p) ? " is-on" : ""}`}>
                  <button className="cz-preset-name" onClick={() => onPatch({ preset: p.name })}
                          style={{ "--sw": p.accent }}>
                    {p.name}
                  </button>
                  <button className="cz-preset-act" onClick={() => saveThemeFile(p)}
                          aria-label={`Save ${p.name} to a file`} title="Save to a file">
                    <Icon name="download" size={13} />
                  </button>
                  <ConfirmButton className="cz-preset-act" confirmLabel="Delete?" title="Delete"
                                 onConfirm={() => onPatch({ delete_preset: p.name })}>
                    <Icon name="trash" size={13} />
                  </ConfirmButton>
                </span>
              ))}
            </div>
            <div className="cz-preset-bar">
              <AddField placeholder="Name this look to save it" button="Save" mono={false} label="Theme name"
                        onAdd={(name) => onPatch({ save_preset: name })} />
              <button className="btn btn--ghost" onClick={() => themeFile.current?.click()}>
                <Icon name="upload" size={14} />Import…
              </button>
              <input ref={themeFile} type="file" accept=".json,application/json" hidden onChange={importTheme} />
            </div>
            {error && <div className="cz-err" role="alert">{error}</div>}
          </section>

          <section className="cz-sec">
            <h3>Colours</h3>
            <div className="cz-colors">
              <div className="cz-swatches" role="radiogroup" aria-label="Accent colour">
                {Object.entries(THEMES).map(([name, [a, a2]]) => {
                  const on = accent === a;
                  return (
                    <button key={name} role="radio" aria-checked={on} aria-label={name} title={name}
                            className={`cz-swatch ${on ? "cz-swatch--on" : ""}`} style={{ "--sw": a }}
                            onClick={() => onPatch({ accent: a, accent2: a2 })}>
                      {on && <Icon name="check" size={14} stroke={2.4} />}
                    </button>
                  );
                })}
              </div>
              <span className="cz-picks">
                <label className="cz-pick" title="Any colour">
                  <input type="color" value={accent} onChange={(e) => live({ accent: e.target.value })} />
                  Main
                </label>
                <label className="cz-pick" title="Follows the main colour until you pick one">
                  <input type="color" value={(s.accent2 || "#6fe9ff").toLowerCase()}
                         onChange={(e) => live({ accent2: e.target.value })} />
                  Second
                </label>
              </span>
            </div>
          </section>

          <section className="cz-sec">
            <h3>Look</h3>
            <div className="cz-look">
              {CHOICES.map((c) => (
                <div key={c.key} className="cz-look-row">
                  <span className="cz-look-l">{c.label}</span>
                  <Seg label={c.label} value={s[c.key]} options={c.options}
                       onPick={(v) => onPatch({ [c.key]: v })} />
                </div>
              ))}
              {[["corners", "Corners", 24, "px"], ["glow", "Glow", 100, "%"]].map(([k, label, max, unit]) => (
                <label key={k} className="cz-look-row">
                  <span className="cz-look-l">{label}</span>
                  <input type="range" className="cz-range" min={0} max={max} value={s[k] ?? 0}
                         onChange={(e) => live({ [k]: Number(e.target.value) })} />
                  <span className="cz-look-v">{s[k] ?? 0}{unit}</span>
                </label>
              ))}
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
                <button role="radio" aria-checked={background === "image"}
                        className={`cz-bg ${background === "image" ? "is-on" : ""}`}
                        onClick={() => (s.image ? onPatch({ background: "image" }) : picture.current?.click())}>
                  <span className="cz-bg-prev cz-bg-prev--image" aria-hidden
                        style={s.image ? { backgroundImage: `url("${s.image}")` } : undefined}>
                    {!s.image && <Icon name="image" size={18} />}
                  </span>
                  <span className="cz-bg-l">Picture</span>
                </button>
              </div>
              {background === "image" && (
                <button className="btn btn--ghost btn--sm" onClick={() => picture.current?.click()}>
                  <Icon name="image" size={13} />Choose another picture…
                </button>
              )}
              <input ref={picture} type="file" accept="image/*" hidden onChange={choosePicture} />
            </section>
            <section className="cz-sec">
              <h3>Density</h3>
              <Seg label="Density" value={s.density || "normal"} onPick={(v) => onPatch({ density: v })}
                   options={DENSITIES.map((d) => [d.id, d.label])} />
            </section>
          </div>

          <section className="cz-sec">
            <h3>Cards</h3>
            <p className="cz-note">Show or hide each card. Drag one — here or by its title on the home screen — to reorder it or move it to the other side (or focus it and press Alt + an arrow key).</p>
            <div className="cz-rails">
              {["left", "right"].map((rail) => {
                const arr = order[rail] || [];
                const line = <div key="drop" className="card-drop" aria-hidden="true" />;
                return (
                  <div key={rail} className={`cz-railcol${drop?.side === rail ? " is-drop" : ""}`} {...dropZone(rail)}>
                    <div className="cz-rail-hd">{rail === "left" ? "Left of the core" : "Right of the core"}</div>
                    {arr.length === 0 && !drop && <div className="cz-rail-empty">Drop a card here.</div>}
                    {arr.flatMap((key) => {
                      const visible = panels[key] !== false;
                      const name = PANEL_LABELS[key] || key;
                      return [
                        drop?.side === rail && drop.before === key && line,
                        <div key={key} className={`cz-panel-row ${visible ? "" : "is-hidden"}`} data-panel={key}
                             draggable tabIndex={0} onDragStart={(e) => dragCard(e, key)}
                             aria-label={`${name}: drag, or Alt + arrow keys, to move`}
                             onKeyDown={(e) => {
                               if (!e.altKey || !ARROWS[e.key] || e.target !== e.currentTarget) return;
                               e.preventDefault();
                               onPatch({ move: { panel: key, direction: ARROWS[e.key] } });
                             }}>
                          <Icon name="grip" size={16} stroke={2.6} className="cz-grip" />
                          <Switch checked={visible} label={`Show ${name}`}
                                  onChange={(on) => onPatch({ panels: { [key]: on } })} />
                          <span className="cz-panel-name">{name}</span>
                        </div>,
                      ];
                    })}
                    {drop?.side === rail && !arr.includes(drop.before) && line}
                  </div>
                );
              })}
            </div>
          </section>
        </div>

        <div className="cz-foot">
          <ConfirmButton confirmLabel="Reset everything?" onConfirm={() => onPatch({ reset: true })}
                         title="Back to the default look and layout (your saved themes stay)">
            Reset to defaults
          </ConfirmButton>
          <button className="btn btn--primary" onClick={onClose}>Done</button>
        </div>
      </div>
    </div>
  );
}
