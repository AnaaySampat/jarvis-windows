import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { WebcamCamera } from "./webcam";
import { defaults } from "./engine";
const T = 1_800_000_000_000;
class FakeWorker {
  static instances: FakeWorker[] = [];
  onmessage: ((event: { data: Record<string, unknown> }) => void) | null = null;
  onerror: (() => void) | null = null;
  terminate = vi.fn();
  constructor() {
    FakeWorker.instances.push(this);
  }
  postMessage(data: {
    type: string;
    seq?: number;
    observedAt?: number;
    image?: ImageBitmap;
  }) {
    Promise.resolve().then(() => {
      if (data.type === "init") this.onmessage?.({ data: { type: "ready" } });
      else
        this.onmessage?.({
          data: {
            type: "frame",
            frame: {
              seq: data.seq,
              observedAt: data.observedAt,
              pose: "UNKNOWN",
              score: 1,
              landmarks: [],
            },
          },
        });
    });
  }
}
let camera: WebcamCamera,
  track: { stop: ReturnType<typeof vi.fn>; onended: (() => void) | null },
  stream: MediaStream,
  getUserMedia: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T);
  FakeWorker.instances = [];
  track = { stop: vi.fn(), onended: null };
  stream = {
    getTracks: () => [track],
    getVideoTracks: () => [track],
  } as unknown as MediaStream;
  getUserMedia = vi.fn(async () => stream);
  vi.stubGlobal("navigator", {
    mediaDevices: { getUserMedia, enumerateDevices: async () => [] },
  });
  const doc = new EventTarget() as EventTarget & {
    hidden: boolean;
    createElement: (name: string) => unknown;
  };
  doc.hidden = false;
  doc.createElement = () => ({
    muted: true,
    playsInline: true,
    srcObject: null,
    readyState: 2,
    videoWidth: 640,
    videoHeight: 480,
    get currentTime() {
      return Date.now() / 1000;
    },
    play: vi.fn(async () => {}),
    pause: vi.fn(),
  });
  const win = new EventTarget() as EventTarget & { location: { href: string } };
  win.location = { href: "http://localhost/" };
  vi.stubGlobal("document", doc);
  vi.stubGlobal("window", win);
  vi.stubGlobal("Worker", FakeWorker);
  vi.stubGlobal(
    "createImageBitmap",
    vi.fn(async () => ({ close: vi.fn() })),
  );
  camera = new WebcamCamera();
});
afterEach(async () => {
  await camera.stop();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});
async function start() {
  const result = await camera.start(defaults());
  await vi.advanceTimersByTimeAsync(150);
  return result;
}
describe("Windows webcam lifecycle", () => {
  it("emits epoch timestamps and closes tracks and worker on stop", async () => {
    expect((await start()).ok).toBe(true);
    const state = await camera.poll();
    expect(state.running).toBe(true);
    expect(state.frame?.observedAt).toBeGreaterThanOrEqual(T);
    await camera.stop();
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect(FakeWorker.instances[0].terminate).toHaveBeenCalledTimes(1);
  });
  it("stops late tracks when camera permission resolves after cancellation", async () => {
    let grant: (value: MediaStream) => void = () => {};
    getUserMedia.mockImplementation(
      () =>
        new Promise<MediaStream>((resolve) => {
          grant = resolve;
        }),
    );
    const pending = camera.start(defaults());
    await vi.advanceTimersByTimeAsync(0);
    await camera.stop();
    grant(stream);
    expect((await pending).ok).toBe(false);
    expect(track.stop).toHaveBeenCalledTimes(1);
    expect((await camera.poll()).running).toBe(false);
  });
  it("cancels a pending photo countdown when stopped", async () => {
    await start();
    const result = expect(camera.photo()).rejects.toThrow("camera stopped");
    await camera.stop();
    await result;
  });
  it("stops camera use when the window is hidden", async () => {
    await start();
    Object.defineProperty(document, "hidden", { value: true, writable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect((await camera.poll()).running).toBe(false);
    expect(track.stop).toHaveBeenCalledTimes(1);
  });
  it("expires a five-minute session without a user response", async () => {
    await start();
    await vi.advanceTimersByTimeAsync(300000);
    expect((await camera.poll()).running).toBe(false);
    expect(track.stop).toHaveBeenCalledTimes(1);
  });
  it("stops on pagehide even before document.hidden changes", async () => {
    await start();
    window.dispatchEvent(new Event("pagehide"));
    expect((await camera.poll()).running).toBe(false);
    expect(track.stop).toHaveBeenCalledTimes(1);
  });
});
