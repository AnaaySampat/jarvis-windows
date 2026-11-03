import { afterEach, describe, expect, it, vi } from "vitest";
import { controlPresentation, shouldShowControlTint, startControlTintLease } from "./controlTint";

const running = [{ kind: "computer", status: "running" }];
const flush = async () => { for (let i = 0; i < 12; i++) await Promise.resolve(); };

afterEach(() => vi.useRealTimers());

describe("desktop control screen tint", () => {
  it("describes an idle armed lease as ready, not active control", () => {
    const idle = controlPresentation(true, false, false);
    expect(idle.banner).toBe("Computer control is armed");
    expect(idle.badge).toBe("READY");
    expect(controlPresentation(true, true, false).badge).toBe("LIVE");
    expect(controlPresentation(true, true, true).badge).toBe("PAUSED");
    expect(controlPresentation(false, true, false).badge).toBe("WAITING");
    expect(controlPresentation(true, false, true).badge).toBe("READY");
  });
  it("requires a connected, armed and unpaused desktop task", () => {
    expect(shouldShowControlTint(true, { armed: true }, running)).toBe(true);
    expect(shouldShowControlTint(false, { armed: true }, running)).toBe(false);
    expect(shouldShowControlTint(true, { armed: false }, running)).toBe(false);
    expect(shouldShowControlTint(true, { armed: true, paused: true }, running)).toBe(false);
    expect(shouldShowControlTint(true, { armed: true }, [])).toBe(false);
    expect(shouldShowControlTint(true, { armed: true }, [{ kind: "computer", status: "done" }])).toBe(false);
    expect(shouldShowControlTint(true, { armed: true }, [{ kind: "browser", status: "running" }])).toBe(false);
  });

  it("renews while running and hides immediately on cleanup without further heartbeats", async () => {
    vi.useFakeTimers();
    const invoke = vi.fn().mockResolvedValue(undefined);
    const stop = startControlTintLease(true, invoke);
    await flush();
    vi.advanceTimersByTime(2000);
    await flush();
    expect(invoke.mock.calls.filter(([, args]) => args.active)).toHaveLength(3);
    stop(); stop();
    await flush();
    expect(invoke).toHaveBeenLastCalledWith("set_control_tint", { active: false });
    const count = invoke.mock.calls.length;
    vi.advanceTimersByTime(5000);
    await flush();
    expect(invoke).toHaveBeenCalledTimes(count);
  });

  it("keeps a delayed show before its later hide", async () => {
    vi.useFakeTimers();
    let release;
    const invoke = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }))
      .mockResolvedValue(undefined);
    const stop = startControlTintLease(true, invoke);
    await flush();
    stop();
    expect(invoke).toHaveBeenCalledTimes(1);
    release();
    await flush();
    expect(invoke).toHaveBeenLastCalledWith("set_control_tint", { active: false });
  });

  it("does not schedule heartbeats when hidden", async () => {
    vi.useFakeTimers();
    const invoke = vi.fn().mockResolvedValue(undefined);
    const stop = startControlTintLease(false, invoke);
    await flush();
    vi.advanceTimersByTime(5000);
    await flush();
    expect(invoke).toHaveBeenCalledTimes(1);
    expect(invoke).toHaveBeenCalledWith("set_control_tint", { active: false });
    stop();
    await flush();
  });

  it("coalesces old renewals so stop is next after an in-flight request", async () => {
    vi.useFakeTimers();
    let release;
    const invoke = vi.fn().mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }))
      .mockResolvedValue(undefined);
    const stop = startControlTintLease(true, invoke);
    await flush();
    vi.advanceTimersByTime(10000);
    stop();
    release();
    await flush();
    expect(invoke).toHaveBeenCalledTimes(2);
    expect(invoke).toHaveBeenLastCalledWith("set_control_tint", { active: false });
  });
});
