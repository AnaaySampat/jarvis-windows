import { describe, expect, it } from "vitest";
import { agentTaskStatus, reduceSetupProgress, viewAcceptsEvent, boundAgentImages } from "./useWebSocket";

describe("auxiliary view memory", () => {
  it("does not retain full chat, memory or telemetry in auxiliary windows", () => {
    for (const view of ["overlay", "browser-panel", "control-overlay"]) {
      for (const event of ["history", "memory", "stream_delta", "telemetry"]) {
        expect(viewAcceptsEvent(view, event)).toBe(false);
      }
      expect(viewAcceptsEvent(view, "status")).toBe(true);
    }
    expect(viewAcceptsEvent("main", "history")).toBe(true);
  });

  it("bounds screenshots across tasks while keeping recent images and old step text", () => {
    const tasks = [
      { id: "old", items: [{ kind: "step", line: "Observed" }, { kind: "shot", image: "123456" }] },
      { id: "new", items: [{ kind: "shot", image: "abcdef" }] },
    ];
    const bounded = boundAgentImages(tasks, 8);
    expect(bounded[0].items).toEqual([{ kind: "step", line: "Observed" }]);
    expect(bounded[1]).toBe(tasks[1]);
    expect(tasks[0].items).toHaveLength(2);
  });
});

describe("agentTaskStatus", () => {
  it("preserves uncertainty instead of treating every non-success as failure", () => {
    for (const status of ["unverified", "partial", "blocked", "failed"]) {
      expect(agentTaskStatus({ ok: false, status })).toBe(status);
    }
    expect(agentTaskStatus({ ok: true, status: "succeeded" })).toBe("done");
    expect(agentTaskStatus({ ok: false, status: "unverified", stopped: true })).toBe("stopped");
  });

  it("supports terminal events from older backends", () => {
    expect(agentTaskStatus({ ok: true })).toBe("done");
    expect(agentTaskStatus({ ok: false })).toBe("failed");
  });
});

describe("reduceSetupProgress", () => {
  it("tracks each asset once, in arrival order, through to complete", () => {
    let s = reduceSetupProgress(null, { stage: "start", total: 2 });
    s = reduceSetupProgress(s, { stage: "downloading", asset: "whisper", label: "Whisper" });
    s = reduceSetupProgress(s, { stage: "downloading", asset: "piper" });
    s = reduceSetupProgress(s, { stage: "done", asset: "whisper", label: "Whisper" });
    s = reduceSetupProgress(s, { stage: "error", asset: "piper", error: "timeout" });
    expect(s.order).toEqual(["whisper", "piper"]);
    expect(s.items.whisper.status).toBe("done");
    expect(s.items.piper).toEqual({ label: "piper", status: "error", error: "timeout" });
    expect(reduceSetupProgress(s, { stage: "complete" }).complete).toBe(true);
  });

  it("adopts a snapshot wholesale (late-connecting HUD)", () => {
    const s = reduceSetupProgress({ total: 9, order: ["x"], items: {}, complete: false }, {
      stage: "snapshot", order: ["a"], items: { a: { status: "done" } }, complete: true,
    });
    expect(s).toEqual({ total: 1, order: ["a"], items: { a: { status: "done" } }, complete: true });
  });

  it("ignores junk and never mutates the previous state", () => {
    const prev = { total: 1, order: [], items: {}, complete: false };
    expect(reduceSetupProgress(prev, null)).toBe(prev);
    reduceSetupProgress(prev, { stage: "downloading", asset: "a" });
    expect(prev.order).toEqual([]);
  });
});
