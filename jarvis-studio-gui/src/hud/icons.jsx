/* icons.jsx — one hairline icon set for the HUD chrome (replaces the mixed
   emoji glyphs, which rendered in a different style/size on every system). */

const P = {
  memory:   "M9 4a3 3 0 0 0-3 3v.2A3 3 0 0 0 4 10a3 3 0 0 0 1 2.2A3 3 0 0 0 7 17a3 3 0 0 0 5 2V5.5A2.5 2.5 0 0 0 9 4Zm6 0a3 3 0 0 1 3 3v.2A3 3 0 0 1 20 10a3 3 0 0 1-1 2.2A3 3 0 0 1 17 17a3 3 0 0 1-5 2M12 9h-2m2 5H9m3-5h2m-2 5h3",
  skills:   "M12 3v4m0 10v4M3 12h4m10 0h4M6.3 6.3l2.8 2.8m5.8 5.8 2.8 2.8m0-11.4-2.8 2.8m-5.8 5.8-2.8 2.8",
  caps:     "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18Zm0-5v.01M12 13a2 2 0 1 0-2-2",
  power:    "M12 3v8m5.7-5.7a8 8 0 1 1-11.4 0",
  chat:     "M4 5h16v11H9l-5 4V5Zm4 5h8M8 13h5",
  volume:   "M4 9v6h4l5 4V5L8 9H4Zm12.5-.5a5 5 0 0 1 0 7M19 6a8.5 8.5 0 0 1 0 12",
  mute:     "M4 9v6h4l5 4V5L8 9H4Zm12 1 4 4m0-4-4 4",
  mic:      "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3Zm-6 9a6 6 0 0 0 12 0m-6 6v3",
  wake:     "M12 3a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V6a3 3 0 0 0-3-3ZM4 10v2m16-2v2M12 18v3",
  talk:     "M7 8h10M7 12h6m-9 8 3-4h11a2 2 0 0 0 2-2V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v14Z",
  globe:    "M12 21a9 9 0 1 0 0-18 9 9 0 0 0 0 18ZM3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3Z",
  activity: "M3 12h4l3-8 4 16 3-8h4",
  palette:  "M12 21a9 9 0 1 1 9-9c0 2-1.5 3-3.5 3H16a2 2 0 0 0-1.5 3.3A1.6 1.6 0 0 1 12 21ZM7.5 11h.01M10 7.5h.01M14.5 7.5h.01",
  settings: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Zm7.4-3a7.4 7.4 0 0 0-.1-1.2l2-1.6-2-3.4-2.4 1a7.3 7.3 0 0 0-2-1.2L14.5 3h-5l-.4 2.6a7.3 7.3 0 0 0-2 1.2l-2.4-1-2 3.4 2 1.6a7.4 7.4 0 0 0 0 2.4l-2 1.6 2 3.4 2.4-1a7.3 7.3 0 0 0 2 1.2l.4 2.6h5l.4-2.6a7.3 7.3 0 0 0 2-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2Z",
  stop:     "M7 7h10v10H7z",
  close:    "M6 6l12 12M18 6 6 18",
};

export function Icon({ name, size = 18, stroke = 1.5 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
         strokeWidth={stroke} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={P[name]} />
    </svg>
  );
}
