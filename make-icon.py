#!/usr/bin/env python3
"""
make-icon.py — Build branded JARVIS icons from the Iron-Man helmet asset.

    python make-icon.py [path-to-logo.png]

Defaults to the in-repo helmet line-art. Produces, in
jarvis-studio-gui/src-tauri/icons/:

    icon.ico          — multi-resolution Windows app icon (helmet on dark)
    32x32.png         — bundle icon
    128x128.png       — bundle icon
    128x128@2x.png    — bundle icon (256px)
    tray_icon.rgba    — 32x32 raw RGBA, transparent bg (embedded in the Rust
                        binary via include_bytes!), so it shows as a cyan glyph
                        in the system tray.

The source helmet is cyan line-art on a white background; we key out the white,
recolour the strokes to the HUD accent, add a soft glow, and seat it on a dark
rounded-square plate that matches the app background (#04070d).
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter
import numpy as np

ROOT     = Path(__file__).parent
ICONS    = ROOT / "jarvis-studio-gui" / "src-tauri" / "icons"
DEFAULT  = ROOT / "jarvis-studio-gui" / "public" / "assets" / "ironman-helmet-kept.png"

ACCENT   = (95, 220, 255)    # bright HUD cyan for the strokes
GLOW     = (0, 200, 255)     # glow tint
BG_IN    = (12, 29, 41)      # plate centre
BG_OUT   = (4, 7, 13)        # plate edge (== app --bg-0)


def keyed_helmet(src: Path, box: int) -> Image.Image:
    """Recolour the helmet strokes to cyan (keeping the source alpha), crop to
    content, and fit within box×box preserving the portrait aspect ratio."""
    img = Image.open(src).convert("RGBA")
    data = np.array(img, dtype=np.float32)

    alpha = np.clip(data[:, :, 3] * 1.7, 0, 255)  # boost faint anti-aliased lines
    out = np.zeros_like(data)
    out[:, :, 0] = ACCENT[0]
    out[:, :, 1] = ACCENT[1]
    out[:, :, 2] = ACCENT[2]
    out[:, :, 3] = alpha
    helmet = Image.fromarray(out.astype(np.uint8))

    bbox = helmet.getbbox()
    if bbox:
        helmet = helmet.crop(bbox)
    helmet.thumbnail((box, box), Image.LANCZOS)
    return helmet


def thicken(helmet: Image.Image, radius: int) -> Image.Image:
    """Fatten thin strokes so they survive at small sizes."""
    a = helmet.split()[3].filter(ImageFilter.MaxFilter(radius))
    helmet.putalpha(a)
    return helmet


def dark_plate(size: int) -> Image.Image:
    """Rounded-square dark plate with a soft radial gradient."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    cx = cy = (size - 1) / 2.0
    dist = np.sqrt((xx - cx) ** 2 + (yy - cy) ** 2) / (size * 0.72)
    dist = np.clip(dist, 0, 1)
    plate = np.zeros((size, size, 3), dtype=np.float32)
    for i in range(3):
        plate[:, :, i] = BG_IN[i] * (1 - dist) + BG_OUT[i] * dist
    rgb = Image.fromarray(plate.astype(np.uint8)).convert("RGBA")

    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255
    )
    rgb.putalpha(mask)

    # thin inner cyan rim for polish
    ring = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(ring).rounded_rectangle(
        [int(size * 0.04), int(size * 0.04), int(size * 0.96), int(size * 0.96)],
        radius=int(size * 0.18), outline=(*ACCENT, 70), width=max(1, size // 96),
    )
    return Image.alpha_composite(rgb, ring)


def branded(size: int, src: Path, plate: bool) -> Image.Image:
    canvas = dark_plate(size) if plate else Image.new("RGBA", (size, size), (0, 0, 0, 0))

    box = int(size * (0.76 if plate else 0.94))
    helmet = keyed_helmet(src, box)
    # MaxFilter sizes must be odd.
    helmet = thicken(helmet, 5 if size >= 128 else 3)

    hw, hh = helmet.size
    off = ((size - hw) // 2, (size - hh) // 2)

    # soft cyan glow behind the strokes
    ga = helmet.split()[3].filter(ImageFilter.GaussianBlur(max(1, size // 40)))
    glow_rgba = np.zeros((hh, hw, 4), dtype=np.uint8)
    glow_rgba[:, :, 0], glow_rgba[:, :, 1], glow_rgba[:, :, 2] = GLOW
    glow_rgba[:, :, 3] = (np.array(ga, dtype=np.float32) * 0.5).astype(np.uint8)
    glow = Image.fromarray(glow_rgba)

    canvas.alpha_composite(glow, off)
    canvas.alpha_composite(helmet, off)
    return canvas


def main() -> None:
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT
    if not src.exists():
        print(f"Error: source image not found: {src}")
        sys.exit(1)

    ICONS.mkdir(parents=True, exist_ok=True)
    print(f"Building JARVIS icons from {src.name}…")

    master = branded(256, src, plate=True)
    master.save(ICONS / "icon.ico",
                format="ICO",
                sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("  + icon.ico")

    for name, sz in (("32x32.png", 32), ("128x128.png", 128), ("128x128@2x.png", 256)):
        branded(sz, src, plate=True).save(ICONS / name)
        print(f"  + {name}")

    tray = branded(32, src, plate=False)
    (ICONS / "tray_icon.rgba").write_bytes(tray.tobytes("raw", "RGBA"))
    print("  + tray_icon.rgba")

    print("\nDone. Restart the app (python start.py); the Rust tray icon needs a rebuild.")


if __name__ == "__main__":
    main()
