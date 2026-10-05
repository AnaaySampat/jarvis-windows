/* ViewportScale.jsx — scales the fixed-size HUD stage (1442×902) to fit the
   window while preserving aspect. Ported from jarvis-hud/hud-app.js mount. */

import { useState, useEffect } from "react";

const BASE_W = 1442;
const BASE_H = 902;

export default function ViewportScale({ children }) {
  const [view, setView] = useState({ scale: 1, width: BASE_W });

  useEffect(() => {
    const onResize = () => {
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const baseRatio = BASE_W / BASE_H;
      const viewRatio = vw / vh;
      const scale = viewRatio > baseRatio
        ? vh / BASE_H
        : Math.min(vw / BASE_W, vh / BASE_H);
      const width = viewRatio > baseRatio
        ? Math.max(BASE_W, vw / scale)
        : BASE_W;
      setView({ scale, width });
    };
    onResize();
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  return (
    <div className="hud-viewport">
      <div className="hud-stage" style={{ width: view.width, transform: `scale(${view.scale})` }}>
        {children}
      </div>
    </div>
  );
}
