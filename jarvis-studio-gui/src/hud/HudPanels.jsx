/* HudPanels.jsx — all the ringing data panels + chat & OCR overlays.
   Ported from jarvis-hud/hud-panels.jsx and wired to LIVE backend data
   (telemetry / weather / netinfo / schedule / messages) passed in as props.
   Exports each panel + STATUS_META + useClock  */

import { useState, useEffect, useRef, memo } from "react";
import { CornerBox, RadialGauge, MiniRadial, BarMeter, SegBar, Sparkline, TickStrip, DataRow } from "./HudGauges";
import { Waveform } from "./HudCore";
import ResponseRenderer from "../components/ResponseRenderer";
import { Icon } from "./icons";

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
export function SchedulePanel({ items, collapsible, open, onToggle, runAction }) {
  const list = items || [];
  const editable = typeof runAction === "function";

  const addItem = () => {
    const task = window.prompt("New agenda item — what is it?");
    if (!task || !task.trim()) return;
    const time = (window.prompt("At what time? (e.g. 09:00 — leave blank for none)") || "").trim();
    runAction({ type: "schedule", do: "add", day: "today", time, task: task.trim() });
  };
  const editItem = (it) => {
    const task = (window.prompt("Edit task:", it.task) || "").trim();
    const time = (window.prompt("Edit time (e.g. 09:00):", it.time || "") || "").trim();
    if (!task && !time) return;
    runAction({
      type: "schedule", do: "edit", day: "today",
      match: it.task, new_task: task || it.task, new_time: time || it.time,
    });
  };
  const removeItem = (it) =>
    runAction({ type: "schedule", do: "remove", day: "today", match: it.task });

  return (
    <CornerBox title="Agenda" code="TODAY" slot="schedule"
               collapsible={collapsible} open={open} onToggle={onToggle}>
      <div className="sch">
        {list.length === 0 ? (
          <div className="sch-empty">Nothing scheduled today.</div>
        ) : list.map((it, i) => (
          <div key={i} className={`sch-row ${it.now ? "now" : ""} ${it.done ? "done" : ""}`}>
            <span className="sch-t">{it.time}</span>
            <span className="sch-track"><span className="sch-dot" /></span>
            <span className="sch-task">{it.task}</span>
            {it.duration && <span className="sch-dur">{it.duration}</span>}
            {editable && (
              <span className="sch-edit">
                <button title="Edit" onClick={() => editItem(it)}>✎</button>
                <button title="Remove" onClick={() => removeItem(it)}>✕</button>
              </span>
            )}
          </div>
        ))}
      </div>
      {editable && (
        <button className="sch-add" onClick={addItem}>＋ Add item</button>
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
    { l: "SKILLS",       i: "skills", on: onOpenSkills },
    { l: "MEMORY",       i: "memory", on: onOpenMemory },
    { l: "CAPABILITIES", i: "caps",   on: onOpenCaps },
    { l: "POWER",        i: "power",  on: onOpenPower },
  ];
  return (
    <div className="dock" data-slot="dock">
      {items.map((it) => (
        <button key={it.l} className="dock-btn" onClick={it.on}>
          <span className="dock-i"><Icon name={it.i} /></span>
          <span className="dock-l">{it.l}</span>
        </button>
      ))}
      <button className="dock-btn dock-btn--chat" onClick={onOpenChat}>
        <span className="dock-i"><Icon name="chat" /></span>
        <span className="dock-l">CONVERSATION</span>
        {chatCount > 0 && <span className="dock-badge">{chatCount}</span>}
      </button>
    </div>
  );
}

/* ───────── Skills overlay — recording, QR, file-system access ───────── */
export function SkillsOverlay({ open, onClose, recordings = {}, runAction, onCommand, allowedDirs = [], onSaveDirs }) {
  const [newDir, setNewDir] = useState("");
  const rec = (media) => {
    const active = recordings[media];
    runAction({ type: "record", media, do: active ? "stop" : "start" });
  };
  const makeQr = () => {
    const v = window.prompt("What link or text should the QR code contain?");
    if (v && v.trim()) runAction({ type: "qr_code", text: v.trim() });
  };
  const browse = () => {
    const v = window.prompt("Which approved folder should I list?");
    if (v && v.trim()) runAction({ type: "list_dir", path: v.trim() });
  };
  const addDir = () => {
    const d = newDir.trim().replace(/^["']|["']$/g, "");
    if (d && !allowedDirs.includes(d)) onSaveDirs([...allowedDirs, d]);
    setNewDir("");
  };
  const removeDir = (d) => onSaveDirs(allowedDirs.filter((x) => x !== d));

  // A render helper, not a component: a component declared in render remounts
  // its buttons on every render.
  const recRow = (media, label) => (
    <button key={media} className={`skill-rec ${recordings[media] ? "skill-rec--on" : ""}`} onClick={() => rec(media)}>
      <span className="skill-rec-dot" />
      <span className="skill-rec-l">{label}</span>
      <span className="skill-rec-state">{recordings[media] ? "■ STOP" : "● START"}</span>
    </button>
  );

  return (
    <div className={`sideov ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="sideov-scrim" onClick={onClose} />
      <aside className="sideov-panel">
        <header className="sideov-hd">
          <span className="sideov-title">SKILLS</span>
          <button className="chatov-x" onClick={onClose}>✕</button>
        </header>
        <div className="sideov-body">
          <section className="skill-sec">
            <h4>Recording</h4>
            {recRow("audio", "Audio (microphone)")}
            {recRow("video", "Video (webcam)")}
            {recRow("screen", "Screen")}
            <p className="skill-note">Recordings run until you stop them — saved to your Jarvis storage folder.</p>
          </section>

          <section className="skill-sec">
            <h4>Quick tools</h4>
            <div className="skill-tools">
              <button className="skill-tool" onClick={makeQr}>▤ QR Code</button>
              <button className="skill-tool" onClick={() => runAction({ type: "screenshot" })}>▣ Screenshot</button>
              <button className="skill-tool" onClick={browse}>📂 List folder</button>
            </div>
          </section>

          <section className="skill-sec">
            <h4>File-system access <span className="skill-safe">SAFE · read-only</span></h4>
            <p className="skill-note">
              JARVIS may only <strong>list</strong> these folders and <strong>read</strong> files inside them
              (each read asks your approval). It can never run, write, move, or delete anything.
            </p>
            <div className="dirlist">
              {allowedDirs.length === 0 && <div className="dirlist-empty">No folders allowed yet.</div>}
              {allowedDirs.map((d) => (
                <div key={d} className="dirlist-row">
                  <span className="dirlist-path" title={d}>{d}</span>
                  <button className="dirlist-rm" title="Disallow" onClick={() => removeDir(d)}>✕</button>
                </div>
              ))}
            </div>
            <div className="dirlist-add">
              <input value={newDir} placeholder="C:\Users\you\Documents"
                     onChange={(e) => setNewDir(e.target.value)}
                     onKeyDown={(e) => { if (e.key === "Enter") addDir(); }} />
              <button className="dirlist-addbtn" onClick={addDir}>ALLOW</button>
            </div>
          </section>
        </div>
      </aside>
    </div>
  );
}

/* ───────── Capabilities overlay — what JARVIS can do ───────── */
const CAPABILITIES = [
  { group: "Conversation", items: ["Answer questions, explain, advise", "Render charts, tables, schedules & flowcharts", "Translate text (typed or from the camera)", "Talk by wake word, Ctrl+Space, always-on listening, native voice, or chat", "Conversation mode — short, natural back-and-forth", "Speaks with a natural British neural voice (offline Piper, or ElevenLabs)", "Attach an image or document in chat and ask about it"] },
  { group: "Memory", items: ["Remembers durable facts about you across sessions", "Recalls recent conversation context", "Forgets facts on request"] },
  { group: "Your computer", items: ["Open / close apps & folders", "Web search & open links", "Volume up/down, exact volume, mute, media keys", "Lock the screen", "Read your clipboard on request"] },
  { group: "Web browser (his hands)", items: ["Autopilot: give a whole task — “play lofi on YouTube” — and he silently opens, clicks & types until it's done", "You only hear the outcome — never the steps or numbers", "Sees the page's real buttons & links (JS)", "Reads & summarises a page"] },
  { group: "Desktop autopilot", items: ["Drives desktop apps by their real, named controls (asks once)", "Focuses or launches the right window himself", "Kill switch: top-left corner slam, Stop button, or “disarm”"] },
  { group: "Power (asks first)", items: ["Shut down · Restart · Sleep · Hibernate · Log off"] },
  { group: "Create & capture", items: ["Screenshots", "QR codes", "Generate images (Gemini, needs a Gemini key)", "Record audio, webcam, or screen (until you stop)", "Make PDFs from text", "Open files he created"] },
  { group: "Read (text-only model)", items: ["Read & summarise PDFs", "List approved folders", "Read approved text files (asks first)"] },
  { group: "Eyes & senses", items: ["Look at your screen and describe/answer (vision)", "Live system, power, weather, network & agenda readouts"] },
  { group: "Out in the world", items: ["Current weather + multi-day forecast", "Nearby places — food, cafés, pharmacy, ATM, fuel…", "Directions & travel time to a place", "Latest news headlines by topic"] },
  { group: "Reminders & routines", items: ["One-off reminders & timers", "Recurring routines / spoken briefings", "Manage your daily agenda"] },
  { group: "Learns & adapts", items: ["Teach him playbooks — named multi-step recipes", "Restyle the HUD — colour, background, density", "Show / hide / rearrange panels by voice"] },
  { group: "Apps", items: ["Spotify — play/pause/next/previous, play a song"] },
  { group: "This HUD", items: ["Open/close panels (skills, power, terminal, chat, settings)", "Expand/collapse system, power, weather, network, agenda", "Clear the chat / start a new conversation", "Mute or silence himself"] },
];
export function CapabilitiesOverlay({ open, onClose, onCommand }) {
  return (
    <div className={`sideov sideov--left ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="sideov-scrim" onClick={onClose} />
      <aside className="sideov-panel">
        <header className="sideov-hd">
          <span className="sideov-title">CAPABILITIES</span>
          <button className="chatov-x" onClick={onClose}>✕</button>
        </header>
        <div className="sideov-body">
          <p className="skill-note">Just ask in plain language — by voice ("Hey Jarvis") or chat.</p>
          {CAPABILITIES.map((c) => (
            <section key={c.group} className="cap-sec">
              <h4>{c.group}</h4>
              <ul className="cap-list">
                {c.items.map((it, i) => <li key={i}>{it}</li>)}
              </ul>
            </section>
          ))}
        </div>
      </aside>
    </div>
  );
}

/* ───────── Power overlay — shutdown / restart / sleep ───────── */
export function PowerOverlay({ open, onClose, runAction }) {
  const act = (command) => { runAction({ type: "power", command }); onClose(); };
  const btns = [
    { l: "SLEEP",     c: "sleep",    i: "🌙", note: "Suspend to RAM" },
    { l: "RESTART",   c: "restart",  i: "🔄", note: "Reboot (asks first)" },
    { l: "SHUT DOWN", c: "shutdown", i: "⏻", note: "Power off (asks first)", danger: true },
    { l: "LOCK",      c: "lock",     i: "🔒", note: "Lock the screen" },
    { l: "HIBERNATE", c: "hibernate",i: "❄", note: "Save & power off (asks first)" },
    { l: "LOG OFF",   c: "logoff",   i: "🚪", note: "Sign out (asks first)" },
  ];
  return (
    <div className={`powov ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="powov-scrim" onClick={onClose} />
      <div className="powov-panel" role="dialog" aria-label="Power options">
        <header className="powov-hd">
          <span className="powov-title">POWER</span>
          <button className="chatov-x" onClick={onClose}>✕</button>
        </header>
        <div className="powov-grid">
          {btns.map((b) => (
            <button key={b.c} className={`powov-btn ${b.danger ? "powov-btn--danger" : ""}`} onClick={() => act(b.c)}>
              <span className="powov-i">{b.i}</span>
              <span className="powov-l">{b.l}</span>
              <span className="powov-note">{b.note}</span>
            </button>
          ))}
        </div>
        <p className="skill-note">Shut down, restart, hibernate & log off ask for your approval first.</p>
      </div>
    </div>
  );
}


/* ───────── Memory overlay — everything JARVIS has stored, with forget ───────── */
const MEM_KINDS = [
  { key: "facts", kind: "fact", label: "About you", color: "var(--ac)",
    note: "JARVIS reads these before every reply." },
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

function memDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString([], { month: "short", day: "numeric", ...(sameYear ? {} : { year: "numeric" }) });
}

// "1) click X. 2) read the page." → ["click X", "read the page"]
const memSteps = (s) => (s || "").split(/\s*\d+\)\s*/).map((x) => x.trim().replace(/\.$/, "")).filter(Boolean);

export function MemoryOverlay({ open, onClose, memory, onRemember, onForget, onOpenConversation, onOpenChat }) {
  const [tab, setTab] = useState("facts");
  const [draft, setDraft] = useState("");
  const [armed, setArmed] = useState(null);   // id awaiting its confirming second click
  const [focus, setFocus] = useState(null);   // id picked on the timeline
  const mainRef = useRef(null);
  const m = memory || {};
  const lists = {
    facts: [...(m.facts || [])].reverse(),
    playbooks: m.playbooks || [],
    learned: [...(m.learned || [])].reverse(),
    conversations: m.conversations || [],
  };

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

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

  const pick = (key) => { setTab(key); setFocus(null); setArmed(null); mainRef.current?.scrollTo(0, 0); };
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
  const list = lists[tab];

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
          <button className="memx-close" onClick={onClose} aria-label="Close memory"><Icon name="close" /></button>
        </header>

        <div className="memx-tl" aria-hidden="true">
          <div className="memx-tl-track">
            {dated.map(({ x, k }) => (
              <button key={`${k.key}-${x.id}`} tabIndex={-1} className="memx-tick"
                      style={{ left: at(x.ts), "--kc": k.color }}
                      title={`${k.label} · ${memDate(x.ts)} — ${memTitle(k.key, x)}`}
                      onClick={() => { setTab(k.key); setFocus(x.id); setArmed(null); }} />
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
                <span className="memx-count">{memory ? lists[k.key].length : "–"}</span>
              </button>
            ))}
            <p className="memx-navnote">Forgetting is permanent. You can also say “forget that…”.</p>
          </nav>

          <div className="memx-main" ref={mainRef}>
            <div className="memx-sec-hd">
              <h3>{cur.label}</h3>
              <p>{cur.note}</p>
            </div>

            {tab === "facts" && (
              <form className="memx-add" onSubmit={remember}>
                <input value={draft} onChange={(e) => setDraft(e.target.value)} aria-label="New fact"
                       placeholder="Something JARVIS should always know — e.g. I'm vegetarian" maxLength={300} />
                <button type="submit" disabled={!draft.trim()}>Remember</button>
              </form>
            )}

            {tab === "conversations" && m.current > 0 && (
              <div className="memx-row memx-row--current">
                <span className="memx-text">This conversation</span>
                <span className="memx-meta">{m.current} messages</span>
                <button type="button" className="memx-open" onClick={onOpenChat}>Open</button>
              </div>
            )}

            {!memory && <p className="memx-empty">Reading memory…</p>}
            {memory && list.length === 0 && (
              <p className="memx-empty">
                {{
                  facts: "Nothing yet. Tell JARVIS “remember that…”, or add something above.",
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
// skip both re-render and ResponseRenderer's re-parse. runAction is a stable
// useCallback, so it doesn't break the memo.
const ChatMessage = memo(function ChatMessage({ m, runAction }) {
  return (
    <div className={`cmsg cmsg--${m.role === "user" ? "user" : "jarvis"}`}>
      <span className="cmsg-role">{m.role === "user" ? "YOU" : "J.A.R.V.I.S"}</span>
      <div className="cmsg-bubble">
        {m.role === "user"
          ? m.text
          : <ResponseRenderer text={m.text} actions={m.actions} runAction={runAction} />}
      </div>
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

export function ChatOverlay({ open, onClose, status, messages = [], onSend, onStop, onReset, onUpload, runAction,
                              conversations = [], onNewChat, onOpenConversation, onDeleteConversation,
                              onRefreshConversations, onClearConversations, taskModelTiers = [] }) {
  const [draft, setDraft] = useState("");
  const [taskTier, setTaskTier] = useState("moderate");
  const [histOpen, setHistOpen] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const bodyRef = useRef(null);
  const fileRef = useRef(null);
  const tierOptions = taskModelTiers.length === 3 ? taskModelTiers : [
    { value: "dumb", label: "Dumb" },
    { value: "moderate", label: "Moderate" },
    { value: "very_smart", label: "Very smart" },
  ];

  const startNewChat = () => { onNewChat?.(); setHistOpen(false); };
  const toggleHist = () => {
    setConfirmClear(false);
    setHistOpen((o) => {
      const next = !o;
      if (next) onRefreshConversations?.();   // re-pull Recents when the drawer opens
      return next;
    });
  };
  const openConv = (id) => { onOpenConversation?.(id); setHistOpen(false); };
  // Two-click guard so a stray tap can't wipe the whole archive.
  const clearAll = () => {
    if (confirmClear) { onClearConversations?.(); setConfirmClear(false); }
    else { setConfirmClear(true); }
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

  const send = () => {
    const t = draft.trim();
    if (!t) return;
    onSend(t, taskTier);
    setDraft("");
  };

  // JARVIS is mid-reply when thinking/working or a bubble is still streaming —
  // show a Stop button (interrupts generation + speech) in place of Send.
  const busy = status === "thinking" || status === "working"
    || messages.some((m) => m.streaming);

  return (
    <div className={`chatov ${open ? "open" : ""}`} aria-hidden={!open}>
      <div className="chatov-scrim" onClick={onClose} />
      <aside className="chatov-panel">
        <header className="chatov-hd">
          <span className="chatov-title">CONVERSATION LOG</span>
          <div className="chatov-hd-btns">
            <button className="chatov-x chatov-new" title="New chat — saves this one to history" onClick={startNewChat}>＋</button>
            <button className={`chatov-x ${histOpen ? "chatov-x--on" : ""}`} title="Conversation history" onClick={toggleHist}>🕘</button>
            <button className="chatov-x" onClick={onClose}>✕</button>
          </div>
        </header>

        {/* Recents — saved conversations; slides over the log when the 🕘 is toggled */}
        <div className={`chatov-hist ${histOpen ? "open" : ""}`}>
          <div className="chatov-hist-hd">
            <span className="chatov-hist-title">RECENTS</span>
            <div className="chatov-hist-hd-btns">
              <button className="chatov-newbtn" onClick={startNewChat}>＋ NEW CHAT</button>
              {conversations.length > 0 && (
                <button className={`chatov-clearall ${confirmClear ? "confirm" : ""}`}
                        title="Delete all saved conversations" onClick={clearAll}>
                  {confirmClear ? "SURE?" : "CLEAR ALL"}
                </button>
              )}
              <button className="chatov-x" title="Back to chat" onClick={toggleHist}>✕</button>
            </div>
          </div>
          <div className="chatov-hist-list">
            {conversations.length === 0 && (
              <div className="chatov-hist-empty">No saved conversations yet. Start a new chat and this one moves here.</div>
            )}
            {conversations.map((c) => (
              <button key={c.id} className="chatov-hist-item" onClick={() => openConv(c.id)}>
                <span className="chatov-hist-name">{c.title}</span>
                <span className="chatov-hist-meta">
                  {timeAgo(c.ts)}{c.count ? ` · ${c.count} msg${c.count === 1 ? "" : "s"}` : ""}
                </span>
                <span className="chatov-hist-del" title="Delete this chat" role="button"
                      onClick={(e) => { e.stopPropagation(); onDeleteConversation?.(c.id); }}>🗑</span>
              </button>
            ))}
          </div>
        </div>
        <div className="chatov-body" ref={bodyRef}>
          {messages.length === 0 && (
            <div className="cmsg cmsg--jarvis">
              <span className="cmsg-role">J.A.R.V.I.S</span>
              <div className="cmsg-bubble">Standing by, sir. Ask me anything, or say "Hey Jarvis".</div>
            </div>
          )}
          {messages.map((m) => (
            <ChatMessage key={m.id} m={m} runAction={runAction} />
          ))}
          {status === "thinking" && (
            <div className="cmsg cmsg--jarvis"><span className="cmsg-role">J.A.R.V.I.S</span>
              <div className="cmsg-bubble cmsg-typing"><i /><i /><i /></div></div>
          )}
        </div>
        <div className="chatov-input">
          <select
            className="chatov-tier"
            value={taskTier}
            onChange={(e) => setTaskTier(e.target.value)}
            title="Choose the model capability for this message"
            aria-label="Model capability for this task"
          >
            {tierOptions.map((tier) => (
              <option key={tier.value} value={tier.value}>
                {tier.label}
              </option>
            ))}
          </select>
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
                onClick={() => fileRef.current?.click()}
              >
                📎
              </button>
            </>
          )}
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") send(); }}
            placeholder="Message Jarvis…"
          />
          {busy && onStop ? (
            <button className="chatov-send chatov-stop" title="Stop response" onClick={onStop}>■</button>
          ) : (
            <button className="chatov-send" onClick={send}>➤</button>
          )}
        </div>
      </aside>
    </div>
  );
}
