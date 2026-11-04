/* global Vision, importScripts */
// A classic worker is intentional: the MediaPipe WASM loader uses importScripts.
let recognizer = null;
const labels = {
  Open_Palm: "OPEN_PALM",
  Victory: "PEACE",
  Thumb_Up: "THUMBS_UP",
  Thumb_Down: "THUMBS_DOWN",
  Pointing_Up: "POINT_UP",
  ILoveYou: "LOVE",
  Closed_Fist: "FIST",
};
self.onmessage = async ({ data }) => {
  if (data.type === "init") {
    try {
      importScripts(
        new URL("gesture-assets/vision_bundle.js", self.location.href).href,
      );
      const files = await Vision.FilesetResolver.forVisionTasks(
        new URL("gesture-assets/wasm", self.location.href).href,
      );
      recognizer = await Vision.GestureRecognizer.createFromOptions(files, {
        baseOptions: {
          modelAssetPath: new URL(
            "gesture-assets/gesture_recognizer.task",
            self.location.href,
          ).href,
          delegate: "CPU",
        },
        runningMode: "VIDEO",
        numHands: 2,
        cannedGesturesClassifierOptions: { maxResults: 2, scoreThreshold: 0 },
        minHandDetectionConfidence: 0.65,
        minHandPresenceConfidence: 0.65,
        minTrackingConfidence: 0.65,
      });
      self.postMessage({ type: "ready" });
    } catch (error) {
      self.postMessage({
        type: "error",
        message:
          "Could not initialize the bundled gesture model: " +
          String(error.message || error),
      });
    }
    return;
  }
  if (data.type === "frame") {
    try {
      if (!recognizer) throw new Error("Gesture model is not ready.");
      const result = recognizer.recognizeForVideo(
        data.image,
        performance.now(),
      );
      const categories = [...(result.gestures[0] || [])].sort((a, b) => b.score - a.score);
      const category = categories[0];
      self.postMessage({
        type: "frame",
        frame: {
          seq: data.seq,
          observedAt: data.observedAt,
          pose: labels[category?.categoryName] || "UNKNOWN",
          score: category?.score || 0,
          runnerUpScore: categories[1]?.score || 0,
          handCount: result.landmarks.length,
          landmarks: (result.landmarks[0] || []).map((p) => [p.x, p.y, p.z]),
        },
      });
    } catch (error) {
      self.postMessage({
        type: "error",
        message: "Gesture inference failed: " + String(error.message || error),
      });
    } finally {
      data.image?.close();
    }
  }
};
