# Home screen customization

Built 2026-10-20, in this order.

1. **Theme presets.** The look of the home screen is a set of settings in Customize:
   colours (swatches or any colour), font (Clean / HUD), cards (Clean / HUD),
   centrepiece (orb / arc reactor), gauges (bars / rings), corners (0–24 px), glow
   (0–100) and the background, which can be your own picture. Save the current look
   under a name, apply it again later, save it to a `.jarvis-theme.json` file and
   import someone else's. Modern and Iron Man are the two built-in presets; they keep
   your colours. A reset keeps your saved themes.
2. **Drag to arrange cards.** Drag a card by its title on the home screen, or its row
   in Customize, to reorder it or move it to the other side. In Customize, Alt + an
   arrow key moves the focused card.
3. **JARVIS designs a theme.** "Make my home screen look like…" — the
   `customize_screen` tool's `design` action sets the look settings (and can save
   them as a named theme). It only changes the look, so the layout can't break.

Where: `llm/groq_bridge.py` (`SCREEN_PRESETS`, `clean_look`, `set_screen` — every
value is checked there before it can reach the CSS), `actions/__init__.py`
(`_screen_patch`), `hud/HudApp.jsx`, `hud/ironman.css` (the HUD card style and font),
`hud/widgets.jsx` (dragging), `components/Customize.jsx`.

Not planned for now: a free-form grid editor, user-made cards, custom CSS.
