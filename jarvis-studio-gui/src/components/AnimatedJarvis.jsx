import { useRef, useEffect } from "react";

/**
 * AnimatedJarvis — a JARVIS / arc-reactor style HUD core.
 *
 * Pure canvas: a glowing tri-core surrounded by counter-rotating tick rings,
 * radial spokes and a sweeping radar arc, all wrapped in a breathing cyan glow.
 * Every parameter eases between per-state "moods", so the core feels alive
 * whether it's idle, listening, thinking or speaking.
 *
 */

// Per-state mood: [r,g,b] accent, rotation speed, glow energy, sweep speed.
const MOODS = {
  idle:      { col: [0, 200, 255],  speed: 0.45, energy: 0.34, sweep: 0.5,  breathe: 0.05 },
  listening: { col: [0, 229, 255],  speed: 1.4,  energy: 0.95, sweep: 1.6,  breathe: 0.10 },
  thinking:  { col: [120, 180, 255],speed: 1.0,  energy: 0.75, sweep: 2.4,  breathe: 0.08 },
  speaking:  { col: [255, 180, 70], speed: 1.8,  energy: 1.0,  sweep: 1.0,  breathe: 0.13 },
};

function lerp(a, b, t) { return a + (b - a) * t; }
function lerpCol(c1, c2, t) {
  return [Math.round(lerp(c1[0], c2[0], t)),
          Math.round(lerp(c1[1], c2[1], t)),
          Math.round(lerp(c1[2], c2[2], t))];
}

export default function AnimatedJarvis({ status = "idle", size = 120 }) {
  const cvs = useRef(null);
  const raf = useRef(null);
  const cur = useRef({ col: [...MOODS.idle.col], speed: 0.45, energy: 0.34,
                       sweep: 0.5, breathe: 0.05 });

  useEffect(() => {
    const canvas = cvs.current;
    if (!canvas) return;
    const ctx = canvas.getContext("2d");
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    ctx.scale(dpr, dpr);

    let t0 = null;

    function frame(ts) {
      if (t0 == null) t0 = ts;
      const t = (ts - t0) / 1000;
      const target = MOODS[status] ?? MOODS.idle;
      const c = cur.current;
      const k = 0.07;
      c.speed   = lerp(c.speed,   target.speed,   k);
      c.energy  = lerp(c.energy,  target.energy,  k);
      c.sweep   = lerp(c.sweep,   target.sweep,   k);
      c.breathe = lerp(c.breathe, target.breathe, k);
      c.col     = lerpCol(c.col, target.col, k);

      const w = size, h = size;
      const cx = w / 2, cy = h / 2;
      const R = w * 0.40;
      const [r, g, b] = c.col;
      const rgb = (a) => `rgba(${r},${g},${b},${a})`;
      const pulse = 0.5 + 0.5 * Math.sin(t * (1.2 + c.energy * 2));

      ctx.clearRect(0, 0, w, h);

      // ── Outer breathing glow ──────────────────────────────────────────────
      const glowR = R * (1.35 + c.breathe * 5 * pulse);
      const og = ctx.createRadialGradient(cx, cy, R * 0.4, cx, cy, glowR);
      og.addColorStop(0, rgb(0.30 * c.energy));
      og.addColorStop(0.55, rgb(0.08 * c.energy));
      og.addColorStop(1, "rgba(0,0,0,0)");
      ctx.fillStyle = og;
      ctx.fillRect(0, 0, w, h);

      // ── Dark glass disc ───────────────────────────────────────────────────
      const disc = ctx.createRadialGradient(cx, cy - R * 0.2, 0, cx, cy, R);
      disc.addColorStop(0, "rgba(10,22,34,0.96)");
      disc.addColorStop(1, "rgba(2,6,12,0.99)");
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.fillStyle = disc;
      ctx.fill();

      // ── Tick ring (rotates clockwise) ─────────────────────────────────────
      const ticks = 48;
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(t * c.speed * 0.5);
      for (let i = 0; i < ticks; i++) {
        const ang = (i / ticks) * Math.PI * 2;
        const long = i % 4 === 0;
        const r0 = R * (long ? 0.74 : 0.80);
        const r1 = R * 0.88;
        ctx.beginPath();
        ctx.moveTo(Math.cos(ang) * r0, Math.sin(ang) * r0);
        ctx.lineTo(Math.cos(ang) * r1, Math.sin(ang) * r1);
        ctx.lineWidth = long ? 1.6 : 0.8;
        ctx.strokeStyle = rgb(long ? 0.55 : 0.25);
        ctx.stroke();
      }
      ctx.restore();

      // ── Segmented arc ring (rotates counter-clockwise) ────────────────────
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(-t * c.speed * 0.8);
      const segs = 3;
      for (let i = 0; i < segs; i++) {
        const a0 = (i / segs) * Math.PI * 2 + 0.25;
        const a1 = a0 + (Math.PI * 2 / segs) * 0.6;
        ctx.beginPath();
        ctx.arc(0, 0, R * 0.66, a0, a1);
        ctx.lineWidth = 2.4;
        ctx.strokeStyle = rgb(0.7);
        ctx.stroke();
      }
      ctx.restore();

      // ── Radar sweep ───────────────────────────────────────────────────────
      const sweepAng = t * c.sweep;
      const sweep = ctx.createConicGradient
        ? ctx.createConicGradient(sweepAng, cx, cy)
        : null;
      if (sweep) {
        sweep.addColorStop(0, rgb(0.0));
        sweep.addColorStop(0.06, rgb(0.22 * c.energy));
        sweep.addColorStop(0.12, rgb(0.0));
        sweep.addColorStop(1, rgb(0.0));
        ctx.save();
        ctx.beginPath();
        ctx.arc(cx, cy, R * 0.62, 0, Math.PI * 2);
        ctx.clip();
        ctx.fillStyle = sweep;
        ctx.fillRect(0, 0, w, h);
        ctx.restore();
      }

      // ── Radial spokes ─────────────────────────────────────────────────────
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(t * c.speed * 0.3);
      for (let i = 0; i < 6; i++) {
        const ang = (i / 6) * Math.PI * 2;
        ctx.beginPath();
        ctx.moveTo(Math.cos(ang) * R * 0.30, Math.sin(ang) * R * 0.30);
        ctx.lineTo(Math.cos(ang) * R * 0.56, Math.sin(ang) * R * 0.56);
        ctx.lineWidth = 1;
        ctx.strokeStyle = rgb(0.3);
        ctx.stroke();
      }
      ctx.restore();

      // ── Arc-reactor tri-core ──────────────────────────────────────────────
      const coreR = R * (0.30 + 0.03 * pulse);
      ctx.save();
      ctx.translate(cx, cy);
      ctx.rotate(t * c.speed * 0.6);
      ctx.beginPath();
      for (let i = 0; i < 3; i++) {
        const ang = (i / 3) * Math.PI * 2 - Math.PI / 2;
        const px = Math.cos(ang) * coreR, py = Math.sin(ang) * coreR;
        i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
      }
      ctx.closePath();
      ctx.lineWidth = 2.5;
      ctx.strokeStyle = rgb(0.9);
      ctx.shadowBlur = 16 * (0.5 + c.energy);
      ctx.shadowColor = rgb(0.9);
      ctx.stroke();
      ctx.restore();

      // ── White-hot center ──────────────────────────────────────────────────
      const core = ctx.createRadialGradient(cx, cy, 0, cx, cy, coreR * 0.9);
      core.addColorStop(0, `rgba(255,255,255,${0.85 + 0.15 * pulse})`);
      core.addColorStop(0.4, rgb(0.8));
      core.addColorStop(1, rgb(0));
      ctx.beginPath();
      ctx.arc(cx, cy, coreR * 0.9, 0, Math.PI * 2);
      ctx.fillStyle = core;
      ctx.fill();

      // ── Outer rim ─────────────────────────────────────────────────────────
      ctx.beginPath();
      ctx.arc(cx, cy, R, 0, Math.PI * 2);
      ctx.lineWidth = 1.4;
      ctx.strokeStyle = rgb(0.5);
      ctx.stroke();

      raf.current = requestAnimationFrame(frame);
    }

    const onVis = () => {
      if (document.hidden) cancelAnimationFrame(raf.current);
      else { t0 = null; raf.current = requestAnimationFrame(frame); }
    };
    document.addEventListener("visibilitychange", onVis);
    raf.current = requestAnimationFrame(frame);

    return () => {
      cancelAnimationFrame(raf.current);
      document.removeEventListener("visibilitychange", onVis);
    };
  }, [status, size]);

  return (
    <canvas
      ref={cvs}
      style={{ width: size, height: size, display: "block", flexShrink: 0,
               userSelect: "none" }}
      aria-hidden="true"
    />
  );
}
