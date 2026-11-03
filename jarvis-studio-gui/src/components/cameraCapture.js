// Capture a bounded still frame. Camera access stays in the panel lifecycle.
export function captureFrame(video, createCanvas = () => document.createElement("canvas")) {
  const width = video?.videoWidth, height = video?.videoHeight;
  if (!width || !height) throw new Error("The camera is warming up. Try again in a moment.");
  const scale = Math.min(1, 1280 / Math.max(width, height));
  const canvas = createCanvas();
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const context = canvas.getContext("2d");
  if (!context) throw new Error("This window could not capture a camera image.");
  context.drawImage(video, 0, 0, canvas.width, canvas.height);
  return canvas.toDataURL("image/jpeg", 0.9);
}

export const EMPTY_CAMERA_REQUEST = { id: null, image: "", result: null, pending: false, error: "" };

// A timed-out/closed request can finish after a later capture. Keep the matched
// result, rather than letting the WebSocket's most recent event replace it.
export function cameraRequestReducer(state, action) {
  switch (action.type) {
    case "start":
      return { id: action.id, image: action.image, result: null, pending: true, error: "" };
    case "result":
      return state.id != null && action.result?.requestId === state.id
        ? { ...state, result: action.result, pending: false, error: "" } : state;
    case "timeout":
      return state.pending && action.id === state.id
        ? { ...state, pending: false, error: "Camera translation timed out. Please try again." } : state;
    case "disconnected":
      return state.pending
        ? { ...state, pending: false, error: "The connection closed before the result arrived. Capture again after reconnecting." } : state;
    case "reset":
      return EMPTY_CAMERA_REQUEST;
    default:
      return state;
  }
}
