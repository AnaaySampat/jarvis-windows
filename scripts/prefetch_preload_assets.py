"""Build-time prefetch of the heavy runtime assets into a `preload-assets/` dir.

Run by the BUILD machine (via scripts/package-backend.ps1) using the build venv's
Python, BEFORE staging the frozen backend. It downloads, once, on the dev box:

  • Chromium               → preload-assets/ms-playwright/   (~170MB)
  • Piper en_GB-alan voice → preload-assets/piper/           (~60MB)

so the shipped installer carries them and a fresh install needs no
first-run download. The backend reads exactly these locations at runtime (see
actions/browser.py `_activate_bundled_chromium`, tts/tts_engine.py `_bundled_piper_dir`).

The robust auto-download in provisioning.py remains as a SELF-REPAIR fallback: if a
bundled asset is ever missing/corrupt, first launch re-fetches just that one.

Usage:
    python scripts/prefetch_preload_assets.py <target-preload-assets-dir> [--skip browser,voice]

Exit code is non-zero if any *requested* asset failed, so the build can fail loudly.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

_PIPER_VOICE = "en_GB-alan-medium"


def _ok(msg: str) -> None:
    print(f"  [prefetch] {msg}", flush=True)


def _fetch_chromium(target: Path) -> None:
    """Download Chromium into target/ms-playwright via the Playwright CLI."""
    dest = target / "ms-playwright"
    if dest.is_dir() and any(dest.glob("chromium-*/chrome-win*/chrome.exe")):
        _ok(f"Chromium already staged at {dest}")
        return
    dest.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(dest)}
    # `playwright install chromium` honours PLAYWRIGHT_BROWSERS_PATH, so the binary
    # lands straight in the staging dir — no copy from the user cache needed.
    proc = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        env=env, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"playwright install chromium exited {proc.returncode}")
    if not any(dest.glob("chromium-*/chrome-win*/chrome.exe")):
        raise RuntimeError(f"chrome.exe not found under {dest} after install")
    # Prune the ~270MB headless-shell build: JARVIS launches a HEADED Chromium
    # (browser.py `headless=False`), so the shell is pure dead weight in the installer.
    for shell in dest.glob("chromium_headless_shell-*"):
        try:
            shutil.rmtree(shell)
            _ok(f"pruned headless shell {shell.name}")
        except OSError as exc:
            _ok(f"could not prune {shell.name}: {exc}")
    _ok(f"Chromium staged at {dest}")


def _fetch_piper(target: Path) -> None:
    """Download the default Piper voice into target/piper."""
    dest = target / "piper"
    model = dest / f"{_PIPER_VOICE}.onnx"
    if model.is_file():
        _ok(f"Piper voice already staged at {dest}")
        return
    dest.mkdir(parents=True, exist_ok=True)
    from piper.download_voices import download_voice
    download_voice(_PIPER_VOICE, dest)
    if not model.is_file():
        raise RuntimeError(f"{model.name} missing under {dest} after download")
    _ok(f"Piper voice staged at {dest}")


_FETCHERS = {
    "browser": ("Chromium", _fetch_chromium),
    "voice": ("Piper neural voice", _fetch_piper),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="preload-assets directory to populate")
    ap.add_argument("--skip", default="", help="comma list of: browser,voice")
    args = ap.parse_args()

    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}

    failures = []
    for key, (label, fn) in _FETCHERS.items():
        if key in skip:
            _ok(f"SKIP {label}")
            continue
        print(f"[prefetch] {label}...", flush=True)
        try:
            fn(target)
        except Exception as exc:  # noqa: BLE001
            print(f"[prefetch] FAILED {label}: {exc}", flush=True)
            failures.append(label)

    size_mb = sum(p.stat().st_size for p in target.rglob("*") if p.is_file()) / 1e6
    print(f"[prefetch] preload-assets is {size_mb:.0f} MB at {target}", flush=True)
    if failures:
        print(f"[prefetch] ERRORS: {', '.join(failures)}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
