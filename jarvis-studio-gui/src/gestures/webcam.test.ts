import { beforeEach, afterEach, describe, expect, it, vi } from "vitest";
import { WebcamCamera } from "./webcam";
import { defaults } from "./engine";
const T = 1_800_000_000_000;
class FakeWorker {
  static instances: FakeWorker[] = [];
  static autoFrames = true;
  frames: { seq?: number; observedAt?: number; image?: ImageBitmap }[] = [];
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
    if (data.type === "frame") this.frames.push(data);
    Promise.resolve().then(() => {
      if (data.type === "init") this.onmessage?.({ data: { type: "ready" } });
      else if (FakeWorker.autoFrames)
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
  video: HTMLVideoElement,
  getUserMedia: ReturnType<typeof vi.fn>;
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T);
  FakeWorker.instances = [];
  FakeWorker.autoFrames = true;
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
  video = {
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
  } as unknown as HTMLVideoElement;
  doc.createElement = () => video;
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
  it("processes up to fifteen fresh frames per second without an initial interval delay", async () => {
    await camera.start(defaults());
    await vi.advanceTimersByTimeAsync(0);
    expect((await camera.poll()).running).toBe(true);
    await vi.advanceTimersByTimeAsync(1000);
    expect(FakeWorker.instances[0].frames.length).toBeGreaterThanOrEqual(14);
    expect(FakeWorker.instances[0].frames.length).toBeLessThanOrEqual(16);
  });
  it("waits for inference and immediately resumes with the latest video frame", async () => {
    FakeWorker.autoFrames = false;
    await camera.start(defaults());
    await vi.advanceTimersByTimeAsync(500);
    const worker = FakeWorker.instances[0];
    expect(worker.frames).toHaveLength(1);
    expect(createImageBitmap).toHaveBeenCalledTimes(1);
    const first = worker.frames[0];
    worker.onmessage?.({
      data: {
        type: "frame",
        frame: {
          seq: first.seq,
          observedAt: first.observedAt,
          pose: "UNKNOWN",
          score: 1,
          landmarks: [],
        },
      },
    });
    await vi.advanceTimersByTimeAsync(1);
    expect(worker.frames).toHaveLength(2);
    expect(worker.frames[1].observedAt).toBeGreaterThanOrEqual(T + 500);
  });
  it("uses decoded video callbacks and cancels them on stop", async () => {
    let ready: VideoFrameRequestCallback = () => {};
    video.requestVideoFrameCallback = vi.fn((callback) => {
      ready = callback;
      return 42;
    });
    video.cancelVideoFrameCallback = vi.fn();
    await camera.start(defaults());
    await vi.advanceTimersByTimeAsync(100);
    expect(createImageBitmap).not.toHaveBeenCalled();
    ready(0, {} as VideoFrameCallbackMetadata);
    await vi.advanceTimersByTimeAsync(100);
    expect(createImageBitmap).toHaveBeenCalledTimes(1);
    await camera.stop();
    expect(video.cancelVideoFrameCallback).toHaveBeenCalledWith(42);
  });
  it("bounds oversized frames while preserving their aspect ratio", async () => {
    Object.defineProperty(video, "videoWidth", { value: 1920 });
    Object.defineProperty(video, "videoHeight", { value: 1080 });
    await start();
    expect(createImageBitmap).toHaveBeenCalledWith(video, {
      resizeWidth: 640,
      resizeHeight: 360,
      resizeQuality: "low",
    });
  });
  it("does not miss decoded frames that arrive at the requested fifteen fps cadence", async () => {
    let ready: VideoFrameRequestCallback = () => {};
    video.requestVideoFrameCallback = vi.fn((callback) => {
      ready = callback;
      return 1;
    });
    video.cancelVideoFrameCallback = vi.fn();
    await camera.start(defaults());
    for (let frame = 0; frame < 15; frame++) {
      await vi.advanceTimersByTimeAsync(67);
      const callback = ready;
      ready = () => {
        throw new Error("No callback scheduled before the decoded frame");
      };
      callback(performance.now(), {} as VideoFrameCallbackMetadata);
      await vi.advanceTimersByTimeAsync(0);
    }
    expect(FakeWorker.instances[0].frames).toHaveLength(15);
  });
  it("does not process duplicate video frames and stops if the feed stalls", async () => {
    Object.defineProperty(video, "currentTime", { value: 1 });
    await start();
    await vi.advanceTimersByTimeAsync(1000);
    expect(createImageBitmap).toHaveBeenCalledTimes(1);
    await vi.advanceTimersByTimeAsync(10000);
    expect((await camera.poll()).running).toBe(false);
    expect((await camera.poll()).summary).toContain("No gesture frames");
    expect(track.stop).toHaveBeenCalledTimes(1);
  });
  it("closes a bitmap created after stop without posting it to an old worker", async () => {
    let resolve: (image: ImageBitmap) => void = () => {};
    const image = { close: vi.fn() } as unknown as ImageBitmap;
    vi.mocked(createImageBitmap).mockImplementationOnce(
      () =>
        new Promise((done) => {
          resolve = done;
        }),
    );
    await camera.start(defaults());
    await vi.advanceTimersByTimeAsync(0);
    await camera.stop();
    resolve(image);
    await vi.advanceTimersByTimeAsync(0);
    expect(image.close).toHaveBeenCalledTimes(1);
    expect(FakeWorker.instances[0].frames).toHaveLength(0);
  });
  it("closes a bitmap if transfer to the worker fails", async () => {
    await camera.start(defaults());
    const worker = FakeWorker.instances[0];
    vi.spyOn(worker, "postMessage").mockImplementation(() => {
      throw new Error("Worker gone");
    });
    await vi.advanceTimersByTimeAsync(0);
    const bitmap = await vi.mocked(createImageBitmap).mock.results[0].value;
    expect(bitmap.close).toHaveBeenCalledTimes(1);
    expect((await camera.poll()).running).toBe(false);
    expect(worker.terminate).toHaveBeenCalledTimes(1);
  });
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
  it("cancels webcam startup when hidden while the model is loading", async () => {
    vi.spyOn(FakeWorker.prototype, "postMessage").mockImplementationOnce(
      () => {},
    );
    const pending = camera.start(defaults());
    Object.defineProperty(document, "hidden", { value: true, writable: true });
    document.dispatchEvent(new Event("visibilitychange"));
    expect((await pending).ok).toBe(false);
    expect(getUserMedia).not.toHaveBeenCalled();
    expect(FakeWorker.instances[0].terminate).toHaveBeenCalledTimes(1);
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
