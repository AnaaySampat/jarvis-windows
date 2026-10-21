import { useState, useEffect, useRef } from "react";
import QRCode from "qrcode";
import { Icon } from "../hud/icons";
import { Card, Row, Switch, Guide, Note, ConfirmButton, ChipList, AddField } from "./controls";

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

  const eta = (s) => {
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)} min`;
    if (s < 86400) return `${Math.round(s / 3600)} h`;
    return "tomorrow";
  };

  return (
    <Card title="Free-tier routes"
          status={benched.length
            ? <span className="tag tag--warn">{benched.length} benched</span>
            : <span className="tag tag--ok">All available</span>}
          sub={counts.length
            ? `Keys in rotation: ${counts.map(([p, n]) => `${n} ${p}`).join(" · ")}.`
            : "No API keys configured yet."}
          actions={benched.length > 0 && (
            <button className="btn btn--sm" onClick={() => onSave({ clear_quota_benches: true })}>
              Clear benches
            </button>
          )}>
      {benched.length > 0 && (
        <div className="sx-card-body">
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
        </div>
      )}
      <Guide summary="What is a route?">
        A route is one model on one key. After a rate limit JARVIS benches it and uses the
        next one instead of failing — clearing is only needed if you&apos;ve fixed the cause.
      </Guide>
    </Card>
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

  return (<>
    <Card title="Pair a phone"
          sub="Control this PC from your phone over Tailscale — on the same network or a different one."
          actions={(
            <button className="btn btn--sm btn--primary"
                    onClick={() => onCreatePairing && onCreatePairing("Aura phone")}>
              <Icon name="qr" size={14} />{qr ? "New code" : "Generate pairing code"}
            </button>
          )}>
      <div className="sx-card-body">
        {devicePairing && !devicePairing.ok && (
          <Note tone="err">Couldn&apos;t create a pairing code: {devicePairing.reason || "unknown error"}.</Note>
        )}
        {devicePairing && devicePairing.ok && !tsReady && (
          <Note tone="warn">
            Tailscale isn&apos;t connected on this PC, so the phone can&apos;t reach it. Start
            Tailscale, then generate a new code.
          </Note>
        )}
        {qr ? (
          <div className="qr-block">
            <img className="qr-img" src={qr} alt="JARVIS pairing QR code" />
            <div className="qr-meta">
              <span>On the phone&apos;s <b>Remote PC</b> screen, scan the code, then type this PIN once:</span>
              {pin && <span className="qr-pin" aria-label={`PIN ${pin.split("").join(" ")}`}>{pin}</span>}
              <span>Address <b>{devicePairing.host || "—"}:8765</b></span>
              {fp && <span style={{ wordBreak: "break-all" }}>Host fingerprint <code>{fp.slice(0, 24)}…</code></span>}
              <span><span className="tag tag--busy">Expires in about 2 minutes</span></span>
            </div>
          </div>
        ) : (
          <ol className="steps">
            <li>Install Tailscale on this PC and the phone, signed in to the same account.</li>
            <li>Press <b>Generate pairing code</b> and scan it from the phone&apos;s Remote PC screen.</li>
            <li>Type the PIN shown here into the phone. The PIN is the approval: a code alone can&apos;t pair.</li>
          </ol>
        )}
      </div>
    </Card>

    <Card title="Paired phones" status={<span className="tag tag--plain tag--off">{paired.length}</span>}>
      {paired.length === 0 && <div className="empty-line">No phones paired yet.</div>}
      {paired.map((d) => (
        <Row key={d.device_id}
             title={<>
               <Icon name="phone" size={15} />{d.name}
               {d.connected
                 ? <span className="tag tag--ok">Connected</span>
                 : <span className="tag tag--off">Offline</span>}
             </>}
             desc={<>
               {d.connected ? "Connected now" : `Last seen ${timeAgo(d.last_seen)}`}
               {" · paired "}{timeAgo(d.paired_at)}
               {" · "}<code title={d.fingerprint}>{d.device_id.slice(0, 14)}…</code>
             </>}>
          <ConfirmButton title="Disconnect and remove this phone; it can pair again with a new code + PIN"
                         onConfirm={() => onRevokeDevice && onRevokeDevice(d.device_id)}>
            Remove
          </ConfirmButton>
        </Row>
      ))}
    </Card>
  </>);
}

// WhatsApp channel (backend whatsapp_channel.py): JARVIS as a linked device of the user's
// WhatsApp, by QR. Commands come from the account's own "Message yourself" chat or from
// numbers listed here, starting with "Jarvis" or replying to JARVIS; replies and approvals
// go back there.
const WA_STATUS = {
  off: ["Off", "off"],
  connecting: ["Connecting…", "busy"],
  linking: ["Scan the code to link", "busy"],
  connected: ["Linked", "ok"],
  logged_out: ["Unlinked from the phone", "warn"],
  error: ["Not connected", "err"],
};

/**
 * MCP servers (backend: mcp_client.py). Each server's tools become JARVIS tools; a
 * call that changes something is asked about first unless the server is trusted.
 * Secret values (env vars, headers) are write-only here: the backend never sends
 * them back, and leaving one blank keeps what's saved.
 */
const MCP_STATUS = { ready: ["Connected", "ok"], starting: ["Connecting…", "busy"], failed: ["Failed", "err"],
                     stopped: ["Stopped", "off"], disabled: ["Off", "off"] };

function splitCommand(line) {
  return [...(line || "").matchAll(/"([^"]*)"|'([^']*)'|(\S+)/g)].map((m) => m[1] ?? m[2] ?? m[3]);
}

function parseSecrets(text) {
  const out = {};
  for (const line of (text || "").split("\n")) {
    const i = line.indexOf("=") >= 0 ? line.indexOf("=") : line.indexOf(":");
    if (i > 0) out[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  }
  return out;
}

const TOOLS_SHOWN = 10;

function McpServer({ srv, send, onMcp }) {
  const [allTools, setAllTools] = useState(false);
  const tools = srv.tools || [];
  const [label, tone] = MCP_STATUS[srv.status] || [srv.status, "off"];
  const shown = allTools ? tools : tools.slice(0, TOOLS_SHOWN);
  return (
    <Card title={<><Icon name="plug" size={15} />{srv.name}</>}
          status={<span className={`tag tag--${tone}`}>
            {label}{srv.status === "ready" ? ` · ${tools.length} tool${tools.length === 1 ? "" : "s"}` : ""}
          </span>}
          sub={<code className="mono-line">{srv.url || [srv.command, ...(srv.args || [])].join(" ")}</code>}
          actions={<>
            <button className="btn btn--sm btn--ghost" onClick={() => onMcp && onMcp("mcp.restart", { name: srv.name })}>
              <Icon name="restart" size={14} />Reconnect
            </button>
            <ConfirmButton onConfirm={() => onMcp && onMcp("mcp.remove", { name: srv.name })}>Remove</ConfirmButton>
          </>}>
      {(srv.error || tools.length > 0 || (srv.secrets || []).length > 0) && (
        <div className="sx-card-body">
          {srv.error && <Note tone="err">{srv.error}</Note>}
          {srv.status === "ready" && tools.length > 0 && (
            <div className="chips">
              {shown.map((t) => <span key={t} className="chip chip--static"><span>{t}</span></span>)}
              {tools.length > TOOLS_SHOWN && (
                <button className="btn btn--sm btn--ghost" onClick={() => setAllTools(!allTools)}>
                  {allTools ? "Show fewer" : `+${tools.length - TOOLS_SHOWN} more`}
                </button>
              )}
            </div>
          )}
          {(srv.secrets || []).length > 0 && (
            <div className="sx-row-d">Saved secrets: {srv.secrets.join(", ")} — never shown here.</div>
          )}
        </div>
      )}
      <Row label title="Enabled" desc="Off stops the server and hides its tools from JARVIS.">
        <Switch checked={!srv.disabled} label={`${srv.name} enabled`}
                onChange={(on) => send(srv, { disabled: !on })} />
      </Row>
      <Row label title="Trusted" desc="Run its tools that change things without asking you first.">
        <Switch checked={!!srv.trusted} label={`Trust ${srv.name}`}
                onChange={(on) => send(srv, { trusted: on })} />
      </Row>
    </Card>
  );
}

function McpSection({ mcpState, onMcp }) {
  const st = mcpState || { available: true, servers: [] };
  const [name, setName] = useState("");
  const [target, setTarget] = useState("");
  const [secrets, setSecrets] = useState("");
  const [trusted, setTrusted] = useState(false);
  useEffect(() => { onMcp && onMcp("mcp.state"); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const send = (srv, patch) => {
    const keep = Object.fromEntries((srv.secrets || []).map((k) => [k, ""]));   // "" = keep
    const config = srv.transport === "http"
      ? { url: srv.url, headers: keep }
      : { command: srv.command, args: srv.args, env: keep };
    onMcp && onMcp("mcp.save", { name: srv.name, config: { ...config, trusted: srv.trusted,
                                                         disabled: srv.disabled, ...patch } });
  };
  const add = (e) => {
    e.preventDefault();
    if (!name.trim() || !target.trim()) return;
    const isUrl = /^https?:\/\//i.test(target.trim());
    const parts = splitCommand(target.trim());
    const config = isUrl
      ? { url: target.trim(), headers: parseSecrets(secrets) }
      : { command: parts[0] || "", args: parts.slice(1), env: parseSecrets(secrets) };
    onMcp && onMcp("mcp.save", { name: name.trim(), config: { ...config, trusted } });
    setName(""); setTarget(""); setSecrets(""); setTrusted(false);
  };

  return (<>
    {!st.available && (
      <Note tone="warn">
        MCP needs the <b>mcp</b> Python package: <code>pip install mcp</code>, then restart JARVIS.
      </Note>
    )}
    {(st.servers || []).map((srv) => <McpServer key={srv.name} srv={srv} send={send} onMcp={onMcp} />)}
    {(st.servers || []).length === 0 && st.available && (
      <div className="sx-empty">
        <Icon name="plug" size={22} />
        <div>No MCP servers yet. Add one below and its tools become JARVIS tools.</div>
      </div>
    )}

    <Card title="Add a server"
          sub="Tools load when you mention the server by name. Only add servers you trust: each one is a program running on this PC.">
      <form className="sx-card-body" onSubmit={add}>
        <div className="form-grid">
          <div>
            <label className="field-label" htmlFor="mcp-name">Name</label>
            <input id="mcp-name" type="text" className="field" placeholder="github" value={name}
                   onChange={(e) => setName(e.target.value)} />
          </div>
          <div>
            <label className="field-label" htmlFor="mcp-target">Command or URL</label>
            <input id="mcp-target" type="text" className="field field--mono" spellCheck={false}
                   placeholder="npx -y @modelcontextprotocol/server-github"
                   value={target} onChange={(e) => setTarget(e.target.value)} />
          </div>
        </div>
        <div>
          <label className="field-label" htmlFor="mcp-secrets">Secrets <span className="muted">— optional, one per line</span></label>
          <textarea id="mcp-secrets" rows={2} className="field field--mono" spellCheck={false}
                    placeholder={"GITHUB_TOKEN=…     (or a header for a URL — Authorization: Bearer …)"}
                    value={secrets} onChange={(e) => setSecrets(e.target.value)} />
        </div>
        <div className="form-foot">
          <label className="inline-switch">
            <Switch checked={trusted} onChange={setTrusted} label="Trusted" />
            <span>Trusted — run its tools without asking</span>
          </label>
          <button type="submit" className="btn btn--primary" disabled={!name.trim() || !target.trim()}>
            <Icon name="plus" size={14} />Add server
          </button>
        </div>
        {st.result && <Note tone={st.result.ok ? "ok" : "err"}>{st.result.summary}</Note>}
      </form>
      <Guide summary="Already set up in Claude Desktop?">
        <span>
          Paste its <code>mcpServers</code> into <code>{st.config || "%APPDATA%\\Jarvis\\mcp.json"}</code>,
          then Reconnect. Servers started with <code>npx</code> or <code>uvx</code> need Node.js or uv installed.
        </span>
      </Guide>
    </Card>
  </>);
}

/**
 * Connectors (backend: connectors.py). Gmail is built in (an IMAP sign-in with an app
 * password); the rest fill in an MCP server entry from one token, so they also show
 * under MCP Servers. The form's values are secrets: the backend never sends them back.
 */
const CONN_STATUS = { connected: ["Connected", "ok"], connecting: ["Connecting…", "busy"],
                      failed: ["Failed", "err"], off: ["Not connected", "off"] };

function ConnectorForm({ c, onSend, pending, setPending }) {
  const [values, setValues] = useState({});
  const set = (k, v) => setValues((cur) => ({ ...cur, [k]: v }));
  const fields = (c.fields || []).filter((f) => f.key !== "host" || values.provider === "custom");
  const missing = fields.some((f) => !f.optional && !String(values[f.key] ?? (f.options ? f.options[0][0] : "")).trim());
  const submit = (e) => {
    e.preventDefault();
    if (missing || pending) return;
    const withDefaults = Object.fromEntries(fields.map((f) => [f.key, values[f.key] ?? (f.options ? f.options[0][0] : "")]));
    setPending(c.id);
    onSend("connectors.connect", { id: c.id, values: withDefaults });
  };
  return (
    <form className="sx-card-body" onSubmit={submit}>
      <ol className="steps">{(c.steps || []).map((s, i) => <li key={i}>{s}</li>)}</ol>
      {c.link && (
        <div>
          <button type="button" className="btn btn--sm btn--ghost"
                  onClick={() => onSend("run_action", { spec: { type: "open_url", url: c.link } })}>
            <Icon name="external" size={14} />Open {c.link.replace(/^https?:\/\//, "").split("/")[0]}
          </button>
        </div>
      )}
      {fields.map((f) => (
        <div key={f.key}>
          <label className="field-label" htmlFor={`conn-${c.id}-${f.key}`}>
            {f.label}{f.optional && <span className="muted"> — optional</span>}
          </label>
          {f.type === "select" ? (
            <select id={`conn-${c.id}-${f.key}`} className="field" value={values[f.key] ?? f.options[0][0]}
                    onChange={(e) => set(f.key, e.target.value)}>
              {f.options.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select>
          ) : (
            <input id={`conn-${c.id}-${f.key}`} className={`field${f.secret ? " field--mono" : ""}`}
                   type={f.secret ? "password" : f.type === "email" ? "email" : "text"}
                   autoComplete="off" spellCheck={false} placeholder={f.placeholder}
                   value={values[f.key] || ""} onChange={(e) => set(f.key, e.target.value)} />
          )}
        </div>
      ))}
      <div className="form-foot">
        <span className="muted">{c.kind === "email" ? "Checked by signing in once." : "Saved on this PC only."}</span>
        <button type="submit" className="btn btn--primary" disabled={missing || !!pending}>
          <Icon name="plug" size={14} />{pending === c.id ? "Connecting…" : `Connect ${c.name}`}
        </button>
      </div>
    </form>
  );
}

function ConnectorsSection({ connState, onSend }) {
  const st = connState || { connectors: [], mcp_available: true };
  const [open, setOpen] = useState(null);
  // {id, at}: a connect is in flight until the backend's next state (≠ `at`) arrives.
  const [sent, setSent] = useState(null);
  const pending = sent && sent.at === connState ? sent.id : null;
  const setPending = (id) => setSent({ id, at: connState });
  useEffect(() => { onSend && onSend("connectors.state"); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  const list = st.connectors || [];
  const cur = list.find((c) => c.id === open);

  return (<>
    {!st.mcp_available && (
      <Note tone="warn">
        Connectors other than email run as MCP servers, which need the <b>mcp</b> Python package:
        <code> pip install mcp</code>, then restart JARVIS.
      </Note>
    )}
    <div className="conn-grid">
      {list.map((c) => {
        const [label, tone] = CONN_STATUS[c.status] || [c.status, "off"];
        return (
          <button key={c.id} type="button" className={`conn-tile${open === c.id ? " is-on" : ""}`}
                  onClick={() => setOpen(open === c.id ? null : c.id)} aria-expanded={open === c.id}>
            <span className="conn-hd">
              <Icon name={c.icon || "plug"} size={17} /><b>{c.name}</b>
              <span className={`tag tag--${tone}`}>{label}</span>
            </span>
            <span className="conn-d">{c.detail ? `${c.detail} · ` : ""}{c.blurb}</span>
          </button>
        );
      })}
    </div>
    {cur && (
      <Card title={<><Icon name={cur.icon || "plug"} size={15} />{cur.name}</>}
            status={<span className={`tag tag--${(CONN_STATUS[cur.status] || [0, "off"])[1]}`}>
              {(CONN_STATUS[cur.status] || [cur.status])[0]}</span>}
            sub={cur.blurb}
            actions={cur.status !== "off" && (
              <ConfirmButton onConfirm={() => onSend("connectors.disconnect", { id: cur.id })}>Disconnect</ConfirmButton>
            )}>
        {(cur.needs || cur.error || cur.result) && (
          <div className="sx-card-body">
            {cur.needs && <Note tone="warn">{cur.needs}</Note>}
            {cur.error && <Note tone="err">{cur.error}</Note>}
            {cur.result && !cur.error && <Note tone={cur.result.ok ? "ok" : "err"}>{cur.result.summary}</Note>}
          </div>
        )}
        {cur.status === "connected" ? (
          <Guide summary="Use a new key or password">
            <ConnectorForm c={cur} onSend={onSend} pending={pending} setPending={setPending} />
          </Guide>
        ) : (
          <ConnectorForm c={cur} onSend={onSend} pending={pending} setPending={setPending} />
        )}
      </Card>
    )}
    <div className="sx-card-body sx-inline">
      <Note>
        A connected app&apos;s tools that change something (a new issue, a light) ask you first, unless
        you trust it under MCP Servers. JARVIS reads email and writes drafts, but never sends one.
        Anything else with an MCP server can be added under MCP Servers.
      </Note>
    </div>
  </>);
}

function WhatsAppSection({ waState, onWhatsApp }) {
  const st = waState || { status: "off", enabled: false, allow: [] };
  const [allow, setAllow] = useState(null);            // null = show the saved list
  const allowText = allow ?? (st.allow || []).map((n) => `+${n}`).join(", ");
  // The drawn code, kept with the text it encodes: a stale image never shows.
  const [drawn, setDrawn] = useState({ code: "", url: "" });
  const qr = st.qr && drawn.code === st.qr ? drawn.url : "";
  useEffect(() => { onWhatsApp && onWhatsApp("whatsapp.state"); }, []);  // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    let dead = false;
    if (st.qr) {
      QRCode.toDataURL(st.qr, { margin: 2, width: 640, errorCorrectionLevel: "L" })
        .then((url) => { if (!dead) setDrawn({ code: st.qr, url }); })
        .catch(() => {});
    }
    return () => { dead = true; };
  }, [st.qr]);
  const set = (patch) => onWhatsApp && onWhatsApp("whatsapp.set", patch);
  const [label, tone] = WA_STATUS[st.status] || [st.status, "off"];

  return (
    <Card title="WhatsApp" status={<span className={`tag tag--${tone}`}>{label}</span>}
          sub={<>Message JARVIS from anywhere: write to yourself in WhatsApp&apos;s <b>Message yourself</b> chat,
            starting with its name — “Jarvis, what&apos;s on my agenda?”</>}>
      <Row label title="Use WhatsApp"
           desc={st.status === "connected" && st.number ? `Linked as +${st.number}.` : "Links JARVIS like WhatsApp Web."}>
        <Switch checked={!!st.enabled} label="Use WhatsApp" onChange={(on) => set({ enabled: on })} />
      </Row>
      {(st.error || qr) && (
        <div className="sx-card-body">
          {st.error && <Note tone="err">{st.error}</Note>}
          {qr && (
            <div className="qr-block">
              <img className="qr-img" src={qr} alt="WhatsApp link QR code" />
              <div className="qr-meta">
                <span>On your phone: WhatsApp → Settings → <b>Linked devices</b> → <b>Link a device</b>, then scan this.</span>
                <span><span className="tag tag--busy">The code refreshes every few seconds</span></span>
              </div>
            </div>
          )}
        </div>
      )}
      <Row stack title="Other numbers that may command JARVIS"
           desc="Optional — e.g. your own number if JARVIS is linked to a second one. Everyone else, and every group, is ignored.">
        <form className="field-row" onSubmit={(e) => { e.preventDefault(); set({ allow: allowText }); setAllow(null); }}>
          <input type="text" className="field field--mono" placeholder="+91 98765 43210, …" value={allowText}
                 aria-label="Allowed numbers" onChange={(e) => setAllow(e.target.value)} />
          <button type="submit" className="btn" disabled={allow === null}>Save</button>
        </form>
      </Row>
      <Guide summary="How it works">
        <span>
          Replies come back in that chat and the PC stays quiet. Anything beyond a lookup asks you there
          first (reply yes or no), because nobody may be at the PC to see it. Voice notes work, and a photo
          or document with a “Jarvis, …” caption comes with the command. Messages that don&apos;t start with
          “Jarvis” are left alone, unless they reply to one of JARVIS&apos;s.
        </span>
      </Guide>
      <div className="sx-card-body">
        <Note tone="warn">
          This uses an unofficial WhatsApp client (the WhatsApp Web protocol). It isn&apos;t covered by
          WhatsApp&apos;s terms, and WhatsApp can restrict accounts that use one. Linking a second number for
          JARVIS is the cautious option.
        </Note>
      </div>
      {(st.status === "connected" || st.number) && (
        <Row title="Unlink this WhatsApp" desc="Signs JARVIS out of the linked device and forgets it.">
          <ConfirmButton onConfirm={() => onWhatsApp && onWhatsApp("whatsapp.unlink")}>Unlink</ConfirmButton>
        </Row>
      )}
    </Card>
  );
}

// The user's Android TV — network ADB from this PC (backend actions/tv.py); nothing is
// installed on the TV. Saved only once the TV answers, i.e. after the user accepts
// "Allow USB debugging?" on it.
function TvSection({ tvState, onTv }) {
  const config = tvState && tvState.config;
  const result = tvState && tvState.result;
  const [typed, setHost] = useState(null);           // null = show the saved address
  const host = typed ?? (config ? config.host : "");
  const [typedMac, setMac] = useState(null);
  const mac = typedMac ?? (config ? config.mac || "" : "");
  // Busy until the backend answers with a fresh tv.state (a new object).
  const [sentWith, setSentWith] = useState(undefined);
  const busy = sentWith !== undefined && sentWith === tvState;
  useEffect(() => { onTv && onTv("tv.state"); }, []);  // eslint-disable-line react-hooks/exhaustive-deps

  const connect = (e) => {
    e.preventDefault();
    if (busy || !host) return;
    setSentWith(tvState); onTv && onTv("tv.pair", { host });
  };
  const saveMac = (e) => {
    e.preventDefault();
    if (busy || mac === (config.mac || "")) return;
    setSentWith(tvState); setMac(null); onTv && onTv("tv.set_mac", { mac });
  };

  return (<>
    <Card title={<><Icon name="tv" size={15} />{config ? config.model || "Android TV" : "Android TV"}</>}
          status={busy ? <span className="tag tag--busy">Waiting for the TV…</span>
            : config ? <span className="tag tag--ok">Connected</span>
            : <span className="tag tag--off">Not set up</span>}
          sub="Control an Android TV on this network by voice — “play Stranger Things on the TV”. Nothing is installed on the TV.">
      <Row stack title="TV address"
           desc="The TV's IP (on the TV: Settings → Network). Behind a second router? Add its Tailscale address after a comma.">
        <form className="field-row" onSubmit={connect}>
          <input type="text" className="field field--mono" placeholder="192.168.1.50, 100.64.1.2" value={host}
                 aria-label="TV address" onChange={(e) => setHost(e.target.value.trim())} />
          <button type="submit" className="btn" disabled={busy || !host}>
            {busy ? "Waiting…" : config ? "Reconnect" : "Connect"}
          </button>
        </form>
      </Row>
      {(busy || result) && (
        <div className="sx-card-body">
          {busy && <Note>If the TV asks “Allow USB debugging?”, tick <b>Always allow</b> and press OK.</Note>}
          {!busy && result && <Note tone={result.ok ? "ok" : "err"}>{result.summary}</Note>}
        </div>
      )}
      <Guide summary="First-time setup on the TV">
        <ol>
          <li>On the TV: Settings → Device Preferences → About → press <b>Build</b> 7 times.</li>
          <li>Back in Device Preferences → Developer options → turn on <b>USB debugging</b>.</li>
          <li>Enter the TV&apos;s IP above and press Connect, then accept the prompt on the TV.</li>
        </ol>
      </Guide>
      <Guide summary="More than one router at home?">
        <span>
          A second router in router mode gives its Wi-Fi its own network, and the TV can&apos;t be reached
          across it. Set that router to <b>Access Point</b> mode, or install Tailscale on the TV and add its
          100.x address after a comma (192.168.1.50, 100.64.1.2): JARVIS uses whichever answers.
        </span>
      </Guide>
    </Card>

    {config && (
      <Card title="Switching it on from off"
            status={config.mac ? <span className="tag tag--ok">Ready</span> : <span className="tag tag--warn">Needs its MAC</span>}
            sub="Wake-on-LAN. Works only when this PC is on the same network as the TV.">
        <Row stack title="TV network card (MAC address)"
             desc={config.mac
               ? <>JARVIS wakes the TV through <b>{config.mac}</b>.</>
               : "Learned the next time the TV answers — or type it (on the TV: Settings → Network → About)."}>
          <form className="field-row" onSubmit={saveMac}>
            <input type="text" className="field field--mono" placeholder="12:34:56:78:9A:BC" value={mac}
                   aria-label="TV MAC address" onChange={(e) => setMac(e.target.value.trim())} />
            <button type="submit" className="btn" disabled={busy || mac === (config.mac || "")}>Save</button>
          </form>
        </Row>
        <div className="sx-card-body">
          <Note>On the TV, turn on Settings → Network → <b>Remote start</b>.</Note>
        </div>
      </Card>
    )}

    {config && (
      <Card title="Forget this TV" sub="Removes the saved address. You can connect it again any time."
            actions={(
              <ConfirmButton onConfirm={() => { setHost(null); setMac(null); onTv && onTv("tv.unpair"); }}>
                Forget TV
              </ConfirmButton>
            )} />
    )}
  </>);
}

const DEVICE_TABS = [
  { id: "phone", label: "Phone", icon: "phone" },
  { id: "tv", label: "TV", icon: "tv" },
  { id: "whatsapp", label: "WhatsApp", icon: "message" },
];
function DevicesSection(props) {
  const [tab, pick] = useState("phone");
  const phoneOn = (props.devices || []).some((d) => !d.revoked && d.connected);
  const dot = {
    phone: phoneOn ? "ok" : "",
    tv: props.tvState?.config ? "ok" : "",
    whatsapp: props.waState?.status === "connected" ? "ok"
      : ["logged_out", "error"].includes(props.waState?.status) ? "warn" : "",
  };
  return (<>
    <div className="seg" role="tablist" aria-label="Devices">
      {DEVICE_TABS.map((t) => (
        <button key={t.id} role="tab" aria-selected={tab === t.id}
                className={`seg-btn${tab === t.id ? " is-on" : ""}`} onClick={() => pick(t.id)}>
          <Icon name={t.icon} size={15} />{t.label}
          <span className={`seg-dot${dot[t.id] ? ` seg-dot--${dot[t.id]}` : ""}`} aria-hidden />
        </button>
      ))}
    </div>
    {tab === "phone" && (
      <RemotePairingSection devicePairing={props.devicePairing} devices={props.devices}
                            onCreatePairing={props.onCreatePairing} onListDevices={props.onListDevices}
                            onRevokeDevice={props.onRevokeDevice} />
    )}
    {tab === "tv" && <TvSection tvState={props.tvState} onTv={props.onTv} />}
    {tab === "whatsapp" && <WhatsAppSection waState={props.waState} onWhatsApp={props.onTv} />}
  </>);
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
    <Card title="Smart routing"
          status={r.in_progress ? <span className="tag tag--busy">Ranking…</span>
            : models.length ? <span className="tag tag--plain tag--off">{models.length} models</span> : null}
          sub={models.length === 0 && !r.in_progress
            ? "Not ranked yet — JARVIS ranks your models automatically once it can see them. Until then it uses its built-in Gemini/Groq tiers."
            : <>Scores are the Artificial Analysis Intelligence Index ({r.catalog || "benchmark"} snapshot)
              for {c.benchmark || 0} of your {models.length} models
              {c.llm ? <>; {c.llm} newer model{c.llm === 1 ? "" : "s"} estimated by <b>{r.estimated_by || "an LLM"}</b> with Google Search</> : null}
              {c.guess ? <>; {c.guess} not scored yet (placed last in their tier)</> : null}.
              Updates on its own when you add or remove a key.</>}
          actions={onRerank && (
            <button className="btn btn--sm" disabled={!!r.in_progress} onClick={onRerank}
                    title="Look up the models without benchmark data again">
              <Icon name="restart" size={14} />{r.in_progress ? "Ranking…" : "Re-rank now"}
            </button>
          )}>
      {((!r.in_progress && r.last_result) || models.length > 0) && (
        <div className="sx-card-body">
          {!r.in_progress && r.last_result && <Note>{r.last_result}</Note>}
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
        </div>
      )}
      <Guide summary="How a request picks its model">
        Each request gets the weakest tier that can handle it (quick chat → Fast, everyday
        commands → Everyday, hard reasoning → Flagship). If that model&apos;s API fails or is
        rate-limited, the next one in line takes over — then stronger tiers, then weaker.
      </Guide>
    </Card>
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
  { value: "piper",      label: "Piper — offline neural, British",
    desc: "Free, unlimited and private. The voice model (~60 MB) downloads the first time JARVIS speaks." },
  { value: "elevenlabs", label: "ElevenLabs — premium cloud",
    desc: "The most natural voice, but the free tier is ~10k characters a month — best kept for special use." },
  { value: "chirp",      label: "Google Chirp 3 HD — premium British",
    desc: "Runs on your Google Cloud credits." },
  { value: "edge-tts",   label: "Edge TTS — online, British male", desc: "Free online voice from Microsoft Edge." },
  { value: "pyttsx3",    label: "pyttsx3 — offline, robotic", desc: "Windows' built-in voice. Works anywhere, sounds dated." },
  { value: "off",        label: "Off", desc: "Replies show as text only." },
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
  { id: "tavily", name: "Tavily (web search backup)", key: "tavily_api_key", has: "has_tavily_key",
    multi: true, ph: "tvly-…", where: "app.tavily.com",
    note: "Answers web searches only when Gemini can't: no Gemini key, or its search quota is spent." },
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
    placeholder: configured ? "Paste to replace" : p.ph,
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
        <button type="button" className="btn btn--ghost btn--icon" onClick={() => setShow(!show)}
                title={show ? "Hide what you typed" : "Show what you typed"} aria-pressed={show}>
          <Icon name={show ? "eyeOff" : "eye"} size={15} />
        </button>
        {configured && (
          <ConfirmButton className="btn btn--danger" title="Delete this saved key"
                         onConfirm={() => onSave({ remove_keys: [p.key] })}>
            Remove
          </ConfirmButton>
        )}
      </div>
      <div className="kc-foot">
        {p.note ? <span>{p.note}</span> : <span>Get a key at <b>{p.where}</b></span>}
      </div>
    </div>
  );
}

const PROVIDER_MODES = [
  { id: "vertex", title: "Vertex AI", icon: "spark", desc: "Gemini on your Google Cloud credits, through your gcloud login." },
  { id: "gemini", title: "API keys", icon: "key", desc: "Gemini, Groq and every provider you've added a key for." },
  { id: "offline", title: "Offline", icon: "monitor", desc: "A local Ollama model only — no internet, no cloud." },
];

// Sidebar sections. `words` feed the search box so "voice" finds TTS, "phone"
// finds pairing, etc. Section labels are what the README and the backend's spoken
// hints call them ("Settings → Devices → TV"), so keep them in step.
const SECTIONS = [
  { id: "ai", label: "Intelligence", icon: "spark", desc: "Where JARVIS thinks, and with which model.",
    words: "provider model llm vertex gemini offline ollama auto image" },
  { id: "keys", label: "API Keys", icon: "key", desc: "Credentials for model providers and services. Stored on this PC only.",
    words: "api keys groq gemini nvidia openrouter mistral claude anthropic openai gpt grok xai meta llama elevenlabs maps tavily web search credentials" },
  { id: "routing", label: "Routing & Quota", icon: "route", desc: "How requests are spread across your models and keys.",
    words: "smart routing rank tiers quota rate limit benched free" },
  { id: "autopilot", label: "Autopilot", icon: "cursor", desc: "Agent mode, and the operator that drives the browser and desktop for multi-step tasks.",
    words: "autopilot browser desktop operator reasoning vision screenshot agent mode agents simple mode coordinator plan specialist" },
  { id: "voice", label: "Voice", icon: "volume", desc: "How JARVIS sounds, wakes up and speaks up on its own.",
    words: "voice speech tts piper elevenlabs chirp wake word alerts heartbeat checklist digest nightly proactive" },
  { id: "interface", label: "Interface", icon: "layout", desc: "The floating pill, the home screen and keyboard shortcuts.",
    words: "overlay pill floating interface apps hide keyboard shortcuts customize theme" },
  { id: "files", label: "Files & Location", icon: "folder", desc: "Where JARVIS saves things, what it may read, and where you are.",
    words: "storage folder directories files read location weather pin skills terminal" },
  { id: "devices", label: "Devices", icon: "phone", desc: "Your phone, Android TV and WhatsApp.",
    words: "phone remote pairing qr pin tailscale devices mobile tv television android adb netflix whatsapp message chat wake lan" },
  { id: "connectors", label: "Connectors", icon: "plug", desc: "Gmail, GitHub, Notion, Google Calendar and more — set up in a minute.",
    words: "connectors connect gmail email mail inbox imap app password github notion google calendar todoist home assistant brave search zapier slack folders integrations apps" },
  { id: "mcp", label: "MCP Servers", icon: "plug", desc: "Extra tools from Model Context Protocol servers.",
    words: "mcp model context protocol servers tools plugins integrations connectors github notion" },
  { id: "system", label: "System", icon: "wrench", desc: "Resources and the components JARVIS ships with.",
    words: "system ram resources repair components" },
];
// Settings ▸ Autopilot: simple mode (one chat model, as always) or agent mode (agents.py).
const AGENT_MODES = [
  { id: "simple", on: false, icon: "chat", title: "Simple",
    desc: "One assistant with every tool decides what to do — quick for everyday requests." },
  { id: "agents", on: true, icon: "route", title: "Agents",
    desc: "A coordinator sends each request to the chat, browser or desktop agent, and plans tasks that need more than one." },
];

const NAV_GROUPS = [
  { label: "Intelligence", ids: ["ai", "keys", "routing", "autopilot"] },
  { label: "Experience", ids: ["voice", "interface", "files"] },
  { label: "Connections", ids: ["devices", "connectors", "mcp"] },
  { label: "System", ids: ["system"] },
];
let lastSection = "ai";   // reopen where the user left off (per app session)

const SHORTCUTS = [
  [["Ctrl", "Space"], "Hold to talk, from any app"],
  [["Ctrl", "K"], "Command palette — open any menu or action"],
  [["Ctrl", ","], "Settings"],
  [["Ctrl", "S"], "Save, inside Settings"],
  [["Esc"], "Close the open panel"],
];

export default function Settings({ sysInfo, onClose, onSave, onSaved, onSetLocation, onRefreshModels, onRerankModels, onRepairSetup,
  devicePairing, devices, onCreatePairing, onListDevices, onRevokeDevice, tvState, onTv, waState, mcpState, connState,
  onOpenCustomize, initialSection }) {
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
  const [section, setSection] = useState(initialSection || lastSection);
  const [query, setQuery] = useState("");
  const [model, setModel] = useState(sysInfo.model_override ?? sysInfo.model ?? "auto");
  // Provider EDITION (Phase 4): vertex | gemini | offline.
  const [providerMode, setProviderMode] = useState(sysInfo.provider_mode || "vertex");
  const [offlineModel, setOfflineModel] = useState(sysInfo.offline_model ?? "");
  const [ollamaUrl, setOllamaUrl] = useState(sysInfo.ollama_url ?? "http://localhost:11434");
  const pinnedLoc = sysInfo.manual_location;   // {lat,lon,label} or null = automatic
  const [tts,   setTts]   = useState(sysInfo.tts ?? "piper");
  const [dirs, setDirs]   = useState(sysInfo.allowed_dirs ?? []);
  const [storageDir, setStorageDir] = useState(sysInfo.storage_dir ?? "");
  const [keys, setKeys] = useState({});   // key name -> text typed this session
  const [sysAlerts, setSysAlerts] = useState(!!sysInfo.system_alerts);
  const [hb, setHb] = useState({
    enabled: !!sysInfo.heartbeat?.enabled,
    every_min: sysInfo.heartbeat?.every_min ?? 30,
    start: sysInfo.heartbeat?.start ?? "08:00",
    end: sysInfo.heartbeat?.end ?? "22:00",
  });
  const [digest, setDigest] = useState({ enabled: sysInfo.digest?.enabled !== false });
  const [autopilotModel, setAutopilotModel] = useState(sysInfo.autopilot_model ?? "");
  const [autopilotThinking, setAutopilotThinking] = useState(
    (sysInfo.autopilot_thinking ?? -1) === 0 ? "off" : "dynamic");
  const [autopilotVision, setAutopilotVision] = useState(sysInfo.autopilot_vision !== false);
  const [agentMode, setAgentMode] = useState(!!sysInfo.agent_mode);
  const [ovlEnabled, setOvlEnabled] = useState(sysInfo.overlay?.enabled !== false);
  const [ovlApps, setOvlApps] = useState(sysInfo.overlay?.apps ?? []);
  // Preserve whatever mode is in effect (it can be changed by voice via
  // set_overlay) so saving Settings doesn't silently reset it to "except".
  const [ovlMode] = useState(sysInfo.overlay?.mode ?? "except");
  const [closeArmed, setCloseArmed] = useState(false);
  // Focus the dialog so Ctrl+S / Esc work before anything inside is clicked.
  const panelRef = useRef(null);
  useEffect(() => { panelRef.current && panelRef.current.focus(); }, []);

  const addApp = (v) => {
    const a = v.trim().toLowerCase().replace(/\.exe$/, "");
    if (a && !ovlApps.includes(a)) setOvlApps([...ovlApps, a]);
  };
  const addDir = (v) => {
    const d = v.trim().replace(/^["']|["']$/g, "");
    if (d && !dirs.includes(d)) setDirs([...dirs, d]);
  };

  const cfg = {
    model_override: model, tts, allowed_dirs: dirs, system_alerts: sysAlerts, heartbeat: hb, digest,
    overlay: { enabled: ovlEnabled, mode: ovlMode, apps: ovlApps },
    autopilot_model: autopilotModel.trim(),
    autopilot_thinking: autopilotThinking === "off" ? 0 : -1,
    autopilot_vision: autopilotVision,
    agent_mode: agentMode,
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
  // With unsaved edits the first close (Esc, ✕ or a click outside) only warns, so a
  // stray keypress or click can't throw away a pasted key.
  const requestClose = () => {
    if (dirty && !closeArmed) setCloseArmed(true);
    else onClose();
  };

  const onKey = (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      if (dirty) handleSave();
    } else if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      requestClose();
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
  const mcpServers = mcpState?.servers || [];
  const mcpFailed = mcpServers.filter((s) => s.status === "failed").length;
  const mcpReady = mcpServers.filter((s) => s.status === "ready").length;
  const badge = {
    keys: llmConnected ? [llmConnected] : null,
    routing: benched ? [benched, "warn"] : null,
    mcp: mcpFailed ? [mcpFailed, "warn"] : mcpReady ? [mcpReady] : null,
    connectors: (() => {
      const n = (connState?.connectors || []).filter((c) => c.status === "connected").length;
      return n ? [n] : null;
    })(),
  };
  const current = SECTIONS.find((s) => s.id === section) || SECTIONS[0];
  const ttsOpt = TTS_OPTS.find((o) => o.value === tts);
  const providerStatus = {
    vertex: sysInfo.vertex ? ["ADC detected", "ok"] : ["Not signed in", "warn"],
    gemini: [`${LLM_PROVIDERS.filter((p) => sysInfo[p.has]).length} providers`, LLM_PROVIDERS.some((p) => sysInfo[p.has]) ? "ok" : "warn"],
    offline: localOpts.length ? [`${localOpts.length} local model${localOpts.length === 1 ? "" : "s"}`, "ok"] : ["No local models", "off"],
  };

  return (
    <div className="settings-overlay" onClick={requestClose}>
      <div className="settings-panel sx" role="dialog" aria-modal="true" aria-label="Settings" tabIndex={-1}
           onKeyDown={onKey} onClick={e => e.stopPropagation()}
           ref={panelRef}>

        <nav className="sx-nav" aria-label="Settings sections">
          <div className="sx-brand">Settings</div>
          <div className="sx-search-wrap">
            <Icon name="search" size={14} />
            <input className="sx-search" type="search" value={query} placeholder="Search settings…"
                   aria-label="Search settings"
                   onChange={(e) => setQuery(e.target.value)}
                   onKeyDown={(e) => { if (e.key === "Enter" && visible[0]) go(visible[0].id); }} />
          </div>
          <div className="sx-links">
            {NAV_GROUPS.map((g) => {
              const items = visible.filter((s) => g.ids.includes(s.id));
              if (!items.length) return null;
              return (
                <div key={g.label} className="sx-navgroup">
                  <div className="sx-navgroup-l">{g.label}</div>
                  {items.map((s) => (
                    <button key={s.id} className={`sx-link${s.id === current.id ? " sx-link--on" : ""}`}
                            onClick={() => go(s.id)} aria-current={s.id === current.id ? "page" : undefined}>
                      <span className="sx-ico" aria-hidden><Icon name={s.icon} size={16} /></span>
                      <span className="sx-lbl">{s.label}</span>
                      {badge[s.id] && (
                        <span className={`sx-badge${badge[s.id][1] === "warn" ? " sx-badge--warn" : ""}`}>{badge[s.id][0]}</span>
                      )}
                    </button>
                  ))}
                </div>
              );
            })}
            {visible.length === 0 && <div className="sx-none">No settings match “{query}”.</div>}
          </div>
          <div className="sx-kbd"><kbd>Ctrl</kbd>+<kbd>S</kbd> save · <kbd>Esc</kbd> close</div>
        </nav>

        <div className="sx-main">
          <div className="sx-hdr">
            <div className="sx-hdr-txt">
              <h2>{current.label}</h2>
              <p>{current.desc}</p>
            </div>
            <button className="x-btn" onClick={requestClose} aria-label="Close settings"><Icon name="close" size={16} /></button>
          </div>

          <div className="sx-body" key={current.id}>
            {current.id === "ai" && (<>
              <Card title="Provider" sub="Choose where JARVIS's language model runs.">
                <div className="sx-card-body">
                  <div className="opt-grid" role="radiogroup" aria-label="Provider">
                    {PROVIDER_MODES.map((p) => {
                      const [sLabel, sTone] = providerStatus[p.id];
                      return (
                        <button key={p.id} type="button" role="radio" aria-checked={providerMode === p.id}
                                className={`opt${providerMode === p.id ? " is-on" : ""}`}
                                onClick={() => setProviderMode(p.id)}>
                          <span className="opt-hd"><Icon name={p.icon} size={16} /><b>{p.title}</b><span className="opt-radio" /></span>
                          <span className="opt-d">{p.desc}</span>
                          <span className={`tag tag--${sTone}`}>{sLabel}</span>
                        </button>
                      );
                    })}
                  </div>
                  {providerMode === "vertex" && !sysInfo.vertex && (
                    <Note tone="warn">
                      Application Default Credentials weren&apos;t found. Run <code>gcloud auth application-default login</code>,
                      then reopen Settings.
                    </Note>
                  )}
                  {providerMode === "gemini" && (
                    <Note tone={sysInfo.has_gemini_key ? "info" : "warn"}>
                      Runs on the keys in <button className="settings-link" onClick={() => go("keys")}>API Keys</button>; Vertex stays off.
                      {!sysInfo.has_gemini_key && " No Gemini key yet — image generation needs one."}
                    </Note>
                  )}
                </div>
                {providerMode === "offline" && (<>
                  <Row title="Local model" desc="Commands still work; it's slower and less capable than the cloud tiers.">
                    <select className="field" value={offlineModel} onChange={e => setOfflineModel(e.target.value)}
                            aria-label="Local model">
                      <option value="">Auto — first installed model</option>
                      {localOpts.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                    </select>
                  </Row>
                  <Row stack title="Ollama address"
                       extra={localOpts.length === 0 && (
                         <Note tone="warn">
                           No local models found at {ollamaUrl}. Install Ollama, then pull one (e.g. <code>ollama pull llama3.1</code>).
                         </Note>
                       )}>
                    <input type="text" className="field field--mono" value={ollamaUrl} aria-label="Ollama address"
                           onChange={(e) => setOllamaUrl(e.target.value)} placeholder="http://localhost:11434" />
                  </Row>
                </>)}
              </Card>

              <Card title="Chat model" sub="Auto is recommended: it picks a model per request and falls back when one fails.">
                <Row title="Model" desc={allValues.includes(model) ? null : `Custom model id: ${model}`}>
                  <select className="field" value={allValues.includes(model) ? model : "__custom"} aria-label="LLM model"
                          onChange={e => { if (e.target.value !== "__custom") setModel(e.target.value); }}>
                    {modelGroups.map((g) => (
                      <optgroup key={g.label} label={g.label}>
                        {g.opts.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                      </optgroup>
                    ))}
                    {!allValues.includes(model) && <option value="__custom">Custom: {model}</option>}
                  </select>
                </Row>
                <Row stack title="Exact model id" desc="Override with any model id your keys can reach.">
                  <input type="text" className="field field--mono" value={model} aria-label="Exact model id"
                         onChange={(e) => setModel(e.target.value)} placeholder="auto" spellCheck={false} />
                </Row>
                <div className="sx-card-body">
                  {geminiSelected && sysInfo.vertex && (
                    <Note tone="ok">Served via Vertex AI on your Google Cloud credits — no API key needed.</Note>
                  )}
                  {geminiSelected && !sysInfo.vertex && !sysInfo.has_gemini_key && (
                    <Note tone="warn">This is a Gemini model — add your Gemini API key, or sign in with Vertex (gcloud ADC).</Note>
                  )}
                  {model === "auto" && (
                    <Note>
                      Auto sends each request to the weakest ranked model that can handle it, across every
                      provider you have a key for, and falls through to the next if one fails — see{" "}
                      <button className="settings-link" onClick={() => go("routing")}>Routing</button>.
                    </Note>
                  )}
                  {nativeAudio && (
                    <Note>
                      Native-audio dialogue uses Google&apos;s Live API — full-duplex mic-in / voice-out with the
                      model&apos;s own voice (no Whisper, no TTS), with barge-in. Typed messages fall back to a Gemini
                      text model. Needs Gemini access (Vertex credits or a Gemini API key).
                    </Note>
                  )}
                  <div className="sx-row-d">
                    Image generation uses Gemini 2.5 Flash image with any model here — just ask
                    JARVIS to “make an image of…” (needs Gemini access).
                  </div>
                </div>
              </Card>
            </>)}

            {current.id === "keys" && (<>
              <Note>
                Stored locally in app data, outside the project folder, and never shown again after saving.
                Leave a field blank to keep its key. Free-tier limits are per key, so paste several (one per
                line) and JARVIS rotates through them instead of stopping at a rate limit.
              </Note>
              {LLM_PROVIDERS.some((p) => p.tag === "paid" && sysInfo[p.has]) && model === "auto" && (
                <Note tone="warn">
                  A paid provider is connected and the model is Auto, so smart routing may send
                  requests to it — each one is billed to that account. Pick a specific model in{" "}
                  <button className="settings-link" onClick={() => go("ai")}>Intelligence</button>, or remove the key, to keep it off.
                </Note>
              )}
              <div className="sx-group">
                Language models
                <span>{LLM_PROVIDERS.filter((p) => sysInfo[p.has]).length} of {LLM_PROVIDERS.length} connected</span>
              </div>
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
              <div className="sx-row-d">
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
              <Card title="Speech">
                <Row title="Text-to-speech" desc={ttsOpt?.desc}>
                  <select className="field" value={tts} onChange={e => setTts(e.target.value)} aria-label="Text-to-speech">
                    {TTS_OPTS.map(o => <option key={o.value} value={o.value}>{o.label}</option>)}
                  </select>
                </Row>
                {tts === "elevenlabs" && !sysInfo.has_elevenlabs_key && (
                  <div className="sx-card-body">
                    <Note tone="warn">
                      ElevenLabs needs an API key — add it in{" "}
                      <button className="settings-link" onClick={() => go("keys")}>API Keys</button>.
                    </Note>
                  </div>
                )}
                <Row title="Wake word" desc={<>Say it to start talking. You can also hold <kbd>Ctrl</kbd>+<kbd>Space</kbd> from any app.</>}>
                  <span className="sx-value">{sysInfo.wakeWord || "hey_jarvis"}</span>
                </Row>
              </Card>

              <Card title="Speaking up on its own" sub="Everything here can only speak — none of it can act.">
                <Row label title="Spoken system alerts"
                     desc="Speak up when the CPU or memory is maxed out or the battery is low. Agenda reminders and timers work either way.">
                  <Switch checked={sysAlerts} onChange={setSysAlerts} label="Spoken system alerts" />
                </Row>
                <Row label title="Heartbeat check-ins"
                     desc={<>Every so often JARVIS reads your checklist (<code>HEARTBEAT.md</code>) against the time,
                       agenda, battery, CPU and weather, and speaks only when something on it needs you.
                       {sysInfo.heartbeat?.enabled && !sysInfo.heartbeat?.has_items &&
                         " Your checklist is empty, so nothing runs yet."}</>}>
                  <Switch checked={hb.enabled} onChange={(on) => setHb({ ...hb, enabled: on })} label="Heartbeat check-ins" />
                </Row>
                <Row title="Check-in schedule" desc="Each check is one call from your model quota; an empty checklist costs nothing.">
                  <select className="field" value={hb.every_min} disabled={!hb.enabled} aria-label="How often"
                          onChange={(e) => setHb({ ...hb, every_min: Number(e.target.value) })}>
                    {[15, 30, 60, 120].map((m) => <option key={m} value={m}>Every {m} minutes</option>)}
                  </select>
                  <span className="sx-row-d sx-inline">from</span>
                  <input type="time" className="field" value={hb.start} disabled={!hb.enabled} aria-label="Start time"
                         onChange={(e) => setHb({ ...hb, start: e.target.value })} />
                  <span className="sx-row-d sx-inline">to</span>
                  <input type="time" className="field" value={hb.end} disabled={!hb.enabled} aria-label="End time"
                         onChange={(e) => setHb({ ...hb, end: e.target.value })} />
                </Row>
                <Row title="Checklist" desc="“Tell me 10 minutes before a meeting”, “say if the battery is low and unplugged”…">
                  <button className="btn btn--sm" onClick={() => onSave({ heartbeat_open: true })}>
                    <Icon name="edit" size={14} />Edit checklist
                  </button>
                </Row>
                <Row title="Nightly memory digest"
                     desc="After 3 am JARVIS reads what you said to it that day — only your own words — and saves up to five new facts to Memory, plus a short journal note. One model call a night."
                     extra={sysInfo.digest?.learned?.length > 0 && (
                       <div className="chips">
                         <span className="sx-row-d sx-inline">Lately learned</span>
                         {sysInfo.digest.learned.map((l) => (
                           <span key={l.text} className="chip chip--static"><span>{l.text}</span></span>
                         ))}
                       </div>
                     )}>
                  <Switch checked={digest.enabled} onChange={(on) => setDigest({ enabled: on })} label="Nightly memory digest" />
                </Row>
              </Card>
            </>)}

            {current.id === "autopilot" && (<>
              <Card title="How JARVIS works"
                    sub="Agent mode hands each request to a specialist, and lets them hand each other what they found.">
                <div className="sx-card-body">
                  <div className="opt-grid opt-grid--2" role="radiogroup" aria-label="How JARVIS works">
                    {AGENT_MODES.map((m) => (
                      <button key={m.id} type="button" role="radio" aria-checked={agentMode === m.on}
                              className={`opt${agentMode === m.on ? " is-on" : ""}`}
                              onClick={() => setAgentMode(m.on)}>
                        <span className="opt-hd"><Icon name={m.icon} size={16} /><b>{m.title}</b><span className="opt-radio" /></span>
                        <span className="opt-d">{m.desc}</span>
                      </button>
                    ))}
                  </div>
                  <Guide summary="What the agents do">
                    <span>
                      The <b>chat agent</b> answers and does one-step things (timers, music, email, lookups). The
                      <b> browser agent</b> does whole tasks on websites; the <b>desktop agent</b> does them in Windows
                      apps and your files. A request that needs both — “find the cheapest flight to Goa and write it in
                      Notepad” — gets a short plan, run in order: each agent gets what the last one found, and an agent
                      that needs something mid-task asks the other before asking you. A step that fails stops the plan.
                      Anything unclear goes to the chat agent, exactly as in simple mode. Typed and wake-word requests
                      only; the live voice session stays simple.
                    </span>
                  </Guide>
                </div>
              </Card>
              <Card title="Operator" sub="The model that silently drives multi-step browser and desktop tasks. It picks every click internally — you only hear the outcome.">
                <Row stack title="Operator model"
                     desc={<>Leave blank for automatic. Use <code>ollama:&lt;model&gt;</code> to run it fully offline.
                       {sysInfo.autopilot_model_active ? <> Currently <b>{sysInfo.autopilot_model_active}</b>.</> : null}</>}>
                  <input type="text" className="field field--mono" value={autopilotModel} aria-label="Operator model"
                         onChange={(e) => setAutopilotModel(e.target.value)} spellCheck={false}
                         placeholder='auto · a model id · "ollama:llama3.1"' />
                </Row>
                <Row title="Step reasoning"
                     desc={autopilotThinking === "dynamic"
                       ? "Thinks harder on tricky steps and less on obvious ones (recommended)."
                       : "Quickest, but measurably picks worse moves on hard pages."}>
                  <select className="field" value={autopilotThinking} aria-label="Step reasoning"
                          onChange={(e) => setAutopilotThinking(e.target.value)}>
                    <option value="dynamic">Dynamic</option>
                    <option value="off">Off — fastest</option>
                  </select>
                </Row>
                <Row label title="Look at screenshots when stuck"
                     desc="Let the page operator glance at a screenshot when the page's structure isn't enough.">
                  <Switch checked={autopilotVision} onChange={setAutopilotVision} label="Look at screenshots when stuck" />
                </Row>
              </Card>
            </>)}

            {current.id === "interface" && (<>
              <Card title="Floating status pill">
                <Row label title="Show the pill in other apps"
                     desc="A small pill above the taskbar shows whether JARVIS is listening, thinking or speaking, with a stop button. It never shows on the JARVIS window itself.">
                  <Switch checked={ovlEnabled} onChange={setOvlEnabled} label="Show the floating pill" />
                </Row>
                {ovlEnabled && (
                  <Row stack title="Hide it on these apps" desc={<>Use the app&apos;s process name, e.g. <code>chrome</code>, <code>code</code>, <code>discord</code>.</>}
                       extra={<AddField placeholder="App name, e.g. chrome" button="Hide on it" onAdd={addApp} label="App to hide the pill on" />}>
                    <ChipList items={ovlApps} onRemove={(a) => setOvlApps(ovlApps.filter((x) => x !== a))}
                              empty="Shown on every app." removeLabel="Show it again on" />
                  </Row>
                )}
              </Card>
              <Card title="Home screen" sub="Colours, background, density, and which panels show where."
                    actions={onOpenCustomize && (
                      <button className="btn btn--sm" onClick={onOpenCustomize}>
                        <Icon name="palette" size={14} />Open Customize
                      </button>
                    )} />
              <Card title="Keyboard shortcuts">
                <div className="keys-list">
                  {SHORTCUTS.map(([combo, what]) => (
                    <div key={what} className="keys-row">
                      <span>{what}</span>
                      <span>{combo.map((k) => <kbd key={k}>{k}</kbd>)}</span>
                    </div>
                  ))}
                </div>
              </Card>
            </>)}

            {current.id === "files" && (<>
              <Card title="Storage location"
                    sub={<>Everything JARVIS creates — images, QR codes, screenshots, recordings, PDFs — is saved here,
                      in <code>images/</code>, <code>recordings/</code> and <code>documents/</code>.</>}>
                <div className="sx-card-body">
                  <input type="text" className="field field--mono" value={storageDir} aria-label="Storage location"
                         onChange={(e) => setStorageDir(e.target.value)} placeholder="C:\Users\you\Jarvis" />
                </div>
              </Card>

              <Card title="Skill files"
                    status={sysInfo.skills_count
                      ? <span className="tag tag--plain tag--off">{sysInfo.skills_count} found</span> : null}
                    sub="Teach JARVIS a procedure by writing it down as a Markdown file in the skills/ folder."
                    actions={(
                      <button className="btn btn--sm" onClick={() => onSave({ skills_open: true })}>
                        <Icon name="folder" size={14} />Open skills folder
                      </button>
                    )}>
                <Guide summary="How skill files work">
                  <span>
                    Put a <code>name:</code> and <code>description:</code> at the top (OpenClaw-style <code>SKILL.md</code>
                    folders work too). JARVIS sees only the names and descriptions, and reads a skill in full when a
                    request needs it, so long notes cost nothing until they&apos;re used.
                  </span>
                </Guide>
              </Card>

              <Card title="Read-only Terminal — allowed folders"
                    status={<span className="tag tag--ok tag--plain">Read-only</span>}
                    sub={<>JARVIS may <b>list</b> these folders and <b>read</b> files inside them — every file read asks
                      for your approval. It can never run, write, move, or delete anything.</>}>
                <div className="sx-card-body">
                  <ChipList items={dirs} onRemove={(d) => setDirs(dirs.filter((x) => x !== d))}
                            empty="No folders approved — JARVIS can't see any files yet." removeLabel="Disallow" />
                  <AddField placeholder="C:\Users\you\Documents" button="Allow" onAdd={addDir} label="Folder to allow" />
                </div>
              </Card>

              <Card title="My location"
                    status={pinnedLoc?.label
                      ? <span className="tag tag--ok">Pinned</span>
                      : <span className="tag tag--off">Automatic</span>}
                    sub="Used for weather, nearby places and directions. You can also say “set my location to …”.">
                <div className="sx-card-body">
                  {pinnedLoc?.label ? (
                    <div className="field-row">
                      <Note tone="ok">Pinned to <b>{pinnedLoc.label.split(",")[0]}</b>.</Note>
                      <button className="btn btn--sm btn--ghost" onClick={() => onSetLocation && onSetLocation({ clear: true })}>
                        Use automatic
                      </button>
                    </div>
                  ) : (
                    <div className="sx-row-d">
                      On automatic (browser GPS / IP). If weather or distances are off, pin your exact area — it
                      overrides auto-detection everywhere.
                    </div>
                  )}
                  <AddField placeholder="e.g. Powai, Mumbai" button={<><Icon name="pin" size={14} />Pin</>} mono={false}
                            onAdd={(p) => onSetLocation && onSetLocation({ place: p })} label="Place to pin" />
                </div>
              </Card>
            </>)}

            {current.id === "devices" && (
              <DevicesSection devicePairing={devicePairing} devices={devices} onCreatePairing={onCreatePairing}
                              onListDevices={onListDevices} onRevokeDevice={onRevokeDevice}
                              tvState={tvState} waState={waState} onTv={onTv} />
            )}
            {current.id === "connectors" && <ConnectorsSection connState={connState} onSend={onTv} />}
            {current.id === "mcp" && <McpSection mcpState={mcpState} onMcp={onTv} />}

            {current.id === "system" && (<>
              <Card title="Resources">
                <div className="kv">
                  <div className="kv-row"><span>RAM available</span><span>{sysInfo.ram != null ? `${sysInfo.ram} GB` : "—"}</span></div>
                  <div className="kv-row"><span>Active provider</span><span>{sysInfo.provider || "—"}</span></div>
                  <div className="kv-row"><span>Model providers connected</span><span>{llmConnected}</span></div>
                </div>
              </Card>
              <Card title="Runtime components">
                <Row title="Repair components"
                     desc="The browser engine, offline speech model and neural voice ship with JARVIS. If a feature says it “needs setup”, re-fetch what's missing — progress shows on the main screen.">
                  <button className="btn" onClick={() => { if (onRepairSetup) onRepairSetup(); onClose(); }}
                          disabled={!onRepairSetup}>
                    <Icon name="wrench" size={14} />Repair
                  </button>
                </Row>
              </Card>
            </>)}
          </div>

          <div className={`sx-foot${dirty ? " sx-foot--dirty" : ""}${closeArmed ? " sx-foot--nudge" : ""}`}>
            <span className="sx-state" role="status">
              {closeArmed ? "Unsaved changes — close again to discard them, or save"
                : dirty ? `Unsaved changes${pendingKeys ? ` · ${pendingKeys} new key${pendingKeys === 1 ? "" : "s"}` : ""}`
                : "All changes saved"}
            </span>
            <button className="btn btn--ghost" onClick={onClose}>{dirty ? "Discard" : "Close"}</button>
            <button className="btn btn--primary" onClick={handleSave} disabled={!dirty}>Save changes</button>
          </div>
        </div>
      </div>
    </div>
  );
}
