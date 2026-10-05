import { useState, useEffect, useRef } from "react";
import QRCode from "qrcode";

/**
 * Free-tier route health (backend: llm/quota.py, surfaced via get_sysinfo).
 *
 * JARVIS benches a model+key after a rate limit and routes around it, which is what
 * keeps the free tier usable. Without this that's invisible: "I've used up the free
 * quota, back in about 4 minutes" is unverifiable, and a route benched for a day off
 * one bad window only clears as a side effect of re-saving keys. Read-only but for
 * the reset.
 */
function QuotaSection({ sysInfo, onSave }) {
  const quota = sysInfo.quota || {};
  const benched = quota.benched || [];
  const counts = LLM_PROVIDERS.map((p) => [p.name, sysInfo[p.count]]).filter(([, n]) => n > 0);
  const keys = counts.length;

  const eta = (s) => {
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    if (s < 86400) return `${Math.round(s / 3600)} h`;
    return "tomorrow";
  };

  return (
    <div className="settings-sec">
      <label>Free-tier routes</label>
      <div className="settings-hint">
        {keys
          ? `Keys in rotation: ${counts.map(([p, n]) => `${n} ${p}`).join(" · ")}.`
          : "No API keys configured yet."}
      </div>
      {benched.length === 0 ? (
        <div className="settings-hint">All routes available.</div>
      ) : (
        <>
          <div className="quota-list">
            {benched.map((b) => (
              <div className="quota-row" key={`${b.provider}/${b.model}#${b.key}`}>
                <span className="quota-route">
                  {b.provider} / {b.model} · key {b.key + 1}
                </span>
                <span className="quota-eta">back in {eta(b.seconds_left)}</span>
              </div>
            ))}
          </div>
          <button className="dirlist-addbtn" style={{ marginTop: 8 }}
                  onClick={() => onSave({ clear_quota_benches: true })}>
            Clear benches
          </button>
        </>
      )}
      <div className="settings-hint">
        A route is one model on one key. After a rate limit JARVIS benches it and uses the
        next one instead of failing — clearing is only needed if you've fixed the cause.
      </div>
    </div>
  );
}

// Compact "3 min ago" for the paired-device roster (epoch seconds in, "" for junk).
function timeAgo(epochS) {
  const s = Math.floor(Date.now() / 1000 - Number(epochS));
  if (!Number.isFinite(s) || s < 0) return "";
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} d ago`;
}

// Remote mobile control: mint a QR the phone scans to pair over Tailscale, plus a
// 6-digit PIN (shown here, NOT in the QR) you type into the phone once — that PIN
// is the pairing approval, so a device that only grabbed the QR can't pair. Manage
// /revoke paired devices below. Per-task approval lives on the phone (fingerprint).
function RemotePairingSection({ devicePairing, devices, onCreatePairing,
  onListDevices, onRevokeDevice }) {
  const [qr, setQr] = useState("");
  useEffect(() => { onListDevices && onListDevices(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let dead = false;
    const payload = devicePairing && devicePairing.ok && devicePairing.payload;
    if (!payload) { setQr(""); return; }
    QRCode.toDataURL(JSON.stringify(payload), { margin: 2, width: 640, errorCorrectionLevel: "M" })
      .then((url) => { if (!dead) setQr(url); })
      .catch(() => { if (!dead) setQr(""); });
    return () => { dead = true; };
  }, [devicePairing]);

  const paired = (devices || []).filter((d) => !d.revoked);
  const fp = (devicePairing && devicePairing.host_fingerprint) || "";
  const pin = (devicePairing && devicePairing.pin) || "";
  const tsReady = !devicePairing || devicePairing.tailscale_ready;

  return (
    <div className="settings-sec">
      <label>Remote Access (Phone Pairing)</label>
      <div className="settings-hint">
        Control this PC from your phone over Tailscale (same or different network).
        On the phone's Remote PC screen, scan this two-minute code, then type the PIN
        below into the phone once to finish pairing.
      </div>
      <button className="dirlist-addbtn" style={{ marginTop: 8 }}
        onClick={() => onCreatePairing && onCreatePairing("Aura phone")}>
        {qr ? "Regenerate QR" : "Generate pairing QR"}
      </button>

      {devicePairing && !devicePairing.ok && (
        <div className="settings-warn" style={{ marginTop: 8 }}>
          Couldn't create a pairing code: {devicePairing.reason || "unknown error"}.
        </div>
      )}
      {devicePairing && devicePairing.ok && !tsReady && (
        <div className="settings-warn" style={{ marginTop: 8 }}>
          Tailscale isn't connected on this PC, so the phone can't reach it. Start
          Tailscale, then regenerate the code.
        </div>
      )}
      {qr && (
        <div style={{ display: "flex", flexDirection: "column", gap: 10, marginTop: 10, alignItems: "center" }}>
          <img src={qr} alt="JARVIS pairing QR code"
            style={{
              width: "100%", maxWidth: 320, aspectRatio: "1 / 1",
              borderRadius: 8, background: "#fff", padding: 10,
            }} />
          <div className="settings-hint" style={{ margin: 0, textAlign: "center" }}>
            {pin && (
              <div style={{ marginBottom: 6 }}>
                PIN (type into phone): <b style={{ fontSize: 22, letterSpacing: 4 }}>{pin}</b>
              </div>
            )}
            <div>Address: <b>{devicePairing.host || "—"}:8765</b></div>
            {fp && <div style={{ wordBreak: "break-all" }}>Host fingerprint: {fp.slice(0, 24)}…</div>}
            <div>Expires in ~2 minutes.</div>
          </div>
        </div>
      )}

      {paired.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div className="settings-hint" style={{ marginBottom: 4 }}>Paired devices:</div>
          {paired.map((d) => (
            <div key={d.device_id} className="sysinfo-row" style={{ alignItems: "center" }}>
              <span title={d.fingerprint}>
                {d.connected && (
                  <span style={{ color: "#2fd670", marginRight: 6 }} title="Connected right now">●</span>
                )}
                {d.name} ({d.device_id.slice(0, 14)}…)
                <span className="settings-hint" style={{ display: "block", margin: 0 }}>
                  {d.connected ? "connected now" : `last seen ${timeAgo(d.last_seen)}`}
                  {" · paired "}{timeAgo(d.paired_at)}
                </span>
              </span>
              <button className="dir-rm" title="Disconnect and remove this device; it can pair again with a new QR + PIN"
                onClick={() => onRevokeDevice && onRevokeDevice(d.device_id)}>Remove</button>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Smart routing (backend: llm/model_ranker.py). An LLM with Google Search ranks every
 * model the keys can reach; each request goes to the weakest model that is still good
 * enough, falling through to the next on failure. Re-ranked only when a key is added.
 */
const TIER_LABEL = { fast: "Fast", mid: "Everyday", flagship: "Flagship" };

function RoutingSection({ sysInfo, onRerank }) {
  const r = sysInfo.routing || {};
  const models = r.models || [];
  const c = r.counts || {};
  const SOURCE = { benchmark: "Benchmark", llm: "Estimated", guess: "Unscored" };
  return (
    <div className="settings-sec">
      <label>Smart routing</label>
      {models.length === 0 && !r.in_progress ? (
        <div className="settings-hint">
          Not ranked yet — JARVIS ranks your models automatically once it can see them.
          Until then it uses its built-in Gemini/Groq tiers.
        </div>
      ) : (
        <div className="settings-hint">
          Scores are the Artificial Analysis Intelligence Index ({r.catalog || "benchmark"} snapshot)
          for {c.benchmark || 0} of your {models.length} models
          {c.llm ? <>; {c.llm} newer model{c.llm === 1 ? "" : "s"} estimated by <b>{r.estimated_by || "an LLM"}</b> with Google Search</> : null}
          {c.guess ? <>; {c.guess} not scored yet (placed last in their tier)</> : null}.
          Updates on its own when you add or remove a key.
        </div>
      )}
      {onRerank && (
        <div className="route-actions">
          <button className="dirlist-addbtn" disabled={!!r.in_progress} onClick={onRerank}
                  title="Look up the models without benchmark data again">
            {r.in_progress ? "Ranking…" : "Re-rank now"}
          </button>
          {!r.in_progress && r.last_result && <span className="route-result">{r.last_result}</span>}
        </div>
      )}
      {models.length > 0 && (
        <div className="route-list">
          {models.map((m, i) => (
            <div className="route-row" key={m.id} title={m.basis || ""}>
              <span className="route-n">{i + 1}</span>
              <span className="route-id">{m.id}</span>
              <span className={`route-tier route-tier--${m.tier}`}>{TIER_LABEL[m.tier] || m.tier}</span>
              <span className="route-score">{m.score}</span>
              <span className={`route-note route-src--${m.source}`}>
                {SOURCE[m.source] || m.source}{m.basis ? ` · ${m.basis}` : ""}
                {!m.tools ? " · chat only (no tool calling)" : ""}
              </span>
            </div>
          ))}
        </div>
      )}
      <div className="settings-hint">
        Each request gets the weakest tier that can handle it (quick chat → Fast, everyday
        commands → Everyday, hard reasoning → Flagship). If that model's API fails or is
        rate-limited, the next one in line takes over — then stronger tiers, then weaker.
      </div>
    </div>
  );
}

/** Two-click Remove for a saved key (the first click arms, the second deletes). */
function RemoveKey({ name, configured, onSave }) {
  const [armed, setArmed] = useState(false);
  if (!configured) return null;
  return (
    <button type="button" className={`key-remove ${armed ? "key-remove--armed" : ""}`}
            onClick={() => { if (armed) { onSave({ remove_keys: [name] }); setArmed(false); } else setArmed(true); }}
            onBlur={() => setArmed(false)} title="Delete this saved key">
      {armed ? "Confirm" : "Remove"}
    </button>
  );
}

// Fallback model groups — used only until the backend's per-key discovery arrives
// via sysInfo.available_models (Phase 3), which lists ONLY models the active keys
// can actually serve (Vertex curated / Gemini-key / Groq / local Ollama). Kept
// deliberately tiny; the live discovered list supersedes it.
const FALLBACK_GROUPS = [
  { label: "Auto", opts: [{ value: "auto", label: "Auto — pick the best model per request" }] },
  {
    label: "Google Gemini",
    opts: [
      { value: "gemini-3.5-flash",      label: "Gemini 3.5 Flash" },
      { value: "gemini-2.5-flash",      label: "Gemini 2.5 Flash" },
      { value: "gemini-2.5-flash-lite", label: "Gemini 2.5 Flash-Lite" },
    ],
  },
  {
    label: "Groq (cloud · fallback)",
    opts: [
      { value: "llama-3.3-70b-versatile", label: "Llama 3.3 70B" },
      { value: "llama-3.1-8b-instant",    label: "Llama 3.1 8B — fastest" },
    ],
  },
];

const TTS_OPTS = [
  { value: "piper",      label: "Piper (offline neural, British — free & unlimited)" },
  { value: "elevenlabs", label: "ElevenLabs (premium cloud — needs API key)" },
  { value: "chirp",      label: "Google Chirp 3 HD (premium British — on your GCP credits)" },
  { value: "edge-tts",   label: "Edge TTS (online, British male)" },
  { value: "pyttsx3",    label: "pyttsx3 (offline, robotic)" },
  { value: "off",        label: "Off" },
];

const isGemini = (m) => /^(gemini|gemma)/i.test(m || "");

// Every credential the API Keys page manages. `multi` keys accept several, one per
// line (free-tier quota is metered per key); `tag` says whether using it can cost
// money — Auto routing will send turns to any provider that has a key.
const LLM_PROVIDERS = [
  { id: "groq", name: "Groq", tag: "free", multi: true, ph: "gsk_…", where: "console.groq.com/keys" },
  { id: "gemini", name: "Google Gemini", tag: "free", multi: true, ph: "AIza…", where: "aistudio.google.com/apikey" },
  { id: "openrouter", name: "OpenRouter", tag: "free", multi: true, ph: "sk-or-…", where: "openrouter.ai/keys",
    note: "Limited to free (:free) models, so routing never spends your credits." },
  { id: "nvidia", name: "NVIDIA NIM", tag: "free", multi: true, ph: "nvapi-…", where: "build.nvidia.com" },
  { id: "mistral", name: "Mistral", tag: "free", multi: true, ph: "Mistral API key", where: "console.mistral.ai" },
  { id: "anthropic", name: "Anthropic Claude", tag: "paid", multi: true, ph: "sk-ant-…", where: "console.anthropic.com" },
  { id: "openai", name: "OpenAI", tag: "paid", multi: true, ph: "sk-…", where: "platform.openai.com/api-keys" },
  { id: "xai", name: "xAI Grok", tag: "paid", multi: true, ph: "xai-…", where: "console.x.ai" },
  { id: "meta", name: "Meta Llama", multi: true, ph: "Llama API key", where: "llama.developer.meta.com" },
].map((p) => ({ ...p, key: `${p.id}_api_key`, has: `has_${p.id}_key`, count: `${p.id}_key_count` }));

const SERVICE_KEYS = [
  { id: "elevenlabs", name: "ElevenLabs", key: "elevenlabs_api_key", has: "has_elevenlabs_key",
    ph: "for premium TTS", where: "elevenlabs.io" },
  { id: "maps", name: "Google Maps", key: "google_maps_key", has: "has_google_maps_key",
    ph: "AIza… — accurate places & distances", where: "console.cloud.google.com" },
];
const ALL_KEYS = [...LLM_PROVIDERS, ...SERVICE_KEYS];

const TAG_LABEL = { free: "Free tier", paid: "Paid" };

function KeyCard({ p, sysInfo, value, onChange, onSave }) {
  const [show, setShow] = useState(false);
  const configured = !!sysInfo[p.has];
  const n = Number(sysInfo[p.count]) || (configured ? 1 : 0);
  const pending = value.trim() !== "";
  const status = pending ? "Unsaved" : configured ? (n > 1 ? `${n} keys` : "Connected") : "Not set";
  const fieldProps = {
    value, spellCheck: false, autoComplete: "off",
    onChange: (e) => onChange(e.target.value),
    placeholder: configured ? "Saved — paste to replace" : p.ph,
    "aria-label": `${p.name} API key`,
  };
  return (
    <div className={`kc${configured ? " kc--on" : ""}${pending ? " kc--pending" : ""}`}>
      <div className="kc-hd">
        <span className="kc-dot" aria-hidden />
        <span className="kc-name">{p.name}</span>
        {p.tag && <span className={`kc-tag kc-tag--${p.tag}`}>{TAG_LABEL[p.tag]}</span>}
        <span className="kc-status">{status}</span>
      </div>
      <div className="kc-field">
        {p.multi ? (
          <textarea {...fieldProps} className={show ? "" : "kc-mask"} wrap="off"
                    rows={Math.min(4, Math.max(1, value.split("\n").length))} />
        ) : (
          <input {...fieldProps} type={show ? "text" : "password"} />
        )}
        <button type="button" className="kc-eye" onClick={() => setShow(!show)}
                title={show ? "Hide what you typed" : "Show what you typed"}>
          {show ? "Hide" : "Show"}
        </button>
        <RemoveKey name={p.key} configured={configured} onSave={onSave} />
      </div>
      <div className="kc-foot">
        {p.note ? <span>{p.note}</span> : <span>Get a key at <b>{p.where}</b></span>}
      </div>
    </div>
  );
}

// Sidebar sections. `words` feed the search box so "voice" finds TTS, "phone"
// finds pairing, etc.
const SECTIONS = [
  { id: "ai", label: "Intelligence", icon: "◈", words: "provider model llm vertex gemini offline ollama auto image" },
  { id: "keys", label: "API Keys", icon: "⚿", words: "api keys groq gemini nvidia openrouter mistral claude anthropic openai gpt grok xai meta llama elevenlabs maps credentials" },
  { id: "routing", label: "Routing & Quota", icon: "⇄", words: "smart routing rank tiers quota rate limit benched free" },
  { id: "voice", label: "Voice", icon: "◉", words: "voice speech tts piper elevenlabs chirp wake word alerts" },
  { id: "autopilot", label: "Autopilot", icon: "⌖", words: "autopilot browser desktop operator reasoning vision screenshot" },
  { id: "interface", label: "Interface", icon: "▣", words: "overlay pill floating interface apps hide" },
  { id: "files", label: "Files & Location", icon: "⌂", words: "storage folder directories files read location weather pin" },
  { id: "devices", label: "Devices", icon: "⌁", words: "phone remote pairing qr pin tailscale devices mobile" },
  { id: "system", label: "System", icon: "⚙", words: "system ram resources repair components" },
];
let lastSection = "ai";   // reopen where the user left off (per app session)

export default function Settings({ sysInfo, onClose, onSave, onSaved, onSetLocation, onRefreshModels, onRerankModels, onRepairSetup,
  devicePairing, devices, onCreatePairing, onListDevices, onRevokeDevice }) {
  // Re-discover models when the panel opens so the dropdown is current (e.g. a
  // local Ollama model pulled since launch, or a key just added in another client).
  // Cheap — the backend skips the network when credentials are unchanged.
  useEffect(() => { onRefreshModels && onRefreshModels(); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  // Prefer the backend's per-key discovered list; fall back to the curated set
  // until it lands. Both share the {label, opts:[{value,label}]} shape.
  const modelGroups = (Array.isArray(sysInfo.available_models) && sysInfo.available_models.length)
    ? sysInfo.available_models : FALLBACK_GROUPS;
  const allValues = modelGroups.flatMap((g) => g.opts.map((o) => o.value));
  // Local (Ollama) models discovered by the backend — the offline-mode picker.
  const localOpts = (modelGroups.find((g) => /ollama|local/i.test(g.label))?.opts) ?? [];
  const [section, setSection] = useState(lastSection);
  const [query, setQuery] = useState("");
  const [model, setModel] = useState(sysInfo.model_override ?? sysInfo.model ?? "auto");
  // Provider EDITION (Phase 4): vertex | gemini | offline.
  const [providerMode, setProviderMode] = useState(sysInfo.provider_mode || "vertex");
  const [offlineModel, setOfflineModel] = useState(sysInfo.offline_model ?? "");
  const [ollamaUrl, setOllamaUrl] = useState(sysInfo.ollama_url ?? "http://localhost:11434");
  const [locInput, setLocInput] = useState("");
  const pinnedLoc = sysInfo.manual_location;   // {lat,lon,label} or null = automatic
  const pinLocation = () => {
    const p = locInput.trim();
    if (p && onSetLocation) { onSetLocation({ place: p }); setLocInput(""); }
  };
  const [tts,   setTts]   = useState(sysInfo.tts ?? "piper");
  const [dirs, setDirs]   = useState(sysInfo.allowed_dirs ?? []);
  const [newDir, setNewDir] = useState("");
  const [storageDir, setStorageDir] = useState(sysInfo.storage_dir ?? "");
  const [keys, setKeys] = useState({});   // key name -> text typed this session
  const [sysAlerts, setSysAlerts] = useState(!!sysInfo.system_alerts);
  const [autopilotModel, setAutopilotModel] = useState(sysInfo.autopilot_model ?? "");
  const [autopilotThinking, setAutopilotThinking] = useState(
    (sysInfo.autopilot_thinking ?? -1) === 0 ? "off" : "dynamic");
  const [autopilotVision, setAutopilotVision] = useState(sysInfo.autopilot_vision !== false);
  const [ovlEnabled, setOvlEnabled] = useState(sysInfo.overlay?.enabled !== false);
  const [ovlApps, setOvlApps] = useState(sysInfo.overlay?.apps ?? []);
  // Preserve whatever mode is in effect (it can be changed by voice via
  // set_overlay) so saving Settings doesn't silently reset it to "except".
  const [ovlMode] = useState(sysInfo.overlay?.mode ?? "except");
  const [newApp, setNewApp] = useState("");
  const [escArmed, setEscArmed] = useState(false);
  // Focus the dialog so Ctrl+S / Esc work before anything inside is clicked.
  const panelRef = useRef(null);
  useEffect(() => { panelRef.current && panelRef.current.focus(); }, []);

  const addApp = () => {
    const a = newApp.trim().toLowerCase().replace(/\.exe$/, "");
    if (a && !ovlApps.includes(a)) setOvlApps([...ovlApps, a]);
    setNewApp("");
  };
  const removeApp = (a) => setOvlApps(ovlApps.filter((x) => x !== a));

  const addDir = () => {
    const d = newDir.trim().replace(/^["']|["']$/g, "");
    if (d && !dirs.includes(d)) setDirs([...dirs, d]);
    setNewDir("");
  };
  const removeDir = (d) => setDirs(dirs.filter((x) => x !== d));

  const cfg = {
    model_override: model, tts, allowed_dirs: dirs, system_alerts: sysAlerts,
    overlay: { enabled: ovlEnabled, mode: ovlMode, apps: ovlApps },
    autopilot_model: autopilotModel.trim(),
    autopilot_thinking: autopilotThinking === "off" ? 0 : -1,
    autopilot_vision: autopilotVision,
    provider_mode: providerMode,
  };
  // Offline-only fields — sent just for the offline edition so switching modes
  // doesn't trigger needless model re-discovery.
  if (providerMode === "offline") {
    cfg.offline_model = offlineModel.trim();
    cfg.ollama_url = ollamaUrl.trim();
  }
  if (storageDir.trim()) cfg.storage_dir = storageDir.trim();
  for (const p of ALL_KEYS) {
    const v = (keys[p.key] || "").trim();
    if (v) cfg[p.key] = v;
  }
  const snapshot = JSON.stringify(cfg);
  const [initial] = useState(snapshot);
  const dirty = snapshot !== initial;
  const pendingKeys = ALL_KEYS.filter((p) => (keys[p.key] || "").trim()).length;

  const handleSave = () => {
    onSave(cfg);
    onSaved && onSaved(pendingKeys ? `Saved · ${pendingKeys} key${pendingKeys === 1 ? "" : "s"} added` : "Settings saved");
    onClose();
  };

  // Ctrl+S saves; Esc closes — but with unsaved edits the first Esc only warns,
  // so a stray keypress can't throw away a pasted key.
  const onKey = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (dirty) handleSave();
    } else if (e.key === "Escape") {
      e.preventDefault();
      if (dirty && !escArmed) setEscArmed(true);
      else onClose();
    }
  };

  const go = (id) => { setSection(id); lastSection = id; };
  const q = query.trim().toLowerCase();
  const visible = q
    ? SECTIONS.filter((s) => `${s.label} ${s.words}`.toLowerCase().includes(q))
    : SECTIONS;

  const geminiSelected = isGemini(model);
  const nativeAudio = /native-audio/.test(model);
  const llmConnected = LLM_PROVIDERS.filter((p) => sysInfo[p.has]).length + (sysInfo.vertex ? 1 : 0);
  const benched = (sysInfo.quota?.benched || []).length;
  const badge = { keys: llmConnected || null, routing: benched ? `${benched}` : null };
  const current = SECTIONS.find((s) => s.id === section) || SECTIONS[0];

  return (
    <div className="settings-overlay" onClick={onClose}>
      <div className="settings-panel sx" role="dialog" aria-label="Settings" tabIndex={-1}
           onKeyDown={onKey} onClick={e => e.stopPropagation()}
           ref={panelRef}>

        <nav className="sx-nav">
          <div className="sx-brand"><span className="hud-dot" /> SETTINGS</div>
          <input className="sx-search" type="search" value={query} placeholder="Search settings…"
                 onChange={(e) => setQuery(e.target.value)}
                 onKeyDown={(e) => { if (e.key === "Enter" && visible[0]) go(visible[0].id); }} />
          <div className="sx-links">
            {visible.map((s) => (
              <button key={s.id} className={`sx-link${s.id === current.id ? " sx-link--on" : ""}`}
                      onClick={() => go(s.id)} aria-current={s.id === current.id ? "page" : undefined}>
                <span className="sx-ico" aria-hidden>{s.icon}</span>
                <span className="sx-lbl">{s.label}</span>
                {badge[s.id] && (
                  <span className={`sx-badge${s.id === "routing" ? " sx-badge--warn" : ""}`}>{badge[s.id]}</span>
                )}
              </button>
            ))}
            {visible.length === 0 && <div className="sx-none">No settings match “{query}”.</div>}
          </div>
          <div className="sx-kbd"><kbd>Ctrl</kbd>+<kbd>S</kbd> save · <kbd>Esc</kbd> close</div>
        </nav>

        <div className="sx-main">
          <div className="sx-hdr">
            <h2>{current.label}</h2>
            <button className="settings-x" onClick={onClose} aria-label="Close settings">✕</button>
          </div>

          <div className="sx-body" key={current.id}>
            {current.id === "ai" && (<>
              <div className="settings-sec">
                <label>Provider</label>
                <select value={providerMode} onChange={e => setProviderMode(e.target.value)}>
                  <option value="vertex">Vertex AI — Gemini on your Google Cloud credits (ADC)</option>
                  <option value="gemini">API keys — Gemini, Groq and every provider you've added</option>
                  <option value="offline">Offline — a local model only (Ollama, no cloud)</option>
                </select>
                {providerMode === "vertex" && (
                  <div className="settings-hint">
                    Gemini runs through your gcloud ADC login — billed to your Google Cloud
                    project/credits, no API key. {sysInfo.vertex
                      ? "✓ ADC detected."
                      : "⚠ ADC not detected — run `gcloud auth application-default login`."}
                  </div>
                )}
                {providerMode === "gemini" && (
                  <div className="settings-hint">
                    Runs on the keys in <button className="settings-link" onClick={() => go("keys")}>API Keys</button>.
                    Vertex stays off.
                    {!sysInfo.has_gemini_key && " ⚠ No Gemini key yet — image generation needs one."}
                  </div>
                )}
                {providerMode === "offline" && (
                  <>
                    <div className="settings-hint">
                      Runs entirely on a local Ollama model — no internet, no cloud keys.
                      Commands still work via the [ACTION] path; it's slower and less capable
                      than the cloud tiers.
                    </div>
                    <label>Local model</label>
                    <select value={offlineModel} onChange={e => setOfflineModel(e.target.value)}>
                      <option value="">Auto — first installed model</option>
                      {localOpts.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                    {localOpts.length === 0 && (
                      <div className="settings-warn">
                        ⚠ No local models found at {ollamaUrl}. Install Ollama, then pull one
                        (e.g. `ollama pull llama3.1`).
                      </div>
                    )}
                    <input type="text" className="settings-model-id" value={ollamaUrl}
                           onChange={(e) => setOllamaUrl(e.target.value)} placeholder="http://localhost:11434" />
                  </>
                )}
              </div>

              <div className="settings-sec">
                <label>LLM Model</label>
                <select value={allValues.includes(model) ? model : "__custom"}
                        onChange={e => { if (e.target.value !== "__custom") setModel(e.target.value); }}>
                  {modelGroups.map((g) => (
                    <optgroup key={g.label} label={g.label}>
                      {g.opts.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </optgroup>
                  ))}
                  {!allValues.includes(model) && <option value="__custom">Custom: {model}</option>}
                </select>
                <input type="text" className="settings-model-id" value={model}
                       onChange={(e) => setModel(e.target.value)} placeholder="exact model id (override)" />
                {geminiSelected && sysInfo.vertex && (
                  <div className="settings-hint">✓ Served via Vertex AI on your Google Cloud credits — no API key needed.</div>
                )}
                {geminiSelected && !sysInfo.vertex && !sysInfo.has_gemini_key && (
                  <div className="settings-warn">⚠ This is a Gemini model — add your Gemini API key, or sign in with Vertex (gcloud ADC).</div>
                )}
                {model === "auto" && (
                  <div className="settings-hint">
                    Auto sends each request to the weakest ranked model that can handle it, across every
                    provider you have a key for, and falls through to the next if one fails — see{" "}
                    <button className="settings-link" onClick={() => go("routing")}>Routing</button>.
                  </div>
                )}
                {nativeAudio && (
                  <div className="settings-hint">
                    Native-audio dialogue uses Google's Live API — full-duplex mic-in / voice-out with the
                    model's own voice (no Whisper, no TTS), with barge-in. Spoken turns use the Live API;
                    typed messages fall back to a Gemini text model. Needs Gemini access (Vertex credits or a Gemini API key).
                  </div>
                )}
                <div className="settings-hint">
                  Image generation uses Gemini 2.5 Flash image and works on any model here — just ask
                  JARVIS to "make an image of…" (needs Gemini access — Vertex or a Gemini key).
                </div>
              </div>
            </>)}

            {current.id === "keys" && (<>
              <div className="settings-hint">
                Stored locally in app data, outside the project folder — never shown again after saving.
                Leave a field blank to keep its key. Free-tier limits are per key, so paste several
                (one per line) and JARVIS rotates through them instead of stopping at a rate limit.
              </div>
              {LLM_PROVIDERS.some((p) => p.tag === "paid" && sysInfo[p.has]) && model === "auto" && (
                <div className="settings-warn">
                  A paid provider is connected and the model is Auto, so smart routing may send
                  requests to it — each one is billed to that account. Pick a specific model in
                  Intelligence, or remove the key, to keep it off.
                </div>
              )}
              <div className="sx-group">Language models</div>
              <div className="kc-grid">
                {LLM_PROVIDERS.map((p) => (
                  <KeyCard key={p.id} p={p} sysInfo={sysInfo} value={keys[p.key] || ""} onSave={onSave}
                           onChange={(v) => setKeys((k) => ({ ...k, [p.key]: v }))} />
                ))}
              </div>
              <div className="sx-group">Services</div>
              <div className="kc-grid">
                {SERVICE_KEYS.map((p) => (
                  <KeyCard key={p.id} p={p} sysInfo={sysInfo} value={keys[p.key] || ""} onSave={onSave}
                           onChange={(v) => setKeys((k) => ({ ...k, [p.key]: v }))} />
                ))}
              </div>
              <div className="settings-hint">
                Google Places (New) gives accurate nearby places &amp; brand distances (vs the free
                OpenStreetMap fallback). {sysInfo.places_source === "google"
                  ? "Active — using Google."
                  : "On Vertex you don't need a key — just enable “Places API (New)” in your Google Cloud project."}
              </div>
            </>)}

            {current.id === "routing" && (<>
              <RoutingSection sysInfo={sysInfo} onRerank={onRerankModels} />
              <QuotaSection sysInfo={sysInfo} onSave={onSave} />
            </>)}

            {current.id === "voice" && (<>
              <div className="settings-sec">
                <label>Text-to-Speech</label>
                <select value={tts} onChange={e => setTts(e.target.value)}>
                  {TTS_OPTS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                </select>
                {tts === "piper" && (
                  <div className="settings-hint">
                    Offline neural British voice — free, unlimited, private. The voice model
                    (~60 MB) downloads automatically the first time JARVIS speaks.
                  </div>
                )}
                {tts === "elevenlabs" && !sysInfo.has_elevenlabs_key && (
                  <div className="settings-warn">
                    ⚠ ElevenLabs needs an API key — add it in{" "}
                    <button className="settings-link" onClick={() => go("keys")}>API Keys</button>.
                  </div>
                )}
                {tts === "elevenlabs" && (
                  <div className="settings-hint">
                    The most natural voice, but the free tier is ~10k characters/month — best kept
                    for special use; Piper is the better everyday driver.
                  </div>
                )}
              </div>
              <div className="settings-sec">
                <label>Wake Word</label>
                <div className="settings-ro">{sysInfo.wakeWord || "hey_jarvis"}</div>
              </div>
              <div className="settings-sec">
                <label>Proactive Alerts</label>
                <label className="settings-check">
                  <input type="checkbox" className="sw" checked={sysAlerts}
                         onChange={(e) => setSysAlerts(e.target.checked)} />
                  <span>Spoken system alerts (high CPU / RAM / low battery)</span>
                </label>
                <div className="settings-hint">
                  Off by default. When on, JARVIS will occasionally speak up if the CPU or memory
                  is maxed out or the battery is low. Agenda reminders and timers are unaffected.
                </div>
              </div>
            </>)}

            {current.id === "autopilot" && (
              <div className="settings-sec">
                <label>Operator model</label>
                <input type="text" className="settings-model-id" value={autopilotModel}
                       onChange={(e) => setAutopilotModel(e.target.value)}
                       placeholder='blank = auto · model id · "ollama:llama3.1" for local' />
                <div className="settings-hint">
                  The model that silently drives multi-step browser/desktop tasks (it picks each
                  click/type internally — you only hear the outcome). Leave blank for auto.
                  Use <b>ollama:&lt;model&gt;</b> to run it fully offline via a local Ollama server.
                  {sysInfo.autopilot_model_active ? <> Currently: <b>{sysInfo.autopilot_model_active}</b></> : null}
                </div>

                <label>Step reasoning</label>
                <select value={autopilotThinking} onChange={(e) => setAutopilotThinking(e.target.value)}>
                  <option value="dynamic">Dynamic — think as hard as each step needs</option>
                  <option value="off">Off — fastest, weaker on complex steps</option>
                </select>
                <div className="settings-hint">
                  Dynamic lets the operator reason more on tricky steps and less on obvious ones
                  (recommended). Off is quickest but measurably picks worse moves on hard pages.
                </div>

                <label className="settings-check">
                  <input type="checkbox" className="sw" checked={autopilotVision}
                         onChange={(e) => setAutopilotVision(e.target.checked)} />
                  <span>Let the DOM operator glance at page screenshots when stuck</span>
                </label>
              </div>
            )}

            {current.id === "interface" && (
              <div className="settings-sec">
                <label>Floating Overlay</label>
                <label className="settings-check">
                  <input type="checkbox" className="sw" checked={ovlEnabled}
                         onChange={(e) => setOvlEnabled(e.target.checked)} />
                  <span>Show a small status pill when I'm in other apps</span>
                </label>
                <div className="settings-hint">
                  A tiny pill floats above the taskbar when you're working in another app, showing whether
                  JARVIS is listening, processing or responding (with a stop button). It never shows on the
                  JARVIS window itself. Add apps below to <strong>hide</strong> it on them
                  (use the app's process name, e.g. <code>chrome</code>, <code>code</code>, <code>discord</code>).
                </div>
                {ovlEnabled && (
                  <>
                    <div className="dirlist">
                      {ovlApps.length === 0 && (
                        <div className="dirlist-empty">Shown on every app. Add apps to hide it on them.</div>
                      )}
                      {ovlApps.map((a) => (
                        <div key={a} className="dirlist-row">
                          <span className="dirlist-path" title={a}>{a}</span>
                          <button className="dirlist-rm" title="Remove" onClick={() => removeApp(a)}>✕</button>
                        </div>
                      ))}
                    </div>
                    <div className="dirlist-add">
                      <input type="text" value={newApp} placeholder="app name to hide on, e.g. chrome"
                             onChange={(e) => setNewApp(e.target.value)}
                             onKeyDown={(e) => { if (e.key === "Enter") addApp(); }} />
                      <button className="dirlist-addbtn" onClick={addApp}>HIDE</button>
                    </div>
                  </>
                )}
                <div className="settings-hint">
                  Colours, layout and which panels show live in <b>Customize</b> (the palette button
                  on the home screen).
                </div>
              </div>
            )}

            {current.id === "files" && (<>
              <div className="settings-sec">
                <label>Storage Location</label>
                <div className="settings-hint">
                  Everything JARVIS creates (images, QR codes, screenshots, recordings, PDFs) is saved
                  here, in <code>images/</code>, <code>recordings/</code> and <code>documents/</code>.
                </div>
                <input type="text" className="settings-model-id" value={storageDir}
                       onChange={(e) => setStorageDir(e.target.value)} placeholder="C:\Users\you\Jarvis" />
              </div>

              <div className="settings-sec">
                <label>Read-only Terminal — Allowed Directories</label>
                <div className="settings-hint">
                  JARVIS may <strong>list</strong> these folders and <strong>read</strong> files inside them
                  (every file read asks for your approval). It can never run, write, move, or delete anything.
                </div>
                <div className="dirlist">
                  {dirs.length === 0 && (
                    <div className="dirlist-empty">No folders approved — JARVIS can't see any files yet.</div>
                  )}
                  {dirs.map((d) => (
                    <div key={d} className="dirlist-row">
                      <span className="dirlist-path" title={d}>{d}</span>
                      <button className="dirlist-rm" title="Remove" onClick={() => removeDir(d)}>✕</button>
                    </div>
                  ))}
                </div>
                <div className="dirlist-add">
                  <input type="text" value={newDir} placeholder="C:\Users\you\Documents"
                         onChange={(e) => setNewDir(e.target.value)}
                         onKeyDown={(e) => { if (e.key === "Enter") addDir(); }} />
                  <button className="dirlist-addbtn" onClick={addDir}>ADD</button>
                </div>
              </div>

              <div className="settings-sec">
                <label>My Location</label>
                <div className="dirlist-add">
                  <input type="text" value={locInput} placeholder="e.g. Powai, Mumbai — pin if auto-detect is off"
                         onChange={(e) => setLocInput(e.target.value)}
                         onKeyDown={(e) => { if (e.key === "Enter") pinLocation(); }} />
                  <button className="dirlist-addbtn" onClick={pinLocation}>PIN</button>
                </div>
                {pinnedLoc?.label ? (
                  <div className="settings-hint">
                    📍 Pinned to <b>{pinnedLoc.label.split(",")[0]}</b> — used for weather,
                    nearby places &amp; directions.{" "}
                    <button className="settings-link" onClick={() => onSetLocation && onSetLocation({ clear: true })}>
                      Use automatic instead
                    </button>
                  </div>
                ) : (
                  <div className="settings-hint">
                    On automatic (browser GPS / IP). If weather or distances are off, pin your
                    exact area here — it overrides auto-detection everywhere. You can also say
                    "set my location to …".
                  </div>
                )}
              </div>
            </>)}

            {current.id === "devices" && (
              <RemotePairingSection
                devicePairing={devicePairing}
                devices={devices}
                onCreatePairing={onCreatePairing}
                onListDevices={onListDevices}
                onRevokeDevice={onRevokeDevice}
              />
            )}

            {current.id === "system" && (<>
              <div className="settings-sec">
                <label>System Resources</label>
                <div className="sysinfo-box">
                  <div className="sysinfo-row">
                    <span>RAM available</span>
                    <span>{sysInfo.ram != null ? `${sysInfo.ram} GB` : "—"}</span>
                  </div>
                  <div className="sysinfo-row">
                    <span>Active provider</span>
                    <span>{sysInfo.provider || "—"}</span>
                  </div>
                  <div className="sysinfo-row">
                    <span>Model providers connected</span>
                    <span>{llmConnected}</span>
                  </div>
                </div>
              </div>
              <div className="settings-sec">
                <label>Runtime Components</label>
                <div className="settings-hint">
                  Browser engine, offline speech model and the neural voice ship with JARVIS.
                  If a feature reports it "needs setup", re-fetch any missing one here — progress
                  shows on the main screen.
                </div>
                <button className="dirlist-addbtn sx-btn" onClick={() => { if (onRepairSetup) onRepairSetup(); onClose(); }}
                        disabled={!onRepairSetup}>
                  Repair components
                </button>
              </div>
            </>)}
          </div>

          <div className={`sx-foot${dirty ? " sx-foot--dirty" : ""}${escArmed ? " sx-foot--nudge" : ""}`}>
            <span className="sx-state">
              {escArmed ? "Unsaved changes — press Esc again to discard"
                : dirty ? `Unsaved changes${pendingKeys ? ` · ${pendingKeys} new key${pendingKeys === 1 ? "" : "s"}` : ""}`
                : "All changes saved"}
            </span>
            <button className="sx-cancel" onClick={onClose}>{dirty ? "Discard" : "Close"}</button>
            <button className="settings-save" onClick={handleSave} disabled={!dirty}>Save changes</button>
          </div>
        </div>
      </div>
    </div>
  );
}
