import { describe, expect, it, vi } from "vitest";
import { cameraRequestReducer, captureFrame, EMPTY_CAMERA_REQUEST } from "./cameraCapture";

describe("camera frame capture", () => {
  it("does not capture a camera that has not delivered a frame", () => {
    const canvas = vi.fn();
    expect(() => captureFrame({ videoWidth: 0, videoHeight: 0 }, canvas)).toThrow("warming up");
    expect(canvas).not.toHaveBeenCalled();
  });

  it("bounds a large portrait frame while preserving its proportions", () => {
    const drawImage = vi.fn(), toDataURL = vi.fn(() => "data:image/jpeg;base64,frame");
    const canvas = { getContext: () => ({ drawImage }), toDataURL };
    const video = { videoWidth: 2160, videoHeight: 3840 };
    expect(captureFrame(video, () => canvas)).toBe("data:image/jpeg;base64,frame");
    expect([canvas.width, canvas.height]).toEqual([720, 1280]);
    expect(drawImage).toHaveBeenCalledWith(video, 0, 0, 720, 1280);
    expect(toDataURL).toHaveBeenCalledWith("image/jpeg", 0.9);
  });

  it("keeps small frames at their native size and reports unavailable canvas", () => {
    const canvas = { getContext: () => null };
    expect(() => captureFrame({ videoWidth: 640, videoHeight: 480 }, () => canvas)).toThrow("could not capture");
    expect([canvas.width, canvas.height]).toEqual([640, 480]);
  });
});

describe("camera request lifecycle", () => {
  it("keeps a new result when an earlier capture replies late", () => {
    const first = cameraRequestReducer(EMPTY_CAMERA_REQUEST, { type: "start", id: 1, image: "first" });
    const second = cameraRequestReducer(first, { type: "start", id: 2, image: "second" });
    const done = cameraRequestReducer(second, { type: "result", result: { requestId: 2, text: "New" } });
    expect(cameraRequestReducer(done, { type: "result", result: { requestId: 1, text: "Old" } })).toBe(done);
    expect(done.result.text).toBe("New");
    expect(done.image).toBe("second");
  });

  it("ignores old timers and clears a timeout when its matching result arrives", () => {
    const pending = cameraRequestReducer(EMPTY_CAMERA_REQUEST, { type: "start", id: 2, image: "frame" });
    expect(cameraRequestReducer(pending, { type: "timeout", id: 1 })).toBe(pending);
    const timedOut = cameraRequestReducer(pending, { type: "timeout", id: 2 });
    expect(timedOut.pending).toBe(false);
    expect(timedOut.error).toContain("timed out");
    const late = cameraRequestReducer(timedOut, { type: "result", result: { requestId: 2, text: "Hello" } });
    expect(late.error).toBe("");
    expect(late.result.text).toBe("Hello");
  });

  it("makes disconnect recoverable and rejects responses after a reset", () => {
    const pending = cameraRequestReducer(EMPTY_CAMERA_REQUEST, { type: "start", id: 3, image: "frame" });
    const disconnected = cameraRequestReducer(pending, { type: "disconnected" });
    expect(disconnected.pending).toBe(false);
    expect(disconnected.error).toContain("connection closed");
    const reset = cameraRequestReducer(disconnected, { type: "reset" });
    expect(cameraRequestReducer(reset, { type: "result", result: { requestId: 3, text: "Old" } })).toBe(reset);
  });
});
