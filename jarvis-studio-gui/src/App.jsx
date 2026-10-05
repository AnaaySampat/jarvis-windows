import { useState, useEffect, useRef, useMemo, useCallback } from "react";
import { useWebSocket } from "./hooks/useWebSocket";
import ViewportScale from "./hud/ViewportScale";
import { JarvisHUD, DIRECTIONS } from "./hud/HudApp";
import Settings from "./components/Settings";
import Onboarding from "./components/Onboarding";
import SetupProgress from "./components/SetupProgress";
import BootOverlay from "./components/BootOverlay";
import Customize from "./components/Customize";
import AgentActivity from "./components/AgentActivity";
import { Icon } from "./hud/icons";
import { syncBrowserPanel } from "./components/browserPanelWindow";
import { syncControlOverlay } from "./components/controlOverlayWindow";

// Section name (as JARVIS says it) → panels-state key. Module-scope constant —
// it never changes, so there's no reason to rebuild it on every render.
const SECTION_KEY = {
  system: "system", power_panel: "power", weather: "weather",
  network: "network", agenda: "agenda", schedule: "agenda", terminal: "terminal",
};

// Mid-task clarify dialog: the autopilot hit genuine ambiguity and asked ONE
// question. Holds its own input state so typing doesn't re-render the whole app.
function ClarifyPrompt({ request, onRespond }) {
  const [answer, setAnswer] = useState("");
  useEffect(() => { setAnswer(""); }, [request?.id]);
  if (!request) return null;
  const submit = () => onRespond(request.id, answer.trim());
  return (
    <div className="perm-overlay">
      <div className="perm-dialog" role="alertdialog" aria-label="JARVIS needs your input">
        <div className="perm-icon">❓</div>
        <div className="perm-kicker">JARVIS NEEDS YOUR INPUT</div>
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
          <button className="perm-deny" onClick={() => onRespond(request.id, "")}>SKIP</button>
          <button className="perm-approve" disabled={!answer.trim()} onClick={submit}>SEND</button>
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const {
    status, messages, warnings, sysInfo, isConnected, ocrResult,
    telemetry, netInfo, weather, schedule, uiCommand, permissionRequest, clarifyRequest, muted,
    commands, recordings, browserState, controlState, screen, alwaysOn, conversationMode,
    overlay, agentTasks, conversations, setupProgress,
    devicePairing, devices,
    createPairing, listDevices, revokeDevice,
    dismissWarning, clearAgentTasks, sendConfig, refreshModels, rerankModels, repairSetup, sendMessage, sendCommand,
    resetConversation, newConversation, openConversation, deleteConversation, listConversations, clearConversations,
    memory, listMemory, rememberFact, forgetMemory,
    sendOcr, sendLocation, sendManualLocation, triggerListen, stopSpeech,
    stopControl, finishListen, pttStart,
    respondPermission, respondClarify, setMute, runAction, setBrowserOpen, sendUpload,
    sendScreen, setAlwaysOnMode, setConversationModeOn, wsSend,
  } = useWebSocket();

  // App owns the overlay state so JARVIS can drive them via ui_action too.
  const [chatOpen, setChatOpen] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [skillsOpen, setSkillsOpen] = useState(false);
  const [capsOpen, setCapsOpen] = useState(false);
  const [powerOpen, setPowerOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [customizeOpen, setCustomizeOpen] = useState(false);
  const [activityOpen, setActivityOpen] = useState(false);
  const [toast, setToast] = useState(null);   // {text, id} — brief confirmation pill
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(() => setToast(null), 2200);
    return () => clearTimeout(t);
  }, [toast]);
  // Ctrl+, opens Settings (the desktop-app convention).
  useEffect(() => {
    const onKey = (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key === ",") { e.preventDefault(); setShowSettings(true); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

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

  // Collapsible data panels — expanded by default; terminal starts collapsed.
  const [panels, setPanels] = useState({
    system: true, power: true, weather: true, network: true, agenda: true, terminal: false,
  });
  const togglePanel = useCallback((key, val) =>
    setPanels((p) => ({ ...p, [key]: val === undefined ? !p[key] : val })), []);

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
  useEffect(() => {
    if (memoryOpen) listMemory();
  }, [memoryOpen, commands, listMemory]);

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
      case "open_settings": case "settings": setShowSettings(true); break;
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

  return (
    <>
      <ViewportScale>
        <JarvisHUD
          config={hudConfig}
          screen={screen}
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
          onSend={sendMessage}
          onStop={stopSpeech}
          onCommand={sendCommand}
          onReset={resetConversation}
          onUpload={sendUpload}
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
      </ViewportScale>

      {/* Fixed controls layered above the scaled stage. Hidden while a panel that
          covers the top-right (chat, memory, skills) is open — they're
          window-fixed (z 200) and would otherwise draw on top of its header. */}
      {!chatOpen && !memoryOpen && !skillsOpen && (
      <div className="hud-fixed-controls">
        {/* Tap-to-cut: stop whatever JARVIS is saying right now (only while speaking) */}
        {status === "speaking" && (
          <button
            className="hud-fixed-btn hud-fixed-btn--silence"
            title="Stop speaking now"
            onClick={stopSpeech}
          >
            <Icon name="stop" size={16} />
          </button>
        )}
        {/* Persistent mute toggle: keeps speech off (text still shows) until untoggled */}
        <button
          className={`hud-fixed-btn ${muted ? "hud-fixed-btn--muted" : ""}`}
          title={muted ? "Unmute Jarvis" : "Mute Jarvis (text only)"}
          aria-pressed={muted}
          onClick={() => setMute(!muted)}
        >
          <Icon name={muted ? "mute" : "volume"} />
        </button>
        {/* Always-on continuous voice (no wake word) */}
        <button
          className={`hud-fixed-btn ${alwaysOn ? "hud-fixed-btn--listening" : ""}`}
          title={alwaysOn
            ? "Always-on listening is ON — click for wake-word mode"
            : "Turn on always-on listening (no wake word)"}
          aria-pressed={alwaysOn}
          onClick={() => setAlwaysOnMode(!alwaysOn)}
        >
          <Icon name={alwaysOn ? "mic" : "wake"} />
        </button>
        {/* Natural conversation mode (shorter, chattier replies) */}
        <button
          className={`hud-fixed-btn ${conversationMode ? "hud-fixed-btn--listening" : ""}`}
          title={conversationMode ? "Conversation mode ON" : "Conversation mode (natural, brief replies)"}
          aria-pressed={conversationMode}
          onClick={() => setConversationModeOn(!conversationMode)}
        >
          <Icon name="talk" />
        </button>
        {/* JARVIS's own web browser: open / close */}
        {browserState.available && (
          <button
            className={`hud-fixed-btn ${browserState.open ? "hud-fixed-btn--armed" : ""}`}
            title={browserState.open
              ? "JARVIS's browser is open — click to close it"
              : "Open JARVIS's web browser"}
            aria-pressed={browserState.open}
            onClick={() => setBrowserOpen(!browserState.open)}
          >
            <Icon name="globe" />
          </button>
        )}
        {/* Agent Activity — JARVIS's autopilot steps + the screenshots it saw */}
        <button
          className={`hud-fixed-btn ${agentTasks.some((t) => t.status === "running") ? "hud-fixed-btn--armed" : ""}`}
          title="Agent activity — what JARVIS did, with screenshots"
          aria-pressed={activityOpen}
          onClick={() => setActivityOpen(true)}
        >
          <Icon name="activity" />
        </button>
        <button className="hud-fixed-btn" title="Customize the home screen" onClick={() => setCustomizeOpen(true)}><Icon name="palette" /></button>
        <button className="hud-fixed-btn" title="Settings" onClick={() => setShowSettings(true)}><Icon name="settings" /></button>
      </div>
      )}

      {/* Top status stack — banners share one column so they never sit on top of each other */}
      {(browserState.open || controlState.armed || !isConnected) && (
        <div className="hud-top-stack" role="status">
          {controlState.armed && (
            <div className="hud-armed-banner hud-armed-banner--control">
              <span className="hud-armed-dot" />
              <span>⌨ JARVIS IS CONTROLLING YOUR MOUSE &amp; KEYBOARD</span>
              <button className="hud-armed-disarm hud-armed-disarm--stop" onClick={stopControl}>STOP</button>
            </div>
          )}
          {browserState.open && (
            <div className="hud-armed-banner">
              <span className="hud-armed-dot" />
              <span>BROWSER · {browserState.title || browserState.url || "ready"} · panel on the right</span>
              <button className="hud-armed-disarm" onClick={() => setBrowserOpen(false)}>CLOSE</button>
            </div>
          )}
          {!isConnected && (
            <div className="hud-offline">
              ⚠ CORE OFFLINE — start <code>python main.py</code> in <code>jarvis-studio-backend/</code>
            </div>
          )}
        </div>
      )}

      {/* Dependency / runtime warnings from the backend */}
      {warnings.length > 0 && (
        <div className="hud-warns">
          {warnings.map((w) => (
            <div key={w.id} className="hud-warn">
              <span>⚠ {w.text}</span>
              <button onClick={() => dismissWarning(w.id)}>✕</button>
            </div>
          ))}
        </div>
      )}

      {showSettings && (
        <Settings
          sysInfo={sysInfo}
          onClose={() => setShowSettings(false)}
          onSave={(cfg) => sendConfig(cfg)}
          onSaved={(text) => setToast({ text, id: Date.now() })}
          onSetLocation={sendManualLocation}
          onRefreshModels={refreshModels}
          onRerankModels={rerankModels}
          onRepairSetup={repairSetup}
          devicePairing={devicePairing}
          devices={devices}
          onCreatePairing={createPairing}
          onListDevices={listDevices}
          onRevokeDevice={revokeDevice}
        />
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
          <div className="perm-dialog" role="alertdialog" aria-label="Permission required">
            <div className="perm-icon">⚠</div>
            <div className="perm-kicker">AUTHORISATION REQUIRED</div>
            <div className="perm-desc">
              JARVIS wants to <strong>{permissionRequest.description}</strong>.
            </div>
            <div className="perm-sub">This action needs your explicit approval.</div>
            <div className="perm-actions">
              <button className="perm-deny" onClick={() => respondPermission(permissionRequest.id, false)}>
                DENY
              </button>
              <button className="perm-approve" onClick={() => respondPermission(permissionRequest.id, true)}>
                APPROVE
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
