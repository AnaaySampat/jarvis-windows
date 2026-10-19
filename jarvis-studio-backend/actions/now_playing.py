"""What's playing on this PC, so "send this to the TV" can carry on there.

Two halves, read separately because Windows keeps them apart:

- **What and where in it**: Windows' media controls (the panel behind the volume
  flyout, SMTC) know the playing app, the title and the position. Read through the
  `winrt` packages; without them only the link is found and nothing is paused.
- **Which video**: the address bar of the browser window showing it (UI Automation),
  or the page JARVIS's own browser is on.

`parse_link` and `pick` are pure, so the tests drive them without Windows.
"""

from __future__ import annotations

import asyncio
import datetime as _dt
import re
import urllib.parse

# ── Links ─────────────────────────────────────────────────────────────────────

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def _seconds(raw: str) -> int:
    """YouTube's t= / start=: "90", "90s", "1m30s", "1h2m3s"."""
    raw = (raw or "").strip().lower()
    if raw.isdigit():
        return int(raw)
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", raw)
    if not m or not any(m.groups()):
        return 0
    h, mi, s = (int(g or 0) for g in m.groups())
    return h * 3600 + mi * 60 + s


def parse_link(url: str) -> "dict | None":
    """A YouTube or Netflix link → {"service": "youtube", "id", "t"} /
    {"service": "netflix", "kind": "watch"|"title", "id"}; None for anything else.
    Accepts what an address bar shows (often no https://)."""
    raw = (url or "").strip()
    if not raw:
        return None
    if "://" not in raw:
        raw = "https://" + raw
    try:
        u = urllib.parse.urlsplit(raw)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    q = urllib.parse.parse_qs(u.query)
    parts = [p for p in u.path.split("/") if p]
    if host in ("youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"):
        vid = ""
        if host == "youtu.be" and parts:
            vid = parts[0]
        elif parts[:1] == ["watch"]:
            vid = (q.get("v") or [""])[0]
        elif len(parts) >= 2 and parts[0] in ("shorts", "live", "embed"):
            vid = parts[1]
        if not _YT_ID.match(vid):
            return None
        t = (q.get("t") or q.get("start") or [""])[0]
        return {"service": "youtube", "id": vid, "t": _seconds(t)}
    if host == "netflix.com":
        if len(parts) >= 2 and parts[0] in ("watch", "title") and parts[1].isdigit():
            return {"service": "netflix", "kind": parts[0], "id": parts[1]}
        jbv = (q.get("jbv") or [""])[0]          # a title opened over the browse page
        if jbv.isdigit():
            return {"service": "netflix", "kind": "title", "id": jbv}
    return None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def pick(title: str, windows: "list[dict]") -> "dict | None":
    """The browser window showing what's playing: of those whose page is a YouTube or
    Netflix link, the one whose title names the playing title. A YouTube tab always
    does, so one that doesn't is another video; Netflix's tabs are all just "Netflix",
    so there the only Netflix page will do. With no title known, the only page.
    `windows` = [{"title", "url"}]. Returns {"title", "url", "link"} or None."""
    found = [{**w, "link": parse_link(w.get("url", ""))} for w in windows]
    found = [w for w in found if w["link"]]
    want = _norm(title)
    if want:
        named = [w for w in found if want in _norm(w.get("title", ""))]
        if named:
            return named[0]
        found = [w for w in found if w["link"]["service"] == "netflix"]
    return found[0] if len(found) == 1 else None


def position_now(position_s: float, updated: "_dt.datetime | None", playing: bool,
                 now: "_dt.datetime | None" = None) -> int:
    """Where playback is now. Browsers report the position only when it changes
    state, so while it plays, the time since then is added on."""
    pos = max(float(position_s or 0), 0.0)
    if playing and updated is not None:
        now = now or _dt.datetime.now(_dt.timezone.utc)
        pos += max((now - updated).total_seconds(), 0.0)
    return int(pos)


# ── Windows' media controls (SMTC) ───────────────────────────────────────────

_PLAYING = 4          # GlobalSystemMediaTransportControlsSessionPlaybackStatus.PLAYING


def _manager():
    try:
        from winrt.windows.media.control import (  # type: ignore
            GlobalSystemMediaTransportControlsSessionManager as M)
    except ImportError:
        try:
            from winsdk.windows.media.control import (  # type: ignore
                GlobalSystemMediaTransportControlsSessionManager as M)
        except ImportError:
            return None
    return M


def _run(coro):
    """Run a WinRT await from an action's worker thread (no event loop of its own)."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


async def _sessions() -> "list[dict]":
    M = _manager()
    if M is None:
        return []
    mgr = await M.request_async()
    out = []
    for s in mgr.get_sessions():
        try:
            props = await s.try_get_media_properties_async()
            tl = s.get_timeline_properties()
            playing = int(s.get_playback_info().playback_status) == _PLAYING
            pos = tl.position.total_seconds() if tl.position else 0.0
            out.append({"app": str(s.source_app_user_model_id or ""),
                        "title": str(props.title or ""), "artist": str(props.artist or ""),
                        "playing": playing,
                        "position": position_now(pos, tl.last_updated_time, playing),
                        "session": s})
        except Exception:  # noqa: BLE001 — one odd app mustn't hide the rest
            continue
    return out


def media_available() -> bool:
    return _manager() is not None


def playing_on_pc() -> "dict | None":
    """The session that's playing — a browser's first — as {"app", "title", "artist",
    "playing", "position", "session"}; None when nothing plays (or no winrt)."""
    try:
        sessions = _run(_sessions())
    except Exception as exc:  # noqa: BLE001
        print(f"[NowPlaying] media sessions unreadable: {exc}")
        return None
    playing = [s for s in sessions if s["playing"]]
    browsers = [s for s in playing if _BROWSER_APP.search(s["app"])]
    return (browsers or playing or [None])[0]


def pause(found: dict) -> bool:
    """Pause that session. True only when Windows says the pause was accepted."""
    session = (found or {}).get("session")
    if session is None:
        return False
    try:
        return bool(_run(session.try_pause_async()))
    except Exception:  # noqa: BLE001
        return False


_BROWSER_APP = re.compile(r"chrome|msedge|edge|brave|opera|vivaldi|firefox|chromium",
                          re.IGNORECASE)


def is_browser(app: str) -> bool:
    return bool(_BROWSER_APP.search(app or ""))

# ── Which page: the browsers' address bars ────────────────────────────────────

# Chrome, Edge, Brave, Opera and Vivaldi share this window class; Firefox has its own.
_BROWSER_CLASSES = ("Chrome_WidgetWin_1", "MozillaWindowClass")


def browser_windows() -> "list[dict]":
    """[{"title", "url"}] for every browser window's active tab, plus JARVIS's own
    browser page. Address bars are read with UI Automation."""
    out = []
    try:
        from actions import browser
        st = browser.state()
        if st.get("open") and st.get("url"):
            out.append({"title": st.get("title", ""), "url": st["url"]})
    except Exception:  # noqa: BLE001
        pass
    try:
        import uiautomation as auto
    except ImportError:
        return out
    try:
        with auto.UIAutomationInitializerInThread(debug=False):
            for w in auto.GetRootControl().GetChildren():
                if w.ClassName not in _BROWSER_CLASSES or not w.Name:
                    continue
                bar = w.EditControl(searchDepth=12,
                                    RegexName=r"(?i)address|search with|location")
                if not bar.Exists(0.3, 0.1):
                    continue
                try:
                    url = bar.GetValuePattern().Value
                except Exception:  # noqa: BLE001
                    continue
                if url:
                    out.append({"title": w.Name, "url": url})
    except Exception as exc:  # noqa: BLE001
        print(f"[NowPlaying] address bars unreadable: {exc}")
    return out
