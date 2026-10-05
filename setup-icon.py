#!/usr/bin/env python3
"""
setup-icon.py — Build JARVIS icons from the logo PNG.

Usage:
    python setup-icon.py <path-to-logo.png>

Outputs (inside jarvis-studio-gui/src-tauri/icons/):
    icon.ico          — multi-resolution app icon (original background kept)
    tray_icon.rgba    — 32×32 raw RGBA, black background made transparent
                        (embedded in the Rust binary via include_bytes!)
"""

import subprocess
import sys
from pathlib import Path

# ── Auto-install dependencies if missing ───────────────────────────────────
def _ensure_deps() -> None:
    missing = []
    try:
        import PIL  # noqa: F401
    except ImportError:
        missing.append("Pillow")
    try:
        import numpy  # noqa: F401
    except ImportError:
        missing.append("numpy")
    if missing:
        print(f"Installing {', '.join(missing)}…")
        subprocess.run(
            [sys.executable, "-m", "pip", "install"] + missing,
            check=True,
        )

_ensure_deps()

from PIL import Image       # noqa: E402
import numpy as np          # noqa: E402

ROOT    = Path(__file__).parent
OUT_DIR = ROOT / "jarvis-studio-gui" / "src-tauri" / "icons"


def remove_black_background(img: Image.Image,
                             hard: int = 40,
                             soft: int = 55) -> Image.Image:
    """
    Convert near-black pixels to transparent.

    Pixels where max(R,G,B) < hard  → fully transparent.
    Pixels where max(R,G,B) < hard+soft → fade in linearly (smooth edges).
    Bright neon pixels are left fully opaque.
    """
    img = img.convert("RGBA")
    data = np.array(img, dtype=np.float32)

    brightness = np.max(data[:, :, :3], axis=2)          # 0–255
    alpha_f    = np.clip((brightness - hard) / soft, 0.0, 1.0) * 255.0
    data[:, :, 3] = alpha_f

    return Image.fromarray(data.astype(np.uint8))


def main() -> None:
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    src = Path(sys.argv[1])
    if not src.exists():
        print(f"Error: file not found: {src}")
        sys.exit(1)

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Loading {src.name}…")
    img = Image.open(src).convert("RGBA")

    # ── icon.ico — multi-resolution, original background ───────────────────
    ico_path = OUT_DIR / "icon.ico"
    # Pillow generates all sizes from the source image automatically
    img.save(ico_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    print(f"✓  {ico_path.relative_to(ROOT)}")

    # ── tray_icon.rgba — 32×32 transparent background, raw RGBA bytes ──────
    rgba_path = OUT_DIR / "tray_icon.rgba"
    tray = remove_black_background(img)
    tray = tray.resize((32, 32), Image.LANCZOS)
    rgba_path.write_bytes(tray.tobytes("raw", "RGBA"))
    print(f"✓  {rgba_path.relative_to(ROOT)}  ({rgba_path.stat().st_size} bytes)")

    print("\nDone! Restart the app: python start.py")


if __name__ == "__main__":
    main()
