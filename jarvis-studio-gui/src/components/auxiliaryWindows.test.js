import { afterEach, describe, expect, it, vi } from "vitest";
import { syncAuxiliaryWindow, voiceOverlayVisible } from "./auxiliaryWindows";

const native = vi.hoisted(() => ({ invoke: vi.fn(), show: vi.fn() }));
vi.mock("@tauri-apps/api/core", () => ({ invoke: native.invoke }));
vi.mock("@tauri-apps/api/webviewWindow", () => ({
  WebviewWindow: { getByLabel: async () => ({ show: native.show, emit: async () => {} }) },
}));
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

describe("auxiliary window lifecycle", () => {
  it("finishes a delayed create before destroying the now-hidden window", async () => {
    vi.stubGlobal("window", { __TAURI_INTERNALS__: {} });
    let release;
    native.invoke.mockImplementationOnce(() => new Promise(resolve => { release = resolve; }))
      .mockResolvedValue(undefined);
    const open = syncAuxiliaryWindow("browser-panel", true);
    await vi.waitFor(() => expect(native.invoke).toHaveBeenCalledTimes(1));
    const close = syncAuxiliaryWindow("browser-panel", false);
    expect(native.invoke).toHaveBeenCalledTimes(1);
    release();
    await Promise.all([open, close]);
    expect(native.show).toHaveBeenCalledTimes(1);
    expect(native.invoke).toHaveBeenLastCalledWith("set_aux_window", { label: "browser-panel", active: false });
  });

  it("creates the permission overlay even when Jarvis itself is in front", () => {
    const app = { app: "jarvis-studio-gui.exe", on_jarvis: true };
    expect(voiceOverlayVisible(app, { enabled: true }, { id: "approval" })).toBe(true);
    expect(voiceOverlayVisible(app, { enabled: false }, { id: "approval" })).toBe(false);
    expect(voiceOverlayVisible(app, { enabled: true }, null)).toBe(false);
  });

  it("matches configured app names exactly", () => {
    const config = { enabled: true, mode: "only", apps: ["chrome"] };
    expect(voiceOverlayVisible({ app: "chrome.exe" }, config)).toBe(true);
    expect(voiceOverlayVisible({ app: "chromium.exe" }, config)).toBe(false);
  });
});
