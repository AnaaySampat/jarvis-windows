import { describe, expect, it } from "vitest";
import { reduceSetupProgress } from "./useWebSocket";

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
