// The screen tint is a native, click-through surface; this module only renews
// its short lease. A stalled/disconnected webview cannot leave it on forever.
export function shouldShowControlTint(connected, control, tasks) {
  return !!connected && !!control?.armed && !control?.paused
    && (tasks || []).some((task) => task.kind === "computer" && task.status === "running");
}

const CONTROL_PRESENTATION = {
  active: { title: "JARVIS CONTROLLING", badge: "LIVE", banner: "JARVIS is controlling your mouse and keyboard", description: "Working…" },
  paused: { title: "CONTROL PAUSED", badge: "PAUSED", banner: "Computer control is paused", description: "Paused — JARVIS is holding." },
  waiting: { title: "WAITING FOR CONTROL", badge: "WAITING", banner: "Waiting for computer control", description: "Waiting for computer-control permission…" },
  armed: { title: "CONTROL ARMED", badge: "READY", banner: "Computer control is armed", description: "Ready — waiting for a desktop task." },
};

export function controlPresentation(armed, running, paused) {
  return CONTROL_PRESENTATION[!running ? "armed" : !armed ? "waiting" : paused ? "paused" : "active"];
}

// Serialize and coalesce IPC across effect cleanup/restart. A slow WebView must
// not accumulate old "show" renewals ahead of the latest "hide" request.
let pending = null;
let sending = false;
function send(invoke, active) {
  pending = { invoke, active };
  if (sending) return;
  sending = true;
  (async () => {
    while (pending) {
      const request = pending;
      pending = null;
      try { await request.invoke("set_control_tint", { active: request.active }); }
      catch (error) { console.warn("[ControlTint]", error); }
    }
    sending = false;
  })();
}

export function startControlTintLease(active, invoke, timers = globalThis) {
  let disposed = false;
  send(invoke, active);
  const timer = active ? timers.setInterval(() => send(invoke, true), 1000) : null;
  return () => {
    if (disposed) return;
    disposed = true;
    if (timer != null) timers.clearInterval(timer);
    send(invoke, false);
  };
}
