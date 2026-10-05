"""Browser control — JARVIS's hands, scoped to a browser it owns.

This replaces the old, unreliable OS-level mouse/keyboard control. Rather than
guessing pixel coordinates on the live desktop (which never worked well), JARVIS
drives a dedicated Chromium window through **Playwright** and *sees where to
click by injecting JavaScript* that enumerates the real interactive elements on
the page — links, buttons, inputs — with their visible text. It then clicks or
types by that label. Same reliable "list, then act by name" recipe the desktop
code aimed for, but grounded in the live DOM instead of a screenshot.

Design notes (why it's built this way):

  • Playwright's sync API objects are **thread-affine**, and our actions run on
    arbitrary executor threads, so ALL browser work happens on one dedicated
    worker thread that owns the Playwright objects. Public functions submit a job
    to that thread and block for the result — safe to call from anywhere.
  • A **persistent profile** (under the storage root, NOT OneDrive) keeps the
    user logged in to sites across sessions, so JARVIS's browser feels like
    theirs.
  • **Lazy + dependency-guarded:** a missing ``playwright`` install — or an
    un-run ``playwright install chromium`` — just disables the feature with a
    helpful message; it never crashes the app. Every function returns
    ``(ok, message)`` and never raises.

The model never needs pixel coordinates: it calls ``open_url`` → ``list`` (to
see the page's real elements) → ``click``/``type`` by the number or visible text
it sees.
"""

from __future__ import annotations

import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import quote_plus, urlparse

# How long a single browser operation may take before we give up on it.
_OP_TIMEOUT = 60
# The very first launch_persistent_context is slow on Windows (Chromium first-run
# setup + Windows Defender scanning the freshly-touched binaries), so give the
# COLD open a wide budget. Bailing at 60s used to abort the launch and then make
# the follow-up navigate/type fire against a half-ready browser (the "Locator
# timeout" you saw). Warm operations afterwards stay snappy on _OP_TIMEOUT.
_LAUNCH_TIMEOUT = 150
_NAV_TIMEOUT_MS = 30000
# Default auto-wait budget for the context. Kept modest so a stray actionability
# wait fails fast; the click/type helpers run their own SHORT, explicit-timeout
# strategy ladders (see _click_locator / _type_into_locator) rather than leaning
# on one long timeout (the old 15s single-shot is what kept hanging then failing).
_CLICK_TIMEOUT_MS = 8000

# Consent window: the user must approve browser control before JARVIS may act in
# it. Approval lasts a bounded time so it can't be left open forever; read-only
# inspection (list/read/current) is always allowed.
_APPROVE_MINUTES = 15
_approved_until: float = 0.0

_UNAVAILABLE = ("Browser control needs the Playwright package — it isn't installed, "
                "sir. Install it with: pip install playwright  &&  playwright install chromium.")
_NEED_CHROMIUM = ("Playwright is installed but its browser isn't — run "
                  "`playwright install chromium` once, sir, and I'll be ready.")

# ── Module state (the cached snapshot the HUD reads; updated by the worker) ────
_state_cb: "Optional[Callable[[dict], None]]" = None
_last_state: dict = {"available": None, "open": False, "url": "", "title": ""}
_worker: "Optional[_BrowserWorker]" = None
_worker_lock = threading.Lock()

# ── Personal-browser target (the user's real Chrome, driven over CDP) ─────────
# JARVIS normally drives its OWN dedicated Chromium (the persistent profile above).
# When the user explicitly asks for THEIR browser, we attach to their real Chrome
# over the DevTools protocol instead — same action code, a second worker/context.
# The user's Chrome must be exposing the debug port (ensure_personal_available()
# starts it that way when it isn't). We NEVER close or force-kill the user's Chrome.
_PERSONAL_CDP_PORT = 9222
_PERSONAL_CDP_URL = f"http://127.0.0.1:{_PERSONAL_CDP_PORT}"
_worker_personal: "Optional[_BrowserWorker]" = None
_active_target = "own"      # "own" | "personal" — which browser browser.* acts on


# ── JavaScript injected into the page ─────────────────────────────────────────
# Walks the DOM for VISIBLE interactive elements, stamps each with a stable
# data-jarvis-idx, and returns {heading, els: [{n, tag, type, text}]}. This is
# how JARVIS "sees where it can click" — the browser equivalent of reading the
# accessibility tree. It recurses into OPEN shadow roots (cookie banners and web
# components live there, invisible to a plain querySelectorAll), and the Python
# side runs it once per FRAME with a continuing start index, so iframe content
# (consent dialogs especially) gets numbered too. Takes `start`: the element
# number to count from (frames after the first continue the numbering), and
# `limit`: how many non-ad elements the operator can be shown. Geometry is read
# for every candidate (cheap), but the costly reads — computed style, innerText
# — stop at `limit`, on-screen first: a long article has thousands of links and
# styling every one cost ~1s per observation. Stamps are written last, so no
# write invalidates styles between reads. `rest` counts candidates never read.
_JS_LIST_ELEMENTS = r"""
({start, limit}) => {
  // This frame's roots: the document plus every open shadow root, recursively.
  const roots = [document];
  const findShadows = (root) => {
    for (const el of root.querySelectorAll('*'))
      if (el.shadowRoot) { roots.push(el.shadowRoot); findShadows(el.shadowRoot); }
  };
  try { findShadows(document); } catch (e) {}
  // Stale stamps from an earlier list would collide with this pass's numbering
  // (an element that no longer qualifies keeps its old number while a new
  // element gets the same one — click-by-number then hits the wrong element).
  // Clear every old stamp first so each listing is the only source of truth.
  for (const root of roots)
    for (const el of root.querySelectorAll('[data-jarvis-idx]'))
      el.removeAttribute('data-jarvis-idx');
  const SEL = 'a, button, input, textarea, select, [role=button], [role=link],' +
              '[role=tab], [role=menuitem], [role=checkbox], [role=switch],' +
              '[role=textbox], [contenteditable=true], [onclick],' +
              '[tabindex]:not([tabindex="-1"])';
  // Pass 1 — geometry only: candidates, on-screen first, DOM order within each.
  const vw = window.innerWidth || 0, vh = window.innerHeight || 0;
  const onS = [], offS = [];
  for (const root of roots)
  for (const el of root.querySelectorAll(SEL)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    (r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw ? onS : offS).push(el);
  }
  // Pass 2 — the costly reads, until `limit` non-ad elements qualify.
  const out = [], picked = [];
  const cap = limit || 1e9;
  let kept = 0, read = 0;
  for (const group of [onS, offS])
  for (const el of group) {
    if (kept >= cap) break;
    read += 1;
    const onScreen = group === onS;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    const t = (el.tagName || '').toLowerCase();
    const isPw = (el.type || '').toLowerCase() === 'password';
    // Never surface a password field's value — not in the label, not anywhere.
    let label = (el.innerText || (isPw ? '' : el.value) || el.getAttribute('aria-label') ||
                 el.getAttribute('placeholder') || el.getAttribute('title') ||
                 el.getAttribute('name') || el.alt || '').trim();
    label = label.replace(/\s+/g, ' ').slice(0, 90);
    if (!label && (t === 'input' || t === 'textarea'))
      label = '(' + ((el.type || 'text')) + ' field)';
    if (!label && t === 'button') {
      const al = (el.getAttribute('aria-label') || '').toLowerCase();
      if (al.includes('calendar') || al.includes('date')) label = '(calendar button)';
    }
    if (!label) continue;
    // Extra observation data for the operator: where a link goes, what an
    // input currently holds, and toggle state — so it can verify its own work.
    let href = '';
    if (t === 'a' && el.href) {
      try { const u = new URL(el.href); href = (u.pathname + u.search).slice(0, 60); }
      catch (e) {}
    }
    let value = '';
    if (!isPw && (t === 'input' || t === 'textarea') &&
        el.value !== undefined && el.value !== null)
      value = String(el.value).replace(/\s+/g, ' ').slice(0, 40);
    let state = '';
    if (el.checked === true || el.getAttribute('aria-checked') === 'true' ||
        el.getAttribute('aria-selected') === 'true') state = 'on';
    else if (el.checked === false) state = 'off';
    if (el.disabled) state = (state ? state + ',' : '') + 'disabled';
    // Ad / sponsored detection. Two signals: an ad-network href (display ads), OR
    // living inside a promoted/ad renderer (YouTube's promoted "Watch" result has a
    // CLEAN /watch href but sits in ytd-*-pyv/ad-slot — the click-trap that looks
    // like the first real result). Kept out of the click listing on the Python side.
    let ad = false;
    try {
      const hh = String(el.getAttribute('href') || el.href || '');
      ad = /\/(pagead|aclk)\b|[?&](aclk|adurl)=|doubleclick\.net|googleadservices|googlesyndication/i.test(hh) ||
           !!(el.closest && el.closest(
             'ytd-ad-slot-renderer, ytd-promoted-video-renderer,' +
             'ytd-promoted-sparkles-web-renderer, ytd-promoted-sparkles-text-search-renderer,' +
             'ytd-search-pyv-renderer, ytd-in-feed-ad-layout-renderer,' +
             'ytd-companion-slot-renderer, ytd-display-ad-renderer,' +
             '[aria-label*="Sponsored" i]'));
    } catch (e) {}
    picked.push(el);
    out.push({ n: 0, tag: t, type: (el.type || ''), text: label, onScreen,
               href, value, state, ad });
    if (!ad) kept += 1;
  }
  // Pass 3 — stamp the numbers.
  let n = start || 0;
  for (let i = 0; i < out.length; i++) {
    n += 1;
    out[i].n = n;
    picked[i].setAttribute('data-jarvis-idx', String(n));
  }
  const h = document.querySelector('h1');
  const heading = ((h && h.innerText) || '').replace(/\s+/g, ' ').trim().slice(0, 120);
  return { heading, els: out, rest: onS.length + offS.length - read };
}
"""

# How many frames (the main page + iframes) a listing enumerates. Consent
# dialogs (OneTrust/Sourcepoint/TrustArc) ship in their own iframe; ad frames
# are why this is capped rather than unbounded.
_MAX_FRAMES = 8

# How many elements the listing shows the operator (on-screen first). 30 was too
# tight on dense pages — the real target (a result link, a menu item) was often
# in the truncated tail, so the operator clicked a worse visible element or got
# stuck. 45 covers far more real pages; the listing is still tight enough to keep
# token cost (and Groq rate-limit pressure) low.
_LIST_CAP = 45

# Companion side panel width (GUI docks on the right; browser tiles left of it).
_PANEL_DEFAULT_W = 400
_last_panel_w = _PANEL_DEFAULT_W


def _frames(page) -> list:
    """The page's frames, main frame first, capped at _MAX_FRAMES."""
    try:
        rest = [f for f in page.frames if f is not page.main_frame]
        return [page.main_frame] + rest[:_MAX_FRAMES - 1]
    except Exception:  # noqa: BLE001
        return [page.main_frame]


def _locate_by_number(page, num: str):
    """Find the stamped element `num` in WHICHEVER frame it lives in (the listing
    numbers elements across all frames), or None. count() is immediate, so a
    vanished number fails fast instead of eating a locator timeout."""
    for fr in _frames(page):
        try:
            cand = fr.locator(f'[data-jarvis-idx="{num}"]')
            if cand.count() > 0:
                return cand.first
        except Exception:  # noqa: BLE001
            continue
    return None

# Find the single most likely text-entry field (a search box / first input),
# scroll it into view, and FOCUS + click it — all in the page itself. Doing the
# focus in JS (rather than via a Playwright locator + fill) sidesteps Playwright's
# actionability checks, which were the source of the "Locator.click: Timeout"
# failures: an input covered by a cookie banner or rendered by a custom widget is
# perfectly typeable once focused, but fill()/click() refuse to touch it. After
# this returns true we just type with the keyboard into the now-focused element.
_JS_BEST_INPUT = r"""
() => {
  const cands = Array.from(document.querySelectorAll(
    'textarea[name=q], input[name=q], input[type=search], input[type=text],' +
    'input[type=email], input[type=url], input:not([type]), textarea,' +
    '[contenteditable=true], [role=searchbox], [role=textbox]'));
  for (const el of cands) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || cs.opacity === '0') continue;
    if (el.disabled || el.readOnly) continue;
    try { el.scrollIntoView({ block: 'center', inline: 'center' }); } catch (e) {}
    try { el.focus(); } catch (e) {}
    try { el.click(); } catch (e) {}
    el.setAttribute('data-jarvis-input', '1');
    return true;
  }
  return false;
}
"""

_JS_PAGE_TEXT = r"""
() => (document.body ? document.body.innerText : '').replace(/\n{3,}/g, '\n\n').slice(0, 8000)
"""


# ── Availability + state ──────────────────────────────────────────────────────

def available() -> bool:
    """True only if Playwright can be imported (the browser binary is checked
    lazily on first launch)."""
    _activate_bundled_chromium()
    try:
        import playwright  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# ── Chromium provisioning (download-on-first-use) ─────────────────────────────
# The shipped app bundles the Playwright PACKAGE and its Node driver, but NOT the
# ~150MB Chromium binary (that would bloat the installer for every buyer). So the
# first time browser control is used we download Chromium once via the bundled
# driver into the standard %LOCALAPPDATA%\ms-playwright cache; every later run
# finds it and launches straight away. If the download can't complete (offline,
# blocked) we fall back to the existing _NEED_CHROMIUM message — never a crash.

_CHROMIUM_INSTALL_TIMEOUT = 900   # generous: a cold ~150MB fetch on a slow link
_chromium_ready = False           # cached True once Chromium is confirmed present
_chromium_lock = threading.Lock()


def _activate_bundled_chromium() -> None:
    """Point Playwright at a bundled Chromium cache when the installer staged one."""
    candidates = []
    preload = os.environ.get("JARVIS_PRELOAD_ASSETS_DIR")
    if preload:
        candidates.append(Path(preload) / "ms-playwright")
    if getattr(sys, "frozen", False):
        candidates.append(Path(sys.executable).resolve().parent / "preload-assets" / "ms-playwright")

    for path in candidates:
        try:
            if path.is_dir() and any(path.glob("chromium-*/chrome-win*/chrome.exe")):
                os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(path))
                return
        except OSError:
            continue


def _chromium_present(pw) -> bool:
    """True if Playwright's Chromium binary is already downloaded on disk."""
    try:
        exe = pw.chromium.executable_path
    except Exception:  # noqa: BLE001 — not installed yet / driver hiccup
        return False
    return bool(exe) and os.path.isfile(exe)


def _run_driver_install_chromium() -> "tuple[bool, str]":
    """Download Chromium by invoking the bundled Playwright Node driver directly.
    Works inside the frozen app, where `python -m playwright install` isn't
    available (sys.executable is jarvis-backend.exe, not Python). Returns
    (ok, error_tail)."""
    _activate_bundled_chromium()
    try:
        from playwright._impl._driver import (
            compute_driver_executable,
            get_driver_env,
        )
        node, cli = compute_driver_executable()
    except Exception as exc:  # noqa: BLE001
        return False, f"driver unavailable: {exc}"
    kwargs = {
        "env": get_driver_env(),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "timeout": _CHROMIUM_INSTALL_TIMEOUT,
    }
    if os.name == "nt":
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW — no console flash
    try:
        proc = subprocess.run([node, cli, "install", "chromium"], **kwargs)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if proc.returncode == 0:
        return True, ""
    tail = (proc.stdout or "").strip().splitlines()
    return False, (tail[-1] if tail else f"exit {proc.returncode}")


def _ensure_chromium(pw) -> "tuple[bool, str]":
    """Make sure Chromium is installed, downloading it once on first use. Returns
    (ok, message); on failure message is the friendly _NEED_CHROMIUM. Worker
    thread only — it shares the worker's Playwright driver."""
    global _chromium_ready
    _activate_bundled_chromium()
    if _chromium_ready:
        return True, ""
    with _chromium_lock:
        if _chromium_ready or _chromium_present(pw):
            _chromium_ready = True
            return True, ""
        print("[Browser] Chromium not found — downloading it once "
              "(this can take a minute)…", flush=True)
        try:
            _emit_state({**state(),
                         "setup": "Setting up browser control (one-time download)…"})
        except Exception:  # noqa: BLE001 — status is best-effort, never block
            pass
        ok, err = _run_driver_install_chromium()
        if ok and _chromium_present(pw):
            _chromium_ready = True
            print("[Browser] Chromium downloaded — browser control is ready.",
                  flush=True)
            return True, ""
        print(f"[Browser] Chromium auto-install failed: {err}", flush=True)
        try:
            _emit_state(state())  # clear the "setting up…" note so it can't stick
        except Exception:  # noqa: BLE001
            pass
        return False, _NEED_CHROMIUM


def chromium_installed() -> bool:
    """Fast filesystem check (no driver spin-up): is Playwright's Chromium binary
    already on disk — bundled OR previously downloaded? Used by first-run
    provisioning to decide whether the ~150MB fetch is needed."""
    _activate_bundled_chromium()
    roots = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        roots.append(Path(env))
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "ms-playwright")
    for r in roots:
        try:
            if r.is_dir() and any(r.glob("chromium-*/chrome-win*/chrome.exe")):
                return True
        except OSError:
            continue
    return False


def download_chromium() -> "tuple[bool, str]":
    """Public entry point for first-run provisioning: download Chromium once via
    the bundled Node driver. Returns (ok, error). Idempotent — a no-op when it's
    already present — and marks the worker ready so it won't re-check."""
    global _chromium_ready
    if chromium_installed():
        _chromium_ready = True
        return True, ""
    ok, err = _run_driver_install_chromium()
    if ok and chromium_installed():
        _chromium_ready = True
        return True, ""
    return False, (err or "Chromium download failed")


def set_state_callback(cb: "Callable[[dict], None]") -> None:
    """main.py registers this so open/navigate/close can push `browser_state`."""
    global _state_cb
    _state_cb = cb


def state() -> dict:
    """A fast, non-blocking snapshot for the HUD (cached; never drives the worker)."""
    st = dict(_last_state)
    if st.get("available") is None:
        st["available"] = available()
    st["approved"] = is_approved()
    return st


# ── Consent (the user must approve before JARVIS acts in the browser) ─────────

def approve(minutes: float = _APPROVE_MINUTES) -> None:
    """Grant browser control for a bounded window (the 🌐 toggle or an Approve)."""
    global _approved_until
    try:
        m = max(1.0, float(minutes or _APPROVE_MINUTES))
    except (TypeError, ValueError):
        m = _APPROVE_MINUTES
    _approved_until = time.monotonic() + m * 60
    _emit_state({**state(), "approved": True})


def revoke() -> None:
    global _approved_until
    _approved_until = 0.0
    _emit_state({**state(), "approved": False})


def abort() -> bool:
    """Hard-stop the ACTIVE browser NOW (the STOP button). Revokes consent and, for
    JARVIS's OWN browser, force-kills its profile Chromium so a step blocked inside
    Playwright returns at once instead of stalling STOP until a slow page-load/wait
    finishes (matched by the profile dir — it can only ever hit JARVIS's browser).
    For the user's PERSONAL Chrome we NEVER kill it: we only latch the worker so the
    autopilot loop stops between steps. The latch also stops the interrupted action
    from relaunching mid-abort; the next task opens clean. Best-effort; never raises."""
    revoke()
    w = _active_worker()
    if w is not None:
        w._aborted = True   # read on the worker thread by _ensure_page / with_page
    if _active_target != "own":
        return False        # never force-kill the user's real Chrome
    try:
        return _force_kill_browser()
    except Exception:  # noqa: BLE001
        return False


def is_approved() -> bool:
    """True while control is consented (self-expiring)."""
    return time.monotonic() < _approved_until


def is_open() -> bool:
    """True if a browser window is currently up (from the cached state snapshot).

    Used by the permission gate so read-only inspection of an ALREADY-OPEN
    browser stays free, while anything that would LAUNCH a browser still asks
    first — we never open it behind the user's back."""
    return bool(_last_state.get("open"))


def _emit_state(snapshot: dict) -> None:
    global _last_state
    _last_state = snapshot
    if _state_cb:
        try:
            _state_cb(dict(snapshot))
        except Exception:  # noqa: BLE001
            pass


# ── The dedicated worker thread (owns all Playwright objects) ─────────────────

# Watchdog: how far past its own budget the RUNNING job may overrun before the
# worker is declared wedged and the profile's Chromium is force-killed to free it.
_WEDGE_GRACE_S = 20.0


class _BrowserWorker:
    def __init__(self, mode: str = "own") -> None:
        self._mode = "personal" if mode == "personal" else "own"
        self._jobs: "queue.Queue" = queue.Queue()
        self._pw = None
        self._ctx = None
        self._page = None
        self._last_title = ""        # cached so push_state needn't block on title()
        # Watchdog state: when/with what budget the CURRENTLY RUNNING job started
        # (set/cleared on the worker thread; read by submit() on caller threads).
        self._job_started: "Optional[float]" = None
        self._job_budget: float = 0.0
        self._recovered_once = False
        # One-shot latch set by abort() (STOP): suppresses the in-flight action's
        # relaunch-on-close retry so a force-killed browser stays down, then is
        # consumed by the next _ensure_page which relaunches a clean window.
        self._aborted = False
        self._thread = threading.Thread(target=self._loop, name="jarvis-browser", daemon=True)
        self._thread.start()

    # Run `job` (a zero-arg callable returning a tuple) on the worker thread and
    # block for its result. Infra failures become a friendly (False, msg).
    def submit(self, job: "Callable[[], tuple]", timeout: float = _OP_TIMEOUT) -> tuple:
        self._maybe_recover()
        done = threading.Event()
        box: dict = {}

        def wrapped() -> None:
            # The caller gave up on this job while it sat in the queue — a click
            # firing minutes after the user moved on is worse than the failure
            # they already saw, so an abandoned job must never run late.
            if box.get("abandoned"):
                return
            self._job_started = time.monotonic()
            self._job_budget = float(timeout)
            try:
                box["result"] = job()
            except Exception as exc:  # noqa: BLE001
                box["result"] = (False, _friendly(exc))
            finally:
                self._job_started = None
                self._recovered_once = False
                done.set()

        self._jobs.put(wrapped)
        if not done.wait(timeout):
            box["abandoned"] = True
            return (False, "The browser took too long to respond, sir.")
        return box.get("result", (False, "The browser gave no response, sir."))

    def _maybe_recover(self) -> None:
        """Wedge watchdog. The single worker thread means one Playwright call that
        never returns (a page stuck mid-script, a hung driver) head-of-line-blocks
        EVERY later job — each then times out in turn and browser control looks
        dead. When the running job has overrun its own budget plus grace, kill the
        profile's Chromium: the blocked call errors out, the worker thread comes
        back, and the next action relaunches cleanly (the profile lock died with
        the process). One attempt per wedge."""
        started = self._job_started
        if started is None or self._recovered_once:
            return
        budget = self._job_budget or _OP_TIMEOUT
        if time.monotonic() - started < budget + _WEDGE_GRACE_S:
            return
        self._recovered_once = True
        print(f"[Browser] worker wedged (job running {time.monotonic() - started:.0f}s "
              f"on a {budget:.0f}s budget) — force-closing Chromium to recover",
              flush=True)
        _force_kill_browser()

    def _loop(self) -> None:
        while True:
            fn = self._jobs.get()
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass

    # ── Page lifecycle (only ever touched on the worker thread) ───────────────

    def _apply_geolocation(self):
        """Feed JARVIS's known user location into this automation browser so Google
        Maps (and any 'use my location' site) shows the REAL position — the blue dot
        and 'Your location' — instead of a coarse IP guess from this fresh profile.
        Re-applied only when the location changes; runs on the worker thread (where
        the context lives) and never breaks a task."""
        try:
            import places
            lat, lon, _ = places.get_user_coords()
        except Exception:  # noqa: BLE001
            return
        if lat is None or lon is None:
            return
        try:
            coords = (round(float(lat), 6), round(float(lon), 6))
        except (TypeError, ValueError):
            return
        if coords == getattr(self, "_geo_set", None):
            return                          # already applied this exact fix
        try:
            self._ctx.grant_permissions(["geolocation"])
            self._ctx.set_geolocation({"latitude": coords[0], "longitude": coords[1],
                                       "accuracy": 50})
            self._geo_set = coords
            print(f"[Browser] geolocation set to {coords[0]:.5f}, {coords[1]:.5f}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001 — best-effort; never fail a task over it
            print(f"[Browser] couldn't set geolocation: {exc}", flush=True)

    def _ensure_page(self):
        # A STOP/abort() force-killed our Chromium: drop the dead context so this
        # relaunches a fresh one, and clear the latch (its only job was to stop the
        # just-interrupted action from relaunching mid-abort — see with_page/abort).
        if self._aborted:
            self._reset()
            self._aborted = False
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            _t = time.time()
            self._pw = sync_playwright().start()
            print(f"[Browser] playwright driver started in {time.time()-_t:.1f}s", flush=True)
        if self._ctx is None:
            _t = time.time()
            if self._mode == "personal":
                # Attach to the user's REAL Chrome over CDP (their tabs/logins). No
                # launch/profile recovery, no automation hardening, no window docking
                # — it's their browser; we only borrow a context.
                self._ctx = _connect_personal_cdp(self._pw)
                print(f"[Browser] attached to your Chrome over CDP in "
                      f"{time.time()-_t:.1f}s", flush=True)
            else:
                # Recovery ladder (locks → archive incompatible profile) lives in
                # _launch_context_with_recovery — avoids the open/close/open loop
                # when an old profile makes Chromium exit on startup.
                self._ctx = _launch_context_with_recovery(self._pw)
                # Belt-and-suspenders: patch the flag in every document BEFORE its
                # scripts run, in case a future Chromium re-sets it despite the switch.
                try:
                    self._ctx.add_init_script(
                        "Object.defineProperty(navigator, 'webdriver', "
                        "{get: () => undefined});")
                except Exception:  # noqa: BLE001 — init script is best-effort hardening
                    pass
                print(f"[Browser] Chromium launched in {time.time()-_t:.1f}s "
                      f"(if this is consistently high, antivirus is still scanning it - "
                      f"add the ms-playwright Defender exclusion)", flush=True)
                try:
                    dock_browser_window()
                except Exception:  # noqa: BLE001
                    pass
            # Bound every auto-waiting op (click/type/fill/navigation) so a stuck
            # page can't wedge the single worker thread indefinitely.
            try:
                self._ctx.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
                self._ctx.set_default_timeout(_CLICK_TIMEOUT_MS)
            except Exception:  # noqa: BLE001
                pass
            self._page = None
        if self._mode != "personal":
            self._apply_geolocation()
        pages = [p for p in self._ctx.pages if not p.is_closed()]
        if self._mode == "personal" and (self._page is None or self._page.is_closed()):
            # Fresh tab — never adopt one of the user's. (Checking only None meant a
            # closed JARVIS tab fell through to pages[-1]: the user's own last tab,
            # which the next 'open' then navigated away, form input and all.)
            self._page = self._ctx.new_page()
        elif not pages:
            self._page = self._ctx.new_page()
        elif self._page is None or self._page.is_closed():
            self._page = pages[-1]
        return self._page

    def with_page(self, action: "Callable", timeout: float = _OP_TIMEOUT) -> tuple:
        """ensure the browser is up, run action(page), retrying once if the user
        closed the window between calls.

        The FIRST action against a not-yet-launched context pays the slow Chromium
        cold start, so any cold call automatically gets the wide `_LAUNCH_TIMEOUT`
        — not just open_url/open_blank. Previously a `type`/`click`/`list` that
        happened to trigger the cold launch aborted at 60s (the "timed out while
        typing" bug). Warm calls keep their normal, snappy budget."""
        cold = self._ctx is None
        # On the first cold launch of a fresh install, Chromium may not be
        # downloaded yet. Fetch it as its own job with a long budget BEFORE the
        # launch job, so a ~150MB download can't blow the launch timeout (and the
        # launch below then finds the binary ready).
        if cold and not _chromium_ready:
            ok, msg = self.submit(self._ensure_chromium_job, _CHROMIUM_INSTALL_TIMEOUT)
            if not ok:
                return False, msg
        def job() -> tuple:
            before = self._page_count()
            try:
                page = self._ensure_page()
                result = action(page)
            except Exception as exc:  # noqa: BLE001
                # Retry once when the user closed the window mid-session. Cold-
                # launch recovery is handled inside _ensure_page — don't relaunch
                # here or the window flickers open/closed.
                if _is_closed_err(exc) and self._ctx is not None and not self._aborted:
                    self._reset()
                    _prepare_profile_for_launch()
                    page = self._ensure_page()
                    result = action(page)
                else:
                    raise
            self._adopt_newest_page(before)
            self.push_state()
            return result
        return self.submit(job, _LAUNCH_TIMEOUT if cold else timeout)

    def _ensure_chromium_job(self) -> tuple:
        """Worker-thread job: start the Playwright driver if needed, then make sure
        Chromium is installed (downloading it once on first use)."""
        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()
        return _ensure_chromium(self._pw)

    def _page_count(self) -> int:
        try:
            return 0 if self._ctx is None else \
                len([p for p in self._ctx.pages if not p.is_closed()])
        except Exception:  # noqa: BLE001
            return 0

    def _adopt_newest_page(self, pages_before: int) -> None:
        """Follow the action: a click on a target=_blank link / window.open pops a
        NEW tab while self._page stays on the old one — every later list/click
        would then act on the page the user just left. When an action *spawned* a
        tab, adopt the newest one so JARVIS's 'eyes' follow its own navigation
        (a user's manually-chosen tab is left alone otherwise)."""
        try:
            if self._ctx is None:
                return
            pages = [p for p in self._ctx.pages if not p.is_closed()]
            if pages and len(pages) > pages_before and self._page is not pages[-1]:
                self._page = pages[-1]
                try:
                    pages[-1].bring_to_front()
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass

    def close_browser(self) -> tuple:
        def job() -> tuple:
            if self._ctx is None:
                return True, "The browser is already closed, sir."
            self._reset()
            self.push_state()
            return True, "Closed the browser, sir."
        return self.submit(job, 30)

    def _reset(self) -> None:
        if self._mode == "personal":
            # NEVER close the user's real Chrome or its context. Closing the CDP
            # *connection* only disconnects (verified: Chrome and its tabs survive);
            # skipping it leaked one connection per reset.
            try:
                if self._ctx is not None and self._ctx.browser is not None:
                    self._ctx.browser.close()
            except Exception:  # noqa: BLE001
                pass
            self._ctx = None
            self._page = None
            return
        for obj, meth in ((self._ctx, "close"),):
            try:
                if obj is not None:
                    getattr(obj, meth)()
            except Exception:  # noqa: BLE001
                pass
        self._ctx = None
        self._page = None

    def push_state(self) -> None:
        open_ = self._ctx is not None and self._page is not None
        url, title = "", self._last_title
        if open_:
            try:
                if self._page.is_closed():
                    open_ = False
                else:
                    # `.url` is a cached property (non-blocking); title() does a
                    # round-trip that can hang on a navigating page and wedge the
                    # worker, so we use the title cached by open_url/current_page.
                    url = self._page.url or ""
            except Exception:  # noqa: BLE001
                pass
        _emit_state({"available": True, "open": open_, "url": url, "title": title,
                     "approved": is_approved()})


def _get_worker() -> "Optional[_BrowserWorker]":
    """Lazily create the worker for the ACTIVE target (None if Playwright isn't
    installed). 'personal' gets its own CDP-attached worker; 'own' the dedicated one."""
    global _worker, _worker_personal
    if not available():
        return None
    with _worker_lock:
        if _active_target == "personal":
            if _worker_personal is None:
                _worker_personal = _BrowserWorker(mode="personal")
            return _worker_personal
        if _worker is None:
            _worker = _BrowserWorker()
        return _worker


def _active_worker() -> "Optional[_BrowserWorker]":
    """The already-created worker for the active target (no creation) — for abort()."""
    return _worker_personal if _active_target == "personal" else _worker


def set_target(name: str) -> None:
    """Route subsequent browser.* actions to 'own' (JARVIS's dedicated browser) or
    'personal' (the user's real Chrome over CDP). Set at a task's start, reset after;
    single-flight in main.py guarantees no two tasks race on this global."""
    global _active_target
    _active_target = "personal" if str(name or "").lower() == "personal" else "own"


def get_target() -> str:
    return _active_target


# ── Personal Chrome (CDP attach) helpers ─────────────────────────────────────

_PERSONAL_BROWSER_HINTS = (
    "personal browser", "my browser", "my own browser", "my chrome",
    "personal chrome", "my personal browser", "real chrome", "actual browser",
    "in my browser", "on my browser", "using my browser", "my default browser",
    "my regular browser", "my normal browser",
)


def wants_personal_browser(goal: str) -> bool:
    """True when the user explicitly aimed a web task at THEIR browser (not JARVIS's).
    Deterministic keyword match — a model dropping a structured flag is exactly why
    'use my browser' kept landing in the wrong browser."""
    g = " ".join((goal or "").lower().split())
    return any(h in g for h in _PERSONAL_BROWSER_HINTS)


def _cdp_reachable() -> bool:
    """True when something is already exposing the Chrome debug port we use."""
    try:
        import json
        import urllib.request
        with urllib.request.urlopen(_PERSONAL_CDP_URL + "/json/version", timeout=1.5) as r:
            json.load(r)
        return True
    except Exception:  # noqa: BLE001
        return False


def _find_chrome_exe() -> str:
    """Path to the user's Google Chrome, or '' if not found."""
    import shutil
    hit = shutil.which("chrome") or shutil.which("chrome.exe")
    if hit:
        return hit
    for base in (os.environ.get("PROGRAMFILES", ""),
                 os.environ.get("PROGRAMFILES(X86)", ""),
                 os.environ.get("LOCALAPPDATA", "")):
        if not base:
            continue
        cand = os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")
        if os.path.isfile(cand):
            return cand
    return ""


def _user_chrome_profile_dir() -> str:
    """The user's real Chrome 'User Data' root (their Default profile lives inside)."""
    return os.path.join(os.environ.get("LOCALAPPDATA", ""), "Google", "Chrome", "User Data")


def _chrome_running() -> bool:
    """True if any ordinary chrome.exe is already running — a second chrome launched
    with the debug flag would just open a tab in it and never expose the port."""
    try:
        import psutil
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == "chrome.exe":
                return True
    except Exception:  # noqa: BLE001
        pass
    return False


def ensure_personal_available() -> "tuple[bool, str]":
    """Make the user's real Chrome reachable over CDP so 'personal' can attach.

    If the debug port is already up (they started Chrome with it, or a previous run
    did), use it. Otherwise launch their Chrome on their own profile WITH the port —
    but only when no ordinary Chrome is already open (a second chrome.exe just
    delegates to the running instance and never exposes the port). Returns
    (ok, message); on False the caller falls back to JARVIS's own browser. Blocking
    (subprocess + HTTP) — call it OFF the event loop."""
    if _cdp_reachable():
        return True, ""
    exe = _find_chrome_exe()
    if not exe:
        return False, "I couldn't find Google Chrome on this PC, sir."
    if _chrome_running():
        return False, ("Your Chrome is already open without remote control, sir — close "
                       "it and ask again, or I'll just use my own browser.")
    prof = _user_chrome_profile_dir()
    try:
        import subprocess
        subprocess.Popen(
            [exe, f"--remote-debugging-port={_PERSONAL_CDP_PORT}",
             f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check"],
            close_fds=True,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't start your Chrome, sir: {exc}"
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if _cdp_reachable():
            return True, ""
        time.sleep(0.3)
    return False, "Your Chrome didn't expose remote control in time, sir."


def _connect_personal_cdp(pw):
    """Connect to the user's real Chrome over CDP and return its default context
    (their actual tabs/logins). Assumes ensure_personal_available() already made the
    port reachable. Raises on failure (the caller has already fallen back if needed)."""
    conn = pw.chromium.connect_over_cdp(_PERSONAL_CDP_URL, timeout=8000)
    return conn.contexts[0] if conn.contexts else conn.new_context()


def prewarm() -> None:
    """Start the Playwright DRIVER ahead of time (no browser window) so the first
    real open only pays the Chromium launch, not the ~1–2s driver spawn + import.

    Safe to call at startup from any thread: it spins up the worker and starts the
    Node driver in the background; it deliberately does NOT launch a browser (that
    would pop a visible window). Best-effort — never raises."""
    w = _get_worker()
    if w is None:
        return

    def job() -> tuple:
        try:
            if w._pw is None:
                from playwright.sync_api import sync_playwright
                w._pw = sync_playwright().start()
        except Exception:  # noqa: BLE001
            pass
        return (True, "")

    try:
        w._jobs.put(lambda: job())   # fire-and-forget on the worker thread
    except Exception:  # noqa: BLE001
        pass


def _profile_chromium_pids() -> "list[int]":
    """PIDs of Chromium processes using JARVIS's profile dir (best-effort)."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return []
    prof = _profile_dir().lower()
    pids: "list[int]" = []
    for proc in psutil.process_iter(["name", "cmdline", "pid"]):
        try:
            if "chrom" not in (proc.info.get("name") or "").lower():
                continue
            if prof in " ".join(proc.info.get("cmdline") or []).lower():
                pids.append(int(proc.info["pid"]))
        except Exception:  # noqa: BLE001
            continue
    return pids


def _force_kill_browser() -> bool:
    """Last-resort unwedge: kill the Chromium processes running OUR profile (and
    only ours — matched by the profile dir in the command line) so a blocked
    Playwright call errors out and the worker thread comes back. Best-effort."""
    pids = _profile_chromium_pids()
    if not pids:
        return False
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return False
    killed = False
    for pid in pids:
        try:
            psutil.Process(pid).kill()
            killed = True
        except Exception:  # noqa: BLE001
            continue
    if killed:
        print("[Browser] killed wedged Chromium (profile lock released)", flush=True)
    return killed


def _prepare_profile_for_launch() -> None:
    """Clear orphaned processes and stale lock files before a cold launch.

    A profile left locked by a crashed session — or Chromium still shutting down
    from a wedged call — makes ``launch_persistent_context`` open a window and
    die instantly with "Target page, context or browser has been closed"."""
    _force_kill_browser()
    deadline = time.monotonic() + 2.0
    while _profile_chromium_pids() and time.monotonic() < deadline:
        time.sleep(0.15)
    if _profile_chromium_pids():
        return
    prof = _profile_dir()
    for name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        try:
            os.remove(os.path.join(prof, name))
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001
            pass


def _archive_profile() -> bool:
    """Move the current profile aside so the next launch starts clean.

    Used when a profile was written by a different Chromium revision (Playwright
    upgrades change the bundled browser) and the old cookies/history make the
    new binary exit on startup."""
    prof = _profile_dir()
    try:
        if not os.path.isdir(prof):
            os.makedirs(prof, exist_ok=True)
            return False
        if not os.listdir(prof):
            return False
    except Exception:  # noqa: BLE001
        return False
    backup = prof + ".bak-" + time.strftime("%Y%m%d-%H%M%S")
    try:
        os.rename(prof, backup)
        os.makedirs(prof, exist_ok=True)
        print(f"[Browser] archived incompatible profile -> {backup}", flush=True)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[Browser] couldn't archive profile: {exc}", flush=True)
        return False


def _launch_persistent_context(pw):
    """Launch JARVIS's persistent Chromium context (worker-thread only)."""
    return pw.chromium.launch_persistent_context(
        user_data_dir=_profile_dir(),
        headless=False,
        viewport=None,
        ignore_default_args=["--enable-automation"],
        args=[
            "--no-first-run", "--no-default-browser-check", "--start-maximized",
            "--disable-blink-features=AutomationControlled",
            "--disable-background-networking",
            "--disable-component-update",
            "--disable-sync",
            "--disable-default-apps",
            "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding",
            "--disable-background-timer-throttling",
            "--disable-breakpad",
            "--metrics-recording-only",
            "--no-pings",
            "--disable-features=Translate,OptimizationHints,MediaRouter,"
            "InterestFeedContentSuggestions,CalculateNativeWinOcclusion",
        ],
    )


def _profile_last_chromium_exe() -> str:
    """The Chromium binary path recorded in the profile (Windows UTF-16LE)."""
    path = os.path.join(_profile_dir(), "Last Browser")
    if not os.path.isfile(path):
        return ""
    try:
        raw = open(path, "rb").read()  # noqa: SIM115
        if b"\x00" in raw[:4]:
            return raw.decode("utf-16le").strip("\x00 ").strip()
        return raw.decode("utf-8", errors="ignore").strip()
    except Exception:  # noqa: BLE001
        return ""


def _chromium_revision(exe_path: str) -> str:
    m = re.search(r"chromium[-_]?(\d+)", (exe_path or "").replace("\\", "/").lower())
    return m.group(1) if m else ""


def _profile_chromium_mismatch(pw) -> bool:
    """True when the profile was last opened by a different Playwright Chromium."""
    last = _profile_last_chromium_exe()
    try:
        cur = pw.chromium.executable_path or ""
    except Exception:  # noqa: BLE001
        cur = ""
    if not last or not cur:
        return False
    last_rev, cur_rev = _chromium_revision(last), _chromium_revision(cur)
    if last_rev and cur_rev:
        return last_rev != cur_rev
    return os.path.normcase(last) != os.path.normcase(cur)


def _launch_context_with_recovery(pw):
    """Try to launch; on instant-death failures, recover locks then profile."""
    _prepare_profile_for_launch()
    if _profile_chromium_mismatch(pw):
        print("[Browser] profile was last used by a different Chromium build — "
              "archiving before launch", flush=True)
        _archive_profile()
        _prepare_profile_for_launch()
    last_exc: "Optional[Exception]" = None
    archived = False
    for attempt in range(3):
        try:
            return _launch_persistent_context(pw)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if not _is_closed_err(exc):
                raise
            print(f"[Browser] launch failed (attempt {attempt + 1}): "
                  f"{str(exc).splitlines()[0]}", flush=True)
            _force_kill_browser()
            _prepare_profile_for_launch()
            if attempt == 0:
                time.sleep(0.5)
                continue
            if not archived and _archive_profile():
                archived = True
                _prepare_profile_for_launch()
                continue
            break
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("browser launch failed")


def _profile_dir() -> str:
    try:
        import storage
        root = str(storage.get_root())
    except Exception:  # noqa: BLE001
        root = os.path.expanduser("~/Jarvis")
    d = os.path.join(root, "browser-profile")
    try:
        os.makedirs(d, exist_ok=True)
    except Exception:  # noqa: BLE001
        pass
    return d


def _friendly(exc: Exception) -> str:
    s = str(exc)
    if "Executable doesn't exist" in s or "playwright install" in s:
        return _NEED_CHROMIUM
    if isinstance(exc, ImportError):
        return _UNAVAILABLE
    # Trim Playwright's giant multi-line errors to one helpful line.
    first = s.strip().splitlines()[0] if s.strip() else "unknown error"
    return f"The browser ran into a problem: {first}"


def _is_closed_err(exc: Exception) -> bool:
    s = str(exc).lower()
    return ("has been closed" in s or "target page" in s or "browser has been closed" in s
            or "context or browser" in s)


_SETTLE_MS = 1200
_DOM_QUIET_MS = 250

# Resolves once the DOM has gone `quiet` ms without a structural change, or at
# `cap` ms regardless. Attribute/text churn (progress bars, timers) is ignored.
_JS_DOM_QUIET = r"""
([quiet, cap]) => new Promise(resolve => {
  let timer = null, hard = null, obs = null;
  const finish = () => { if (obs) obs.disconnect(); clearTimeout(timer);
                         clearTimeout(hard); resolve(true); };
  obs = new MutationObserver(() => { clearTimeout(timer); timer = setTimeout(finish, quiet); });
  obs.observe(document.documentElement || document, {childList: true, subtree: true});
  timer = setTimeout(finish, quiet);
  hard = setTimeout(finish, cap);
})
"""


def _settle(page) -> None:
    """Give the page a brief moment to finish rendering after navigation, so a
    follow-up element LIST sees the real (often JS-rendered) page instead of a
    blank shell. `domcontentloaded` fires before SPAs paint; waiting for the
    network to go idle closes that race. Best-effort, short, never raises.

    Capped at 1.2s: ad/analytics/streaming pages never reach `networkidle`, so a
    3s cap meant every such navigation paid the full 3s. 1.2s still lets normal
    SPAs paint while keeping the worst case off the user's turn latency.

    Re-settling a document settled before (every autopilot observation after
    the first) returns at once if its network has gone idle since — as it always
    did — and otherwise waits for its DOM to stop changing, under the same cap,
    instead of a blind 1.2s on pages whose network never rests."""
    try:
        doc = page.evaluate("performance.timeOrigin")   # unique per document
    except Exception:  # noqa: BLE001 — mid-navigation: treat as a new document
        doc = None
    if doc is not None and getattr(page, "_jarvis_settled_doc", None) == doc:
        try:
            page.wait_for_load_state("networkidle", timeout=1)   # idle since → done
            return
        except Exception:  # noqa: BLE001 — still busy: watch the DOM instead
            pass
        try:
            page.evaluate(_JS_DOM_QUIET, [_DOM_QUIET_MS, _SETTLE_MS])
        except Exception:  # noqa: BLE001 — navigated away mid-wait: nothing to settle
            pass
        return
    try:
        page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
    except Exception:  # noqa: BLE001 — slow/streaming pages just skip the settle
        pass
    if doc is not None:
        page._jarvis_settled_doc = doc


# ── Direct search routing (skip the on-page search box) ───────────────────────
# Typing into a site's own search box is fragile — YouTube's Polymer masthead
# re-renders during hydration and eats the keystrokes. For the common case of
# "search SITE for X" we just navigate straight to the site's results URL, which
# is fast, deterministic, and immune to whatever the homepage is doing.
_SEARCH_ENGINES = {
    "youtube":    "https://www.youtube.com/results?search_query={q}",
    "google":     "https://www.google.com/search?q={q}",
    "bing":       "https://www.bing.com/search?q={q}",
    "duckduckgo": "https://duckduckgo.com/?q={q}",
    "ddg":        "https://duckduckgo.com/?q={q}",
    "amazon":     "https://www.amazon.com/s?k={q}",
    "reddit":     "https://www.reddit.com/search/?q={q}",
    "wikipedia":  "https://en.wikipedia.org/w/index.php?search={q}",
    "github":     "https://github.com/search?q={q}&type=repositories",
    "spotify":    "https://open.spotify.com/search/{q}",
}


def _engine_for_host(host: str) -> "Optional[str]":
    """Results-URL template for the engine serving `host`, or None."""
    host = (host or "").lower()
    for key, tmpl in _SEARCH_ENGINES.items():
        if key in host:
            return tmpl
    return None


def _search_results_url(site: "Optional[str]", current_url: str, query: str) -> str:
    """Build a direct results URL. Prefers an explicit `site`, then the site we're
    already on, then Google as the fallback."""
    qq = quote_plus(query.strip())
    if site:
        s = str(site).strip().lower()
        for key, tmpl in _SEARCH_ENGINES.items():
            if key == s or key in s:
                return tmpl.format(q=qq)
        if "." in s:   # a domain we don't have a search endpoint for → Google site:
            return "https://www.google.com/search?q=" + quote_plus(f"site:{s} {query.strip()}")
    tmpl = _engine_for_host(urlparse(current_url).netloc if current_url else "")
    if tmpl:
        return tmpl.format(q=qq)
    return "https://www.google.com/search?q=" + qq


# ── Robust click / type helpers ───────────────────────────────────────────────
# The recurring failure was a long single-strategy attempt that timed out (15s)
# when an element was found but not "actionable" (covered by an overlay, a custom
# widget, momentarily off-screen). These helpers try a SHORT ladder of strategies
# — normal → scroll-into-view → force → keyboard — so the common case is fast and
# the awkward case still succeeds instead of hanging then giving up.

def _landed(value: "Optional[str]", text: str) -> bool:
    """Did our text actually make it into the field? (Tolerant: a prefix match
    handles autocomplete that appends suggestions, trimming, etc.)"""
    if not value:
        return False
    v, t = str(value).strip().lower(), text.strip().lower()
    return bool(t) and (v == t or t[:12] in v)


def _focused_value(page) -> str:
    """The text content of whatever element currently has focus — used to VERIFY a
    keystroke actually landed (keys typed into a stale/blurred node land nowhere)."""
    try:
        return page.evaluate(
            "() => { const el = document.activeElement; if (!el) return '';"
            " if (el.value !== undefined && el.value !== null) return String(el.value);"
            " return el.isContentEditable ? (el.innerText || '') : ''; }"
        ) or ""
    except Exception:  # noqa: BLE001
        return ""


def _type_via_keyboard(page, text: str) -> bool:
    """Type into whatever element currently has focus. Clears it first so a search
    box with leftover text doesn't get our query appended."""
    try:
        page.keyboard.press("Control+a")
        page.keyboard.press("Delete")
    except Exception:  # noqa: BLE001 — clearing is best-effort
        pass
    try:
        page.keyboard.type(text, delay=8)
        return True
    except Exception:  # noqa: BLE001
        return False


def _type_best_box(page, text: str) -> bool:
    """Type into the page's main search/text box, surviving hydration re-renders.

    Heavy SPAs (YouTube/Polymer is the worst offender) swap out the masthead DOM
    *after* the page is interactive, which silently drops focus and discards a
    tagged node — so a one-shot "focus then type" lands the keystrokes nowhere and
    reports a false failure. We therefore re-find → focus → type → VERIFY in a
    short loop, retrying until the text is really in the box."""
    deadline = time.time() + 8
    while time.time() < deadline:
        try:
            found = page.evaluate(_JS_BEST_INPUT)   # scroll + focus + click + tag
        except Exception:  # noqa: BLE001
            found = False
        if found and _type_via_keyboard(page, text) and _landed(_focused_value(page), text):
            return True
        time.sleep(0.4)
    return False


def _type_into_locator(page, loc, text: str) -> bool:
    """Type into a specific element via a ladder of increasingly forceful tactics,
    verifying the text landed before declaring success."""
    try:
        loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:  # noqa: BLE001
        pass
    # 1) The fast path: fill() works for the vast majority of plain inputs.
    try:
        loc.fill(text, timeout=4000)
        try:
            if _landed(loc.input_value(timeout=1000), text):
                return True
        except Exception:  # noqa: BLE001 — contenteditable has no input_value(); trust fill
            return True
    except Exception:  # noqa: BLE001
        pass
    # 2) Force-focus through any overlay, then type with the keyboard + verify.
    try:
        loc.click(force=True, timeout=2500)
        if _type_via_keyboard(page, text) and _landed(_focused_value(page), text):
            return True
    except Exception:  # noqa: BLE001
        pass
    # 3) Last resort: just focus and type (works on contenteditable / custom).
    try:
        loc.focus(timeout=2500)
        if _type_via_keyboard(page, text):
            return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _resolve_target_input(page, tgt: "Optional[str]"):
    """Turn a target (element number, placeholder/label, or role name) into a
    locator, or None if nothing matches quickly."""
    if not tgt:
        return None
    if tgt.isdigit():
        return _locate_by_number(page, tgt)
    for getter in (
        lambda: page.get_by_placeholder(tgt, exact=False),
        lambda: page.get_by_label(tgt, exact=False),
        lambda: page.get_by_role("textbox", name=tgt),
        lambda: page.get_by_role("searchbox", name=tgt),
    ):
        try:
            cand = getter().first
            cand.wait_for(state="visible", timeout=2000)
            return cand
        except Exception:  # noqa: BLE001
            continue
    return None


def _click_locator(page, loc, label: str) -> "tuple[bool, str]":
    """Click a located element: scroll into view, normal click, then force-click.
    Retried briefly so a click that lands mid-navigation (element momentarily not
    actionable) gets a second chance instead of failing outright."""
    try:
        loc.scroll_into_view_if_needed(timeout=2000)
    except Exception:  # noqa: BLE001
        pass
    for attempt in range(2):
        try:
            loc.click(timeout=4000)
            return True, f"Clicked {label}."
        except Exception:  # noqa: BLE001
            pass
        try:
            loc.click(force=True, timeout=2500)
            return True, f"Clicked {label}."
        except Exception:  # noqa: BLE001
            if attempt == 0:
                time.sleep(0.5)
    return False, (f"I found {label} but couldn't click it, sir — something on the "
                   f"page may be blocking it.")


# ── Public actions (all return (ok, msg); list also returns the listing) ──────

def _normalize_nav_url(url: str) -> str:
    """Turn a spoken/typed target into a real URL (not a Google search when we
    know the destination — e.g. 'YouTube trending')."""
    u = (url or "").strip()
    if not u:
        return u
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", u):
        return u
    low = u.lower()
    if "youtube" in low and "trending" in low:
        return "https://www.youtube.com/feed/trending"
    if low in ("youtube", "youtube home", "youtube homepage"):
        return "https://www.youtube.com/"
    if " " not in u and "." in u:
        return "https://" + u
    return "https://www.google.com/search?q=" + quote_plus(u)


def open_url(url: str) -> "tuple[bool, str]":
    """Navigate the browser. A bare domain gets https://; a phrase becomes a
    Google search."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE
    u = (url or "").strip()
    if not u:
        return False, "Where should I go, sir?"
    u = _normalize_nav_url(u)

    def action(page):
        page.goto(u, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        _settle(page)
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = ""
        w._last_title = title or w._last_title
        return True, f"Opened {title or u}."
    # with_page() auto-applies the wide cold-launch budget on the first open.
    return w.with_page(action)


def open_blank() -> "tuple[bool, str]":
    """Open the browser window (used by the HUD toggle) at a friendly start page."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE

    def action(page):
        try:
            cur = page.url or ""
        except Exception:  # noqa: BLE001
            cur = ""
        if cur in ("", "about:blank", "chrome://newtab/"):
            page.goto("https://www.google.com", wait_until="domcontentloaded",
                      timeout=_NAV_TIMEOUT_MS)
        return True, "The browser is ready, sir."
    # with_page() auto-applies the wide cold-launch budget on the first open.
    return w.with_page(action)


def list_elements() -> "tuple[bool, str, str]":
    """READ-ONLY: enumerate the page's visible, clickable elements (number + text).

    This is the reliable alternative to pixel-vision: JARVIS sees the real,
    labelled controls (via injected JS) and then clicks one by its number or
    visible text. Returns (ok, message, listing) where listing is fed to the
    model so it can decide what to click next."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE, ""

    def scan(page):
        # Enumerate per frame (main page first, then iframes — consent dialogs
        # live there) with one CONTINUOUS numbering, so a number uniquely names
        # an element no matter which frame holds it.
        els, heading, rest = [], "", 0
        for fi, fr in enumerate(_frames(page)):
            try:
                res = fr.evaluate(_JS_LIST_ELEMENTS,
                                  {"start": len(els), "limit": _LIST_CAP}) or {}
            except Exception as exc:  # noqa: BLE001
                if fi == 0:
                    return els, heading, exc, rest
                continue   # a frame that won't evaluate (detached mid-walk) — skip
            if fi == 0:
                heading = res.get("heading") or ""
            els.extend(res.get("els") or [])
            rest += int(res.get("rest") or 0)
        return els, heading, None, rest

    def action(page):
        # A list often follows a click that triggered a navigation/re-render; give
        # the page the same brief settle navigation gets, so the listing reflects
        # the page the model is ABOUT to act on, not the one it just left.
        _settle(page)
        els, heading, err, rest = scan(page)
        if err is not None:
            # Almost always a navigation landing mid-scan (a submitted search, a
            # late JS redirect) — it destroys the context being read. Read the page
            # it landed on: an error line in place of the page cost a whole step,
            # and once made the completion check reject a correct answer.
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:  # noqa: BLE001
                pass
            _settle(page)
            els, heading, err, rest = scan(page)
            if err is not None:
                return False, f"I couldn't read the page: {_friendly(err)}", ""
        try:
            title = page.title()
        except Exception:  # noqa: BLE001
            title = ""
        if not els:
            return True, f"No interactive elements found on “{title}”.", ""
        # Drop ads/sponsored results so the operator never clicks a promoted "Watch"
        # thinking it's the first genuine result. Numbers stay stamped in the DOM, so
        # click-by-number is unaffected. Fallback: keep them if EVERYTHING looked like
        # an ad, so an over-eager match can never blank the listing.
        real = [e for e in els if not e.get("ad")]
        if real:
            els = real
        # On-screen elements first (what the user actually sees / the model usually
        # wants), then cap the list. This listing is fed back to the LLM on every
        # browser step, so keeping it tight directly cuts token usage (and Groq
        # rate-limit pressure) without losing the elements that matter.
        ordered = sorted(els, key=lambda e: 0 if e.get("onScreen") else 1)
        try:
            url = page.url or ""
        except Exception:  # noqa: BLE001
            url = ""
        # Tell the model WHERE it is: without the page header it can't tell a
        # results page from the homepage and clicks the wrong thing. The page's
        # main heading disambiguates further (titles often don't change on SPAs).
        lines = [f"Page: {title or '(untitled)'} — {url}"]
        if heading and heading.lower() not in (title or "").lower():
            lines.append(f"Heading: {heading}")
        for e in ordered[:_LIST_CAP]:
            kind = e.get("type") or e.get("tag")
            extras = []
            if e.get("href"):
                extras.append(f"→ {e['href']}")
            if e.get("value"):
                extras.append(f'value="{e["value"]}"')
            if e.get("state"):
                extras.append(e["state"])
            suffix = (" (" + ", ".join(extras) + ")") if extras else ""
            mark = "" if e.get("onScreen") else "  (off-screen)"
            lines.append(f"{e['n']}. {e['text']} [{kind}]{suffix}{mark}")
        # `rest`: candidates the scan never read (it stops once enough qualify),
        # so this is an upper bound — it only ever means "there's more below".
        more = max(0, len(ordered) - _LIST_CAP) + rest
        if more:
            lines.append(f"(+{more} more elements — scroll to see them)")
        listing = "\n".join(lines)
        # The full numbered listing is for the MODEL, not the user: return a
        # short human message so cards/TTS never carry the numbers.
        return True, f"Scanned “{title or url}” — {len(ordered) + rest} elements.", listing

    res = w.with_page(action)
    return res if len(res) == 3 else (res[0], res[1], "")


def click(target) -> "tuple[bool, str]":
    """Click an element by its number (from `list`) or by visible text."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE
    t = str(target if target is not None else "").strip()
    if not t:
        return False, "What should I click, sir?"

    def action(page):
        if t.isdigit():
            # Numbers are stamped by `list` (across ALL frames) and vanish on
            # navigation. Failing FAST with a precise message (instead of ~13s of
            # click-ladder timeouts and a misleading "couldn't click it") lets the
            # model immediately re-list and pick a fresh number.
            loc = _locate_by_number(page, t)
            if loc is None:
                return False, (f"Element {t} isn't on this page any more, sir — "
                               f"the page has changed since the last list. List "
                               f"the elements again and use a fresh number.")
            # Label the click with the element's visible text, not its number —
            # the number is internal; "Clicked 'lofi hip hop radio'" is what the
            # log/progress line should say.
            label = f"element {t}"
            try:
                txt = (loc.inner_text(timeout=400) or "").strip().replace("\n", " ")
                if not txt:
                    txt = (loc.get_attribute("aria-label", timeout=300) or "").strip()
                if txt:
                    label = f"“{txt[:48]}…”" if len(txt) > 48 else f"“{txt}”"
            except Exception:  # noqa: BLE001
                pass
            return _click_locator(page, loc, label)
        # By visible text, in whichever frame holds it (consent buttons live in
        # iframes). count()/is_visible() are immediate, so scanning frames × roles
        # stays fast — no per-getter wait like the old single-frame path.
        for fr in _frames(page):
            for getter in (
                lambda f=fr: f.get_by_role("button", name=t),
                lambda f=fr: f.get_by_role("link", name=t),
                lambda f=fr: f.get_by_role("tab", name=t),
                lambda f=fr: f.get_by_text(t, exact=False),
            ):
                try:
                    cand = getter().first
                    if cand.count() == 0 or not cand.is_visible():
                        continue
                except Exception:  # noqa: BLE001
                    continue
                return _click_locator(page, cand, f"“{t}”")
        return False, (f"I couldn't find “{t}” to click on the page, sir. Ask me to "
                       f"list the elements first.")
    return w.with_page(action)


def type_text(text: str, target=None, submit: bool = False) -> "tuple[bool, str]":
    """Type into a field. `target` may be an element number, a placeholder/label,
    or be omitted to use the page's main search/text box. `submit` presses Enter."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE
    text = "" if text is None else str(text)
    tgt = None if target is None else str(target).strip()

    def action(page):
        # Search fast-path: "type X and submit" with no specific field, on a site
        # we know how to query → go straight to its results URL. Far more reliable
        # than typing into a search box that the site re-renders under us.
        if submit and not tgt and text.strip():
            try:
                cur = page.url or ""
            except Exception:  # noqa: BLE001
                cur = ""
            tmpl = _engine_for_host(urlparse(cur).netloc if cur else "")
            if tmpl:
                page.goto(tmpl.format(q=quote_plus(text.strip())),
                          wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
                _settle(page)
                try:
                    w._last_title = page.title() or w._last_title
                except Exception:  # noqa: BLE001
                    pass
                preview = text if len(text) <= 60 else text[:57] + "…"
                return True, f"Searched for “{preview}”."

        loc = _resolve_target_input(page, tgt)
        if loc is not None:
            typed = _type_into_locator(page, loc, text)
        else:
            # No explicit target → type into the page's main text box, retrying
            # through hydration re-renders and verifying the text actually landed.
            typed = _type_best_box(page, text)
        if not typed:
            return False, ("I found the field but couldn't type into it, sir — a pop-up "
                           "may be in the way. Try closing it, or tell me which element "
                           "to use.")
        if submit:
            try:
                page.keyboard.press("Enter")
            except Exception:  # noqa: BLE001
                pass
        preview = text if len(text) <= 60 else text[:57] + "…"
        return True, (f"Typed “{preview}”" + (" and submitted." if submit else "."))
    return w.with_page(action)


def search(query: str, site: "Optional[str]" = None) -> "tuple[bool, str]":
    """Search directly via a site's results URL (reliable; skips the on-page search
    box). `site` may be a name (youtube/google/bing/…) or a domain; if omitted,
    searches the current site, else Google. Opens the browser if needed."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE
    q = (query or "").strip()
    if not q:
        return False, "What should I search for, sir?"

    def action(page):
        try:
            cur = page.url or ""
        except Exception:  # noqa: BLE001
            cur = ""
        url = _search_results_url(site, cur, q)
        page.goto(url, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS)
        _settle(page)
        try:
            w._last_title = page.title() or w._last_title
        except Exception:  # noqa: BLE001
            pass
        where = (str(site).strip() if site else (urlparse(cur).netloc or "the web"))
        preview = q if len(q) <= 60 else q[:57] + "…"
        return True, f"Searched {where} for “{preview}”."
    # Cold-launch budget applies automatically via with_page.
    return w.with_page(action)


def press(keys: str) -> "tuple[bool, str]":
    """Press a key / combo on the page, e.g. 'Enter', 'Tab', 'Control+a'."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE
    k = (keys or "").strip()
    if not k:
        return False, "Which key should I press, sir?"
    # Accept casual key names ('ctrl+s', 'esc', 'page down') as well as
    # Playwright's canonical ones ('Control+s', 'Escape', 'PageDown').
    _KEY_ALIASES = {
        "ctrl": "Control", "control": "Control", "alt": "Alt", "shift": "Shift",
        "cmd": "Meta", "win": "Meta", "meta": "Meta", "enter": "Enter",
        "return": "Enter", "esc": "Escape", "escape": "Escape", "tab": "Tab",
        "space": "Space", "spacebar": "Space", "backspace": "Backspace",
        "del": "Delete", "delete": "Delete", "home": "Home", "end": "End",
        "pageup": "PageUp", "pagedown": "PageDown", "page up": "PageUp",
        "page down": "PageDown", "up": "ArrowUp",
        "down": "ArrowDown", "left": "ArrowLeft", "right": "ArrowRight",
    }
    k = re.sub(r"\s*\+\s*", "+", k)
    k = "+".join(_KEY_ALIASES.get(part.lower(), part) for part in k.split("+"))

    def action(page):
        page.keyboard.press(k)
        return True, f"Pressed {k}."
    return w.with_page(action)


def scroll(amount) -> "tuple[bool, str]":
    """Scroll the page. Positive = down, negative = up (pixels)."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE
    try:
        dy = int(amount)
    except (TypeError, ValueError):
        dy = 600

    def action(page):
        page.evaluate("(dy) => window.scrollBy(0, dy)", dy)
        return True, f"Scrolled {'down' if dy >= 0 else 'up'}."
    return w.with_page(action)


def screenshot_b64(max_side: int = 1280) -> "tuple[bool, str]":
    """READ-ONLY: the CURRENT page as a base64 JPEG data URL (in memory,
    downscaled). On success the message IS the data URL.

    This is the autopilot's vision tie-breaker: when the DOM listing lacks what
    it needs (icon-only buttons, canvas, image-heavy pages), it looks at the
    page with the vision model. Captures ONLY JARVIS's own Playwright page —
    never the desktop — and never launches a browser: with none open there is
    nothing to look at, so we fail fast instead of popping a window. Follows the
    active target, so a personal-Chrome task sees the page it is acting on."""
    w = _active_worker()
    if w is None or w._ctx is None:
        return False, "The browser isn't open, sir — there's no page to look at."

    def job() -> tuple:
        import base64
        import io
        page = w._page
        if page is None or page.is_closed():
            pages = [p for p in w._ctx.pages if not p.is_closed()] if w._ctx else []
            if not pages:
                return False, "The browser isn't on a page, sir."
            page = pages[-1]
        # CSS pixels, not device pixels: on a 150%-scaled display the default
        # captured 1.5x the pixels only for Pillow to shrink them straight back
        # down — ~0.4s of every autopilot step.
        raw = page.screenshot(type="jpeg", quality=70, scale="css")
        # Downscale to keep vision-model tokens reasonable (same cap as the
        # desktop capture_screen_b64). If Pillow is missing, send it as-is.
        try:
            from PIL import Image  # type: ignore
            img = Image.open(io.BytesIO(raw))
            if max(img.size) > max_side:
                img.thumbnail((max_side, max_side))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=70)
                raw = buf.getvalue()
        except Exception:  # noqa: BLE001
            pass
        return True, "data:image/jpeg;base64," + base64.b64encode(raw).decode()

    return w.submit(job, 30)


def aria_snapshot(max_chars: int = 4000) -> "tuple[bool, str]":
    """READ-ONLY: Playwright's YAML accessibility snapshot of the current page —
    an ALTERNATE view of the same content for when the numbered element list is
    stuck or empty (it reads roles/names the CSS walker can miss). On success the
    message IS the snapshot text. Never launches a browser: with none open there
    is nothing to read, so fail fast instead of popping a window."""
    w = _active_worker()
    if w is None or w._ctx is None:
        return False, "The browser isn't open, sir — there's no page to read."

    def job() -> tuple:
        page = w._page
        if page is None or page.is_closed():
            pages = [p for p in w._ctx.pages if not p.is_closed()] if w._ctx else []
            if not pages:
                return False, "The browser isn't on a page, sir."
            page = pages[-1]
        try:
            snap = page.locator("body").aria_snapshot()
        except Exception as exc:  # noqa: BLE001 — old Playwright lacks aria_snapshot
            return False, _friendly(exc)
        snap = (snap or "").strip()
        if not snap:
            return False, "The page has no accessibility tree to read, sir."
        return True, snap[:max_chars]

    return w.submit(job, 20)


def read_page() -> "tuple[bool, str]":
    """Return the page's visible text (for 'read/summarise this page')."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE

    def action(page):
        try:
            txt = page.evaluate(_JS_PAGE_TEXT) or ""
        except Exception as exc:  # noqa: BLE001
            return False, f"I couldn't read the page: {_friendly(exc)}"
        return (True, txt) if txt.strip() else (True, "")
    return w.with_page(action)


def current_page() -> "tuple[bool, str]":
    """The current page's title and URL."""
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE

    def action(page):
        try:
            title, url = page.title(), page.url
        except Exception:  # noqa: BLE001
            title, url = "", ""
        w._last_title = title or w._last_title
        if not url or url == "about:blank":
            return True, "The browser isn't on a page yet, sir."
        return True, f"You're on “{title}” — {url}"
    return w.with_page(action)


def go_back() -> "tuple[bool, str]":
    w = _get_worker()
    if w is None:
        return False, _UNAVAILABLE

    def action(page):
        # go_back returns None both for "no history" and for an in-page (pushState)
        # back, so only an unchanged URL proves nothing happened.
        before = page.url
        if (page.go_back(wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS) is None
                and page.url == before):
            return False, "There's no previous page to go back to, sir."
        _settle(page)
        return True, "Went back, sir."
    return w.with_page(action)


def close() -> "tuple[bool, str]":
    """Close JARVIS's browser window."""
    global _worker
    revoke()
    with _worker_lock:
        w = _worker
    if w is None:
        return True, "The browser is already closed, sir."
    return w.close_browser()


def _coord_scale(hwnd: int) -> float:
    """Physical-pixels-per-logical-pixel for the coordinate space our move/resize
    calls operate in.

    When this process is DPI-UNAWARE, Windows virtualizes coordinates —
    SPI_GETWORKAREA and pygetwindow already report logical px, so the scale is 1.
    When it's DPI-AWARE they report PHYSICAL px, so the GUI's logical panel width
    (a Tauri LogicalSize, i.e. 400 → 400×scale physical) must be multiplied by the
    monitor's real DPI scale; otherwise we reserve too little room and Chromium
    slides under the panel — the "Chrome leaks into the panel" misalignment on
    HiDPI displays. Best-effort; any failure falls back to 1.0 (old behaviour)."""
    import ctypes
    try:
        user32 = ctypes.windll.user32
        user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        ctx = user32.GetThreadDpiAwarenessContext()
        awareness = user32.GetAwarenessFromDpiAwarenessContext(ctx)
    except Exception:  # noqa: BLE001 — pre-1607 Windows: assume unaware (logical coords)
        return 1.0
    if awareness in (0, -1):          # DPI_AWARENESS_UNAWARE / _INVALID → virtualized
        return 1.0
    try:
        dpi = (hwnd and user32.GetDpiForWindow(hwnd)) or user32.GetDpiForSystem()
        return (dpi or 96) / 96.0
    except Exception:  # noqa: BLE001
        return 1.0


def dock_browser_window(panel_width: "Optional[int]" = None) -> None:
    """Resize/move JARVIS's Chromium to the left so the companion panel fits beside it.

    Best-effort on Windows only; never raises."""
    global _last_panel_w
    if os.name != "nt":
        return
    try:
        w_panel = int(panel_width if panel_width is not None else _last_panel_w)
    except (TypeError, ValueError):
        w_panel = _PANEL_DEFAULT_W
    w_panel = max(280, min(w_panel, 640))
    _last_panel_w = w_panel
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        rect = wintypes.RECT()
        user32.SystemParametersInfoW(48, 0, ctypes.byref(rect), 0)
        work_l, work_t = rect.left, rect.top
        work_r, work_b = rect.right, rect.bottom
    except Exception:  # noqa: BLE001
        return
    title_hint = (_last_state.get("title") or "").strip()
    try:
        import pygetwindow as gw
    except Exception:  # noqa: BLE001
        return
    best = None
    best_score = 0
    for win in gw.getAllWindows():
        wt = (win.title or "").strip()
        if not wt or len(wt) < 2:
            continue
        norm = wt.replace(".", "").replace(" ", "").lower()
        if "jarvis" in norm and "chrome" not in norm and "chromium" not in norm:
            continue
        score = 0
        if title_hint and title_hint.lower() in wt.lower():
            score += 5
        low = wt.lower()
        if "chromium" in low:
            score += 3
        elif "chrome" in low:
            score += 2
        if win.width >= 400 and win.height >= 300:
            score += 1
        if score > best_score:
            best_score = score
            best = win
    if best is None or best_score < 2:
        return
    try:
        # Reserve the panel's PHYSICAL width. The GUI sizes it in logical px (Tauri
        # LogicalSize), but our work-area + move/resize run in physical px when this
        # process is DPI-aware — so a flat logical reserve leaves Chromium too wide
        # and it overlaps the panel on HiDPI screens. Scale by the panel's monitor.
        scale = _coord_scale(int(getattr(best, "_hWnd", 0) or 0))
        panel_px = int(round(w_panel * scale))
        browser_w = max(640, work_r - work_l - panel_px)
        browser_h = work_b - work_t
        if best.isMinimized:
            best.restore()
        best.moveTo(work_l, work_t)
        best.resizeTo(browser_w, browser_h)
    except Exception:  # noqa: BLE001
        pass
