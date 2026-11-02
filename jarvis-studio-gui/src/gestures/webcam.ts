import type { GestureConfig, Frame } from "./engine";

const FRAME_INTERVAL_MS = 1000 / 15;
type CameraState = {
  running: boolean;
  starting: boolean;
  foreground: boolean;
  summary: string;
  frame: Frame | null;
};
/** Windows uses its own webcam/WASM worker. No camera data reaches the backend or
 * paired phone. The worker receives one transferable bitmap at a time. */
export class WebcamCamera {
  devices: MediaDeviceInfo[] = [];
  private worker: Worker | null = null;
  private stream: MediaStream | null = null;
  private video: HTMLVideoElement | null = null;
  private frameTimer: ReturnType<typeof setTimeout> | undefined;
  private frameTimeout: ReturnType<typeof setTimeout> | undefined;
  private frameCallback: number | undefined;
  private nextCaptureAt = 0;
  private expiry: ReturnType<typeof setTimeout> | undefined;
  private photoTimer: ReturnType<typeof setTimeout> | undefined;
  private photoReject: ((e: Error) => void) | null = null;
  private epoch = 0;
  private sequence = 0;
  private inFlight = false;
  private lastVideoTime = -1;
  private state: CameraState = {
    running: false,
    starting: false,
    foreground: true,
    summary: "Webcam is off.",
    frame: null,
  };
  private startReject: ((e: Error) => void) | null = null;
  private stopHidden = () => {
    if (document.hidden)
      void this.stop("Webcam stopped because JARVIS was minimized or hidden.");
  };
  private stopPage = () => {
    void this.stop(
      "Webcam stopped because JARVIS was closed or navigated away.",
    );
  };
  constructor() {
    if (typeof navigator !== "undefined") void this.refreshDevices();
  }
  async refreshDevices() {
    try {
      this.devices = (await navigator.mediaDevices.enumerateDevices()).filter(
        (d) => d.kind === "videoinput",
      );
    } catch {
      this.devices = [];
    }
  }
  poll = async () => this.state;
  preview = (element: HTMLVideoElement) => {
    element.srcObject = this.stream;
    void element.play().catch(() => {});
  };
  async start(
    config: GestureConfig,
  ): Promise<{ ok: boolean; summary: string }> {
    if (this.state.running || this.state.starting)
      return { ok: true, summary: "Webcam is already starting or running." };
    const epoch = ++this.epoch;
    this.state = {
      running: false,
      starting: true,
      foreground: true,
      summary: "Loading the local gesture model...",
      frame: null,
    };
    document.addEventListener("visibilitychange", this.stopHidden);
    window.addEventListener("pagehide", this.stopPage);
    try {
      if (document.hidden)
        throw new Error("Keep JARVIS visible to start the webcam.");
      if (!navigator.mediaDevices?.getUserMedia)
        throw new Error("This WebView does not expose camera access.");
      const worker = new Worker(
        new URL(
          (import.meta.env.BASE_URL || "/") + "gesture-worker.js",
          window.location.href,
        ),
      );
      this.worker = worker;
      await new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(
          () => reject(new Error("Gesture model startup timed out.")),
          25000,
        );
        this.startReject = (error) => {
          clearTimeout(timeout);
          reject(error);
        };
        worker.onerror = () => {
          clearTimeout(timeout);
          reject(new Error("The local gesture worker could not load."));
        };
        worker.onmessage = ({ data }) => {
          if (epoch !== this.epoch) {
            clearTimeout(timeout);
            reject(new Error("Webcam startup cancelled."));
            return;
          }
          if (data.type === "ready") {
            clearTimeout(timeout);
            this.startReject = null;
            resolve();
          } else if (data.type === "error") {
            clearTimeout(timeout);
            reject(new Error(data.message));
          }
        };
        worker.postMessage({ type: "init" });
      });
      if (epoch !== this.epoch) throw new Error("Webcam startup cancelled.");
      this.state = {
        ...this.state,
        summary: "Allow access to the selected webcam.",
      };
      const stream = await navigator.mediaDevices.getUserMedia({
        video: {
          ...(config.cameraId ? { deviceId: { exact: config.cameraId } } : {}),
          width: { ideal: 640 },
          height: { ideal: 480 },
          frameRate: { ideal: 15, max: 30 },
        },
        audio: false,
      });
      if (epoch !== this.epoch) {
        stream.getTracks().forEach((t) => t.stop());
        throw new Error("Webcam startup cancelled.");
      }
      this.stream = stream;
      const video = document.createElement("video");
      video.muted = true;
      video.playsInline = true;
      video.srcObject = stream;
      this.video = video;
      await video.play();
      if (epoch !== this.epoch) throw new Error("Webcam startup cancelled.");
      worker.onmessage = ({ data }) => {
        if (epoch !== this.epoch) return;
        if (data.type === "frame") {
          this.inFlight = false;
          this.state = {
            running: true,
            starting: false,
            foreground: !document.hidden,
            summary: this.photoReject
              ? this.state.summary
              : "Webcam on. Lower your hand, then hold a gesture.",
            frame: data.frame,
          };
          this.watchFrames(epoch);
          this.scheduleFrame(epoch);
        } else if (data.type === "error") void this.stop(String(data.message));
      };
      worker.onerror = () => {
        if (epoch === this.epoch)
          void this.stop(
            "Gesture worker stopped unexpectedly. Restart the camera.",
          );
      };
      this.inFlight = false;
      this.lastVideoTime = -1;
      this.nextCaptureAt = 0;
      this.watchFrames(epoch);
      this.scheduleFrame(epoch);
      this.expiry = setTimeout(
        () => void this.stop("Five-minute webcam session ended."),
        300000,
      );
      stream.getVideoTracks().forEach((track) => {
        track.onended = () => {
          if (epoch === this.epoch)
            void this.stop(
              "The webcam disconnected or its permission was revoked.",
            );
        };
      });
      await this.refreshDevices();
      return { ok: true, summary: "Webcam started. Recognition runs locally." };
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (epoch === this.epoch) await this.stop(message);
      return { ok: false, summary: message };
    }
  }
  private watchFrames(epoch: number) {
    clearTimeout(this.frameTimeout);
    this.frameTimeout = setTimeout(() => {
      if (epoch === this.epoch)
        void this.stop(
          "No gesture frames arrived. Restart the webcam to try again.",
        );
    }, 10000);
  }
  private scheduleFrame(epoch: number) {
    if (epoch !== this.epoch || !this.video || this.inFlight) return;
    const video = this.video;
    if (typeof video.requestVideoFrameCallback === "function") {
      // Register before the next decoded frame. Waiting for the rate-limit
      // timer first can miss that frame and halve throughput on a 15 fps feed.
      this.frameCallback = video.requestVideoFrameCallback(() => {
        if (epoch !== this.epoch) return;
        this.frameCallback = undefined;
        if (performance.now() + 2 < this.nextCaptureAt)
          this.scheduleFrame(epoch);
        else void this.capture(epoch);
      });
      return;
    }
    this.frameTimer = setTimeout(
      () => {
        if (epoch !== this.epoch) return;
        void this.capture(epoch);
      },
      Math.max(0, this.nextCaptureAt - performance.now()),
    );
  }
  private async capture(epoch: number) {
    const video = this.video;
    if (epoch !== this.epoch || !video || !this.worker || this.inFlight) return;
    this.nextCaptureAt = performance.now() + FRAME_INTERVAL_MS;
    if (video.readyState < 2 || video.currentTime === this.lastVideoTime) {
      this.scheduleFrame(epoch);
      return;
    }
    this.lastVideoTime = video.currentTime;
    this.inFlight = true;
    const observedAt = Date.now(); // Transport timestamps are epoch ms, not performance.now().
    let image: ImageBitmap | null = null;
    try {
      // Some cameras ignore ideal dimensions. Bound transfer/inference cost
      // while keeping the original stream available for explicit photos.
      const scale = Math.min(
        1,
        640 / Math.max(video.videoWidth, video.videoHeight),
      );
      image = await createImageBitmap(
        video,
        scale < 1
          ? {
              resizeWidth: Math.max(1, Math.round(video.videoWidth * scale)),
              resizeHeight: Math.max(1, Math.round(video.videoHeight * scale)),
              resizeQuality: "low",
            }
          : {},
      );
      if (epoch !== this.epoch || !this.worker) {
        image.close();
        return;
      }
      this.worker.postMessage(
        { type: "frame", image, observedAt, seq: ++this.sequence },
        [image],
      );
      image = null; // Ownership passed to the worker.
    } catch {
      image?.close();
      if (epoch === this.epoch)
        await this.stop("Could not read webcam frames.");
    }
  }
  async stop(summary = "Webcam is off.") {
    this.epoch++;
    this.startReject?.(new Error("Webcam startup cancelled."));
    this.startReject = null;
    clearTimeout(this.frameTimer);
    clearTimeout(this.frameTimeout);
    if (this.frameCallback !== undefined)
      this.video?.cancelVideoFrameCallback(this.frameCallback);
    this.frameCallback = undefined;
    clearTimeout(this.expiry);
    clearTimeout(this.photoTimer);
    this.photoReject?.(
      new Error("Photo cancelled because the camera stopped."),
    );
    this.photoReject = null;
    this.worker?.terminate();
    this.worker = null;
    this.stream?.getTracks().forEach((track) => {
      track.onended = null;
      track.stop();
    });
    this.stream = null;
    if (this.video) {
      this.video.pause();
      this.video.srcObject = null;
      this.video = null;
    }
    this.state = {
      running: false,
      starting: false,
      foreground: true,
      summary,
      frame: null,
    };
    this.inFlight = false;
    document.removeEventListener("visibilitychange", this.stopHidden);
    window.removeEventListener("pagehide", this.stopPage);
    return { ok: true, summary };
  }
  async photo(): Promise<{ ok: boolean; summary: string }> {
    if (!this.state.running || !this.video)
      throw new Error("Start the webcam before taking a photo.");
    if (this.photoReject)
      throw new Error("A photo countdown is already running.");
    const epoch = this.epoch;
    for (let seconds = 3; seconds > 0; seconds--) {
      this.state = { ...this.state, summary: "Photo in " + seconds + "..." };
      await new Promise<void>((resolve, reject) => {
        this.photoReject = reject;
        this.photoTimer = setTimeout(() => {
          this.photoReject = null;
          resolve();
        }, 1000);
      });
      if (epoch !== this.epoch) throw new Error("Photo cancelled.");
    }
    const video = this.video;
    if (!video || !video.videoWidth || document.hidden)
      throw new Error("Keep the camera and JARVIS visible for the photo.");
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    const context = canvas.getContext("2d");
    if (!context) throw new Error("Could not capture the photo.");
    context.drawImage(video, 0, 0);
    const blob = await new Promise<Blob | null>((resolve) =>
      canvas.toBlob(resolve, "image/jpeg", 0.92),
    );
    if (!blob || epoch !== this.epoch)
      throw new Error("Photo capture failed or was cancelled.");
    const url = URL.createObjectURL(blob),
      a = document.createElement("a");
    a.href = url;
    a.download = "jarvis-photo-" + Date.now() + ".jpg";
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    this.state = {
      ...this.state,
      summary: "Photo captured; save/download requested.",
    };
    return { ok: true, summary: this.state.summary };
  }
}
