/* Reactor.jsx — the centre of the Iron Man style (Customize ▸ Style): the helmet
   inside counter-rotating reactor rings, from the original HUD. Same props as Orb,
   so the home screen swaps one for the other. The rings are drawn on a canvas in a
   fixed 522-unit space and scaled by CSS; their speed follows the status. */

import { useRef, useEffect } from "react";

const SIZE = 522;
const MOODS = {
  idle:      { speed: 0.40, energy: 0.34 },
  listening: { speed: 1.30, energy: 0.95 },
  thinking:  { speed: 0.95, energy: 0.75 },
  working:   { speed: 0.80, energy: 0.65 },
  speaking:  { speed: 1.70, energy: 1.00 },
};
const lerp = (a, b, t) => a + (b - a) * t;

function Rings({ status, rgb, layer }) {
  const cvs = useRef(null);
  const cur = useRef({ ...MOODS.idle });

  useEffect(() => {
    const canvas = cvs.current;
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = SIZE * dpr; canvas.height = SIZE * dpr;
    ctx.scale(dpr, dpr);
    const still = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    const [R0, G0, B0] = rgb;
    const rgba = (a) => `rgba(${R0},${G0},${B0},${a})`;
    const cx = SIZE / 2, cy = SIZE / 2, R = SIZE * 0.46;
    let raf, t0 = null;

    function frame(ts) {
      if (t0 == null) t0 = ts;
      const t = (ts - t0) / 1000;
      const tgt = MOODS[status] || MOODS.idle, c = cur.current;
      c.speed = lerp(c.speed, tgt.speed, 0.06);
      c.energy = lerp(c.energy, tgt.energy, 0.06);
      const pulse = 0.5 + 0.5 * Math.sin(t * (1.0 + c.energy * 1.6));
      ctx.clearRect(0, 0, SIZE, SIZE);

      if (layer === "back") {
        // outer dotted ring
        ctx.save(); ctx.translate(cx, cy); ctx.rotate(t * c.speed * 0.25);
        for (let i = 0; i < 90; i++) {
          const a = (i / 90) * Math.PI * 2, on = i % 3 !== 0;
          ctx.beginPath();
          ctx.arc(Math.cos(a) * R * 0.98, Math.sin(a) * R * 0.98, on ? 2.8 : 3.6, 0, Math.PI * 2);
          ctx.fillStyle = rgba(on ? 0.22 : 0.5); ctx.fill();
        }
        ctx.restore();
        // tick ring, clockwise
        ctx.save(); ctx.translate(cx, cy); ctx.rotate(t * c.speed * 0.5);
        for (let i = 0; i < 60; i++) {
          const a = (i / 60) * Math.PI * 2, long = i % 5 === 0;
          const r0 = R * (long ? 0.78 : 0.83), r1 = R * 0.90;
          ctx.beginPath();
          ctx.moveTo(Math.cos(a) * r0, Math.sin(a) * r0);
          ctx.lineTo(Math.cos(a) * r1, Math.sin(a) * r1);
          ctx.lineWidth = long ? 3.6 : 2.7;
          ctx.strokeStyle = rgba(long ? 0.55 : 0.22); ctx.stroke();
        }
        ctx.restore();
        // breathing rim
        ctx.beginPath(); ctx.arc(cx, cy, R * (0.9 + 0.012 * pulse), 0, Math.PI * 2);
        ctx.lineWidth = 3.2; ctx.strokeStyle = rgba(0.3 + 0.2 * pulse); ctx.stroke();
      } else {
        // segmented arcs, counter-clockwise
        ctx.save(); ctx.translate(cx, cy); ctx.rotate(-t * c.speed * 0.7);
        for (let i = 0; i < 7; i++) {
          const a0 = (i / 7) * Math.PI * 2 + 0.18, a1 = a0 + (Math.PI * 2 / 7) * 0.52;
          ctx.beginPath(); ctx.arc(0, 0, R * 0.72, a0, a1);
          ctx.lineWidth = 4.2; ctx.strokeStyle = rgba(0.6); ctx.stroke();
        }
        ctx.restore();
        // inner gauge arcs, clockwise
        ctx.save(); ctx.translate(cx, cy); ctx.rotate(t * c.speed * 0.9);
        ctx.beginPath(); ctx.arc(0, 0, R * 0.6, -0.4, 1.7);
        ctx.lineWidth = 5; ctx.strokeStyle = rgba(0.8); ctx.stroke();
        ctx.beginPath(); ctx.arc(0, 0, R * 0.6, Math.PI - 0.4, Math.PI + 1.2);
        ctx.lineWidth = 3.4; ctx.strokeStyle = rgba(0.4); ctx.stroke();
        ctx.restore();
      }
      if (!still) raf = requestAnimationFrame(frame);
    }
    const onVis = () => {
      cancelAnimationFrame(raf);
      if (!document.hidden) { t0 = null; raf = requestAnimationFrame(frame); }
    };
    document.addEventListener("visibilitychange", onVis);
    raf = requestAnimationFrame(frame);
    return () => { cancelAnimationFrame(raf); document.removeEventListener("visibilitychange", onVis); };
  }, [status, layer, rgb[0], rgb[1], rgb[2]]); // eslint-disable-line react-hooks/exhaustive-deps

  return <canvas ref={cvs} className={`reactor-rings reactor-rings--${layer}`} aria-hidden />;
}

/* A row of bars that moves with what JARVIS is doing (pure CSS, see home.css). */
export function Waveform({ status = "idle", bars = 36 }) {
  return (
    <div className={`im-wave im-wave--${status}`} aria-hidden="true">
      {Array.from({ length: bars }).map((_, i) => (
        <span key={i} style={{ "--h": Math.sin(((i + 0.5) / bars) * Math.PI).toFixed(3),
                               animationDelay: `${-((i * 137) % 900)}ms` }} />
      ))}
    </div>
  );
}

export default function Reactor({ status = "idle", label, onClick, rgb = [0, 229, 255] }) {
  return (
    <button type="button" className={`reactor reactor--${status}`} onClick={onClick}
            aria-label={label} title={label}>
      <span className="reactor-glow" aria-hidden />
      <Rings status={status} rgb={rgb} layer="back" />
      <img className="reactor-helmet" src="/assets/ironman-helmet-kept.png" alt="" draggable={false} />
      <Rings status={status} rgb={rgb} layer="front" />
    </button>
  );
}
