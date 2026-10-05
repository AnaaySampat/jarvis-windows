"""First-run asset provisioning — the SELF-REPAIR safety net.

The heavy runtime assets are normally BUNDLED into the installer (built by
scripts/package-backend.ps1, which prefetches them into preload-assets\):

  • Chromium (~170MB)            — Playwright's browser, for web autopilot
  • faster-whisper base (~150MB) — offline speech-to-text fallback
  • Piper neural voice (~60MB)   — offline British TTS (the daily-driver voice)

So a fresh install works OFFLINE from the first launch. This module then runs as a
GUARANTEE: on the very first launch it confirms each component is present (instant
green ticks), and if any is missing or corrupt — a bundled file got deleted, or this
is a `-Lean` (download-on-first-run) build — it RE-FETCHES just that one, with
retries + backoff and progress surfaced to the HUD via `setup_progress` websocket
events. Every later launch with everything present is silent (no events, no wait).
A manual "Repair components" / "Retry" path re-runs this on demand (force=True).

Everything here is best-effort: a download that still fails after retries degrades
the relevant feature (browser control reports a friendly "needs setup", STT falls
back to cloud, TTS falls back to an OS voice) — it never crashes startup, and the
next launch tries again.
"""

from __future__ import annotations

import asyncio
import time


# ── Presence checks (fast, filesystem-only — no model load, no driver spin-up) ──
def _chromium_present() -> bool:
    try:
        from actions import browser
        return browser.chromium_installed()
    except Exception:  # noqa: BLE001
        return False


def _whisper_present() -> bool:
    try:
        from transcription import whisper_transcriber as wt
        return wt.model_present()
    except Exception:  # noqa: BLE001
        return False


def _piper_present() -> bool:
    try:
        from tts import tts_engine
        return tts_engine.piper_voice_present()
    except Exception:  # noqa: BLE001
        return False


# ── Blocking downloaders (run in an executor) → (ok, error) ─────────────────────
def _download_chromium() -> "tuple[bool, str]":
    from actions import browser
    return browser.download_chromium()


def _download_whisper() -> "tuple[bool, str]":
    from transcription import whisper_transcriber as wt
    try:
        wt._get_model()          # downloads to the HF cache AND warms it
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def _download_piper() -> "tuple[bool, str]":
    from tts import tts_engine
    return tts_engine.ensure_piper_voice()


# key, human label, presence-check, downloader
_ASSETS = (
    ("browser", "browser engine", _chromium_present, _download_chromium),
    ("speech",  "speech model",   _whisper_present,  _download_whisper),
    ("voice",   "neural voice",   _piper_present,    _download_piper),
)


def _safe(fn) -> bool:
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return False


def missing_assets() -> "list[str]":
    """Keys of the assets not yet on disk (fast; never raises)."""
    return [key for key, _label, present, _dl in _ASSETS if not _safe(present)]


# A bundled installer ships every asset already on disk, so provisioning normally
# does nothing. These retries only bite the SELF-REPAIR path — a missing/corrupt
# bundled asset, or the lean (download-on-first-run) build — where a flaky link,
# proxy, or Defender hiccup would otherwise lose the fetch with no second chance.
_DOWNLOAD_RETRIES = 3
_RETRY_BACKOFF_S = (0.0, 3.0, 8.0)   # wait BEFORE attempt i (index clamped)


def _attempt_download(dl) -> "tuple[bool, str]":
    """Run one downloader with bounded retries + backoff. Returns (ok, last_error).
    Blocking — runs in an executor. Never raises."""
    last_err = ""
    for attempt in range(_DOWNLOAD_RETRIES):
        wait = _RETRY_BACKOFF_S[min(attempt, len(_RETRY_BACKOFF_S) - 1)]
        if wait:
            time.sleep(wait)
        try:
            ok, err = dl()
        except Exception as exc:  # noqa: BLE001
            ok, err = False, str(exc)
        if ok:
            return True, ""
        last_err = err or "download failed"
        print(f"[Setup] attempt {attempt + 1}/{_DOWNLOAD_RETRIES} failed: "
              f"{last_err}", flush=True)
    return False, last_err


def _log_setup(summary: str) -> None:
    """Append a one-line, timestamped record to logs/setup.log so a user with a
    failed install has something concrete to share. Best-effort; never raises."""
    try:
        from datetime import datetime
        path = _marker_path().parent / "logs" / "setup.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now().isoformat(timespec='seconds')}] {summary}\n")
    except Exception:  # noqa: BLE001
        pass


def _marker_path():
    """Where the 'first-run setup has run once' flag lives, so the setup screen is
    shown exactly ONCE on first launch (even when every component is already cached)
    and stays silent on later launches."""
    from pathlib import Path
    try:
        import storage
        root = Path(storage.get_root())
    except Exception:  # noqa: BLE001
        import os
        root = Path(os.environ.get("LOCALAPPDATA", ".")) / "JARVIS"
    return root / ".jarvis-setup-complete"


async def provision(emit, *, force: bool = False) -> bool:
    """Run first-run / repair setup, emitting `setup_progress` so the HUD shows a
    setup screen. Returns True if the screen was shown.

    On the VERY FIRST launch (and on a `force` repair) the screen lists EVERY
    component — already-present ones tick green instantly, missing ones download with
    progress (with retries) — so the experience is always visible. Every LATER launch
    is SILENT unless a component is actually missing (then only that one downloads).

    `emit` is the main-loop async emitter: emit(event, data). `force=True` is the
    manual "Repair components" path: it always re-shows the full screen and re-fetches
    anything missing, regardless of the first-run marker."""
    marker = _marker_path()
    first_run = force or not marker.exists()

    if first_run:
        items = list(_ASSETS)                              # show every component once
    else:
        items = [a for a in _ASSETS if not _safe(a[2])]    # only the missing ones
        if not items:
            return False

    loop = asyncio.get_running_loop()
    total = len(items)
    mode = "Repair" if force else ("First run" if first_run else "Catch-up")
    print(f"[Setup] {mode} — {total} component(s).", flush=True)
    await emit("setup_progress", {"stage": "start", "total": total,
                                  "assets": [k for k, _l, _p, _d in items]})

    had_error = False
    results = []
    for index, (key, label, present, dl) in enumerate(items):
        already = _safe(present)
        await emit("setup_progress", {"stage": "downloading", "asset": key,
                                      "label": label, "index": index, "total": total})
        if already:
            await asyncio.sleep(0.35)        # let the green tick register, not flash
            ok, err = True, ""
        else:
            ok, err = await loop.run_in_executor(None, _attempt_download, dl)
        if not ok:
            had_error = True
        status = "done" if ok else "error"
        results.append(f"{key}={'ok' if ok else 'FAIL:' + (err or '')[:120]}")
        print(f"[Setup] {label} {'ready' if ok else 'FAILED: ' + str(err)}.", flush=True)
        await emit("setup_progress", {"stage": status, "asset": key, "label": label,
                                      "index": index, "total": total,
                                      "error": "" if ok else (err or "")[:200]})

    await emit("setup_progress", {"stage": "complete", "total": total,
                                  "had_error": had_error})
    _log_setup(f"{mode}: " + ", ".join(results))
    print(f"[Setup] {mode} complete"
          f"{' (with errors — will retry next launch)' if had_error else ''}.",
          flush=True)
    # Mark setup 'done' ONLY when every component is in place. A failed run leaves the
    # marker absent so the NEXT launch re-shows the full setup screen and retries —
    # instead of silently flipping to 'complete' and hiding a half-installed app.
    if not had_error:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text("done", encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass
    return True
