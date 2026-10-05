"""Spotify integration.

Two tiers, both fail-soft:
  • Full control via the Spotify Web API (spotipy) when API keys are configured —
    play a SPECIFIC song, see what's playing, set volume, transfer playback.
  • Media-key fallback (play/pause/next/previous) that works on the focused
    Spotify window with no keys at all.

To enable full control, add your Spotify keys in the app's setup/Settings (they
are stored in app-data secrets.json as spotify_client_id /
spotify_client_secret / spotify_redirect_uri), then run once to authorise (a
browser window opens). Get keys at https://developer.spotify.com/dashboard.

NOTE: Spotify now REJECTS http://localhost redirect URIs — use the loopback IP
http://127.0.0.1:<port> instead (add the exact same URI in your app's dashboard
settings). We use the Authorization Code flow, which Spotify permits because the
client secret lives only in this backend, never in client-side code.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

import app_secrets
import jarvis_paths

from .. import app_launcher
_SCOPE = "user-read-playback-state user-modify-playback-state"
_sp = None            # cached spotipy client
_sp_creds = None      # (cid, secret, redirect) the cached client was built from


def _creds() -> "tuple[str, str, str]":
    """Read Spotify credentials from the app-data secret store."""
    return (
        app_secrets.get("spotify_client_id"),
        app_secrets.get("spotify_client_secret"),
        # Spotify rejects http://localhost — must be the loopback IP 127.0.0.1.
        app_secrets.get("spotify_redirect_uri") or "http://127.0.0.1:8888/callback",
    )


def _client():
    """Return an authorised spotipy client, or None if not configured/available.

    The client is rebuilt whenever the configured credentials change, so adding
    Spotify keys in Settings takes effect WITHOUT an app restart (the old code
    latched a permanent ``None`` after one credential-less attempt). Building the
    client is cheap and does no network I/O (the OAuth token is fetched lazily on
    the first API call), so re-attempting per command when creds are present but
    auth keeps failing is harmless."""
    global _sp, _sp_creds
    cid, secret, redirect = _creds()
    if not (cid and secret):
        _sp, _sp_creds = None, None
        return None
    if _sp is not None and _sp_creds == (cid, secret, redirect):
        return _sp
    try:
        import spotipy  # type: ignore
        from spotipy.oauth2 import SpotifyOAuth  # type: ignore
        cache = str(jarvis_paths.app_data_dir() / ".spotify_cache")
        _sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
            client_id=cid, client_secret=secret, redirect_uri=redirect,
            scope=_SCOPE, cache_path=cache, open_browser=True,
        ))
        _sp_creds = (cid, secret, redirect)
        return _sp
    except Exception:
        _sp, _sp_creds = None, None
        return None


def _ensure_app_running() -> None:
    """Best-effort: make sure the desktop app is open (API needs an active device)."""
    try:
        app_launcher.open_app("spotify")
    except Exception:
        pass


def _api_or_key(api_call, ok_msg: str, media_key: str) -> Tuple[bool, str]:
    """Try a Spotify Web API call (when authorised); fall back to the OS media
    key on the focused Spotify window. `api_call` is the bound method, or None
    when there's no client."""
    if api_call is not None:
        try:
            api_call()
            return True, ok_msg
        except Exception:  # noqa: BLE001
            pass
    return app_launcher.system_command(media_key)


def handle(command: str, spec: dict) -> Tuple[bool, str]:
    cmd = (command or "").strip().lower()
    query = (spec.get("query") or spec.get("song") or spec.get("track") or spec.get("text") or "").strip()
    sp = _client()

    # ── Play a specific song (needs the Web API) ──────────────────────────────
    if cmd in ("play", "play_song", "start") and query:
        if sp is None:
            return False, ("To play a specific song I need Spotify API keys "
                           "(add them in Settings). "
                           "For now I can only play, pause, and skip.")
        try:
            _ensure_app_running()
            res = sp.search(q=query, type="track", limit=1)
            items = res.get("tracks", {}).get("items", [])
            if not items:
                return False, f"I couldn't find “{query}” on Spotify."
            track = items[0]
            sp.start_playback(uris=[track["uri"]])
            return True, f"Playing {track['name']} by {track['artists'][0]['name']}."
        except Exception as exc:  # noqa: BLE001
            return False, f"Spotify wouldn't play that ({exc}). Is the app open and active?"

    # ── Transport controls (API if available, else media keys) ────────────────
    if cmd in ("play", "resume", "start"):
        return _api_or_key(sp and sp.start_playback, "Resuming Spotify.", "play_pause")

    if cmd in ("pause", "stop"):
        return _api_or_key(sp and sp.pause_playback, "Paused Spotify.", "play_pause")

    if cmd in ("next", "skip", "next_track"):
        return _api_or_key(sp and sp.next_track, "Skipped to the next track.", "next")

    if cmd in ("previous", "prev", "back", "previous_track"):
        return _api_or_key(sp and sp.previous_track, "Back to the previous track.", "previous")

    # ── Volume (Web API only) ─────────────────────────────────────────────────
    if cmd in ("volume", "set_volume"):
        level = spec.get("level", spec.get("value"))
        if sp is None:
            return False, "Setting Spotify's own volume needs the Spotify API keys."
        try:
            pct = max(0, min(100, int(float(level))))
            sp.volume(pct); return True, f"Spotify volume set to {pct}%."
        except Exception as exc:  # noqa: BLE001
            return False, f"Couldn't set the volume: {exc}"

    # ── What's playing (Web API only) ─────────────────────────────────────────
    if cmd in ("current", "now_playing", "whats_playing", "what", "nowplaying"):
        if sp is None:
            return False, "Reading what's playing needs the Spotify API keys."
        try:
            cur = sp.current_playback()
            if not cur or not cur.get("item"):
                return True, "Nothing's playing on Spotify right now."
            it = cur["item"]
            return True, f"Now playing {it['name']} by {it['artists'][0]['name']}."
        except Exception as exc:  # noqa: BLE001
            return False, f"Couldn't read what's playing: {exc}"

    if cmd in ("open", "launch"):
        return app_launcher.open_app("spotify")

    return False, (f"I don't know the Spotify command “{cmd}”. Try play, pause, "
                   "next, previous, or play a specific song.")
