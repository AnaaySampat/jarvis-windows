import { useEffect, useReducer, useRef, useState } from "react";
import { Icon } from "../hud/icons";
import { cameraRequestReducer, captureFrame, EMPTY_CAMERA_REQUEST } from "./cameraCapture";
import "./camera-ocr.css";

const LANGUAGES = [["en", "English"], ["hi", "Hindi"], ["mr", "Marathi"], ["fr", "French"],
  ["de", "German"], ["es", "Spanish"], ["it", "Italian"], ["pt", "Portuguese"],
  ["ar", "Arabic"], ["ja", "Japanese"], ["ko", "Korean"], ["zh", "Chinese"],
  ["ru", "Russian"], ["bn", "Bengali"], ["ta", "Tamil"], ["te", "Telugu"]];

export default function CameraOCR({ onClose, sendOcr, result, connected, onNotify }) {
  const video = useRef(null);
  const timer = useRef(null);
  const [restart, setRestart] = useState(0);
  const [ready, setReady] = useState(false);
  const [error, setError] = useState("");
  const [from, setFrom] = useState("auto");
  const [to, setTo] = useState("en");
  const [request, dispatch] = useReducer(cameraRequestReducer, EMPTY_CAMERA_REQUEST);
  const answer = request.result, captured = request.image, busy = request.pending;

  useEffect(() => {
    let disposed = false, stream;
    if (!navigator.mediaDevices?.getUserMedia) {
      Promise.resolve().then(() => {
        if (!disposed) setError("Camera access is unavailable in this window. Check Windows camera permissions.");
      });
      return () => { disposed = true; };
    }
    navigator.mediaDevices.getUserMedia({ video: { width: { ideal: 1280 }, facingMode: "environment" }, audio: false })
      .then((media) => {
        stream = media;
        if (disposed) { media.getTracks().forEach((track) => track.stop()); return; }
        if (video.current) video.current.srcObject = media;
      }).catch((err) => {
        if (!disposed) setError(err.name === "NotAllowedError"
          ? "Camera permission was denied. Allow this app in Windows Settings → Privacy & security → Camera."
          : err.name === "NotFoundError" ? "No camera was found. Connect a webcam and try again."
          : "The camera could not start. Close other apps using it and try again.");
      });
    return () => { disposed = true; stream?.getTracks().forEach((track) => track.stop()); };
  }, [restart]);

  useEffect(() => () => clearTimeout(timer.current), []);
  useEffect(() => {
    if (result) dispatch({ type: "result", result });
  }, [result]);
  useEffect(() => {
    if (!connected) dispatch({ type: "disconnected" });
  }, [connected]);
  useEffect(() => {
    if (!busy) clearTimeout(timer.current);
  }, [busy]);

  const capture = () => {
    try {
      setError("");
      const frame = captureFrame(video.current);
      const id = sendOcr(frame, from, to);
      if (id == null) throw new Error("JARVIS is disconnected. Reconnect and try again.");
      dispatch({ type: "start", id, image: frame });
      clearTimeout(timer.current);
      timer.current = setTimeout(() => dispatch({ type: "timeout", id }), 185000);
    } catch (err) { setError(err.message); }
  };
  const copy = async (text) => {
    try { await navigator.clipboard.writeText(text); onNotify?.("Copied to clipboard"); }
    catch { setError("Could not copy. Select the text and press Ctrl+C."); }
  };

  return (
    <div className="settings-overlay" onClick={onClose}>
      <section className="settings-panel camera-ocr" role="dialog" aria-modal="true" aria-label="Camera text & translation"
               onClick={(e) => e.stopPropagation()}>
        <header className="camera-ocr-head"><div><h2>Camera text & translation</h2>
          <p>Point at text, then capture a frame to read and translate it.</p></div>
          <button className="x-btn" onClick={onClose} aria-label="Close camera"><Icon name="close" size={16} /></button>
        </header>
        <div className="camera-ocr-body">
          <video ref={video} autoPlay playsInline muted onLoadedData={() => setReady(true)} aria-label="Camera preview" />
          <p className="camera-ocr-note">A captured image can go to your configured Gemini or Groq vision services, including a fallback when one fails. Frames are not saved.</p>
          <div className="camera-ocr-controls">
            <label>Read<select className="field" value={from} onChange={(e) => setFrom(e.target.value)} disabled={busy}>
              <option value="auto">Detect language</option>{LANGUAGES.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
            </select></label>
            <label>Translate to<select className="field" value={to} onChange={(e) => setTo(e.target.value)} disabled={busy}>
              {LANGUAGES.map(([id, name]) => <option key={id} value={id}>{name}</option>)}
            </select></label>
            <button className="btn btn--primary" onClick={capture} disabled={!ready || !connected || busy}>
              <Icon name="camera" size={15} />{busy ? "Reading…" : "Capture & translate"}
            </button>
            {!ready && <button className="btn" onClick={() => {
              clearTimeout(timer.current); dispatch({ type: "reset" }); setReady(false); setError(""); setRestart((n) => n + 1);
            }}>Retry camera</button>}
          </div>
          {(!connected || error || request.error || answer?.error) && <p className="camera-ocr-error" role="alert">
            {!connected ? "JARVIS is disconnected. Waiting to reconnect…" : error || request.error || answer.error}</p>}
          {busy && <p role="status">Reading the captured frame…</p>}
          {answer?.message && <p role="status">{answer.message}</p>}
          {answer && (answer.text || answer.translation) && <div className="camera-ocr-results">
            {captured && <img src={captured} alt="Captured text" />}
            {[["Recognized text", answer.text], ["Translation", answer.translation]].map(([label, text]) => text && (
              <section key={label}><div className="camera-ocr-result-head"><h3>{label}</h3>
                <button className="btn btn--sm" onClick={() => copy(text)}>Copy</button></div><pre>{text}</pre></section>
            ))}
          </div>}
        </div>
      </section>
    </div>
  );
}
