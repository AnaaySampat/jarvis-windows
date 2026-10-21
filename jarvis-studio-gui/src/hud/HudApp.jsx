/* HudApp.jsx — the home screen: a header (menus + quick controls), JARVIS at the
   centre (time, the orb, what it's doing, its last reply and an ask box), and the
   live cards in a column on each side. Responsive: it lays out for the window
   rather than scaling a fixed picture. App owns every open/collapse state so
   JARVIS can drive the screen by voice; `screen` (Customize) holds the look (colours,
   font, corners, glow, card style, centrepiece, gauges, background) and the layout
   (density, which cards show, and where — drag a card's header to move it). */

import { useState, useEffect, useRef } from "react";
import "./hud.css";
import "./home.css";
import Orb from "./Orb";
import Reactor, { Waveform } from "./Reactor";
import "./ironman.css";
import { plainPreview as plain } from "../utils/markdown";
import { Icon } from "./icons";
import {
  DEFAULT_TELEMETRY, SystemWidget, PowerWidget, AgendaWidget, WeatherWidget, NetworkWidget, LogWidget,
  useCardDrop,
} from "./widgets";
import { SkillsOverlay, CapabilitiesOverlay, PowerOverlay, ChatOverlay, MemoryOverlay } from "./HudPanels";
import { SOURCE_LABEL } from "../utils/taskSource";

/* Theme defaults (App recolours them from the user's screen settings). */
export const DIRECTIONS = [
  { id: "default", name: "JARVIS", accent: "#00e5ff", accent2: "#6fe9ff", rgb: [0, 229, 255] },
];

const STATUS_TEXT = {
  idle:      ["Standing by", "Say “Hey Jarvis”, hold Ctrl+Space, or click the orb."],
  listening: ["Listening…", "Go ahead — click the orb to cancel."],
  thinking:  ["Thinking…", "Click the orb to stop."],
  speaking:  ["Speaking", "Click the orb to stop."],
  working:   ["Working on it…", "Click the orb to stop."],
};

const STARTERS = [
  "What's on my agenda today?",
  "What's the weather this week?",
  "Play lofi on YouTube",
];

function greeting(h) {
  return h < 5 ? "Good evening" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : "Good evening";
}

/* Ticks on its own so the rest of the screen doesn't re-render every second. */
function HeroClock() {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);
  const parts = new Intl.DateTimeFormat([], { hour: "numeric", minute: "2-digit" }).formatToParts(now);
  const time = parts.filter((p) => p.type !== "dayPeriod").map((p) => p.value).join("").trim();
  const period = parts.find((p) => p.type === "dayPeriod")?.value;
  return (
    <div className="hero-clock">
      <div className="hero-greet">{greeting(now.getHours())}</div>
      <div className="hero-time">{time}{period && <span>{period}</span>}</div>
      <div className="hero-date">{now.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" })}</div>
    </div>
  );
}

/* The ask box: a message goes straight to JARVIS and the reply appears above it. */
function AskBar({ inputRef, onSend, onListen, status }) {
  const [text, setText] = useState("");
  const submit = (e) => {
    e.preventDefault();
    const t = text.trim();
    if (!t) return;
    onSend(t);
    setText("");
  };
  return (
    <form className="ask" onSubmit={submit}>
      <input ref={inputRef} value={text} onChange={(e) => setText(e.target.value)}
             placeholder="Ask JARVIS anything…" aria-label="Ask JARVIS" />
      {!text && <kbd className="ask-kbd" title="Press / to type here">/</kbd>}
      <button type="button" className={`ask-btn${status === "listening" ? " is-on" : ""}`}
              onClick={onListen} aria-label="Talk" title="Talk (or hold Ctrl+Space)">
        <Icon name="mic" size={17} />
      </button>
      <button type="submit" className="ask-btn ask-btn--send" disabled={!text.trim()} aria-label="Send" title="Send">
        <Icon name="send" size={17} />
      </button>
    </form>
  );
}

const NAV = [
  { id: "chat", label: "Chat", icon: "chat" },
  { id: "memory", label: "Memory", icon: "memory" },
  { id: "skills", label: "Skills", icon: "skills" },
  { id: "caps", label: "Capabilities", icon: "caps" },
];

export function JarvisHUD({
  config,
  screen,                       // home-screen look & layout (see the header comment)
  onArrange,                    // a card was dragged: the new {left, right} order
  toolbar,                      // the quick controls (App owns their state)
  // live data
  status = "idle",
  activeTask,                   // the running autopilot task {goal, source, …}, if any
  agentMode = false,            // agent mode (agents.py) is on: a pill by the name says so
  telemetry,
  weather,
  netInfo,
  schedule,
  messages = [],
  commands = [],
  recordings = {},
  isConnected = false,
  allowedDirs = [],
  // collapsible card state (controlled by App so JARVIS can toggle them)
  panels = {}, onTogglePanel,
  // overlays (App owns these so JARVIS can drive them too)
  chatOpen, setChatOpen,
  skillsOpen, setSkillsOpen,
  capsOpen, setCapsOpen,
  powerOpen, setPowerOpen,
  memoryOpen, setMemoryOpen,
  // handlers
  onSend,
  onAsk,                        // send a message and show the chat (Capabilities "Try")
  onNotify,                     // brief confirmation toast
  onStop,
  onListen,
  onReset,
  onUpload,
  taskModelTiers = [],
  runAction,
  onSaveDirs,
  // conversation history ("Recents")
  conversations = [],
  onNewChat,
  onOpenConversation,
  onDeleteConversation,
  onRefreshConversations,
  onClearConversations,
  // memory view
  memory, onRemember, onForget, onSaveRoutine, onRunRoutine, routineResult,
}) {
  const c = config;
  const d = telemetry || DEFAULT_TELEMETRY;
  const askRef = useRef(null);
  const p = (k, def = true) => (panels[k] === undefined ? def : panels[k]);
  const tog = (k) => (v) => onTogglePanel && onTogglePanel(k, v);
  const closeMemory = () => setMemoryOpen(false);

  // Screen config: the look, then the layout (visibility + per-side order + density).
  const sc = screen || {};
  const vis = sc.panels || {};
  const order = sc.order || { left: ["system", "power", "agenda"], right: ["weather", "network", "terminal"] };
  const background = sc.background || "grid";
  const density = sc.density || "normal";
  const hudCards = sc.cards === "hud";
  const reactor = sc.core === "reactor";
  const Core = reactor ? Reactor : Orb;
  const rings = sc.gauges === "rings";
  const lookStyle = {
    "--ac": c.accent, "--ac2": c.accent2,
    "--h-r": `${sc.corners ?? 14}px`,
    "--glow": (sc.glow ?? 40) / 100,
    // the backend only keeps a base64 image data: URL, so it can't break out of url("")
    ...(background === "image" && sc.image ? { "--bg-img": `url("${sc.image}")` } : {}),
  };
  const shown = (key) => vis[key] !== false;
  const [drop, dropZone] = useCardDrop(order, (o) => onArrange && onArrange(o));

  // "/" puts the cursor in the ask box (when nothing is open over the home screen).
  const overlayOpen = chatOpen || skillsOpen || capsOpen || powerOpen || memoryOpen;
  useEffect(() => {
    const onKey = (e) => {
      if (e.key !== "/" || e.ctrlKey || e.metaKey || e.altKey || overlayOpen) return;
      const t = e.target;
      if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
      if (document.querySelector(".settings-overlay, .cmdk-overlay, .perm-overlay")) return;
      e.preventDefault();
      askRef.current?.focus({ preventScroll: true });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [overlayOpen]);

  const CARDS = {
    system: <SystemWidget key="system" d={d} live={!!telemetry} open={p("system")} onToggle={tog("system")} rings={rings} />,
    power: <PowerWidget key="power" d={d} open={p("power")} onToggle={tog("power")} rings={rings} />,
    agenda: <AgendaWidget key="agenda" items={schedule} open={p("agenda")} onToggle={tog("agenda")} runAction={runAction} />,
    weather: <WeatherWidget key="weather" weather={weather} open={p("weather")} onToggle={tog("weather")} />,
    network: <NetworkWidget key="network" d={d} live={!!telemetry} netInfo={netInfo} open={p("network")} onToggle={tog("network")} />,
    terminal: <LogWidget key="terminal" commands={commands} open={p("terminal", false)} onToggle={tog("terminal")} />,
  };
  const column = (side) => {
    const cards = (order[side] || []).filter(shown).filter((k) => CARDS[k]);
    const line = <div key="drop" className="card-drop" aria-hidden="true" />;
    const out = cards.flatMap((k) => (drop?.side === side && drop.before === k ? [line, CARDS[k]] : [CARDS[k]]));
    return drop?.side === side && !cards.includes(drop.before) ? [...out, line] : out;
  };

  // A task from the phone runs without a turn of its own here, so the status can be
  // idle while it works: the running task still says what JARVIS is doing.
  const shownStatus = status === "idle" && activeTask ? "working" : status;
  const [title, orbHint] = STATUS_TEXT[shownStatus] || STATUS_TEXT.idle;
  const hint = reactor ? orbHint.replace("the orb", "the reactor") : orbHint;
  const busy = shownStatus !== "idle";
  const taskLine = activeTask
    ? `${SOURCE_LABEL[activeTask.source] ? `${SOURCE_LABEL[activeTask.source]}: ` : ""}${activeTask.goal}`
    : "";
  const lastReplyAt = messages.map((m) => m.role !== "user").lastIndexOf(true);
  const lastReply = lastReplyAt >= 0 ? messages[lastReplyAt] : null;
  const lastAsk = [...messages.slice(0, lastReplyAt >= 0 ? lastReplyAt : messages.length)].reverse()
    .find((m) => m.role === "user");
  const replyText = lastReply ? plain(lastReply.text) : "";
  const replyExtras = lastReply?.actions?.length || 0;
  const openNav = {
    chat: [chatOpen, () => setChatOpen(true)],
    memory: [memoryOpen, () => setMemoryOpen(true)],
    skills: [skillsOpen, () => setSkillsOpen(true)],
    caps: [capsOpen, () => setCapsOpen(true)],
  };

  return (
    <div className={`hud home home--bg-${background} home--${density} home--font-${sc.font || "clean"} home--cards-${sc.cards || "clean"}`}
         style={lookStyle}>
      <div className="home-bg" aria-hidden="true" />
      {hudCards && <div className="im-frame" aria-hidden="true" />}

      <header className="home-top">
        <div className="home-brand">
          <span className="home-logo" aria-hidden />
          <span className="home-name">JARVIS</span>
          <span className={`home-conn${isConnected ? " is-on" : ""}`}>{isConnected ? "Online" : "Offline"}</span>
          {agentMode && <span className="home-conn is-on" title="Agent mode: each request goes to a specialist agent">Agents</span>}
        </div>
        <nav className="home-nav" aria-label="Menus">
          {NAV.map((n) => (
            <button key={n.id} className={`home-nav-btn${openNav[n.id][0] ? " is-on" : ""}`}
                    onClick={openNav[n.id][1]} title={n.label}>
              <Icon name={n.icon} size={16} /><span>{n.label}</span>
            </button>
          ))}
        </nav>
        <div className="home-tools">{toolbar}</div>
      </header>

      <main className="home-main">
        <aside className="home-col home-col--left" aria-label="Left cards" {...dropZone("left")}>{column("left")}</aside>

        <section className="home-hero" aria-label="JARVIS">
          <HeroClock />
          <Core status={shownStatus} rgb={c.rgb} onClick={busy ? onStop : onListen}
                label={busy ? "Stop" : "Talk to JARVIS"} />
          <div className="hero-status" role="status">
            {hudCards && <Waveform status={shownStatus} />}
            <div className={`hero-title hero-title--${shownStatus}`}><span className="hero-dot" />{title}</div>
            <div className="hero-hint">{shownStatus === "working" && taskLine ? taskLine : hint}</div>
          </div>

          {lastReply && (
            <button type="button" className="hero-reply" onClick={() => setChatOpen(true)} title="Open the conversation">
              {lastAsk && <span className="hero-reply-q">{plain(lastAsk.text)}</span>}
              <span className="hero-reply-a">
                {replyText || (lastReply.streaming ? "…" : replyExtras ? "Done — see the result in the chat." : "…")}
              </span>
              <span className="hero-reply-more">
                {replyExtras > 0 && `${replyExtras} result${replyExtras === 1 ? "" : "s"} · `}Open conversation
                <Icon name="arrowRight" size={13} />
              </span>
            </button>
          )}

          <AskBar inputRef={askRef} onSend={(t) => onSend(t)} onListen={onListen} status={status} />

          {messages.length === 0 && (
            <div className="hero-starters">
              {STARTERS.map((s) => (
                <button key={s} className="hero-starter" onClick={() => onSend(s)}>{s}</button>
              ))}
            </div>
          )}
          {hudCards && <div className="im-tag" aria-hidden="true">Just A Rather Very Intelligent System</div>}
        </section>

        <aside className="home-col home-col--right" aria-label="Right cards" {...dropZone("right")}>{column("right")}</aside>
      </main>

      {/* ── overlays ── */}
      <SkillsOverlay
        open={skillsOpen} onClose={() => setSkillsOpen(false)}
        recordings={recordings} runAction={runAction}
        allowedDirs={allowedDirs} onSaveDirs={onSaveDirs}
        onShowResult={() => { setSkillsOpen(false); setChatOpen(true); }}
      />
      <CapabilitiesOverlay open={capsOpen} onClose={() => setCapsOpen(false)} onAsk={onAsk} />
      <PowerOverlay open={powerOpen} onClose={() => setPowerOpen(false)} runAction={runAction} />
      <MemoryOverlay
        open={memoryOpen} onClose={closeMemory} memory={memory}
        onRemember={onRemember} onForget={onForget}
        onSaveRoutine={onSaveRoutine} onRunRoutine={onRunRoutine} routineResult={routineResult}
        onOpenChat={() => { closeMemory(); setChatOpen(true); }}
        onOpenConversation={(id) => { onOpenConversation(id); closeMemory(); setChatOpen(true); }}
      />
      <ChatOverlay
        open={chatOpen} onClose={() => setChatOpen(false)}
        status={status} messages={messages} onSend={onSend} onStop={onStop} onReset={onReset}
        onUpload={onUpload} taskModelTiers={taskModelTiers} runAction={runAction} onNotify={onNotify}
        conversations={conversations} onNewChat={onNewChat}
        onOpenConversation={onOpenConversation} onDeleteConversation={onDeleteConversation}
        onRefreshConversations={onRefreshConversations} onClearConversations={onClearConversations}
      />
    </div>
  );
}
