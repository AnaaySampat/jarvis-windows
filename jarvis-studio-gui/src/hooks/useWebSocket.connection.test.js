import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

// Exercise the actual socket callbacks without a browser or native WebView.
// Only React's storage/scheduling is substituted; events use the production hook.
const hooks = vi.hoisted(() => ({ states: [], refs: [], effects: [], stateIndex: 0, refIndex: 0 }));
vi.mock("react", () => ({
  useState(initial) {
    const index = hooks.stateIndex++;
    if (!(index in hooks.states)) hooks.states[index] = initial;
    return [hooks.states[index], next => {
      hooks.states[index] = typeof next === "function" ? next(hooks.states[index]) : next;
    }];
  },
  useRef(initial) {
    const index = hooks.refIndex++;
    if (!(index in hooks.refs)) hooks.refs[index] = { current: initial };
    return hooks.refs[index];
  },
  useEffect(effect) { hooks.effects.push(effect); },
  useCallback(callback) { return callback; },
}));
vi.mock("@tauri-apps/api/core", () => ({ invoke: vi.fn().mockResolvedValue("") }));
vi.mock("@tauri-apps/api/app", () => ({ getVersion: vi.fn().mockResolvedValue("test") }));

import { useWebSocket } from "./useWebSocket";

let sockets;
let cleanup;
function HookHarness() {
  return useWebSocket();
}
function render() {
  hooks.stateIndex = 0;
  hooks.refIndex = 0;
  hooks.effects = [];
  return HookHarness();
}
function event(socket, name, data) {
  socket.onmessage({ data: JSON.stringify({ event: name, data }) });
}

beforeEach(() => {
  hooks.states = [];
  hooks.refs = [];
  sockets = [];
  cleanup = undefined;
  vi.useFakeTimers();
  vi.stubGlobal("WebSocket", class {
    static OPEN = 1;
    readyState = 1;
    constructor() { sockets.push(this); }
    send() {}
    close() { this.readyState = 3; this.onclose?.(); }
  });
});

afterEach(() => {
  cleanup?.();
  vi.clearAllTimers();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

describe("socket loss during computer control", () => {
  it("clears live consent/activity, preserves completed history, and trusts only fresh reconnect events", async () => {
    render();
    cleanup = hooks.effects[0]();
    await vi.waitFor(() => expect(sockets).toHaveLength(1));
    expect(sockets).toHaveLength(1);
    const first = sockets[0];
    first.onopen();
    event(first, "agent_task", { id: "completed", kind: "computer", goal: "Earlier task" });
    event(first, "agent_task_end", { id: "completed", ok: true, summary: "Finished earlier" });
    event(first, "agent_task", { id: "active", kind: "computer", goal: "Current task" });
    event(first, "agent_step", { id: "active", line: "Observed Notepad" });
    event(first, "control_state", { armed: true, available: true, seconds_left: 60, paused: true });
    event(first, "permission_request", { id: "expired-request" });
    event(first, "clarify_request", { id: "expired-question" });
    event(first, "status", "thinking");
    const before = render();
    expect(before.isConnected).toBe(true);
    expect(before.controlState.armed).toBe(true);
    expect(before.agentTasks[1].status).toBe("running");

    first.close();
    const lost = render();
    expect(lost.isConnected).toBe(false);
    expect(lost.controlState).toEqual({ armed: false, available: false, seconds_left: 0, paused: false });
    expect(lost.status).toBe("idle");
    expect(lost.permissionRequest).toBeNull();
    expect(lost.clarifyRequest).toBeNull();
    expect(lost.agentTasks[0]).toBe(before.agentTasks[0]);
    expect(lost.agentTasks[1]).toMatchObject({ status: "unverified", summary: expect.stringContaining("Connection lost") });
    expect(lost.agentTasks[1].items).toBe(before.agentTasks[1].items);

    // A queued event on the closed socket cannot re-arm the UI or claim success.
    event(first, "control_state", { armed: true });
    event(first, "agent_task_end", { id: "active", ok: true });
    expect(render().controlState.armed).toBe(false);
    expect(render().agentTasks[1].status).toBe("unverified");

    vi.advanceTimersByTime(3000);
    expect(sockets).toHaveLength(2);
    const second = sockets[1];
    second.onopen();
    expect(render().isConnected).toBe(true);
    expect(render().controlState.armed).toBe(false);
    expect(render().agentTasks.some(task => task.status === "running")).toBe(false);
    event(second, "control_state", { armed: true, available: true, seconds_left: 20, paused: false });
    event(second, "agent_task", { id: "active", kind: "computer", goal: "Current task" });
    const replayed = render();
    expect(replayed.agentTasks).toHaveLength(2);
    expect(replayed.agentTasks[0]).toBe(before.agentTasks[0]);
    expect(replayed.agentTasks[1]).toMatchObject({ id: "active", status: "running", summary: "" });
    expect(replayed.agentTasks[1].items).toBe(before.agentTasks[1].items);
    event(second, "agent_task", { id: "fresh", kind: "computer", goal: "New task" });
    expect(render().controlState.armed).toBe(true);
    expect(render().agentTasks.at(-1)).toMatchObject({ id: "fresh", status: "running" });

    // A delayed duplicate close from the old connection must not clear new state.
    first.onclose();
    expect(render().isConnected).toBe(true);
    expect(render().controlState.armed).toBe(true);
  });
});
