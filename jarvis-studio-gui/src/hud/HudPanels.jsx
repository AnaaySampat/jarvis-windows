/* HudPanels.jsx — all the ringing data panels + chat & OCR overlays.
   Ported from jarvis-hud/hud-panels.jsx and wired to LIVE backend data
   (telemetry / weather / netinfo / schedule / messages) passed in as props.
   Exports each panel + STATUS_META + useClock  */

import { useState, useEffect, useRef, useCallback, memo } from "react";
import { CornerBox, RadialGauge, MiniRadial, BarMeter, SegBar, Sparkline, TickStrip, DataRow } from "./HudGauges";
import { Waveform } from "./HudCore";
import ResponseRenderer from "../components/ResponseRenderer";
import { Icon } from "./icons";
import { ChipList, AddField } from "../components/controls";

/* Sensible idle defaults so the HUD looks alive before the first telemetry
   frame arrives over the socket. */
export const DEFAULT_TELEMETRY = {
  cpu: 0, ram: 0, disk: 0, diskActivity: 0, gpu: 0, net: 0,
  down: 0, up: 0, ping: 0, temp: null,
  batteryPct: null, charging: false, remaining: "",
  ramTotalGb: 16, diskUsedGb: 0, diskTotalGb: 0, gpuName: "",
};

// Coerce a telemetry field to a finite number (→0). A partial frame missing
// cpu/ram/disk would otherwise blow up `d.cpu.toFixed(...)` and crash the panel
// — the gpu/net reads already use `?? 0`, so this just makes the rest consistent.
const n0 = (x) => (Number.isFinite(x) ? x : (Number.isFinite(+x) ? +x : 0));

/* ───────── status labels (idle→listening→thinking→speaking) ───────── */
export const STATUS_META = {
  idle:      { label: 'STANDBY — say "Hey Jarvis"', col: "#00c8ff" },
  listening: { label: "LISTENING…", col: "#00e5ff" },
  thinking:  { label: "PROCESSING REQUEST…", col: "#78b4ff" },
  speaking:  { label: "RESPONDING…", col: "#ffb648" },
  working:   { label: "EXECUTING TASK…", col: "#22e39a" },
};

/* ───────── live clock ───────── */
export function useClock() {
  const [now, setNow] = useState(new Date());
  useEffect(() => { const id = setInterval(() => setNow(new Date()), 1000); return () => clearInterval(id); }, []);
  return now;
}

/* ───────── rolling history of a live value (for real sparklines) ───────── */
function useHistory(value, points = 46) {
  const [hist, setHist] = useState(() => Array(points).fill(0));
  useEffect(() => {
    setHist((h) => [...h.slice(1), Number.isFinite(value) ? value : 0]);
  }, [value]);
  return hist;
}

export function ClockPanel({ rgb }) {
  const now = useClock();
  const hh = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false });
  const ss = now.toLocaleTimeString([], { second: "2-digit" }).replace(/\D/g, "").padStart(2, "0");
  const date = now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  const yr = now.getFullYear();
  return (
    <div className="clock">
      <div className="clock-main">
        <span className="clock-hh">{hh}</span>
        <span className="clock-ss">{ss}</span>
      </div>
      <div className="clock-date">{date} · {yr}</div>
      <TickStrip count={46} />
    </div>
  );
}

/* ───────── status + waveform ───────── */
export function StatusPanel({ status, rgb }) {
  const meta = STATUS_META[status] ?? STATUS_META.idle;
  return (
    <div className="statp">
      <div className="statp-row">
        <span className="statp-dot" style={{ background: meta.col, boxShadow: `0 0 12px ${meta.col}` }} />
        <span className="statp-label" style={{ color: meta.col }}>{meta.label}</span>
      </div>
      <Waveform status={status} bars={42} height={44} rgb={rgb} />
    </div>
  );
}

/* ───────── system stats (radials) ───────── */
export function SysStatsPanel({ d, collapsible, open, onToggle }) {
  const tempSub = d.temp != null ? `${n0(d.temp).toFixed(0)}°C` : "—";
  const ramSub = d.ramTotalGb ? `${Math.round(d.ramTotalGb)} GB` : "—";
  return (
    <CornerBox title="System" code="SYS·01" slot="sysstats"
               collapsible={collapsible} open={open} onToggle={onToggle}>
      <div className="sys-grid">
        <RadialGauge value={d.cpu} label="CPU" size={106} sub={tempSub} />
        <RadialGauge value={d.ram} label="MEMORY" size={106} sub={ramSub} />
      </div>
      <div className="sys-mini">
        <MiniRadial value={d.gpu ?? 0} label="GPU" size={60} />
        <MiniRadial value={d.diskActivity ?? 0} label="DISK" size={60} />
        <MiniRadial value={d.net ?? 0} label="NET" size={60} />
      </div>
    </CornerBox>
  );
}

/* compact variant for tight rails */
export function SysStatsCompact({ d }) {
  const cpuHist = useHistory(d.cpu);
  const diskRight = d.diskTotalGb
    ? `${Math.round(n0(d.diskUsedGb))} / ${Math.round(n0(d.diskTotalGb))} GB` : `${n0(d.disk).toFixed(0)}%`;
  return (
    <CornerBox title="System Telemetry" code="SYS·01" slot="sysstats">
      <BarMeter value={d.cpu} label="CPU" right={`${n0(d.cpu).toFixed(0)}%`} warn={n0(d.cpu) > 80} />
      <BarMeter value={d.ram} label="MEMORY" right={`${((n0(d.ram) / 100) * (d.ramTotalGb || 16)).toFixed(1)} GB`} />
      <BarMeter value={d.gpu ?? 0} label="GPU" right={d.gpu != null ? `${n0(d.gpu).toFixed(0)}%` : "—"} />
      <BarMeter value={d.disk} label="DISK" right={diskRight} />
      <div className="sys-spark">
        <span className="sys-spark-lbl">CPU LOAD HISTORY</span>
        <Sparkline points={46} height={36} data={cpuHist} />
      </div>
    </CornerBox>
  );
}

/* ───────── power / battery ───────── */
export function PowerPanel({ d, collapsible, open, onToggle }) {
  const pct = d.batteryPct;
  const hasBattery = pct != null;
  const shown = hasBattery ? pct : 100;
  const label = !hasBattery ? "AC POWER" : d.charging ? "CHARGING" : "BATTERY";
  return (
    <CornerBox title="Power" code="PWR" slot="power"
               collapsible={collapsible} open={open} onToggle={onToggle}>
      <div className="pwr">
        <RadialGauge value={shown} label={label} size={96} unit="%" />
        <div className="pwr-meta">
          <DataRow k="SOURCE" v={d.charging || !hasBattery ? "AC MAINS" : "CELL"} accent />
          <DataRow k="STATE" v={!hasBattery ? "PLUGGED IN" : d.charging ? "CHARGING" : "DISCHARGING"} />
          <DataRow k="REMAINING" v={d.remaining || "—"} />
          <DataRow k="LEVEL" v={hasBattery ? `${Math.round(pct)}%` : "100%"} accent />
        </div>
      </div>
      <SegBar value={shown} segs={18} label="OUTPUT" />
    </CornerBox>
  );
}

/* ───────── weather ───────── */
const DEFAULT_WEATHER = {
  temp: null, condition: "—", location: "LOCATING…",
  humidity: null, wind: "—", aqi: null, hours: [],
};
export function WeatherPanel({ weather, collapsible, open, onToggle }) {
  const w = weather || DEFAULT_WEATHER;
  return (
    <CornerBox title="Weather" code="ATM" slot="weather"
               collapsible={collapsible} open={open} onToggle={onToggle}>
      <div className="wx-now">
        <span className="wx-temp">{w.temp != null ? `${Math.round(w.temp)}°` : "—"}</span>
        <div className="wx-meta">
          <span className="wx-cond">{(w.condition || "—").toUpperCase()}</span>
          <span className="wx-loc">{(w.location || "—").toUpperCase()}</span>
        </div>
      </div>
      <div className="wx-stats">
        <DataRow k="HUMIDITY" v={w.humidity != null ? `${Math.round(w.humidity)}%` : "—"} />
        <DataRow k="WIND" v={w.wind || "—"} />
        {w.aqi != null && <DataRow k="AQI" v={`${w.aqi}`} accent />}
      </div>
      {w.hours?.length > 0 && (
        <div className="wx-hours">
          {w.hours.slice(0, 6).map((h, i) => (
            <div key={i} className="wx-h">
              <span className="wx-h-t">{h.t}</span>
              <span className="wx-h-i">{h.i}</span>
              <span className="wx-h-c">{h.c != null ? `${Math.round(n0(h.c))}°` : "—"}</span>
            </div>
          ))}
        </div>
      )}
    </CornerBox>
  );
}

/* ───────── network / connectivity ───────── */
// Live throughput: 1-decimal under 10 Mbps so background/idle traffic is still
// visible instead of rounding to a dead "0"; whole numbers once it's busy.
const fmtMbps = (v) => (v == null ? "0.0" : v < 10 ? v.toFixed(1) : v.toFixed(0));

export function NetworkPanel({ d, netInfo, collapsible, open, onToggle }) {
  const n = netInfo || {};
  const downHist = useHistory(d.down);
  return (
    <CornerBox title="Network" code="NET·LINK" slot="network"
               collapsible={collapsible} open={open} onToggle={onToggle}>
      <div className="net-rates">
        <div className="net-rate">
          <span className="net-arrow">▼</span>
          <span className="net-num">{fmtMbps(d.down)}</span>
          <span className="net-u">Mbps DOWN</span>
        </div>
        <div className="net-rate">
          <span className="net-arrow up">▲</span>
          <span className="net-num">{fmtMbps(d.up)}</span>
          <span className="net-u">Mbps UP</span>
        </div>
      </div>
      <Sparkline points={42} height={34} data={downHist} />
      <div className="net-meta">
        <DataRow k="PING" v={d.ping ? `${d.ping} ms` : "—"} accent />
        <DataRow k="LOCAL IP" v={n.localIp || "—"} />
        <DataRow k="PUBLIC IP" v={n.publicIp || "—"} />
        <DataRow k="LOCATION" v={n.location || "—"} />
      </div>
    </CornerBox>
  );
}

/* ───────── schedule / agenda (editable) ───────── */
// Add / edit an agenda item in place (was two chained window.prompt()s).
function AgendaForm({ initial, onSave, onCancel }) {
  const [task, setTask] = useState(initial?.task || "");
  const [time, setTime] = useState(/^\d{1,2}:\d{2}$/.test(initial?.time || "") ? initial.time.padStart(5, "0") : "");
  const submit = (e) => {
    e.preventDefault();
    if (task.trim()) onSave({ task: task.trim(), time });
  };
  return (
    <form className="sch-form" onSubmit={submit}
          onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); onCancel(); } }}>
      <input type="time" className="sch-form-time" value={time} onChange={(e) => setTime(e.target.value)}
             aria-label="Time (optional)" />
      <input className="sch-form-task" value={task} onChange={(e) => setTask(e.target.value)} autoFocus
             placeholder={initial ? "Task" : "What is it?"} aria-label="Task" maxLength={120} />
      <button type="submit" className="sch-form-btn sch-form-btn--ok" disabled={!task.trim()}
              aria-label={initial ? "Save" : "Add"} title={initial ? "Save" : "Add"}>
        <Icon name="check" size={15} stroke={2} />
      </button>
      <button type="button" className="sch-form-btn" onClick={onCancel} aria-label="Cancel" title="Cancel">
        <Icon name="close" size={14} stroke={2} />
      </button>
    </form>
  );
}

export function SchedulePanel({ items, collapsible, open, onToggle, runAction }) {
  const list = items || [];
  const editable = typeof runAction === "function";
  const [editing, setEditing] = useState(null);   // null | "new" | index of the row being edited

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
    <CornerBox title="Agenda" code="TODAY" slot="schedule"
               collapsible={collapsible} open={open} onToggle={onToggle}>
      <div className="sch">
        {list.length === 0 && editing !== "new" ? (
          <div className="sch-empty">Nothing scheduled today.</div>
        ) : list.map((it, i) => (editing === i ? (
          <AgendaForm key={i} initial={it} onSave={(v) => editItem(it, v)} onCancel={() => setEditing(null)} />
        ) : (
          <div key={i} className={`sch-row ${it.now ? "now" : ""} ${it.done ? "done" : ""}`}>
            <span className="sch-t">{it.time}</span>
            <span className="sch-track"><span className="sch-dot" /></span>
            <span className="sch-task">{it.task}</span>
            {it.duration && <span className="sch-dur">{it.duration}</span>}
            {editable && (
              <span className="sch-edit">
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
        <button className="sch-add" onClick={() => setEditing("new")}>
          <Icon name="plus" size={14} /> Add item
        </button>
      )}
    </CornerBox>
  );
}

/* ───────── command log — JARVIS's mini "terminal" (collapsible) ───────── */
export function TerminalPanel({ commands = [], open, onToggle }) {
  const bodyRef = useRef(null);
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [commands, open]);
  const fmtTime = (ts) => {
    try { return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" }); }
    catch { return ""; }
  };
  return (
    <CornerBox title="Terminal" code="LOG" slot="terminal" collapsible open={open} onToggle={onToggle}>
      <div className="term" ref={bodyRef}>
        <div className="term-line term-line--sys">JARVIS command log — read-only.</div>
        {commands.length === 0 && <div className="term-line term-dim">no commands executed yet.</div>}
        {commands.map((c, i) => (
          <div key={i} className={`term-line ${c.ok === false ? "term-err" : ""}`}>
            <span className="term-t">{fmtTime(c.ts)}</span>
            <span className="term-prompt">{c.ok === false ? "✗" : "›"}</span>
            <span className="term-cmd">{c.type}{c.target ? ` ${c.target}` : ""}</span>
            {c.message && <span className="term-out">— {c.message}</span>}
          </div>
        ))}
      </div>
    </CornerBox>
  );
}

/* ───────── bottom dock launcher (replaces the quick-actions grid) ───────── */
export function DockBar({ onOpenSkills, onOpenCaps, onOpenPower, onOpenMemory, onOpenChat, chatCount = 0 }) {
  const items = [
    { l: "SKILLS",       i: "skills", on: onOpenSkills, tip: "Record, QR codes, screenshots, folders" },
    { l: "MEMORY",       i: "memory", on: onOpenMemory, tip: "What JARVIS knows and remembers" },
    { l: "CAPABILITIES", i: "caps",   on: onOpenCaps,   tip: "Everything you can ask — with examples" },
    { l: "POWER",        i: "power",  on: onOpenPower,  tip: "Lock, sleep, restart, shut down" },
  ];
  return (
    <nav className="dock" data-slot="dock" aria-label="Main menu">
      {items.map((it) => (
        <button key={it.l} className="dock-btn" onClick={it.on} title={it.tip}>
          <span className="dock-i"><Icon name={it.i} /></span>
          <span className="dock-l">{it.l}</span>
        </button>
      ))}
      <button className="dock-btn dock-btn--chat" onClick={onOpenChat} title="Chat with JARVIS">
        <span className="dock-i"><Icon name="chat" /></span>
        <span className="dock-l">CONVERSATION</span>
        {chatCount > 0 && <span className="dock-badge" aria-label={`${chatCount} messages`}>{chatCount}</span>}
      </button>
    </nav>
  );
}

/* Shared header for the slide-over panels. */
function SideHeader({ title, sub, onClose }) {
  return (
    <header className="sideov-hd">
      <div className="sideov-hd-txt">
        <span className="sideov-title">{title}</span>
        {sub && <span className="sideov-sub">{sub}</span>}
      </div>
      <button className="x-btn" onClick={onClose} aria-label={`Close ${title.toLowerCase()}`}>
        <Icon name="close" size={16} />
      </button>
    </header>
  );
}

/* ───────── Skills overlay — recording, QR, file-system access ───────── */
const RECORDERS = [
  { media: "audio", label: "Microphone", icon: "mic", note: "Audio only" },
  { media: "video", label: "Webcam", icon: "video", note: "Video with sound" },
  { media: "screen", label: "Screen", icon: "monitor", note: "Your whole display" },
];

export function SkillsOverlay({ open, onClose, recordings = {}, runAction, allowedDirs = [], onSaveDirs, onShowResult }) {
  const [tool, setTool] = useState(null);     // null | "qr" | "list" — the open quick-tool form
  const [text, setText] = useState("");
  const rec = (media) => {
    const active = recordings[media];
    runAction({ type: "record", media, do: active ? "stop" : "start" });
  };
  // The answer to a direct action arrives as a chat reply (a QR code is drawn there),
  // so hand over to the chat to show it.
  const runTool = (spec) => { runAction(spec); setTool(null); setText(""); onShowResult && onShowResult(); };
  const submitTool = (e) => {
    e.preventDefault();
    const v = text.trim();
    if (!v) return;
    if (tool === "qr") runTool({ type: "qr_code", text: v });
    else runTool({ type: "list_dir", path: v });
  };
  const addDir = (v) => {
    const d = v.trim().replace(/^["']|["']$/g, "");
    if (d && !allowedDirs.includes(d)) onSaveDirs([...allowedDirs, d]);
  };
  const pickTool = (t) => { setTool(tool === t ? null : t); setText(""); };
  const recording = RECORDERS.filter((r) => recordings[r.media]).length;

  return (
    <div className={`sideov ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="sideov-scrim" onClick={onClose} />
      <aside className="sideov-panel" role="dialog" aria-label="Skills">
        <SideHeader title="SKILLS" sub="Quick tools JARVIS can run for you right now" onClose={onClose} />
        <div className="sideov-body">
          <section className="skill-sec">
            <h4>Recording {recording > 0 && <span className="tag tag--err">{recording} live</span>}</h4>
            {RECORDERS.map((r) => {
              const on = !!recordings[r.media];
              return (
                <button key={r.media} className={`skill-rec ${on ? "skill-rec--on" : ""}`} onClick={() => rec(r.media)}
                        aria-pressed={on}>
                  <span className="skill-rec-ico"><Icon name={r.icon} size={17} /></span>
                  <span className="skill-rec-txt">
                    <span className="skill-rec-l">{r.label}</span>
                    <span className="skill-rec-n">{on ? "Recording…" : r.note}</span>
                  </span>
                  <span className="skill-rec-state">
                    <Icon name={on ? "stop" : "record"} size={14} />{on ? "Stop" : "Start"}
                  </span>
                </button>
              );
            })}
            <p className="skill-note">Recordings run until you stop them, and are saved to your Jarvis storage folder.</p>
          </section>

          <section className="skill-sec">
            <h4>Quick tools</h4>
            <div className="skill-tools">
              <button className={`skill-tool ${tool === "qr" ? "is-on" : ""}`} onClick={() => pickTool("qr")} aria-expanded={tool === "qr"}>
                <Icon name="qr" size={18} /><span>QR code</span>
              </button>
              <button className="skill-tool" onClick={() => runTool({ type: "screenshot" })}>
                <Icon name="camera" size={18} /><span>Screenshot</span>
              </button>
              <button className={`skill-tool ${tool === "list" ? "is-on" : ""}`} onClick={() => pickTool("list")}
                      aria-expanded={tool === "list"} disabled={allowedDirs.length === 0}
                      title={allowedDirs.length ? "" : "Allow a folder below first"}>
                <Icon name="folder" size={18} /><span>List folder</span>
              </button>
            </div>
            {tool && (
              <form className="skill-form" onSubmit={submitTool}
                    onKeyDown={(e) => { if (e.key === "Escape") { e.preventDefault(); setTool(null); } }}>
                <input autoFocus value={text} onChange={(e) => setText(e.target.value)} spellCheck={false}
                       list={tool === "list" ? "skills-dirs" : undefined}
                       placeholder={tool === "qr" ? "A link or text for the QR code" : "An allowed folder, or a folder inside one"}
                       aria-label={tool === "qr" ? "QR code contents" : "Folder to list"} />
                {tool === "list" && (
                  <datalist id="skills-dirs">{allowedDirs.map((d) => <option key={d} value={d} />)}</datalist>
                )}
                <button type="submit" className="btn btn--primary" disabled={!text.trim()}>
                  {tool === "qr" ? "Create" : "List"}
                </button>
              </form>
            )}
          </section>

          <section className="skill-sec">
            <h4>File-system access <span className="skill-safe">SAFE · READ-ONLY</span></h4>
            <p className="skill-note">
              JARVIS may only <strong>list</strong> these folders and <strong>read</strong> files inside them
              (each read asks your approval). It can never run, write, move, or delete anything.
            </p>
            <ChipList items={allowedDirs} onRemove={(d) => onSaveDirs(allowedDirs.filter((x) => x !== d))}
                      empty="No folders allowed yet." removeLabel="Disallow" />
            <AddField placeholder="C:\Users\you\Documents" button="Allow" onAdd={addDir} label="Folder to allow" />
          </section>
        </div>
      </aside>
    </div>
  );
}

/* ───────── Capabilities overlay — what JARVIS can do ───────── */
const CAPABILITIES = [
  { group: "Conversation", items: ["Answer questions, explain, advise", "Render charts, tables, schedules & flowcharts", "Translate text (typed or from the camera)", "Talk by wake word, Ctrl+Space, always-on listening, native voice, or chat", "Conversation mode — short, natural back-and-forth", "Speaks with a natural British neural voice (offline Piper, or ElevenLabs)", "Attach an image or document in chat and ask about it"],
    try: ["Make a table of the planets and their moons", "Explain how a heat pump works"] },
  { group: "Memory", items: ["Remembers durable facts about you across sessions", "Recalls recent conversation context", "Forgets facts on request"],
    try: ["What do you remember about me?"] },
  { group: "Your computer", items: ["Open / close apps & folders", "Web search & open links", "Volume up/down, exact volume, mute, media keys", "Lock the screen", "Read your clipboard on request"],
    try: ["Set the volume to 30", "Open my Downloads folder"] },
  { group: "Web browser (his hands)", items: ["Autopilot: give a whole task — “play lofi on YouTube” — and he silently opens, clicks & types until it's done", "You only hear the outcome — never the steps or numbers", "Sees the page's real buttons & links (JS)", "Reads & summarises a page"],
    try: ["Play lofi on YouTube"] },
  { group: "Desktop autopilot", items: ["Drives desktop apps by their real, named controls (asks once)", "Focuses or launches the right window himself", "Kill switch: top-left corner slam, Stop button, or “disarm”"] },
  { group: "Power (asks first)", items: ["Shut down · Restart · Sleep · Hibernate · Log off"] },
  { group: "Create & capture", items: ["Screenshots", "QR codes", "Generate images (Gemini, needs a Gemini key)", "Record audio, webcam, or screen (until you stop)", "Make PDFs from text", "Open files he created"],
    try: ["Generate an image of a red fox in the snow", "Make a QR code for my Wi-Fi name"] },
  { group: "Read (text-only model)", items: ["Read & summarise PDFs", "List approved folders", "Read approved text files (asks first)"] },
  { group: "Eyes & senses", items: ["Look at your screen and describe/answer (vision)", "Live system, power, weather, network & agenda readouts"],
    try: ["What's on my screen?"] },
  { group: "Out in the world", items: ["Current weather + multi-day forecast", "Nearby places — food, cafés, pharmacy, ATM, fuel…", "Directions & travel time to a place", "Latest news headlines by topic"],
    try: ["What's the weather this week?", "Find a pharmacy nearby", "Latest tech headlines"] },
  { group: "Reminders & routines", items: ["One-off reminders & timers", "Recurring routines / spoken briefings", "Manage your daily agenda"],
    try: ["Remind me in 10 minutes to stretch", "What's on my agenda today?"] },
  { group: "Learns & adapts", items: ["Teach him playbooks — named multi-step recipes", "Restyle the HUD — colour, background, density", "Show / hide / rearrange panels by voice"],
    try: ["Make the screen amber"] },
  { group: "Apps", items: ["Spotify — play/pause/next/previous, play a song"],
    try: ["Play Bohemian Rhapsody on Spotify"] },
  { group: "This HUD", items: ["Open/close panels (skills, power, terminal, chat, settings)", "Expand/collapse system, power, weather, network, agenda", "Clear the chat / start a new conversation", "Mute or silence himself"],
    try: ["Collapse all the panels"] },
];

export function CapabilitiesOverlay({ open, onClose, onAsk }) {
  const [query, setQuery] = useState("");
  const q = query.trim().toLowerCase();
  const groups = q
    ? CAPABILITIES.map((c) => (c.group.toLowerCase().includes(q) ? c
        : { ...c, items: c.items.filter((it) => it.toLowerCase().includes(q)),
            try: (c.try || []).filter((t) => t.toLowerCase().includes(q)) }))
        .filter((c) => c.items.length || c.try.length)
    : CAPABILITIES;
  const tryIt = (text) => { onClose(); onAsk && onAsk(text); };
  const searchRef = useRef(null);
  useEffect(() => {
    if (!open) return undefined;
    const t = setTimeout(() => searchRef.current?.focus({ preventScroll: true }), 120);
    return () => clearTimeout(t);
  }, [open]);

  return (
    <div className={`sideov sideov--left ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="sideov-scrim" onClick={onClose} />
      <aside className="sideov-panel" role="dialog" aria-label="Capabilities">
        <SideHeader title="CAPABILITIES" sub="Just ask in plain language — by voice or in chat" onClose={onClose} />
        <div className="sideov-search">
          <Icon name="search" size={15} />
          <input ref={searchRef} type="search" value={query} onChange={(e) => setQuery(e.target.value)}
                 placeholder="Search what JARVIS can do…" aria-label="Search capabilities" />
        </div>
        <div className="sideov-body">
          {groups.length === 0 && (
            <div className="cap-none">
              Nothing matches “{query}”.{" "}
              <button className="cap-try" onClick={() => tryIt(query)}><Icon name="send" size={12} />Ask JARVIS anyway</button>
            </div>
          )}
          {groups.map((c) => (
            <section key={c.group} className="cap-sec">
              <h4>{c.group}</h4>
              {c.items.length > 0 && (
                <ul className="cap-list">
                  {c.items.map((it, i) => <li key={i}>{it}</li>)}
                </ul>
              )}
              {c.try?.length > 0 && (
                <div className="cap-tries">
                  <span className="cap-tries-l">Try</span>
                  {c.try.map((t) => (
                    <button key={t} className="cap-try" onClick={() => tryIt(t)} title="Send this to JARVIS">
                      “{t}”
                    </button>
                  ))}
                </div>
              )}
            </section>
          ))}
        </div>
      </aside>
    </div>
  );
}

/* ───────── Power overlay — shutdown / restart / sleep ───────── */
const POWER_GROUPS = [
  { label: "Right away", items: [
    { l: "Lock", c: "lock", i: "lock", note: "Lock the screen" },
    { l: "Sleep", c: "sleep", i: "moon", note: "Suspend to RAM" },
  ] },
  { label: "Asks for your approval first", items: [
    { l: "Restart", c: "restart", i: "restart", note: "Reboot the PC" },
    { l: "Shut down", c: "shutdown", i: "power", note: "Power off", danger: true },
    { l: "Hibernate", c: "hibernate", i: "snow", note: "Save to disk & power off" },
    { l: "Log off", c: "logoff", i: "logout", note: "Sign out of Windows" },
  ] },
];

export function PowerOverlay({ open, onClose, runAction }) {
  const act = (command) => { runAction({ type: "power", command }); onClose(); };
  return (
    <div className={`powov ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="powov-scrim" onClick={onClose} />
      <div className="powov-panel" role="dialog" aria-label="Power options">
        <header className="powov-hd">
          <span className="powov-title">POWER</span>
          <button className="x-btn" onClick={onClose} aria-label="Close power options"><Icon name="close" size={16} /></button>
        </header>
        {POWER_GROUPS.map((g) => (
          <section key={g.label} className="powov-sec">
            <h4 className="powov-sec-l">{g.label}</h4>
            <div className={`powov-grid powov-grid--${g.items.length}`}>
              {g.items.map((b) => (
                <button key={b.c} className={`powov-btn ${b.danger ? "powov-btn--danger" : ""}`} onClick={() => act(b.c)}>
                  <span className="powov-i"><Icon name={b.i} size={24} stroke={1.4} /></span>
                  <span className="powov-l">{b.l}</span>
                  <span className="powov-note">{b.note}</span>
                </button>
              ))}
            </div>
          </section>
        ))}
        <p className="skill-note">You can also say it — “lock the screen”, “restart in a minute”.</p>
      </div>
    </div>
  );
}


/* ───────── Memory overlay — everything JARVIS has stored, with forget ───────── */
const MEM_KINDS = [
  { key: "facts", kind: "fact", label: "About you", color: "var(--ac)",
    note: "JARVIS reads these before every reply." },
  { key: "routines", kind: "routine", label: "Routines", color: "#c7a6ff",
    note: "Steps JARVIS runs when you say a routine's name, at its time, or when you press Run." },
  { key: "playbooks", kind: "playbook", label: "Playbooks", color: "#6ee7a8",
    note: "Recipes JARVIS follows when you say one of their phrases." },
  { key: "learned", kind: "learned", label: "Learned routines", color: "var(--amber)",
    note: "Steps saved from tasks JARVIS finished on its own, reused as a head start next time." },
  { key: "conversations", kind: "conversation", label: "Saved chats", color: "#8fa9bd",
    note: "Past conversations. Open one to pick up where you left off." },
];

const memTitle = (key, x) =>
  key === "facts" ? x.text : key === "conversations" ? x.title
    : key === "learned" ? (x.triggers?.[0] || x.name) : x.name;

// Everything a memory says, for the search box.
const memText = (key, x) => [memTitle(key, x), x.prompt, x.steps, x.when, ...(x.triggers || [])]
  .filter(Boolean).join(" ").toLowerCase();

function memDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString([], { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

// "1) click X. 2) read the page." → ["click X", "read the page"]
const memSteps = (s) => (s || "").split(/\s*\d+\)\s*/).map((x) => x.trim().replace(/\.$/, "")).filter(Boolean);

// Memory ▸ Routines ▸ Add / Edit. A routine needs a name to be run by, a time, or both.
const DAY_SETS = { daily: "Every day", weekdays: "Weekdays", weekends: "Weekends" };
const daysKey = (d) => (d === "daily" ? "daily"
  : JSON.stringify(d) === JSON.stringify(["mon", "tue", "wed", "thu", "fri"]) ? "weekdays"
  : JSON.stringify(d) === JSON.stringify(["sat", "sun"]) ? "weekends" : "daily");

function RoutineForm({ initial, onSave, onCancel, result }) {
  const [f, setF] = useState(() => ({
    name: initial?.named ? initial.name : "", prompt: initial?.prompt || "",
    time: initial?.time || "", days: daysKey(initial?.days),
  }));
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const submit = (e) => {
    e.preventDefault();
    onSave({ ...f, id: initial?.id || "" });
  };
  return (
    <form className="memx-rform" onSubmit={submit}>
      <input value={f.name} onChange={set("name")} placeholder="Name — e.g. movie night"
             aria-label="Routine name" maxLength={60} autoFocus />
      <textarea value={f.prompt} onChange={set("prompt")} rows={3} aria-label="Routine steps"
                placeholder="The steps, as you'd say them — e.g. open Netflix on the TV, set the volume to 30" />
      <div className="memx-rform-row">
        <label>At <input type="time" value={f.time} onChange={set("time")} aria-label="Time (optional)" /></label>
        <select value={f.days} onChange={set("days")} disabled={!f.time} aria-label="Days">
          {Object.entries(DAY_SETS).map(([k, l]) => <option key={k} value={k}>{l}</option>)}
        </select>
        <span className="memx-rform-hint">{f.time ? "" : "No time: it runs when you ask."}</span>
        <button type="button" className="memx-open" onClick={onCancel}>Cancel</button>
        <button type="submit" className="memx-rform-save"
                disabled={!f.prompt.trim() || (!f.name.trim() && !f.time)}>Save</button>
      </div>
      {result && <p className={result.ok ? "memx-rform-ok" : "memx-rform-err"}>{result.message}</p>}
    </form>
  );
}

export function MemoryOverlay({ open, onClose, memory, onRemember, onForget, onOpenConversation, onOpenChat,
  onSaveRoutine, onRunRoutine, routineResult }) {
  const [tab, setTab] = useState("facts");
  const [editing, setEditing] = useState(null);   // null | "new" | a routine
  const [sentAt, setSentAt] = useState(null);     // routineResult when the form was sent
  const [draft, setDraft] = useState("");
  const [query, setQuery] = useState("");
  const [armed, setArmed] = useState(null);   // id awaiting its confirming second click
  const [focus, setFocus] = useState(null);   // id picked on the timeline
  const mainRef = useRef(null);
  const m = memory || {};
  const lists = {
    facts: [...(m.facts || [])].reverse(),
    routines: m.routines || [],
    playbooks: m.playbooks || [],
    learned: [...(m.learned || [])].reverse(),
    conversations: m.conversations || [],
  };

  // Scroll only the list pane — scrollIntoView would also scroll the HUD's
  // overflow:hidden ancestors and shift the whole stage sideways.
  useEffect(() => {
    const main = mainRef.current;
    const el = focus && document.getElementById(`mem-${focus}`);
    if (main && el) main.scrollTo({ top: el.offsetTop - main.clientHeight / 3, behavior: "smooth" });
  }, [focus, tab]);

  // Timeline: every dated memory as a tick between the oldest one and now.
  const now = m.now || 0;
  const dated = MEM_KINDS.flatMap((k) => lists[k.key].filter((x) => x.ts).map((x) => ({ x, k })));
  const t0 = Math.min(now - 86400, ...dated.map((d) => d.x.ts));
  const at = (ts) => `${((ts - t0) / (now - t0)) * 100}%`;

  const pick = (key) => {
    setTab(key); setFocus(null); setArmed(null); setEditing(null); mainRef.current?.scrollTo(0, 0);
  };
  // The form closes once the backend says the routine saved; a refusal stays on it.
  const freshResult = routineResult && routineResult !== sentAt ? routineResult : null;
  const formFor = freshResult?.ok ? null : editing;
  const edit = (r) => { setSentAt(routineResult); setEditing(r); };
  const saveRoutine = (r) => { setSentAt(routineResult); onSaveRoutine && onSaveRoutine(r); };
  const forget = (e, kind, id) => {
    e.preventDefault();                       // inside <summary>: don't toggle the row
    if (armed !== id) { setArmed(id); return; }
    setArmed(null);
    onForget(kind, id);
  };
  const forgetBtn = (kind, id, label = "Forget") => (
    <button type="button" className={`memx-forget ${armed === id ? "is-armed" : ""}`}
            onClick={(e) => forget(e, kind, id)} onBlur={() => armed === id && setArmed(null)}>
      {armed === id ? "Confirm" : label}
    </button>
  );
  const remember = (e) => {
    e.preventDefault();
    if (draft.trim()) { onRemember(draft.trim()); setDraft(""); }
  };
  const row = (id) => ({ id: `mem-${id}`, className: focus === id ? "is-focus" : "" });
  const cur = MEM_KINDS.find((k) => k.key === tab);
  const q = query.trim().toLowerCase();
  const list = q ? lists[tab].filter((x) => memText(tab, x).includes(q)) : lists[tab];
  const hits = q ? Object.fromEntries(MEM_KINDS.map((k) => [k.key, lists[k.key].filter((x) => memText(k.key, x).includes(q)).length])) : null;

  return (
    <div className={`memx ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="memx-scrim" onClick={onClose} />
      <section className="memx-panel" role="dialog" aria-label="Memory">
        <header className="memx-hd">
          <div className="memx-hd-txt">
            <span className="memx-kicker">MEMORY</span>
            <h2 className="memx-title">What JARVIS knows</h2>
            {m.location && <p className="memx-loc">Kept on this PC in <code>{m.location}</code></p>}
          </div>
          <div className="memx-hd-tools">
            <div className="memx-search">
              <Icon name="search" size={15} />
              <input type="search" value={query} onChange={(e) => setQuery(e.target.value)}
                     placeholder="Search memory…" aria-label="Search memory" />
            </div>
            <button className="memx-close" onClick={onClose} aria-label="Close memory"><Icon name="close" /></button>
          </div>
        </header>

        <div className="memx-tl" aria-hidden="true">
          <div className="memx-tl-track">
            {dated.map(({ x, k }) => (
              <button key={`${k.key}-${x.id}`} tabIndex={-1} className="memx-tick"
                      style={{ left: at(x.ts), "--kc": k.color }}
                      title={`${k.label} · ${memDate(x.ts)} — ${memTitle(k.key, x)}`}
                      onClick={() => { setTab(k.key); setFocus(x.id); setArmed(null); setQuery(""); }} />
            ))}
          </div>
          <div className="memx-tl-axis"><span>{memDate(t0)}</span><span>Today</span></div>
        </div>

        <div className="memx-body">
          <nav className="memx-nav" aria-label="Memory sections">
            {MEM_KINDS.map((k) => (
              <button key={k.key} className={`memx-navbtn ${tab === k.key ? "is-on" : ""}`}
                      aria-current={tab === k.key} onClick={() => pick(k.key)} style={{ "--kc": k.color }}>
                <span className="memx-sw" />
                <span className="memx-navl">{k.label}</span>
                <span className="memx-count">
                  {!memory ? "–" : hits ? `${hits[k.key]}/${lists[k.key].length}` : lists[k.key].length}
                </span>
              </button>
            ))}
            <p className="memx-navnote">Forgetting is permanent. You can also say “forget that…”.</p>
          </nav>

          <div className="memx-main" ref={mainRef}>
            <div className="memx-sec-hd">
              <h3>{cur.label}</h3>
              <p>{cur.note}</p>
            </div>

            {tab === "facts" && !q && (
              <form className="memx-add" onSubmit={remember}>
                <input value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="New fact"
                       placeholder="Something JARVIS should always know — e.g. I'm vegetarian" maxLength={300} />
                <button type="submit" disabled={!draft.trim()}>Remember</button>
              </form>
            )}

            {tab === "routines" && !q && (formFor ? (
              <RoutineForm key={formFor === "new" ? "new" : formFor.id}
                           initial={formFor === "new" ? null : formFor} result={freshResult}
                           onSave={saveRoutine} onCancel={() => setEditing(null)} />
            ) : (
              <div className="memx-add">
                <button type="button" onClick={() => edit("new")}>Add a routine</button>
              </div>
            ))}

            {tab === "conversations" && m.current > 0 && !q && (
              <div className="memx-row memx-row--current">
                <span className="memx-text">This conversation</span>
                <span className="memx-meta">{m.current} messages</span>
                <button type="button" className="memx-open" onClick={onOpenChat}>Open</button>
              </div>
            )}

            {!memory && <p className="memx-empty">Reading memory…</p>}
            {memory && q && list.length === 0 && (
              <p className="memx-empty">
                Nothing in {cur.label.toLowerCase()} matches “{query}”.
                {MEM_KINDS.filter((k) => k.key !== tab && hits[k.key]).map((k) => (
                  <button key={k.key} type="button" className="memx-jump" onClick={() => pick(k.key)}>
                    {hits[k.key]} in {k.label}
                  </button>
                ))}
              </p>
            )}
            {memory && !q && list.length === 0 && (
              <p className="memx-empty">
                {{
                  facts: "Nothing yet. Tell JARVIS “remember that…”, or add something above.",
                  routines: "No routines. Add one above, or say “make a routine called…”.",
                  playbooks: "No playbooks. Teach one by saying “learn a playbook called…”.",
                  learned: "Nothing learned yet. Routines appear here after JARVIS completes a multi-step task.",
                  conversations: "No saved chats. Starting a new chat saves the current one here.",
                }[tab]}
              </p>
            )}

            <ul className="memx-list">
              {tab === "facts" && list.map((f) => (
                <li key={f.id} {...row(f.id)}>
                  <div className="memx-row">
                    <span className="memx-text">{f.text}</span>
                    <span className="memx-meta">{memDate(f.ts)}</span>
                    {forgetBtn("fact", f.id)}
                  </div>
                </li>
              ))}

              {tab === "routines" && list.map((r) => (
                <li key={r.id} {...row(r.id)}>
                  <div className="memx-card">
                    <div className="memx-row">
                      <span className="memx-name">{r.name}</span>
                      {r.origin === "phone" && <span className="memx-badge memx-badge--phone">From phone</span>}
                      <span className="memx-meta">
                        {r.when}{r.last === new Date().toISOString().slice(0, 10) ? " · ran today" : ""}
                      </span>
                      {forgetBtn("routine", r.id, "Remove")}
                      <button type="button" className="memx-open" onClick={() => edit(r)}>Edit</button>
                      <button type="button" className="memx-open memx-open--run"
                              onClick={() => { onRunRoutine && onRunRoutine(r.id); onClose(); }}>
                        <Icon name="play" size={12} />Run
                      </button>
                    </div>
                    <p className="memx-steps">{r.prompt}</p>
                  </div>
                </li>
              ))}

              {tab === "playbooks" && list.map((p) => (
                <li key={p.id} {...row(p.id)}>
                  <div className="memx-card">
                    <div className="memx-row">
                      <span className="memx-name">{p.name}</span>
                      {p.builtin && <span className="memx-badge">Built in</span>}
                      <span className="memx-meta">{memDate(p.ts)}</span>
                      {forgetBtn("playbook", p.id, p.builtin ? "Disable" : "Remove")}
                    </div>
                    {p.triggers.length > 0 && (
                      <div className="memx-trig">
                        <span>When you say</span>
                        {p.triggers.map((t) => <em key={t}>{t}</em>)}
                      </div>
                    )}
                    <p className="memx-steps">{p.steps}</p>
                  </div>
                </li>
              ))}

              {tab === "learned" && list.map((p) => {
                const steps = memSteps(p.steps);
                return (
                  <li key={p.id} {...row(p.id)}>
                    <details className="memx-card memx-card--fold" open={focus === p.id || undefined}>
                      <summary className="memx-row">
                        <span className="memx-name">{memTitle("learned", p)}</span>
                        <span className="memx-meta">{steps.length} steps · {memDate(p.ts)}</span>
                        {forgetBtn("learned", p.id)}
                      </summary>
                      <ol className="memx-ol">{steps.map((s, i) => <li key={i}>{s}</li>)}</ol>
                    </details>
                  </li>
                );
              })}

              {tab === "conversations" && list.map((c) => (
                <li key={c.id} {...row(c.id)}>
                  <div className="memx-row">
                    <span className="memx-text">{c.title}</span>
                    <span className="memx-meta">{c.count} messages · {memDate(c.ts)}</span>
                    {forgetBtn("conversation", c.id, "Delete")}
                    <button type="button" className="memx-open" onClick={() => onOpenConversation(c.id)}>Open</button>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        </div>
      </section>
    </div>
  );
}


/* ───────── connection/comms list (radial layout density) ───────── */
export function CommsList({ title = "Channels", code = "COMMS", slot = "comms", connected = false }) {
  const rows = [
    `UPLINK · GROQ ${connected ? "· LINKED" : "· OFFLINE"}`,
    "WAKE WORD · ACTIVE", "WHISPER · LOCAL", "TTS · LOCAL",
    "OCR · STANDBY", "ACTIONS · READY",
  ];
  return (
    <CornerBox title={title} code={code} slot={slot}>
      <div className="comms">
        {rows.map((r, i) => (
          <div key={i} className="comms-row">
            <span className="comms-pip" />
            <span className="comms-txt">{r}</span>
          </div>
        ))}
      </div>
    </CornerBox>
  );
}

/* ───────── chat: button + slide-over overlay (LIVE) ───────── */
export function ChatButton({ onOpen, count = 0 }) {
  return (
    <button className="chatbtn" data-slot="chatbtn" onClick={onOpen}>
      <span className="chatbtn-i">❯_</span>
      <span className="chatbtn-l">CONVERSATION</span>
      {count > 0 && <span className="chatbtn-badge">{count}</span>}
    </button>
  );
}

// One chat bubble, memoized: during streaming only the live message's object
// identity changes (useWebSocket replaces just that entry), so finalized bubbles
// skip both re-render and ResponseRenderer's re-parse. runAction and onCopy are
// stable callbacks, so they don't break the memo.
const ChatMessage = memo(function ChatMessage({ m, runAction, onCopy }) {
  const mine = m.role === "user";
  return (
    <div className={`cmsg cmsg--${mine ? "user" : "jarvis"}`}>
      <span className="cmsg-role">{mine ? "YOU" : "J.A.R.V.I.S"}</span>
      <div className="cmsg-bubble">
        {mine
          ? m.text
          : <ResponseRenderer text={m.text} actions={m.actions} runAction={runAction} />}
      </div>
      {!mine && !m.streaming && m.text && onCopy && (
        <button type="button" className="cmsg-copy" onClick={() => onCopy(m.text)} aria-label="Copy reply" title="Copy reply">
          <Icon name="copy" size={13} />Copy
        </button>
      )}
    </div>
  );
});

// Relative "time ago" for the Recents list (ts is epoch seconds from the backend).
function timeAgo(ts) {
  if (!ts) return "";
  const s = Math.max(0, Math.floor(Date.now() / 1000 - ts));
  if (s < 60) return "just now";
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return `${Math.floor(d / 7)}w ago`;
}

// Starters for an empty chat: one tap sends it.
const CHAT_STARTERS = [
  "What's on my agenda today?",
  "What's the weather this week?",
  "Play lofi on YouTube",
  "What can you do?",
];

export function ChatOverlay({ open, onClose, status, messages = [], onSend, onStop, onReset, onUpload, runAction,
                              conversations = [], onNewChat, onOpenConversation, onDeleteConversation,
                              onRefreshConversations, onClearConversations, taskModelTiers = [], onNotify }) {
  const [draft, setDraft] = useState("");
  const [taskTier, setTaskTier] = useState("moderate");
  const [histOpen, setHistOpen] = useState(false);
  const [histQuery, setHistQuery] = useState("");
  const [confirmClear, setConfirmClear] = useState(false);
  const [confirmDel, setConfirmDel] = useState(null);   // id awaiting its confirming second click
  const bodyRef = useRef(null);
  const fileRef = useRef(null);
  const inputRef = useRef(null);
  const tierOptions = taskModelTiers.length === 3 ? taskModelTiers : [
    { value: "dumb", label: "Dumb" },
    { value: "moderate", label: "Moderate" },
    { value: "very_smart", label: "Very smart" },
  ];

  const startNewChat = () => { onNewChat?.(); setHistOpen(false); inputRef.current?.focus({ preventScroll: true }); };
  const toggleHist = () => {
    setConfirmClear(false); setConfirmDel(null); setHistQuery("");
    setHistOpen((o) => {
      const next = !o;
      if (next) onRefreshConversations?.();   // re-pull Recents when the drawer opens
      return next;
    });
  };
  const openConv = (id) => { onOpenConversation?.(id); setHistOpen(false); };
  // Two-click guards so a stray tap can't wipe one chat or the whole archive.
  const clearAll = () => {
    if (confirmClear) { onClearConversations?.(); setConfirmClear(false); }
    else { setConfirmClear(true); }
  };
  const deleteConv = (e, id) => {
    e.stopPropagation();
    if (confirmDel === id) { onDeleteConversation?.(id); setConfirmDel(null); }
    else setConfirmDel(id);
  };

  const pickFile = (e) => {
    const file = e.target.files?.[0];
    if (file && onUpload) onUpload(file, draft.trim(), taskTier);
    setDraft("");
    e.target.value = ""; // allow re-selecting the same file
  };

  // Auto-scroll to the newest message / typing indicator.
  useEffect(() => {
    if (bodyRef.current) bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  }, [messages, status, open]);

  // Ready to type as soon as the panel slides in.
  useEffect(() => {
    if (!open) return undefined;
    const t = setTimeout(() => inputRef.current?.focus({ preventScroll: true }), 120);
    return () => clearTimeout(t);
  }, [open]);

  // The composer grows with what's typed, up to a few lines, then scrolls.
  useEffect(() => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  }, [draft]);

  const send = (text = draft) => {
    const t = text.trim();
    if (!t) return;
    onSend(t, taskTier);
    setDraft("");
  };
  const copy = useCallback((text) => {
    navigator.clipboard?.writeText(text)
      .then(() => onNotify && onNotify("Copied to the clipboard"))
      .catch(() => {});
  }, [onNotify]);

  // JARVIS is mid-reply when thinking/working or a bubble is still streaming —
  // show a Stop button (interrupts generation + speech) in place of Send.
  const busy = status === "thinking" || status === "working"
    || messages.some((m) => m.streaming);
  const hq = histQuery.trim().toLowerCase();
  const shownConvs = hq ? conversations.filter((c) => (c.title || "").toLowerCase().includes(hq)) : conversations;

  return (
    <div className={`chatov ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="chatov-scrim" onClick={onClose} />
      <aside className="chatov-panel" role="dialog" aria-label="Conversation"
             onKeyDown={(e) => { if (e.key === "Escape" && histOpen) { e.preventDefault(); toggleHist(); } }}>
        <header className="chatov-hd">
          <div className="sideov-hd-txt">
            <span className="chatov-title">CONVERSATION</span>
            <span className="sideov-sub">
              {messages.length ? `${messages.length} message${messages.length === 1 ? "" : "s"} in this chat` : "A new chat"}
            </span>
          </div>
          <div className="chatov-hd-btns">
            <button className="x-btn" title="New chat — saves this one to Recents" aria-label="New chat" onClick={startNewChat}>
              <Icon name="plus" size={17} />
            </button>
            <button className={`x-btn ${histOpen ? "is-on" : ""}`} title="Recent chats" aria-label="Recent chats"
                    aria-pressed={histOpen} onClick={toggleHist}>
              <Icon name="history" size={17} />
            </button>
            <button className="x-btn" onClick={onClose} aria-label="Close conversation"><Icon name="close" size={16} /></button>
          </div>
        </header>

        {/* Recents — saved conversations; slides over the log when toggled */}
        <div className={`chatov-hist ${histOpen ? "open" : ""}`} aria-hidden={!histOpen}>
          <div className="chatov-hist-hd">
            <span className="chatov-hist-title">RECENTS</span>
            <div className="chatov-hist-hd-btns">
              <button className="btn btn--primary btn--sm" onClick={startNewChat}><Icon name="plus" size={14} />New chat</button>
              {conversations.length > 0 && (
                <button className={`btn btn--danger btn--sm ${confirmClear ? "is-armed" : ""}`}
                        title="Delete all saved conversations" onClick={clearAll} onBlur={() => setConfirmClear(false)}>
                  {confirmClear ? "Delete all?" : "Clear all"}
                </button>
              )}
              <button className="x-btn" title="Back to chat" aria-label="Back to chat" onClick={toggleHist}>
                <Icon name="close" size={16} />
              </button>
            </div>
          </div>
          {conversations.length > 0 && (
            <div className="chatov-hist-search">
              <Icon name="search" size={15} />
              <input type="search" value={histQuery} onChange={(e) => setHistQuery(e.target.value)}
                     placeholder="Search saved chats…" aria-label="Search saved chats" />
            </div>
          )}
          <div className="chatov-hist-list">
            {conversations.length === 0 && (
              <div className="chatov-hist-empty">No saved conversations yet. Start a new chat and this one moves here.</div>
            )}
            {conversations.length > 0 && shownConvs.length === 0 && (
              <div className="chatov-hist-empty">No saved chat matches “{histQuery}”.</div>
            )}
            {shownConvs.map((c) => (
              <div key={c.id} className="chatov-hist-item" role="button" tabIndex={0}
                   onClick={() => openConv(c.id)}
                   onKeyDown={(e) => { if (e.key === "Enter") openConv(c.id); }}>
                <span className="chatov-hist-name">{c.title}</span>
                <span className="chatov-hist-meta">
                  {timeAgo(c.ts)}{c.count ? ` · ${c.count} msg${c.count === 1 ? "" : "s"}` : ""}
                </span>
                <button type="button" className={`chatov-hist-del ${confirmDel === c.id ? "is-armed" : ""}`}
                        title={confirmDel === c.id ? "Click again to delete" : "Delete this chat"}
                        aria-label={`Delete ${c.title}`}
                        onClick={(e) => deleteConv(e, c.id)} onBlur={() => setConfirmDel(null)}>
                  {confirmDel === c.id ? "Delete?" : <Icon name="trash" size={14} />}
                </button>
              </div>
            ))}
          </div>
        </div>
        <div className="chatov-body" ref={bodyRef}>
          {messages.length === 0 && (
            <div className="chat-start">
              <div className="chat-start-hi">Standing by, sir.</div>
              <div className="chat-start-sub">Ask me anything — type below, or say “Hey Jarvis”.</div>
              <div className="chat-start-list">
                {CHAT_STARTERS.map((s) => (
                  <button key={s} className="chat-starter" onClick={() => send(s)}>
                    <Icon name="chevRight" size={14} />{s}
                  </button>
                ))}
              </div>
            </div>
          )}
          {messages.map((m) => (
            <ChatMessage key={m.id} m={m} runAction={runAction} onCopy={copy} />
          ))}
          {status === "thinking" && (
            <div className="cmsg cmsg--jarvis"><span className="cmsg-role">J.A.R.V.I.S</span>
              <div className="cmsg-bubble cmsg-typing"><i /><i /><i /></div></div>
          )}
        </div>
        <div className="chatov-compose">
          <div className="chatov-input">
            {onUpload && (
              <>
                <input
                  ref={fileRef}
                  type="file"
                  accept="image/*,.pdf,.txt,.md,.csv,.json,.log"
                  onChange={pickFile}
                  style={{ display: "none" }}
                />
                <button
                  className="chatov-attach"
                  title="Attach an image or document"
                  aria-label="Attach an image or document"
                  onClick={() => fileRef.current?.click()}
                >
                  <Icon name="attach" size={18} />
                </button>
              </>
            )}
            <textarea
              ref={inputRef}
              rows={1}
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(); } }}
              placeholder="Message Jarvis…"
              aria-label="Message"
            />
            {busy && onStop ? (
              <button className="chatov-send chatov-stop" title="Stop the response" aria-label="Stop the response" onClick={onStop}>
                <Icon name="stop" size={15} stroke={2} />
              </button>
            ) : (
              <button className="chatov-send" onClick={() => send()} disabled={!draft.trim()} title="Send (Enter)" aria-label="Send">
                <Icon name="send" size={18} />
              </button>
            )}
          </div>
          <div className="chatov-foot">
            <label className="chatov-tier-wrap" title="How capable a model to use for this message">
              <span>Model</span>
              <select
                className="chatov-tier"
                value={taskTier}
                onChange={(e) => setTaskTier(e.target.value)}
                aria-label="Model capability for this task"
              >
                {tierOptions.map((tier) => (
                  <option key={tier.value} value={tier.value}>
                    {tier.label}
                  </option>
                ))}
              </select>
            </label>
            <span className="chatov-hint"><kbd>Enter</kbd> send · <kbd>Shift</kbd>+<kbd>Enter</kbd> new line</span>
          </div>
        </div>
      </aside>
    </div>
  );
}
