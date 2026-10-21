/* widgets.jsx — the home screen's cards: System, Power, Agenda, Weather, Network and
   the command log. Each one collapses to a one-line summary in its header (JARVIS
   can expand/collapse them by voice too: App owns the open state), and is dragged
   by that header to rearrange them. */

import { useState, useEffect, useRef, useId } from "react";
import { Icon } from "./icons";

/* Sensible idle defaults so the cards look right before the first telemetry
   frame arrives over the socket. */
export const DEFAULT_TELEMETRY = {
  cpu: 0, ram: 0, disk: 0, diskActivity: 0, gpu: 0, net: 0,
  down: 0, up: 0, ping: 0, temp: null,
  batteryPct: null, charging: false, remaining: "",
  ramTotalGb: 16, diskUsedGb: 0, diskTotalGb: 0, gpuName: "",
};

// Coerce a telemetry field to a finite number (→0): a partial frame missing a
// field must not render "NaN" or crash a card.
const n0 = (x) => (Number.isFinite(x) ? x : (Number.isFinite(+x) ? +x : 0));
const pct = (x) => `${Math.round(n0(x))}%`;

/* Rolling history of a live value, for the sparklines. `value` is null until the
   first telemetry frame; that first reading fills the line, so it starts level
   instead of climbing from a made-up zero. */
function useHistory(value, points = 40) {
  const [hist, setHist] = useState(null);
  useEffect(() => {
    if (value == null) return;
    const v = Number.isFinite(value) ? value : 0;
    setHist((h) => (h ? [...h.slice(1), v] : Array(points).fill(v)));
  }, [value, points]);
  return hist || Array(points).fill(0);
}

/* ───────── arranging the cards ───────── */
/* A card is dragged by its header (here) or its row in Customize, and dropped on
   either column. The columns know only the order; `[data-panel]` marks each card. */
const CARD_MIME = "text/x-jarvis-card";

export function dragCard(e, key) {
  e.dataTransfer.setData(CARD_MIME, key);
  e.dataTransfer.effectAllowed = "move";
  const card = e.currentTarget.closest("[data-panel]");
  const r = card.getBoundingClientRect();
  e.dataTransfer.setDragImage(card, e.clientX - r.left, e.clientY - r.top);
}

/* `order` with card `key` moved to `side`, just before card `before` (or last). */
export function placeCard(order, key, side, before) {
  if (before === key) return order;
  const next = {
    left: (order.left || []).filter((k) => k !== key),
    right: (order.right || []).filter((k) => k !== key),
  };
  const i = before ? next[side].indexOf(before) : -1;
  next[side].splice(i < 0 ? next[side].length : i, 0, key);
  return next;
}

/* Drop handlers for a column, and where a dragged card would land ({side, before}). */
export function useCardDrop(order, onArrange) {
  const [drop, setDrop] = useState(null);
  const beforeAt = (e) => [...e.currentTarget.querySelectorAll("[data-panel]")].find((el) => {
    const r = el.getBoundingClientRect();
    return e.clientY < r.top + r.height / 2;
  })?.dataset.panel || null;
  const zone = (side) => ({
    onDragOver: (e) => {
      if (!e.dataTransfer.types.includes(CARD_MIME)) return;
      e.preventDefault();
      const before = beforeAt(e);
      if (drop?.side !== side || drop?.before !== before) setDrop({ side, before });
    },
    onDragLeave: (e) => { if (!e.currentTarget.contains(e.relatedTarget)) setDrop(null); },
    onDrop: (e) => {
      const key = e.dataTransfer.getData(CARD_MIME);
      if (!key) return;
      e.preventDefault();
      setDrop(null);
      onArrange(placeCard(order, key, side, beforeAt(e)));
    },
  });
  return [drop, zone];
}

/* ───────── building blocks ───────── */
export function Widget({ id, icon, title, meta, summary, open, onToggle, children }) {
  return (
    <section className={`wd${open ? "" : " wd--closed"}`} data-panel={id}>
      <button type="button" className="wd-hd" onClick={() => onToggle && onToggle(!open)}
              draggable onDragStart={(e) => dragCard(e, id)}
              aria-expanded={open} title={open ? "Collapse (drag to move)" : "Expand (drag to move)"}>
        <span className="wd-ico"><Icon name={icon} size={15} /></span>
        <span className="wd-title">{title}</span>
        <span className="wd-meta">{open ? meta : summary}</span>
        <Icon name="chevDown" size={14} className="wd-chev" />
      </button>
      {open && <div className="wd-body">{children}</div>}
    </section>
  );
}

function Bar({ value, tone }) {
  const v = Math.max(0, Math.min(100, n0(value)));
  return (
    <div className="bar" role="presentation">
      <span className={tone ? `bar--${tone}` : ""} style={{ width: `${v}%` }} />
    </div>
  );
}

const loadTone = (v) => (n0(v) >= 90 ? "err" : n0(v) >= 75 ? "warn" : "");

/* A sparkline of a real series, auto-scaled so small-but-real activity shows. */
function Spark({ data, height = 34 }) {
  const id = useId();
  const series = data.map((p) => (Number.isFinite(p) ? Math.max(0, p) : 0));
  const max = Math.max(1, ...series);
  const w = 100;
  const pts = series.map((p, i) => `${(i / Math.max(1, series.length - 1)) * w},${height - (p / max) * (height - 2) - 1}`);
  return (
    <svg className="spark2" viewBox={`0 0 ${w} ${height}`} preserveAspectRatio="none" height={height} aria-hidden="true">
      <defs>
        <linearGradient id={id} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="var(--ac)" stopOpacity="0.28" />
          <stop offset="1" stopColor="var(--ac)" stopOpacity="0" />
        </linearGradient>
      </defs>
      <polygon points={`0,${height} ${pts.join(" ")} ${w},${height}`} fill={`url(#${id})`} />
      <polyline points={pts.join(" ")} fill="none" stroke="var(--ac)" strokeWidth="1.5"
                vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  );
}

/* Gauges ▸ Rings: a 270° ring gauge with tick marks and the value in the middle. */
function Ring({ value, label, sub, size = 104, stroke = 7 }) {
  const v = Math.max(0, Math.min(100, n0(value)));
  const r = size / 2 - stroke - 5, c = 2 * Math.PI * r, arc = c * 0.75, mid = size / 2;
  return (
    <div className="ring" style={{ width: size }}>
      <svg viewBox={`0 0 ${size} ${size}`} width={size} height={size} aria-hidden="true">
        <g transform={`rotate(135 ${mid} ${mid})`}>
          <circle cx={mid} cy={mid} r={r} className="ring-track" strokeWidth={stroke}
                  strokeDasharray={`${arc} ${c}`} />
          <circle cx={mid} cy={mid} r={r} className="ring-fill" strokeWidth={stroke}
                  strokeDasharray={`${(v / 100) * arc} ${c}`} />
        </g>
        {Array.from({ length: 11 }).map((_, i) => {
          const a = ((135 + i * 27) * Math.PI) / 180, r1 = r + stroke / 2 + 2, r2 = r1 + (i % 5 ? 3 : 5);
          return <line key={i} className="ring-tick" x1={mid + Math.cos(a) * r1} y1={mid + Math.sin(a) * r1}
                       x2={mid + Math.cos(a) * r2} y2={mid + Math.sin(a) * r2} />;
        })}
      </svg>
      <div className="ring-c"><b>{Math.round(v)}<small>%</small></b>{sub && <span>{sub}</span>}</div>
      {label && <div className="ring-l">{label}</div>}
    </div>
  );
}

/* Gauges ▸ Rings: an LED segment strip for the battery. */
function Leds({ value, segs = 20 }) {
  const lit = Math.round((Math.max(0, Math.min(100, n0(value))) / 100) * segs);
  return (
    <div className="leds" aria-hidden="true">
      {Array.from({ length: segs }).map((_, i) => <span key={i} className={i < lit ? "on" : ""} />)}
    </div>
  );
}

function Stat({ label, value, sub, bar, tone, title }) {
  return (
    <div className="stat" title={title}>
      <div className="stat-l">{label}</div>
      <div className="stat-v">{value}</div>
      {bar != null && <Bar value={bar} tone={tone} />}
      {sub && <div className="stat-s">{sub}</div>}
    </div>
  );
}

function KV({ k, v, mono }) {
  return (
    <div className="kv2-row">
      <span>{k}</span>
      <span className={mono ? "kv2-mono" : ""} title={typeof v === "string" ? v : undefined}>{v}</span>
    </div>
  );
}

/* ───────── System ───────── */
export function SystemWidget({ d, live, open, onToggle, rings }) {
  const cpuHist = useHistory(live ? n0(d.cpu) : null);
  const total = n0(d.ramTotalGb);
  const used = (n0(d.ram) / 100) * total;
  return (
    <Widget id="system" icon="cpu" title="System" open={open} onToggle={onToggle}
            meta={d.temp != null ? `${Math.round(n0(d.temp))}°C` : null}
            summary={`CPU ${pct(d.cpu)} · RAM ${pct(d.ram)}`}>
      {rings ? (<>
        <div className="rings">
          <Ring value={d.cpu} label="CPU" sub={d.temp != null ? `${Math.round(n0(d.temp))}°C` : null} />
          <Ring value={d.ram} label="Memory" sub={total ? `${Math.round(total)} GB` : null} />
        </div>
        <div className="rings rings--mini">
          <Ring value={d.gpu} label="GPU" size={70} stroke={5} />
          <Ring value={d.diskActivity} label="Disk I/O" size={70} stroke={5} />
          <Ring value={d.disk} label="Storage" size={70} stroke={5} />
        </div>
      </>) : (<>
      <div className="stat-pair">
        <Stat label="CPU" value={pct(d.cpu)} bar={d.cpu} tone={loadTone(d.cpu)}
              sub={d.temp != null ? `${Math.round(n0(d.temp))}°C` : "Processor load"} />
        <Stat label="Memory" value={pct(d.ram)} bar={d.ram} tone={loadTone(d.ram)}
              sub={total ? `${used.toFixed(1)} of ${Math.round(total)} GB` : "In use"} />
      </div>
      <Spark data={cpuHist} />
      <div className="mini3">
        <div title={d.gpuName || "Graphics"}><span>GPU</span><b>{d.gpu != null ? pct(d.gpu) : "—"}</b></div>
        <div title="How busy the disk is"><span>Disk I/O</span><b>{pct(d.diskActivity)}</b></div>
        <div title={d.diskTotalGb ? `${Math.round(n0(d.diskUsedGb))} of ${Math.round(n0(d.diskTotalGb))} GB used` : "Storage used"}>
          <span>Storage</span><b>{pct(d.disk)}</b>
        </div>
      </div>
      </>)}
    </Widget>
  );
}

/* ───────── Power ───────── */
export function PowerWidget({ d, open, onToggle, rings }) {
  const level = d.batteryPct;
  const has = level != null;
  const state = !has ? "Plugged in" : d.charging ? "Charging" : "On battery";
  const sub = !has ? "No battery detected"
    : d.charging ? "Connected to power"
    : d.remaining && d.remaining !== "On AC" ? `${d.remaining} left` : "Estimating time left…";
  const tone = has && !d.charging ? (level < 15 ? "err" : level < 30 ? "warn" : "") : "";
  return (
    <Widget id="power" icon={d.charging || !has ? "charging" : "battery"} title="Power" open={open} onToggle={onToggle}
            meta={state} summary={has ? `${Math.round(level)}% · ${state.toLowerCase()}` : "Plugged in"}>
      <div className="pwr2">
        {rings && has ? <Ring value={level} size={88} stroke={6} />
          : <span className="pwr2-v">{has ? `${Math.round(level)}%` : "AC"}</span>}
        <span className="pwr2-txt"><b>{state}</b><span>{sub}</span></span>
      </div>
      {has && (rings ? <Leds value={level} /> : <Bar value={level} tone={tone} />)}
    </Widget>
  );
}

/* ───────── Agenda (editable) ───────── */
// Add / edit an agenda item in place.
function AgendaForm({ initial, onSave, onCancel }) {
  const [task, setTask] = useState(initial?.task || "");
  const [time, setTime] = useState(/^\d{1,2}:\d{2}$/.test(initial?.time || "") ? initial.time.padStart(5, "0") : "");
  const submit = (e) => {
    e.preventDefault();
    if (task.trim()) onSave({ task: task.trim(), time });
  };
  return (
    <form className="ag-form" onSubmit={submit}
          onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); onCancel(); } }}>
      <input type="time" className="ag-form-time" value={time} onChange={(e) => setTime(e.target.value)}
             aria-label="Time (optional)" />
      <input className="ag-form-task" value={task} onChange={(e) => setTask(e.target.value)} autoFocus
             placeholder={initial ? "Task" : "What is it?"} aria-label="Task" maxLength={120} />
      <button type="submit" className="ag-btn ag-btn--ok" disabled={!task.trim()}
              aria-label={initial ? "Save" : "Add"} title={initial ? "Save" : "Add"}>
        <Icon name="check" size={15} stroke={2} />
      </button>
      <button type="button" className="ag-btn" onClick={onCancel} aria-label="Cancel" title="Cancel">
        <Icon name="close" size={14} stroke={2} />
      </button>
    </form>
  );
}

export function AgendaWidget({ items, open, onToggle, runAction }) {
  const list = items || [];
  const editable = typeof runAction === "function";
  const [editing, setEditing] = useState(null);   // null | "new" | index of the row being edited
  const next = list.find((it) => it.now) || list.find((it) => !it.done);

  const addItem = ({ task, time }) => {
    runAction({ type: "schedule", do: "add", day: "today", time, task });
    setEditing(null);
  };
  const editItem = (it, { task, time }) => {
    runAction({
      type: "schedule", do: "edit", day: "today",
      match: it.task, new_task: task || it.task, new_time: time || it.time,
    });
    setEditing(null);
  };
  const removeItem = (it) =>
    runAction({ type: "schedule", do: "remove", day: "today", match: it.task });

  return (
    <Widget id="agenda" icon="calendar" title="Agenda" open={open} onToggle={onToggle}
            meta={list.length ? `${list.length} item${list.length === 1 ? "" : "s"}` : null}
            summary={next ? `${next.time ? `${next.time} · ` : ""}${next.task}` : "Nothing scheduled"}>
      <div className="ag">
        {list.length === 0 && editing !== "new" && (
          <div className="wd-empty">Nothing scheduled today.</div>
        )}
        {list.map((it, i) => (editing === i ? (
          <AgendaForm key={i} initial={it} onSave={(v) => editItem(it, v)} onCancel={() => setEditing(null)} />
        ) : (
          <div key={i} className={`ag-row${it.now ? " is-now" : ""}${it.done ? " is-done" : ""}`}>
            <span className="ag-t">{it.time || "—"}</span>
            <span className="ag-dot" aria-hidden />
            <span className="ag-task">
              {it.task}
              {it.now && <span className="ag-now">Now</span>}
            </span>
            {it.duration && <span className="ag-dur">{it.duration}</span>}
            {editable && (
              <span className="ag-edit">
                <button title="Edit" aria-label={`Edit ${it.task}`} onClick={() => setEditing(i)}>
                  <Icon name="edit" size={13} />
                </button>
                <button title="Remove" aria-label={`Remove ${it.task}`} onClick={() => removeItem(it)}>
                  <Icon name="trash" size={13} />
                </button>
              </span>
            )}
          </div>
        )))}
        {editing === "new" && <AgendaForm onSave={addItem} onCancel={() => setEditing(null)} />}
      </div>
      {editable && editing !== "new" && (
        <button className="wd-add" onClick={() => setEditing("new")}>
          <Icon name="plus" size={14} />Add to today
        </button>
      )}
    </Widget>
  );
}

/* ───────── Weather ───────── */
// The backend's condition glyphs (weather.py _WMO) → icons.
const WX_GLYPH = { "○": "sun", "◔": "cloudSun", "◑": "cloudSun", "☁": "cloud", "▒": "fog",
                   "˙": "drizzle", "☂": "rain", "❄": "snow", "⚡": "storm",
                   // emoji, in case a source sends those instead
                   "☀": "sun", "☀️": "sun", "⛅": "cloudSun", "🌤": "cloudSun", "🌥": "cloud",
                   "🌧": "rain", "🌦": "rain", "⛈": "storm", "🌩": "storm", "🌨": "snow", "🌫": "fog" };
function wxIcon(glyph, condition = "") {
  if (WX_GLYPH[glyph]) return WX_GLYPH[glyph];
  const c = condition.toLowerCase();
  if (/thunder/.test(c)) return "storm";
  if (/snow/.test(c)) return "snow";
  if (/rain|shower/.test(c)) return "rain";
  if (/drizzle/.test(c)) return "drizzle";
  if (/fog/.test(c)) return "fog";
  if (/partly|mainly/.test(c)) return "cloudSun";
  if (/cloud|overcast/.test(c)) return "cloud";
  return "sun";
}
const aqiTone = (a) => (a <= 50 ? "Good" : a <= 100 ? "Moderate" : a <= 150 ? "Unhealthy for some" : "Unhealthy");

export function WeatherWidget({ weather, open, onToggle }) {
  const w = weather || {};
  const temp = w.temp != null ? `${Math.round(n0(w.temp))}°` : "—";
  const hours = (w.hours || []).slice(0, 6);
  const icon = wxIcon(hours[0]?.i, w.condition || "");
  return (
    <Widget id="weather" icon={icon} title="Weather" open={open} onToggle={onToggle}
            meta={w.location && w.location !== "—" ? w.location.split(",")[0] : null}
            summary={w.temp != null ? `${temp} ${w.condition || ""}` : "Locating…"}>
      {!weather ? (
        <div className="wd-empty">Finding your location…</div>
      ) : (<>
        <div className="wx2">
          <Icon name={icon} size={38} stroke={1.3} className="wx2-ico" />
          <span className="wx2-temp">{temp}</span>
          <span className="wx2-txt">
            <b>{w.condition || "—"}</b>
            <span title={w.location}>{w.location || "—"}</span>
          </span>
        </div>
        <div className="wx2-facts">
          <span title="Humidity"><Icon name="droplet" size={14} />{w.humidity != null ? `${Math.round(n0(w.humidity))}%` : "—"}</span>
          <span title="Wind"><Icon name="wind" size={14} />{w.wind || "—"}</span>
          {w.aqi != null && <span title={`Air quality: ${aqiTone(n0(w.aqi))}`}><Icon name="leaf" size={14} />AQI {w.aqi}</span>}
        </div>
        {hours.length > 0 && (
          <div className="wx2-hours">
            {hours.map((h, i) => (
              <div key={i} className="wx2-h">
                <span>{h.t === "NOW" ? "Now" : `${h.t}:00`}</span>
                <Icon name={wxIcon(h.i)} size={17} stroke={1.4} />
                <b>{h.c != null ? `${Math.round(n0(h.c))}°` : "—"}</b>
              </div>
            ))}
          </div>
        )}
      </>)}
    </Widget>
  );
}

/* ───────── Network ───────── */
// 1 decimal under 10 Mbps so idle traffic is still visible; whole numbers once busy.
const mbps = (v) => (v == null ? "0.0" : v < 10 ? n0(v).toFixed(1) : n0(v).toFixed(0));

export function NetworkWidget({ d, live, netInfo, open, onToggle }) {
  const n = netInfo || {};
  const downHist = useHistory(live ? n0(d.down) : null);
  return (
    <Widget id="network" icon="wifi" title="Network" open={open} onToggle={onToggle}
            meta={d.ping ? `${d.ping} ms` : null}
            summary={`↓ ${mbps(d.down)} · ↑ ${mbps(d.up)} Mbps`}>
      <div className="net2">
        <div><Icon name="arrowDown" size={15} /><b>{mbps(d.down)}</b><span>Mbps down</span></div>
        <div><Icon name="arrowUp" size={15} /><b>{mbps(d.up)}</b><span>Mbps up</span></div>
      </div>
      <Spark data={downHist} />
      <div className="kv2">
        <KV k="Ping" v={d.ping ? `${d.ping} ms` : "—"} />
        <KV k="Local IP" v={n.localIp || "—"} mono />
        <KV k="Public IP" v={n.publicIp || "—"} mono />
        <KV k="Location" v={n.location || "—"} />
      </div>
    </Widget>
  );
}

/* ───────── Command log (read-only) ───────── */
export function LogWidget({ commands = [], open, onToggle }) {
  const bodyRef = useRef(null);
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [commands, open]);
  const fmtTime = (ts) => {
    try { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
    catch { return ""; }
  };
  const last = commands[commands.length - 1];
  return (
    <Widget id="terminal" icon="terminal" title="Command log" open={open} onToggle={onToggle}
            meta={commands.length ? `${commands.length}` : null}
            summary={!last ? "Nothing run yet" : last.ok === false ? `${last.type} failed` : last.message || last.type}>
      <div className="log" ref={bodyRef}>
        {commands.length === 0 && <div className="wd-empty">Actions JARVIS runs show up here.</div>}
        {commands.slice(-40).map((c, i) => (
          <div key={i} className={`log-row${c.ok === false ? " is-err" : ""}`}>
            <span className="log-ico"><Icon name={c.ok === false ? "close" : "check"} size={12} stroke={2} /></span>
            <span className="log-txt">
              <code>{c.type}{c.target ? ` ${c.target}` : ""}</code>
              {c.message && <span>{c.message}</span>}
            </span>
            <span className="log-t">{fmtTime(c.ts)}</span>
          </div>
        ))}
      </div>
    </Widget>
  );
}
