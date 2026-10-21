import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import { JarvisHUD, DIRECTIONS } from "./hud/HudApp";
import Settings from "./components/Settings";
import Onboarding from "./components/Onboarding";
import SetupProgress from "./components/SetupProgress";
import BootOverlay from "./components/BootOverlay";
import Customize from "./components/Customize";
import AgentActivity from "./components/AgentActivity";
import CommandPalette from "./components/CommandPalette";
import { Icon } from "./hud/icons";
import { syncBrowserPanel } from "./components/browserPanelWindow";
import { syncControlOverlay } from "./components/controlOverlayWindow";

// Section name (as JARVIS says it) → panels-state key. Module-scope constant —
// it never changes, so there's no reason to rebuild it on every render.
const SECTION_KEY = {
  system: "system", power_panel: "power", weather: "weather",
  network: "network", agenda: "agenda", schedule: "agenda", terminal: "terminal",
};

// Settings pages the command palette can open directly (ids from Settings.jsx).
const SETTINGS_PAGES = [
  ["ai", "Intelligence", "spark", "provider model llm vertex ollama"],
  ["keys", "API Keys", "key", "groq gemini openai anthropic credentials"],
  ["routing", "Routing & Quota", "route", "rate limit benched rank"],
  ["autopilot", "Autopilot", "cursor", "operator browser desktop"],
  ["voice", "Voice", "volume", "tts wake word heartbeat digest alerts"],
  ["interface", "Interface", "layout", "overlay pill shortcuts"],
  ["files", "Files & Location", "folder", "storage folders location skills"],
  ["devices", "Devices", "phone", "phone pairing tv whatsapp"],
  ["connectors", "Connectors", "plug", "connect gmail email github notion calendar todoist home assistant zapier apps integrations"],
  ["mcp", "MCP Servers", "plug", "tools integrations"],
  ["system", "System", "wrench", "repair components ram"],
];

// Mid-task clarify dialog: the autopilot hit genuine ambiguity and asked ONE
// question. Holds its own input state so typing doesn't re-render the whole app.
function ClarifyPrompt({ request, onRespond }) {
  const [answer, setAnswer] = useState("");
  useEffect(() => { setAnswer(""); }, [request?.id]);
  if (!request) return null;
  const submit = () => onRespond(request.id, answer.trim());
  return (
    <div className="perm-overlay">
      <div className="perm-dialog perm-dialog--ask" role="alertdialog" aria-modal="true" aria-label="JARVIS needs your input">
        <div className="perm-icon"><Icon name="caps" size={34} stroke={1.4} /></div>
        <div className="perm-kicker">JARVIS has a question</div>
        <div className="perm-desc">{request.question}</div>
        <input
          className="clarify-input"
          autoFocus
          value={answer}
          onChange={(e) => setAnswer(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && answer.trim()) submit(); }}
          placeholder="Type your answer…"
        />
        <div className="perm-actions">
          <button className="perm-deny" onClick={() => onRespond(request.id, "")}>Skip</button>
          <button className="perm-approve" disabled={!answer.trim()} onClick={submit}>Send</button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const {
    status, messages, warnings, sysInfo, isConnected,
    telemetry, netInfo, weather, schedule, uiCommand, permissionRequest, clarifyRequest, muted,
    commands, recordings, browserState, controlState, screen, alwaysOn, conversationMode,
    overlay, agentTasks, conversations, setupProgress,
    devicePairing, devices, tvState, waState, mcpState, connState,
    createPairing, listDevices, revokeDevice,
    dismissWarning, clearAgentTasks, sendConfig, refreshModels, rerankModels, repairSetup, sendMessage,
    resetConversation, newConversation, openConversation, deleteConversation, listConversations, clearConversations,
    memory, listMemory, rememberFact, forgetMemory, saveRoutine, runRoutine, routineResult,
    sendLocation, sendManualLocation, triggerListen, stopSpeech,
    stopControl, finishListen, pttStart,
    respondPermission, respondClarify, setMute, runAction, setBrowserOpen, sendUpload,
    sendScreen, setAlwaysOnMode, setConversationModeOn, wsSend,
  } = useWebSocket();

  // App owns the overlay state so JARVIS can drive them via ui_action too.
  const [chatOpen, setChatOpen] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [settingsPage, setSettingsPage] = useState(null);   // section to open Settings at
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [capsOpen, setCapsOpen] = useState(false);
  const [powerOpen, setPowerOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [customizeOpen, setCustomizeOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [toast, setToast] = useState(null);   // {text, id} — brief confirmation pill
  const notify = useCallback((text) => setToast({ text, id: Date.now() }), []);
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2200);
    return () => clearTimeout(t);
  }, [toast]);

  const openSettings = useCallback((page = null) => {
    setSettingsPage(page);
    setShowSettings(true);
  }, []);

  // Keyboard: Ctrl+, opens Settings (the desktop-app convention), Ctrl+K the command
  // palette, and Esc closes the topmost panel. Panels that use Esc themselves first
  // (Settings, the palette, a zoomed screenshot, chat's Recents) call preventDefault.
  const blocking = !!permissionRequest || !!clarifyRequest;
  useEffect(() => {
    const onKey = (e) => {
      const mod = e.ctrlKey || e.metaKey;
      if (mod && e.key === ",") { e.preventDefault(); openSettings(); return; }
      if (mod && e.key.toLowerCase() === "k" && !showSettings && !blocking) {
        e.preventDefault(); setPaletteOpen((o) => !o); return;
      }
      if (e.key !== "Escape" || e.defaultPrevented || showSettings || paletteOpen || blocking) return;
      const open = [
        [customizeOpen, setCustomizeOpen], [activityOpen, setActivityOpen], [memoryOpen, setMemoryOpen],
        [powerOpen, setPowerOpen], [chatOpen, setChatOpen], [skillsOpen, setSkillsOpen], [capsOpen, setCapsOpen],
      ].find(([isOpen]) => isOpen);
      if (open) { e.preventDefault(); open[1](false); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [openSettings, showSettings, paletteOpen, blocking, customizeOpen, activityOpen, memoryOpen,
      powerOpen, chatOpen, skillsOpen, capsOpen]);

  // Effective HUD config: the base preset recolored by the user/JARVIS screen
  // settings. Memoized so it keeps a stable identity across the many re-renders
  // driven by the websocket hot path (telemetry ~1.5s, stream_delta many/sec) —
  // otherwise a fresh object every render cascades new props through the whole
  // HUD tree and defeats any child memoization.
  const hudConfig = useMemo(() => ({
    ...DIRECTIONS[0],
    accent: screen?.accent || DIRECTIONS[0].accent,
    accent2: screen?.accent2 || DIRECTIONS[0].accent2,
    rgb: screen?.rgb || DIRECTIONS[0].rgb,
  }), [screen?.accent, screen?.accent2, screen?.rgb]);

  // The menus outside the HUD stage (Settings, Customize, dialogs) take the same
  // accent, so a theme change recolours the whole app rather than just the HUD.
  useEffect(() => {
    const root = document.documentElement.style;
    root.setProperty("--accent", hudConfig.accent);
    root.setProperty("--accent-soft", hudConfig.accent2);
  }, [hudConfig.accent, hudConfig.accent2]);

  // Collapsible data panels — expanded by default; terminal starts collapsed.
  const [panels, setPanels] = useState({
    system: true, power: true, weather: true, network: true, agenda: true, terminal: false,
  });
  const togglePanel = useCallback((key, val) =>
    setPanels((p) => ({ ...p, [key]: val === undefined ? !p[key] : val })), []);
  const setAllPanels = useCallback((open) =>
    setPanels((p) => Object.fromEntries(Object.keys(p).map((k) => [k, open]))), []);

  // Save the read-only-terminal directory allowlist (Skills panel + Settings).
  const saveDirs = useCallback((dirs) => sendConfig({ allowed_dirs: dirs }), [sendConfig]);

  // ── Precise location for accurate weather + distances (browser GPS → backend) ─
  // enableHighAccuracy:true asks Windows for WiFi-based positioning (~tens–hundreds
  // of metres) instead of coarse IP geolocation, which was 2–3 km off. maximumAge:0
  // forces a fresh fix (not a stale cached one); we keep the MOST accurate reading
  // and let watchPosition refine it for ~30 s (the first fix is often coarse and
  // tightens as more readings arrive), then stop the sensor.
  const coordsRef = useRef(null);
  useEffect(() => {
    if (!navigator.geolocation) return;
    let best = null;
    const opts = { enableHighAccuracy: true, timeout: 20000, maximumAge: 0 };
    const onPos = (pos) => {
      const { latitude, longitude, accuracy } = pos.coords;
      if (!best || accuracy < best.accuracy) {       // keep the tightest fix seen
        best = { lat: latitude, lon: longitude, accuracy };
        coordsRef.current = best;
        sendLocation(best.lat, best.lon, accuracy);
      }
    };
    navigator.geolocation.getCurrentPosition(onPos, () => {}, opts);
    const watchId = navigator.geolocation.watchPosition(onPos, () => {}, opts);
    const stop = setTimeout(() => navigator.geolocation.clearWatch(watchId), 30000);
    return () => { navigator.geolocation.clearWatch(watchId); clearTimeout(stop); };
  }, [sendLocation]);

  // ── Global Ctrl+Space (registered in Rust) → push-to-talk ───────────────────
  // Hold to talk: pressing emits "ptt-start" (begin recording, ignore silence),
  // releasing emits "ptt-stop" (finish + transcribe what was said). Works even
  // when JARVIS is in the background. "trigger-listen" is kept as a back-compat
  // single-shot (tap-to-listen) in case an older binary emits it.
  useEffect(() => {
    const unlisteners = [];
    let disposed = false;
    (async () => {
      try {
        const { listen } = await import("@tauri-apps/api/event");
        unlisteners.push(await listen("ptt-start", () => pttStart()));
        unlisteners.push(await listen("ptt-stop", () => finishListen()));
        unlisteners.push(await listen("trigger-listen", () => triggerListen()));
        if (disposed) unlisteners.forEach((u) => u && u());
      } catch { /* browser dev preview — no Tauri runtime */ }
    })();
    return () => {
      disposed = true;
      unlisteners.forEach((u) => u && u());
    };
  }, [pttStart, finishListen, triggerListen]);

  // ── Bring JARVIS to the front when it needs your approval ───────────────────
  // FALLBACK only: the floating overlay pill now shows the Approve/Deny card
  // wherever the user is, so the main window doesn't need to steal focus. Only
  // when the overlay is disabled in Settings do we surface this window instead
  // (otherwise the prompt would sit unseen behind other apps).
  useEffect(() => {
    if (!permissionRequest) return;
    if (overlay?.enabled !== false) return;   // overlay pill is handling it
    (async () => {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const win = getCurrentWindow();
        await win.show();
        await win.unminimize();
        await win.setFocus();
      } catch { /* browser dev preview — no Tauri runtime */ }
    })();
  }, [permissionRequest, overlay?.enabled]);

  // ── Browser companion panel (right rail beside Playwright Chromium) ─────────
  const browserPanelOpen = !!browserState.open
    || (agentTasks || []).some((t) => t.kind === "browser" && t.status === "running");
  useEffect(() => {
    syncBrowserPanel(browserPanelOpen, wsSend);
  }, [browserPanelOpen, wsSend]);

  // ── "JARVIS CONTROLLING" overlay (shown during desktop computer control) ────
  // Driven from the main window (a hidden window can't reliably show itself).
  const controlOverlayActive = !!controlState.armed
    || (agentTasks || []).some((t) => t.kind === "computer" && t.status === "running");
  useEffect(() => {
    syncControlOverlay(controlOverlayActive);
  }, [controlOverlayActive]);

  // Memory view: pull a fresh snapshot on open, and again after any executed
  // action while it's open (a spoken "remember…"/"forget…" changes what it shows).
  // The palette reads it too, for its "Run routine" entries.
  useEffect(() => {
    if (memoryOpen || paletteOpen) listMemory();
  }, [memoryOpen, paletteOpen, commands, listMemory]);

  // Re-send coords once the socket (re)connects, in case GPS resolved first.
  useEffect(() => {
    if (isConnected && coordsRef.current) {
      sendLocation(coordsRef.current.lat, coordsRef.current.lon);
    }
  }, [isConnected, sendLocation]);

  // ── JARVIS controlling its own interface (ui_action events) ────────────────
  useEffect(() => {
    if (!uiCommand) return;
    const cmd = uiCommand.cmd;
    // Generic expand_/collapse_/toggle_<section>
    const m = /^(expand|collapse|toggle)_(.+)$/.exec(cmd || "");
    if (m) {
      // "minimize/expand ALL the panels" — apply the verb to every panel at once.
      if (m[2] === "all") {
        setPanels((p) => Object.fromEntries(Object.keys(p).map((k) =>
          [k, m[1] === "toggle" ? !p[k] : m[1] === "expand"])));
        return;
      }
      const key = SECTION_KEY[m[2]] || m[2];
      if (m[1] === "expand") togglePanel(key, true);
      else if (m[1] === "collapse") togglePanel(key, false);
      else togglePanel(key);
      // terminal lives in a rail too — toggling its panel is enough
      return;
    }
    switch (cmd) {
      case "open_chat": case "open_conversation": case "conversation": setChatOpen(true); break;
      case "close_chat": setChatOpen(false); break;
      case "open_settings": case "settings": openSettings(); break;
      case "close_settings": setShowSettings(false); break;
      case "open_skills": case "skills": setSkillsOpen(true); break;
      case "close_skills": setSkillsOpen(false); break;
      case "open_capabilities": case "capabilities": setCapsOpen(true); break;
      case "close_capabilities": setCapsOpen(false); break;
      case "open_power": case "power_menu": setPowerOpen(true); break;
      case "close_power": setPowerOpen(false); break;
      case "open_memory": case "memory": setMemoryOpen(true); break;
      case "close_memory": setMemoryOpen(false); break;
      case "open_terminal": case "terminal": togglePanel("terminal", true); break;
      case "close_terminal": togglePanel("terminal", false); break;
      case "open_activity": case "activity": setActivityOpen(true); break;
      case "close_activity": setActivityOpen(false); break;
      case "listen": case "mic": triggerListen(); break;
      case "stop_speaking": case "stop": stopSpeech(); break;
      case "clear_chat": case "new_conversation": case "reset": resetConversation(); break;
      default: break;
    }
  }, [uiCommand?.nonce]); // eslint-disable-line react-hooks/exhaustive-deps

  // Send a message as if typed in chat, and show the chat so the reply is seen.
  const askInChat = useCallback((text) => {
    sendMessage(text);
    setChatOpen(true);
  }, [sendMessage]);

  const running = agentTasks.some((t) => t.status === "running");

  // ── Command palette entries ────────────────────────────────────────────────
  const paletteCommands = useMemo(() => {
    const cmd = (id, group, label, icon, run, extra = {}) => ({ id, group, label, icon, run, ...extra });
    const rec = (media, label) => cmd(`rec-${media}`, "Tools",
      `${recordings[media] ? "Stop" : "Start"} ${label} recording`, "record",
      () => runAction({ type: "record", media, do: recordings[media] ? "stop" : "start" }),
      { keywords: `record capture ${media}`, state: recordings[media] ? "Recording" : null });
    return [
      cmd("chat", "Panels", "Open conversation", "chat", () => setChatOpen(true), { keywords: "chat talk messages log" }),
      cmd("new-chat", "Panels", "New conversation", "plus", () => { newConversation(); setChatOpen(true); },
        { keywords: "chat reset clear fresh" }),
      cmd("memory", "Panels", "Memory — what JARVIS knows", "memory", () => setMemoryOpen(true),
        { keywords: "facts routines playbooks remember forget saved chats" }),
      cmd("skills", "Panels", "Skills", "skills", () => setSkillsOpen(true), { keywords: "record qr screenshot folders" }),
      cmd("caps", "Panels", "What can JARVIS do?", "caps", () => setCapsOpen(true), { keywords: "capabilities help features" }),
      cmd("activity", "Panels", "Agent activity", "activity", () => setActivityOpen(true),
        { keywords: "autopilot steps screenshots tasks", state: running ? "Running" : null }),
      cmd("power", "Panels", "Power options", "power", () => setPowerOpen(true), { keywords: "shutdown restart sleep lock" }),
      cmd("customize", "Panels", "Customize the home screen", "palette", () => setCustomizeOpen(true),
        { keywords: "theme preset colour color accent font glow corners background picture image layout panels cards drag arrange iron man" }),
      cmd("settings", "Panels", "Settings", "settings", () => openSettings(), { keys: ["Ctrl", ","] }),
      cmd("agent-mode", "Assistant", sysInfo.agent_mode ? "Switch to simple mode" : "Switch to agent mode", "route",
        () => { sendConfig({ agent_mode: !sysInfo.agent_mode }); notify(sysInfo.agent_mode ? "Simple mode" : "Agent mode: specialists on each request"); },
        { keywords: "agents agent mode simple mode coordinator browser desktop specialist plan", state: sysInfo.agent_mode ? "Agents" : null }),
      cmd("connect-gmail", "Connections", "Connect Gmail", "message", () => openSettings("connectors"),
        { keywords: "email mail inbox connector app password" }),
      cmd("connectors", "Connections", "Connect an app (GitHub, Notion, Calendar…)", "plug", () => openSettings("connectors"),
        { keywords: "connectors integrations github notion google calendar todoist home assistant zapier brave folders" }),

      cmd("listen", "Voice", "Start listening", "mic", triggerListen, { keywords: "talk speak microphone", keys: ["Ctrl", "Space"] }),
      cmd("stop", "Voice", "Stop speaking", "stop", stopSpeech, { keywords: "silence quiet interrupt" }),
      cmd("mute", "Voice", muted ? "Unmute JARVIS" : "Mute JARVIS (text only)", muted ? "volume" : "mute",
        () => setMute(!muted), { keywords: "sound silence voice", state: muted ? "Muted" : null }),
      cmd("always-on", "Voice", alwaysOn ? "Turn off always-on listening" : "Turn on always-on listening", "wake",
        () => setAlwaysOnMode(!alwaysOn), { keywords: "wake word continuous hands free", state: alwaysOn ? "On" : null }),
      cmd("conv-mode", "Voice", conversationMode ? "Turn off conversation mode" : "Turn on conversation mode", "talk",
        () => setConversationModeOn(!conversationMode), { keywords: "natural brief chatty", state: conversationMode ? "On" : null }),

      cmd("screenshot", "Tools", "Take a screenshot", "camera", () => runAction({ type: "screenshot" }), { keywords: "capture screen" }),
      rec("audio", "audio"), rec("video", "webcam"), rec("screen", "screen"),
      ...(browserState.available ? [cmd("browser", "Tools", browserState.open ? "Close JARVIS's browser" : "Open JARVIS's browser",
        "globe", () => setBrowserOpen(!browserState.open), { keywords: "web chromium", state: browserState.open ? "Open" : null })] : []),
      cmd("lock", "Tools", "Lock the screen", "lock", () => runAction({ type: "power", command: "lock" }), { keywords: "power away" }),

      cmd("expand-all", "Home screen", "Expand all panels", "chevDown", () => setAllPanels(true), { keywords: "open show" }),
      cmd("collapse-all", "Home screen", "Collapse all panels", "chevUp", () => setAllPanels(false), { keywords: "minimize hide close" }),
      cmd("terminal", "Home screen", panels.terminal ? "Collapse the terminal log" : "Expand the terminal log", "keyboard",
        () => togglePanel("terminal"), { keywords: "command log" }),

      ...((memory?.routines) || []).map((r) => cmd(`routine-${r.id}`, "Routines", `Run routine: ${r.name}`, "play",
        () => { runRoutine(r.id); notify(`Running “${r.name}”`); }, { keywords: `routine ${r.when || ""}` })),

      ...SETTINGS_PAGES.map(([id, label, icon, words]) => cmd(`settings-${id}`, "Settings", `Settings: ${label}`, icon,
        () => openSettings(id), { keywords: `preferences configure ${words}` })),
    ];
  }, [recordings, runAction, newConversation, running, openSettings, triggerListen, stopSpeech, muted, setMute,
      alwaysOn, setAlwaysOnMode, conversationMode, setConversationModeOn, browserState.available, browserState.open,
      setBrowserOpen, setAllPanels, panels.terminal, togglePanel, memory?.routines, runRoutine, notify,
      sysInfo.agent_mode, sendConfig]);

  // Quick controls, shown in the home screen's header.
  const toolbar = (
    <div className="tb" role="toolbar" aria-label="Quick controls">
      <div className="tb-group" role="group" aria-label="Voice">
        {/* Tap-to-cut: stop whatever JARVIS is saying right now (only while speaking) */}
        {status === "speaking" && (
          <button className="hud-fixed-btn hud-fixed-btn--silence" data-tip="Stop speaking now"
                  aria-label="Stop speaking now" onClick={stopSpeech}>
            <Icon name="stop" size={16} />
          </button>
        )}
        {/* Persistent mute toggle: keeps speech off (text still shows) until untoggled */}
        <button
          className={`hud-fixed-btn ${muted ? "hud-fixed-btn--muted" : ""}`}
          data-tip={muted ? "Muted — replies show as text. Click to unmute" : "Mute JARVIS (text only)"}
          aria-label={muted ? "Unmute JARVIS" : "Mute JARVIS"}
          aria-pressed={muted}
          onClick={() => setMute(!muted)}
        >
          <Icon name={muted ? "mute" : "volume"} />
        </button>
        {/* Always-on continuous voice (no wake word) */}
        <button
          className={`hud-fixed-btn ${alwaysOn ? "hud-fixed-btn--listening" : ""}`}
          data-tip={alwaysOn
            ? "Always listening — click for wake-word mode"
            : "Listen without the wake word"}
          aria-label="Always-on listening"
          aria-pressed={alwaysOn}
          onClick={() => setAlwaysOnMode(!alwaysOn)}
        >
          <Icon name={alwaysOn ? "mic" : "wake"} />
        </button>
        {/* Natural conversation mode (shorter, chattier replies) */}
        <button
          className={`hud-fixed-btn ${conversationMode ? "hud-fixed-btn--listening" : ""}`}
          data-tip={conversationMode ? "Conversation mode is on — click to turn off" : "Conversation mode: short, natural replies"}
          aria-label="Conversation mode"
          aria-pressed={conversationMode}
          onClick={() => setConversationModeOn(!conversationMode)}
        >
          <Icon name="talk" />
        </button>
      </div>
      <div className="tb-group" role="group" aria-label="Agents">
        {/* JARVIS's own web browser: open / close */}
        {browserState.available && (
          <button
            className={`hud-fixed-btn ${browserState.open ? "hud-fixed-btn--armed" : ""}`}
            data-tip={browserState.open ? "JARVIS's browser is open — click to close it" : "Open JARVIS's web browser"}
            aria-label="JARVIS's web browser"
            aria-pressed={browserState.open}
            onClick={() => setBrowserOpen(!browserState.open)}
          >
            <Icon name="globe" />
          </button>
        )}
        {/* Agent Activity — JARVIS's autopilot steps + the screenshots it saw */}
        <button
          className={`hud-fixed-btn ${running ? "hud-fixed-btn--busy" : ""}`}
          data-tip={running ? "A task is running — see its steps" : "Agent activity: steps and screenshots"}
          aria-label="Agent activity"
          aria-pressed={activityOpen}
          onClick={() => setActivityOpen(true)}
        >
          <Icon name="activity" />
        </button>
      </div>
      <div className="tb-group" role="group" aria-label="App">
        <button className="hud-fixed-btn" data-tip="Command palette  (Ctrl+K)" aria-label="Command palette"
                onClick={() => setPaletteOpen(true)}><Icon name="search" /></button>
        <button className="hud-fixed-btn" data-tip="Customize the home screen" aria-label="Customize the home screen"
                onClick={() => setCustomizeOpen(true)}><Icon name="palette" /></button>
        <button className="hud-fixed-btn" data-tip="Settings  (Ctrl+,)" aria-label="Settings"
                onClick={() => openSettings()}><Icon name="settings" /></button>
        <button className="hud-fixed-btn" data-tip="Power: lock, sleep, restart…" aria-label="Power options"
                onClick={() => setPowerOpen(true)}><Icon name="power" /></button>
      </div>
    </div>
  );

  return (
    <>
      <JarvisHUD
        config={hudConfig}
        toolbar={toolbar}
        activeTask={agentTasks.find((t) => t.status === "running")}
        agentMode={!!sysInfo.agent_mode}
        screen={screen}
        onArrange={(order) => sendScreen({ order })}
        status={status}
        telemetry={telemetry}
        weather={weather}
        netInfo={netInfo}
        schedule={schedule}
        messages={messages}
        commands={commands}
        recordings={recordings}
        isConnected={isConnected}
        allowedDirs={sysInfo.allowed_dirs || []}
        panels={panels} onTogglePanel={togglePanel}
        chatOpen={chatOpen} setChatOpen={setChatOpen}
        skillsOpen={skillsOpen} setSkillsOpen={setSkillsOpen}
        capsOpen={capsOpen} setCapsOpen={setCapsOpen}
        powerOpen={powerOpen} setPowerOpen={setPowerOpen}
        memoryOpen={memoryOpen} setMemoryOpen={setMemoryOpen}
        memory={memory} onRemember={rememberFact} onForget={forgetMemory}
        onSaveRoutine={saveRoutine} onRunRoutine={runRoutine} routineResult={routineResult}
        onSend={sendMessage}
        onAsk={askInChat}
        onStop={stopSpeech}
        onListen={triggerListen}
        onReset={resetConversation}
        onUpload={sendUpload}
        onNotify={notify}
        taskModelTiers={sysInfo.task_model_tiers || []}
        runAction={runAction}
        onSaveDirs={saveDirs}
        conversations={conversations}
        onNewChat={newConversation}
        onOpenConversation={openConversation}
        onDeleteConversation={deleteConversation}
        onRefreshConversations={listConversations}
        onClearConversations={clearConversations}
      />

      {/* Top status stack — banners share one column so they never sit on top of each other */}
      {(browserState.open || controlState.armed || !isConnected) && (
        <div className="hud-top-stack" role="status">
          {controlState.armed && (
            <div className="hud-armed-banner hud-armed-banner--control">
              <span className="hud-armed-dot" />
              <span><Icon name="keyboard" size={15} /> JARVIS is controlling your mouse and keyboard</span>
              <button className="hud-armed-disarm hud-armed-disarm--stop" onClick={stopControl}>Stop</button>
            </div>
          )}
          {browserState.open && (
            <div className="hud-armed-banner">
              <span className="hud-armed-dot" />
              <span><Icon name="globe" size={15} /> JARVIS&apos;s browser{browserState.title || browserState.url ? `: ${browserState.title || browserState.url}` : " is open"}</span>
              <button className="hud-armed-disarm" onClick={() => setBrowserOpen(false)}>Close</button>
            </div>
          )}
          {!isConnected && (
            <div className="hud-offline">
              <Icon name="warning" size={15} /> Can&apos;t reach JARVIS — start <code>python main.py</code> in <code>jarvis-studio-backend/</code>
            </div>
          )}
        </div>
      )}

      {/* Dependency / runtime warnings from the backend */}
      {warnings.length > 0 && (
        <div className="hud-warns" role="status">
          {warnings.map((w) => (
            <div key={w.id} className="hud-warn">
              <Icon name={w.link ? "arrowRight" : "warning"} size={15} />
              <span className="hud-warn-txt">{w.text}</span>
              {w.link && (
                <button className="hud-warn-act" onClick={() => { runAction({ type: "open_url", url: w.link }); dismissWarning(w.id); }}>
                  Get it
                </button>
              )}
              <button aria-label="Dismiss" title="Dismiss" onClick={() => dismissWarning(w.id)}>
                <Icon name="close" size={14} />
              </button>
            </div>
          ))}
        </div>
      )}

      {showSettings && (
        <Settings
          sysInfo={sysInfo}
          initialSection={settingsPage}
          onClose={() => setShowSettings(false)}
          onSave={(cfg) => sendConfig(cfg)}
          onSaved={notify}
          onSetLocation={sendManualLocation}
          onRefreshModels={refreshModels}
          onRerankModels={rerankModels}
          onRepairSetup={repairSetup}
          onOpenCustomize={() => { setShowSettings(false); setCustomizeOpen(true); }}
          devicePairing={devicePairing}
          devices={devices}
          onCreatePairing={createPairing}
          onListDevices={listDevices}
          onRevokeDevice={revokeDevice}
          tvState={tvState}
          waState={waState}
          mcpState={mcpState}
          connState={connState}
          onTv={(type, data) => wsSend({ type, data })}
        />
      )}

      {paletteOpen && (
        <CommandPalette commands={paletteCommands} onClose={() => setPaletteOpen(false)} onAsk={askInChat} />
      )}

      {toast && <div key={toast.id} className="hud-toast" role="status">{toast.text}</div>}

      {customizeOpen && (
        <Customize
          screen={screen}
          onClose={() => setCustomizeOpen(false)}
          onPatch={(patch) => sendScreen(patch)}
        />
      )}

      {activityOpen && (
        <AgentActivity
          tasks={agentTasks}
          onClose={() => setActivityOpen(false)}
          onClear={clearAgentTasks}
        />
      )}

      {/* Booting: shown until the backend WebSocket connects (frozen backend start
          + first-run download). Client-side only, so it shows even if the backend
          is slow or failed to start. */}
      <BootOverlay connected={isConnected} />

      {/* First-run setup: API keys + storage location (blocks until done) */}
      {isConnected && sysInfo.needs_setup && (
        <Onboarding sysInfo={sysInfo} onSave={(cfg) => sendConfig(cfg)} />
      )}

      {/* First-run asset download (Chromium / Whisper / Piper). Shown after the
          API-key onboarding so the flow reads keys → setup → ready. */}
      {isConnected && !sysInfo.needs_setup && (
        <SetupProgress progress={setupProgress} onRepair={repairSetup} />
      )}

      {/* ── Approve/Deny gate for dangerous actions ── */}
      {permissionRequest && (
        <div className="perm-overlay">
          <div className="perm-dialog" role="alertdialog" aria-modal="true" aria-label="Permission required">
            <div className="perm-icon"><Icon name="warning" size={34} stroke={1.4} /></div>
            <div className="perm-kicker">Your approval is needed</div>
            <div className="perm-desc">
              JARVIS wants to <strong>{permissionRequest.description}</strong>.
            </div>
            <div className="perm-sub">This action needs your explicit approval.</div>
            <div className="perm-actions">
              <button className="perm-deny" onClick={() => respondPermission(permissionRequest.id, false)}>
                Deny
              </button>
              <button className="perm-approve" onClick={() => respondPermission(permissionRequest.id, true)}>
                Allow
              </button>
            </div>
          </div>
        </div>
      )}

      {/* ── Mid-task clarify: the autopilot needs one answer to proceed ── */}
      <ClarifyPrompt request={clarifyRequest} onRespond={respondClarify} />
    </>
  );
}
