"""The user's Android TV, driven over network ADB: the TV's own "USB debugging" on the
home Wi-Fi (port 5555), with this PC as the ADB client, so nothing is installed on the
TV. The first connection makes the TV ask "Allow USB debugging?"; the user accepts once
(ticking "Always allow") and the TV remembers this PC's key.

Port of the Android app's remote/tv.ts + TvAdb.kt. Every action reads the TV's state
back before claiming it worked: the focused window for app launches, AudioService for
volume/mute, PowerManager for power, and the app's media session for playback. A
remote-button press has no readback and is reported as sent, never as done. What was
measured on the user's Sony KD-55X8500D (Android TV 8) is in aura-android's
docs/STATUS.md "TV control"; the traps are repeated below where they bite.

Needs `pip install adb-shell` (lazy-imported; without it only TV control is off).
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import re
import socket
import struct
import threading
import time
import unicodedata
import urllib.parse

import jarvis_paths
from actions import wol

# ── Setup (the TV's address) ──────────────────────────────────────────────────

_CFG_PATH = jarvis_paths.app_data_dir() / "tv.json"
# App-data, never the repo: this key is what the TV trusts.
_KEY_PATH = jarvis_paths.app_data_dir() / "tv_adbkey"


def config() -> "dict | None":
    """{"host", "model", "mac"} once a TV has answered, else None. `host` may list
    several addresses for the same TV, comma-separated (see _targets); `mac` is for
    waking it from off (actions/wol.py), "" until known."""
    try:
        cfg = json.loads(_CFG_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — missing or corrupt = no TV
        return None
    host = str(cfg.get("host") or "")
    return {"host": host, "model": str(cfg.get("model") or "")[:80],
            "mac": wol.norm_mac(cfg.get("mac"))} if _targets(host) else None


def _save(cfg: dict) -> None:
    _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CFG_PATH.write_text(json.dumps(cfg), encoding="utf-8")


def _read_mac(host: str) -> str:
    """The TV's own MAC (for Wake-on-LAN), read over ADB while it's reachable."""
    ok, out, _ = shell(host, "ip -o link")
    return wol.mac_from_ip_link(out) if ok else ""


def set_mac(mac: str) -> "tuple[bool, str]":
    """Settings → Devices → TV: the MAC typed in by hand (the TV's network settings
    show it as the MAC address)."""
    cfg = config()
    if not cfg:
        return False, "Set up the TV first."
    clean = wol.norm_mac(mac)
    if mac and not clean:
        return False, "That isn't a MAC address — it looks like 12:34:56:78:9A:BC."
    _save({**cfg, "mac": clean})
    return True, "Saved the TV's MAC address." if clean else "Forgot the TV's MAC address."


def configured() -> bool:
    return config() is not None


def pair(host: str) -> "tuple[bool, str]":
    """Saved only once the TV actually answers, which on the first connection means
    the user accepted "Allow USB debugging?" on the TV."""
    host = ", ".join(f"{h}:{p}" if p != _DEFAULT_PORT else h for h, p in _targets(host))
    if not host:
        return False, "That isn't a TV address — use the IP in the TV's network settings."
    ok, out, summary = shell(host, "getprop ro.product.model")
    if not ok:
        return False, summary or "The TV didn't answer."
    model = out.strip()[:80]
    mac = _read_mac(host) or (config() or {}).get("mac", "")
    _save({"host": host, "model": model, "mac": mac})
    return True, f"Connected to {model or 'the TV'}."


def unpair() -> None:
    _drop()
    _CFG_PATH.unlink(missing_ok=True)


# ── Transport (TvAdb.kt) ──────────────────────────────────────────────────────

_DEFAULT_PORT = 5555
_CONNECT_TIMEOUT_S = 5.0
# Also how long the user has to accept the TV's first "Allow USB debugging?" prompt.
_AUTH_TIMEOUT_S = 30.0
_MAX_COMMAND = 4000
_TARGET = re.compile(r"^([A-Za-z0-9.-]{1,253})(?::(\d{1,5}))?$")

_lock = threading.Lock()
_dev = None
_dev_target = ""


def _parse_target(raw: str) -> "tuple[str, int] | None":
    m = _TARGET.match((raw or "").strip())
    if not m:
        return None
    port = int(m.group(2) or _DEFAULT_PORT)
    return (m.group(1), port) if 1 <= port <= 65535 else None


# The TV's addresses, tried in turn: its address on the home Wi-Fi, and optionally a
# second one — the TV's Tailscale address — for when this PC is on another router's
# network (a second router in router mode gives its Wi-Fi its own subnet, and nothing
# on the first router can open a connection into it). The one that answered last goes
# first, so a network change costs one connect timeout, once.
_last_good = ""


def _key(target: "tuple[str, int]") -> str:
    return f"{target[0]}:{target[1]}"


def _targets(host: str) -> "list[tuple[str, int]]":
    """Every valid address in `host` ("192.168.1.20, 100.101.1.2"), last good first."""
    out: "list[tuple[str, int]]" = []
    for part in re.split(r"[,;\s]+", host or ""):
        t = _parse_target(part)
        if t and t not in out:
            out.append(t)
    return sorted(out, key=lambda t: _key(t) != _last_good)


def _same_subnet_hint(target: "tuple[str, int]") -> str:
    """When the TV's LAN address isn't on this PC's network, say why it can't be
    reached and what fixes it. '' when that isn't the explanation."""
    try:
        tv = ipaddress.ip_address(target[0])
    except ValueError:
        return ""                          # a name: nothing to compare
    if not tv.is_private or tv in ipaddress.ip_network("100.64.0.0/10"):
        return ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect((target[0], target[1]))      # picks the route; sends nothing
            mine = ipaddress.ip_address(s.getsockname()[0])
    except OSError:
        return ""
    if ipaddress.ip_network(f"{mine}/24", strict=False) == \
            ipaddress.ip_network(f"{tv}/24", strict=False):
        return ""
    return (f" This PC is on {mine} and the TV is at {tv}: different networks, which is "
            "what happens when a second router (a TP-Link connected by cable, say) runs its "
            "own Wi-Fi in router mode. Set that router to Access Point mode so every Wi-Fi "
            "is one network, or install Tailscale on the TV and add its 100.x address "
            "after a comma in Settings → Devices.")


def _connection(target: "tuple[str, int]"):
    global _dev, _dev_target
    key = f"{target[0]}:{target[1]}"
    if _dev is not None and _dev_target == key:
        return _dev
    _drop()
    from adb_shell.adb_device import AdbDeviceTcp
    from adb_shell.auth.keygen import keygen
    from adb_shell.auth.sign_cryptography import CryptographySigner
    if not _KEY_PATH.exists() or not _KEY_PATH.with_name("tv_adbkey.pub").exists():
        _KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        keygen(str(_KEY_PATH))
    dev = AdbDeviceTcp(target[0], target[1], default_transport_timeout_s=_CONNECT_TIMEOUT_S)
    dev.connect(rsa_keys=[CryptographySigner(str(_KEY_PATH))], auth_timeout_s=_AUTH_TIMEOUT_S)
    _dev, _dev_target = dev, key
    return dev


def _drop() -> None:
    global _dev, _dev_target
    try:
        if _dev is not None:
            _dev.close()
    except Exception:  # noqa: BLE001
        pass
    _dev, _dev_target = None, ""


def _describe(exc: "Exception | None", target: "tuple[str, int]") -> str:
    where = f"{target[0]}:{target[1]}"
    msg = str(exc or "")
    name = type(exc).__name__ if exc else ""
    if "unauthorized" in msg.lower() or "auth" in name.lower():
        return ("The TV didn't allow this PC. Accept \"Allow USB debugging?\" on the TV "
                "(tick Always allow), then try again.")
    if isinstance(exc, OSError) or "timeout" in name.lower():
        # Another router's subnet usually shows up as a timeout: the packets are dropped.
        hint = _same_subnet_hint(target)
        if hint:
            return f"I can't reach the TV at {where}." + hint
    if isinstance(exc, (TimeoutError,)) or "timeout" in name.lower():
        return (f"The TV at {where} didn't answer in time. If it's showing \"Allow USB "
                "debugging?\", accept it (tick Always allow) and try again.")
    if isinstance(exc, OSError):
        return (f"I can't reach the TV at {where}. Check it's on, on the same network as "
                "this PC, with USB debugging on.")
    return f"The TV connection failed: {msg or name or 'unknown error'}"


def _refused_us(exc: "Exception | None") -> bool:
    """The TV answered and said no: another address for it won't change that."""
    return "unauthorized" in str(exc or "").lower() or "auth" in type(exc).__name__.lower()


def _on_tv(host: str, command: str, run) -> "tuple[bool, bytes, str]":
    """Run `run(device)` → bytes on the TV's ADB connection: (ran, output, why-not)."""
    global _last_good
    targets = _targets(host)
    if not targets:
        return False, b"", "That isn't a valid TV address."
    if not command.strip() or len(command) > _MAX_COMMAND:
        return False, b"", "Refused an empty or oversized TV command."
    try:
        import adb_shell  # noqa: F401
    except ImportError:
        return False, b"", "TV control needs the adb-shell package — pip install adb-shell."
    with _lock:
        last, at = None, targets[0]
        for target in targets:
            for _ in range(2):
                reusing = _dev is not None and _dev_target == _key(target)
                try:
                    dev = _connection(target)
                except Exception as exc:  # noqa: BLE001 — not reachable this way
                    last, at = exc, target
                    _drop()
                    break
                try:
                    out = run(dev) or b""
                except Exception as exc:  # noqa: BLE001
                    last, at = exc, target
                    _drop()
                    # A TV that went to standby drops the socket without closing it, so
                    # the first command on a reused one fails: reconnect once. A command
                    # that failed on a fresh connection isn't sent down another path.
                    if reusing:
                        continue
                    return False, b"", _describe(exc, target)
                _last_good = _key(target)
                return True, out, ""
            if _refused_us(last):
                break
        return False, b"", _describe(last, at)


def shell(host: str, command: str) -> "tuple[bool, str, str]":
    """(ran, output, why-not). `ran` says the command reached the TV, nothing more."""
    ok, raw, why = _on_tv(host, command,
                          lambda d: d.shell(command, timeout_s=30, decode=False))
    return ok, raw.decode("utf-8", "replace"), why


def exec_out(host: str, command: str) -> "tuple[bool, bytes, str]":
    """Binary-safe output (a PNG from screencap)."""
    return _on_tv(host, command, lambda d: d.exec_out(command, timeout_s=30, decode=False))


def first_frame(host: str, size: str = "1280x720") -> "tuple[bool, bytes, str]":
    """One frame from `screenrecord --output-format=frames` (measured on the user's TV,
    2026-10-11): a 20-byte header — u32 LE length-after-this-field, width, height, bytes
    per row, format (3 = RGB888) — then top-down RGB rows. screenrecord mirrors the
    screen onto a virtual display, where Android draws copy-protected (secure) layers
    black instead of refusing the whole capture the way screencap does.

    It streams until its time limit, and the TV's toybox (0.7.3) has no `head -c` to
    cut it short, so after the first frame the connection is dropped: that closes the
    stream, and screenrecord exits on the broken pipe."""
    command = f"screenrecord --output-format=frames --size {size} --time-limit 3 -"

    def grab(dev) -> bytes:
        buf, need = bytearray(), 0
        stream = dev.streaming_shell(command, read_timeout_s=15, decode=False)
        try:
            for chunk in stream:
                buf += chunk
                if not need and len(buf) >= 4:
                    need = 4 + struct.unpack_from("<I", buf)[0]
                if need and len(buf) >= need:
                    break
        finally:
            stream.close()
            _drop()
        return bytes(buf[:need]) if need and len(buf) >= need else b""

    return _on_tv(host, command, grab)


# ── Web search for Netflix ids (grounded Gemini, which lives on the main loop) ─

_loop: "asyncio.AbstractEventLoop | None" = None


def attach_loop(loop: "asyncio.AbstractEventLoop") -> None:
    global _loop
    _loop = loop


def _web_search(query: str) -> "tuple[bool, str]":
    """Actions run on executor threads; the grounded search's HTTP client belongs to
    the main loop, so the coroutine is marshalled there."""
    loop = _loop
    if loop is None or not loop.is_running():
        return False, "web search isn't running"
    try:
        asyncio.get_running_loop()
        return False, "web search can't run from the event loop"   # would deadlock
    except RuntimeError:
        pass
    from llm import groq_bridge
    try:
        ok, text, _src = asyncio.run_coroutine_threadsafe(
            groq_bridge.web_answer(query), loop).result(timeout=60)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc) or "the search timed out"
    return ok, text


# ── The TV (tv.ts) ────────────────────────────────────────────────────────────

NETFLIX = "com.netflix.ninja"
YOUTUBE = "com.google.android.youtube.tv"

# Spoken app name (lower-case, letters and digits only) → package. Anything else is
# matched against the TV's installed package names.
_APPS = {
    "netflix": NETFLIX,
    "youtube": YOUTUBE,
    "primevideo": "com.amazon.amazonvideo.livingroom",
    "amazonprime": "com.amazon.amazonvideo.livingroom",
    "amazonprimevideo": "com.amazon.amazonvideo.livingroom",
    "prime": "com.amazon.amazonvideo.livingroom",
    "hotstar": "in.startv.hotstar",
    "jiohotstar": "in.startv.hotstar",
    "disneyhotstar": "in.startv.hotstar",
    "sonyliv": "com.sonyliv",
    "spotify": "com.spotify.tv.android",
    "playstore": "com.android.vending",
    "googleplay": "com.android.vending",
    "settings": "com.android.tv.settings",
}
_APP_NAMES = {
    NETFLIX: "Netflix",
    YOUTUBE: "YouTube",
    "com.amazon.amazonvideo.livingroom": "Prime Video",
    "in.startv.hotstar": "Hotstar",
    "com.sonyliv": "SonyLIV",
    "com.spotify.tv.android": "Spotify",
    "com.android.vending": "the Play Store",
    "com.android.tv.settings": "Settings",
    "com.google.android.tvlauncher": "the home screen",
    "com.google.android.leanbacklauncher": "the home screen",
}

# Remote buttons → Android keycodes.
_KEYS = {
    "up": 19, "down": 20, "left": 21, "right": 22,
    "ok": 23, "select": 23, "enter": 23,
    "back": 4, "home": 3,
    "play_pause": 85, "play": 126, "pause": 127, "stop": 86,
    "next": 87, "previous": 88, "fast_forward": 90, "rewind": 89,
    "channel_up": 166, "channel_down": 167,
    "input": 178, "settings": 176, "captions": 175,
}
_KEY_SLEEP = 223
_KEY_WAKEUP = 224
_KEY_MUTE = 164

_PACKAGE = re.compile(r"^[A-Za-z0-9_.]+$")


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def shell_quote(s: str) -> str:
    """POSIX single-quoting: the only safe way to put any text into a TV shell command."""
    return "'" + s.replace("'", "'\\''") + "'"


class TvUnreachable(Exception):
    pass


class Tv:
    def __init__(self, sh, wait=time.sleep, now=time.monotonic):
        self._shell = sh          # command → (ran, output, why-not)
        self.wait = wait
        self.now = now

    def sh(self, command: str) -> str:
        ok, out, why = self._shell(command)
        if not ok:
            raise TvUnreachable(why or "I couldn't reach the TV.")
        return out

    def keyevent(self, *codes: int) -> None:
        self.sh("input keyevent " + " ".join(str(c) for c in codes))

    def focused_package(self) -> str:
        """The package in front, or "" when the focused window isn't an app's."""
        out = self.sh("dumpsys window | grep mCurrentFocus")
        m = re.search(r"mCurrentFocus=Window\{\S+ \S+ ([A-Za-z0-9_.]+)/", out)
        return m.group(1) if m else ""

    def wait_for_focus(self, pkg: str, tries: int = 10) -> bool:
        for _ in range(tries):
            if self.focused_package() == pkg:
                return True
            self.wait(1)
        return False

    def resolve_app(self, name: str) -> "str | None":
        key = _alnum(name)
        if not key:
            return None
        if key in _APPS:
            return _APPS[key]
        pkgs = [ln.strip().removeprefix("package:").strip()
                for ln in self.sh("pm list packages").splitlines()]
        matches = sorted((p for p in pkgs if _PACKAGE.match(p) and key in _alnum(p)), key=len)
        return matches[0] if matches else None

    def open_app(self, name: str) -> "tuple[bool, str]":
        pkg = self.resolve_app(name)
        if not pkg:
            return False, f"There's no app called “{name}” on the TV."
        on, why = self.screen_on()
        if not on:
            return False, why
        label = _APP_NAMES.get(pkg, name.strip())
        # TV apps register under LEANBACK_LAUNCHER; a phone-style app only under LAUNCHER.
        out = self.sh(f"monkey -p {pkg} -c android.intent.category.LEANBACK_LAUNCHER 1")
        if re.search(r"No activities found", out, re.I):
            out = self.sh(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1")
            if re.search(r"No activities found", out, re.I):
                return False, f"{label} is installed on the TV but can't be opened."
        if self.wait_for_focus(pkg):
            return True, f"Opened {label} on the TV."
        now = self.focused_package()
        return False, (f"I asked the TV to open {label}, but it's showing "
                       f"{_APP_NAMES.get(now) or now or 'something else'} instead.")

    def sessions(self) -> "dict[str, tuple[int, int, int]]":
        """Every media session's (state, position, updated), by package. `updated` is
        when the app last reported its state (the TV's uptime, ms)."""
        found: "dict[str, tuple[int, int, int]]" = {}
        pkg = ""
        for line in self.sh("dumpsys media_session").splitlines():
            p = re.match(r"^\s*package=([A-Za-z0-9_.]+)", line)
            if p:
                pkg = p.group(1)
            s = re.search(r"state=PlaybackState \{state=(-?\d+), position=(-?\d+)", line)
            if s and pkg and pkg not in found:
                u = re.search(r"updated=(-?\d+)", line)
                found[pkg] = (int(s.group(1)), int(s.group(2)), int(u.group(1)) if u else 0)
        return found

    def playing_now(self, pkg: str, since: "tuple | None" = None
                    ) -> "tuple[bool, tuple | None, tuple | None]":
        """Playing = state 3 AND the position moving between two reads. State alone
        lies: Netflix reports state 3 at position 0 while "Who's watching?" is up.
        YouTube's TV app reports its position only when its state changes (2026-10-20:
        902099 for 95 s while playing), so for it state 3 in both reads is enough —
        but after a new link only once it has reported since (`since`, its session
        before the link): the last video's leftover state 3 isn't the new one playing."""
        before = self.sessions().get(pkg)
        if not before or before[0] != 3:
            return False, before, None
        self.wait(2.5)
        after = self.sessions().get(pkg)
        moved = bool(after) and (after[1] > before[1] + 500
                                 or (pkg == YOUTUBE and after != since))
        return bool(after and after[0] == 3 and moved), before, after

    def playback(self, what: str) -> "tuple[bool, str]":
        """Pause/resume by the app's reported state, not by key name: Netflix on the
        user's TV treats MEDIA_PLAY and MEDIA_PAUSE as the same toggle (pause twice =
        pause, then play), so a blind "pause" to something already paused starts it.
        Only when the state is known to differ does the toggle get pressed."""
        pkg = self.focused_package()
        label = _APP_NAMES.get(pkg, "the TV app")
        want = "paused" if what == "pause" else "playing"

        def is_wanted() -> bool:
            playing, before, _after = self.playing_now(pkg)
            return playing if what == "play" else (not playing and before is not None)

        if pkg not in self.sessions():
            self.keyevent(_KEYS["pause"] if what == "pause" else _KEYS["play"])
            return True, f"Sent {what} to the TV ({label} doesn't report whether it's playing)."
        if is_wanted():
            return True, f"{label} is already {want}."
        self.keyevent(_KEYS["play_pause"])
        self.wait(1)
        if is_wanted():
            return True, f"Paused {label}." if what == "pause" else f"{label} is playing."
        return False, f"I pressed {what}, but {label} isn't {want}."

    def netflix_play(self, title: str, lookup, episode_of, season: int = 0,
                     episode: int = 0) -> "tuple[bool, str]":
        """"Play <title>" on Netflix. Netflix ignores its own title links unless the
        intent carries the extra `source=30`; with it the link opens the title and
        Netflix autoplays it. On a cold start Netflix shows "Who's watching?" first and
        keeps the link across it, so one OK press on the highlighted (last-used)
        profile continues."""
        show = lookup(title)
        if "error" in show:
            return False, show["error"]
        # A named episode opens its own watch link; otherwise the show's page, where
        # Netflix resumes wherever the profile left off (what its own Play button does).
        path, name = f"title/{show['id']}", show["name"]
        if season or episode:
            season, episode = season or 1, episode or 1
            ep = episode_of(show["id"], season, episode)
            if "error" in ep:
                return False, ep["error"]
            path = f"watch/{ep['id']}"
            name = (f"{show['name']} season {season}, episode {episode}"
                    + (f" (“{ep['title']}”)" if ep.get("title") else ""))
        return self.netflix_open(path, name)

    def netflix_open(self, path: str, name: str) -> "tuple[bool, str]":
        """Open a Netflix `title/<id>` or `watch/<id>` link and confirm it plays."""
        on, why = self.screen_on()
        if not on:
            return False, why
        cold = not self.sh(f"pidof {NETFLIX}").strip()
        before = None if cold else self.playing_now(NETFLIX)
        sent_at = self.now()
        self.sh(f"am start -a android.intent.action.VIEW -d "
                f"{shell_quote('https://www.netflix.com/' + path)} "
                f"-f 0x30000000 -e source 30 -n {NETFLIX}/.MainActivity")
        pressed_ok = pressed_play = saw_stop = False
        elapsed = 0.0
        while elapsed < 45:
            self.wait(2.5)
            elapsed += 2.5
            s = self.sessions().get(NETFLIX)
            if not s or s[0] != 3:
                saw_stop = True
                # Nothing playing or loading 10 s on: Netflix sits on the show's page, its
                # Play button focused — it autoplays only a show the profile is partway
                # through (2026-10-20, "play The Big Bang Theory"). One OK plays it.
                if (not pressed_play and elapsed >= 10 and (not s or s[0] in (0, 1, 2))
                        and self.focused_package() == NETFLIX):
                    self.keyevent(_KEYS["ok"])
                    pressed_play = True
                continue
            # ponytail: the profile gate is recognised by its signature (state 3 stuck
            # at position 0) and only after a cold start; one OK picks the last profile.
            if cold and not pressed_ok and s[1] == 0 and elapsed >= 10:
                self.keyevent(_KEYS["ok"])
                pressed_ok = True
                continue
            if s[1] <= 0:
                continue
            playing, _b, after = self.playing_now(NETFLIX)
            if not playing or not after:
                continue
            # Netflix was already playing: only a stop or a jump in position shows the
            # link replaced what was on. The same show at the same spot can't be told apart.
            was_on = before is not None and before[0] and before[2]
            if was_on and not saw_stop:
                expected = before[2][1] + (self.now() - sent_at) * 1000
                if abs(after[1] - expected) < 15_000:
                    return False, f"Netflix is playing, but I couldn't confirm it switched to {name}."
            return True, f"Playing {name} on Netflix."
        return False, (f"I opened {name} on Netflix, but I couldn't confirm it started "
                       "playing — it may be waiting on the TV screen.")

    def cast(self, link: dict, start_s: int = 0, name: str = "") -> "tuple[bool, str]":
        """A link from now_playing.parse_link on the TV. YouTube starts at `start_s`
        (checked against the position the TV reports); Netflix resumes wherever the
        profile left off, which is where it was just paused on the PC or phone."""
        label = f"“{name}”" if name else "it"
        if link.get("service") == "netflix" and str(link.get("id", "")).isdigit():
            return self.netflix_open(f"{link.get('kind') or 'title'}/{link['id']}",
                                     label if name else "the title")
        vid = str(link.get("id", ""))
        if link.get("service") != "youtube" or not re.fullmatch(r"[A-Za-z0-9_-]{11}", vid):
            return False, "Only YouTube and Netflix links can be sent to the TV."
        start = max(int(start_s or 0), 0)
        url = f"https://www.youtube.com/watch?v={vid}" + (f"&t={start}s" if start >= 5 else "")
        on, why = self.screen_on()
        if not on:
            return False, why
        gate = self.youtube_gate(woke=bool(why))
        prior = self.sessions().get(YOUTUBE)
        link_cmd = f"am start -a android.intent.action.VIEW -d {shell_quote(url)} {YOUTUBE}"
        self.sh(link_cmd)
        if not self.wait_for_focus(YOUTUBE):
            return False, "I asked the TV to open YouTube, but it didn't come up."
        for i in range(8):
            self.wait(2)
            playing, _before, after = self.playing_now(YOUTUBE, since=prior)
            if not playing:
                if gate and i == 2:          # still nothing: past "Who's watching?", again
                    gate = False
                    self.keyevent(_KEYS["ok"])
                    self.wait(4)
                    self.sh(link_cmd)
                continue
            if start >= 5 and after and after[1] < (start - 10) * 1000:
                # Not "from the start": a TV already playing this video just carries on
                # where it was (seen 2026-10-20).
                return True, (f"Playing {label} on YouTube on the TV, but not from "
                              f"{_clock(start)}: it didn't take the start time.")
            return True, (f"Playing {label} on YouTube on the TV"
                          + (f" from {_clock(start)}." if start >= 5 else "."))
        return False, f"I opened {label} in YouTube on the TV, but couldn't confirm it started playing."

    def youtube_play(self, query: str) -> "tuple[bool, str]":
        """YouTube's own search link (verified on the TV), then OK on the top result."""
        q = query.strip()[:200]
        url = "https://www.youtube.com/results?search_query=" + urllib.parse.quote(q, safe="")
        on, why = self.screen_on()
        if not on:
            return False, why
        gate = self.youtube_gate(woke=bool(why))
        prior = self.sessions().get(YOUTUBE)
        search_cmd = f"am start -a android.intent.action.VIEW -d {shell_quote(url)} {YOUTUBE}"
        self.sh(search_cmd)
        if not self.wait_for_focus(YOUTUBE):
            return False, "I asked the TV to open YouTube, but it didn't come up."
        self.wait(5)   # results render before OK means "the top result"
        self.keyevent(_KEYS["ok"])
        for i in range(6):
            self.wait(2)
            if self.playing_now(YOUTUBE, since=prior)[0]:
                return True, f"Playing the top YouTube result for “{q}” on the TV."
            if gate and i == 1:              # that OK picked the account: search again
                gate = False
                self.sh(search_cmd)
                self.wait(5)
                self.keyevent(_KEYS["ok"])
        return False, (f"I opened YouTube's results for “{q}” on the TV, but couldn't "
                       "confirm a video started.")

    def volume(self) -> "tuple[int, int] | None":
        m = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]",
                      self.sh("media volume --stream 3 --get"))
        return (int(m.group(1)), int(m.group(3))) if m else None

    def set_volume(self, level) -> "tuple[bool, str]":
        cur = self.volume()
        if not cur:
            return False, "The TV didn't report its volume."
        if level is None:
            return False, "What volume should the TV be at?"
        want = max(0, min(cur[1], round(level)))
        self.sh(f"media volume --stream 3 --set {want}")
        now = self.volume()
        if now and now[0] != want:
            # Fall back to the remote's own volume keys for the difference.
            diff = want - now[0]
            self.keyevent(*([24 if diff > 0 else 25] * min(abs(diff), 50)))
            now = self.volume()
        if now and now[0] == want:
            return True, f"TV volume is {want}."
        return False, (f"I tried to set the TV volume to {want}, but it reports "
                       f"{now[0] if now else 'no level'}.")

    def volume_step(self, direction: int, step: int) -> "tuple[bool, str]":
        cur = self.volume()
        if not cur:
            return False, "The TV didn't report its volume."
        return self.set_volume(cur[0] + direction * abs(step))

    def muted(self) -> "bool | None":
        m = re.search(r"Muted:\s*(true|false)",
                      self.sh("dumpsys audio | grep -A1 -- '- STREAM_MUSIC:'"))
        return m.group(1) == "true" if m else None

    def toggle_mute(self) -> "tuple[bool, str]":
        before = self.muted()
        self.keyevent(_KEY_MUTE)
        self.wait(0.8)
        after = self.muted()
        if after is None:
            return True, "Sent mute to the TV (it doesn't report whether it's muted)."
        if after == before:
            return False, f"I sent mute, but the TV is still {'muted' if after else 'unmuted'}."
        return True, "The TV is muted." if after else "The TV is unmuted."

    def wakefulness(self) -> str:
        m = re.search(r"mWakefulness=(\w+)", self.sh("dumpsys power | grep mWakefulness="))
        return m.group(1) if m else ""

    def power(self, on: bool) -> "tuple[bool, str]":
        want = "Awake" if on else "Asleep"
        if self.wakefulness() == want:
            return True, f"The TV is already {'on' if on else 'off'}."
        self.keyevent(_KEY_WAKEUP if on else _KEY_SLEEP)
        for _ in range(6):
            self.wait(1)
            w = self.wakefulness()
            if w == want or (not on and w == "Dozing"):
                return True, f"Turned the TV {'on' if on else 'off'}."
        return False, f"I sent the TV the {'wake' if on else 'sleep'} key, but it didn't change."

    def youtube_gate(self, woke: bool) -> bool:
        """Whether YouTube will likely ask "Who's watching?" first: it does after the TV
        wakes and on a cold start, and swallows the link (2026-10-20). ADB can't see it
        (uiautomator finds nothing in YouTube's TV app), so cast/youtube_play press OK
        once if nothing starts — the highlighted, last-used account (the user's choice,
        like Netflix's profile gate) — and send the link again."""
        return woke or not self.sh(f"pidof {YOUTUBE}").strip()

    def screen_on(self) -> "tuple[bool, str]":
        """Before starting anything. A TV in standby with its network up answers ADB but
        shows nothing, so an app opened there plays to a dark screen (2026-10-20: with
        Remote start on, this TV never goes further than that). Unknown → carry on."""
        if self.wakefulness() in ("Asleep", "Dozing"):
            return self.power(True)
        return True, ""

    def key(self, spec: str) -> "tuple[bool, str]":
        words = re.findall(r"[a-z]+|\d+", (spec or "").lower())
        nums = [w for w in words if w.isdigit()]
        count = min(max(int(nums[0]) if nums else 1, 1), 20)
        name = "_".join(w for w in words
                        if not w.isdigit() and w not in ("times", "x", "button", "key"))
        code = _KEYS.get(name)
        if code is None:
            return False, f"The TV remote has no “{spec}” button I can press."
        self.keyevent(*([code] * count))
        return True, f"Sent {name.replace('_', ' ')}{f' ×{count}' if count > 1 else ''} to the TV."

    def type_text(self, text: str) -> "tuple[bool, str]":
        """Type into the focused text field. `input text` takes %s for a space, and
        can only type what the virtual keyboard map has (no emoji or non-Latin)."""
        t = re.sub(r"\s+", " ", text or "").strip()[:120]
        if not t:
            return False, "There was nothing to type."
        out = self.sh("input text " + shell_quote(t.replace(" ", "%s")))
        if re.search(r"Exception|Error", out):
            return False, f"The TV couldn't type “{t}”."
        return True, f"Typed “{t}” on the TV."

    def status(self) -> "tuple[bool, str]":
        if self.wakefulness() != "Awake":
            return True, "The TV is off (in standby)."
        pkg = self.focused_package()
        vol = self.volume()
        return True, (f"The TV is on, showing {_APP_NAMES.get(pkg) or pkg or 'an unknown screen'}"
                      + (f", volume {vol[0]}" if vol else "") + ".")


def _clock(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _int(value, default=None):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def run_tv(tv: Tv, action: str, target: str = "", app: str = "", season: int = 0,
           episode: int = 0, lookup=None, episode_of=None) -> "tuple[bool, str]":
    """One typed `tv_control` action → (ok, what the TV says happened)."""
    action = (action or "").strip().lower()
    target = str(target or "")
    try:
        if action == "open_app":
            return tv.open_app(target)
        if action == "play":
            if not target.strip():
                return tv.playback("play")
            if _alnum(app) == "youtube":
                return tv.youtube_play(target)
            return tv.netflix_play(target, lookup or lookup_netflix_title,
                                   episode_of or netflix_episode, season, episode)
        if action in ("pause", "resume"):
            return tv.playback("pause" if action == "pause" else "play")
        if action in ("volume_up", "volume_down"):
            return tv.volume_step(1 if action == "volume_up" else -1, _int(target) or 5)
        if action == "set_volume":
            return tv.set_volume(_int(target))
        if action == "mute":
            return tv.toggle_mute()
        if action in ("power_on", "power_off"):
            return tv.power(action == "power_on")
        if action == "key":
            return tv.key(target)
        if action == "status":
            return tv.status()
        return False, f"I don't know the TV action '{action}'."
    except TvUnreachable as exc:
        return False, str(exc)


def run(spec: dict) -> dict:
    """actions._run_one entry point for a {"type": "tv", ...} spec → an action result."""
    cfg = config()
    if not cfg:
        return {"type": "tv", "ok": False,
                "message": "No TV is set up yet — add it under Settings → Devices, sir."}
    do = str(spec.get("do") or spec.get("action") or "").strip().lower()
    target = str(spec.get("target") or spec.get("level") or "")
    if do == "look":
        return look(cfg["host"], target)
    if do == "task":
        goal = (target or str(spec.get("goal") or "")).strip()
        if not goal:
            return {"type": "tv_task", "ok": False,
                    "message": "What should I do on the TV, sir?"}
        # Deferred like browser_task/computer_task: it awaits a vision model per
        # step, so main.py runs it (tv_operator.run_task) with the activity feed.
        return {"type": "tv_task", "ok": True, "needs_autopilot": True, "kind": "tv",
                "goal": goal, "message": f"Working on it: {goal[:80]}", "speak": ""}
    app = str(spec.get("app") or "")
    if do == "open_app" and not target.strip():
        target = app          # live: the model named the app in "app", not "target"
    tv = Tv(lambda cmd: shell(cfg["host"], cmd))
    woke = None
    if do == "power_on" or (do in ("play", "open_app", "cast") and cfg.get("mac")):
        woke = wake(cfg)
        if woke is not None and not woke[0]:
            return {"type": "tv", "ok": False, "message": woke[1]}
    if do == "cast":
        try:
            ok, msg = cast(tv, target)
        except TvUnreachable as exc:
            ok, msg = False, str(exc)
    else:
        ok, msg = run_tv(tv, do, target, app or "netflix",
                         _int(spec.get("season"), 0), _int(spec.get("episode"), 0))
    if ok and woke and do == "power_on":
        msg = "Turned the TV on (woke it over the network)."
    if ok and not cfg.get("mac"):
        mac = _read_mac(cfg["host"])     # learn it while the TV answers, for next time
        if mac:
            _save({**cfg, "mac": mac})
    return {"type": "tv", "ok": ok, "message": msg}


def cast(tv: Tv, target: str, np=None) -> "tuple[bool, str]":
    """"Send this to the TV": a YouTube/Netflix link in `target`, or else whatever is
    playing on this PC right now — found by its title in Windows' media controls and
    its page in the browser's address bar. It's paused here only once the TV plays it
    (Netflix the other way round: paused first, so the TV resumes at the same spot)."""
    if np is None:
        from actions import now_playing as np
    if target.strip():
        link = np.parse_link(target)
        if not link:
            return False, "That isn't a YouTube or Netflix link I can play on the TV."
        return tv.cast(link, link.get("t", 0))
    found = np.playing_on_pc()
    title = (found or {}).get("title", "")
    if found is None and np.media_available():
        return False, "Nothing is playing on this PC right now."
    if found and not np.is_browser(found["app"]):
        return False, (f"“{title or 'That'}” is playing in {found['app'] or 'an app'}; only "
                       "YouTube or Netflix in a browser can be sent to the TV.")
    page = np.pick(title, np.browser_windows())
    if page is None:
        if found and title:
            return False, (f"I can see “{title}” playing on this PC, but not which YouTube or "
                           "Netflix page it's on — bring its tab to the front and ask again.")
        return False, ("I couldn't find a YouTube or Netflix page open on this PC. (Install "
                       "the winrt-Windows.Media.Control package so I can see what's playing.)")
    link, name = page["link"], title or ""
    # Only without winrt can nothing be playing here: then all there is to go by is the
    # address bar (2026-10-20: a stale &t=600s started the TV 6 minutes behind the PC).
    blind = "" if found else (" (I went by the address bar: install winrt-Windows.Media."
                              "Control so I can start where you are and pause it here.)")
    if link["service"] == "netflix":
        paused = bool(found) and np.pause(found)
        if paused:
            tv.wait(3)                       # let Netflix save where it is
        ok, msg = tv.cast(link, 0, name)
        if paused:
            msg += " Paused it on the PC." if ok else " (It's paused on the PC.)"
        return ok, msg + blind
    start = found["position"] if found else link.get("t", 0)
    ok, msg = tv.cast(link, start, name)
    if ok and found:
        msg += " Paused it on the PC." if np.pause(found) else " (I couldn't pause it on the PC.)"
    elif ok:
        msg += " It's still playing on the PC."
    return ok, msg + blind


_WAKE_WAIT_S = 30.0


def wake(cfg: dict, wait=time.sleep, now=time.monotonic) -> "tuple[bool, str] | None":
    """If the TV doesn't answer ADB (fully off, or deep standby with its network down),
    send its MAC a Wake-on-LAN packet and wait for ADB to come back. None: it was
    already reachable (or there's no MAC to wake it with) — carry on as usual."""
    ok, _, why = shell(cfg["host"], "echo ok")
    # Only a TV that can't be reached at all might be off: one that refused this PC,
    # or a missing package, is reported by the command itself.
    if ok or not cfg.get("mac") or not why.startswith(("I can't reach", "The TV at")):
        return None
    if "different networks" in why:
        return False, why          # a broadcast can't cross into another router's network
    # The TV's home-network address (not its Tailscale one) gives the subnet broadcast.
    near = next((h for h, _ in _targets(cfg["host"]) if len(wol.targets(h)) > 1), "")
    sent, msg = wol.send(cfg["mac"], near)
    if not sent:
        return False, msg
    deadline = now() + _WAKE_WAIT_S
    while now() < deadline:
        wait(2.0)
        if shell(cfg["host"], "echo ok")[0]:
            return True, "Woke the TV over the network."
    # Both causes, not just "off": on 2026-10-20 the TV was on (the phone could reach it)
    # while this PC couldn't for minutes at a time, and Remote start was already on.
    return False, ("I couldn't reach the TV, so I sent it a wake-up packet, but it still didn't "
                   f"answer within {int(_WAKE_WAIT_S)} seconds. If it's off, Settings → Network "
                   "→ Remote start on the TV (it may be called 'Turn on with Wi-Fi/LAN') lets "
                   "me wake it; if it's on, this PC can't reach it over the network right now.")


# ── Seeing the screen ─────────────────────────────────────────────────────────

_BLACK = 16          # every channel at or below this = a black frame


def frame_image(frame: bytes):
    """A first_frame() record → a PIL image, or None if it isn't one."""
    from PIL import Image
    if len(frame) < 20:
        return None
    _len, w, h, stride, fmt = struct.unpack_from("<5I", frame)
    if fmt != 3 or not (0 < w <= 4096 and 0 < h <= 4096) or stride < w * 3 \
            or len(frame) < 20 + stride * h:
        return None
    return Image.frombytes("RGB", (w, h), frame[20:20 + stride * h], "raw", "RGB", stride)


# Raw screencap is gzipped on the TV: measured 1.3s for a 1080p frame, against 4.1s for
# `screencap -p` (the TV's slow PNG encoder) and 1.7s raw. A TV without gzip says so.
_SCREENCAP_GZ = "command -v gzip >/dev/null && screencap | gzip -1 || echo NOGZIP"


def raw_screencap_image(data: bytes):
    """`screencap | gzip -1` output → (image, refused). (None, True): screencap refused
    — it prints nothing while a secure layer is up. (None, None): not a gzip'd raw
    screencap (no gzip on this TV, or an unknown pixel format) → use `screencap -p`."""
    import gzip
    from PIL import Image
    try:
        raw = gzip.decompress(data)
    except Exception:  # noqa: BLE001
        return None, None
    if not raw:
        return None, True
    if len(raw) < 12:
        return None, None
    w, h, fmt = struct.unpack_from("<3I", raw)
    header = len(raw) - w * h * 4        # 12 bytes on Android 8; 16 from Android 9
    rawmode = {1: "RGBX", 2: "RGBX", 5: "BGRX"}.get(fmt)   # RGBA / RGBX / BGRA_8888
    if header not in (12, 16) or not rawmode or not (0 < w <= 8192 and 0 < h <= 8192):
        return None, None
    return Image.frombytes("RGB", (w, h), raw[header:], "raw", rawmode), False


def capture(host: str, max_side: int = 1280, fmt: str = "JPEG", grab=None) -> dict:
    """The TV's screen for a vision model → {"ok", "image" (data URL), "sig", "partial",
    "blocked", "why"}.

    screencap first: the full picture — unless a copy-protected (secure) surface is
    visible, when Android refuses the whole capture (empty output; logcat "FB is
    protected: PERMISSION_DENIED"). Netflix marks its video AND its own menus secure.
    Then one frame from screenrecord, which draws only the secure layers black: a
    usable picture with holes ("partial"), or all black ("blocked" — nothing on screen
    can be seen). `grab` = (exec_out, first_frame), swapped out by the tests."""
    import base64
    import io
    from PIL import Image
    run_exec, run_frame = grab or (exec_out, first_frame)
    ok, data, why = run_exec(host, _SCREENCAP_GZ)
    if not ok:
        return {"ok": False, "why": why}
    img, refused = raw_screencap_image(data)
    if img is None and refused is None:
        ok, png, why = run_exec(host, "screencap -p")
        if not ok:
            return {"ok": False, "why": why}
        try:
            img = Image.open(io.BytesIO(png)).convert("RGB") if png else None
        except Exception:  # noqa: BLE001 — garbled → try the other path
            img = None
    partial = False
    if img is None:
        ok, frame, why = run_frame(host, "1280x720")
        if not ok:
            return {"ok": False, "why": why}
        img = frame_image(frame)
        if img is None or max(hi for _lo, hi in img.getextrema()) <= _BLACK:
            return {"ok": True, "blocked": True}
        partial = True
    img.thumbnail((max_side, max_side))
    buf = io.BytesIO()
    if fmt.upper() == "PNG":
        img.save(buf, format="PNG")
        mime = "image/png"
    else:
        img.save(buf, format="JPEG", quality=85)
        mime = "image/jpeg"
    grey = img.convert("L").resize((64, 36))
    darkest, brightest = grey.getextrema()
    return {"ok": True, "blocked": False, "partial": partial,
            "image": f"data:{mime};base64," + base64.b64encode(buf.getvalue()).decode(),
            "sig": grey.tobytes(),
            # One flat colour: an app still loading (YouTube sat on flat grey, live).
            "blank": brightest - darkest <= 6}


def look(host: str, question: str, tv: "Tv | None" = None, grab=None) -> dict:
    """tv_control look → a deferred vision question about the TV's screen (main.py asks
    the vision model with this image), or the honest reason there's nothing to see."""
    tv = tv or Tv(lambda cmd: shell(host, cmd))
    try:
        if tv.wakefulness() != "Awake":
            return {"type": "tv", "ok": True, "message": "The TV is off (in standby)."}
        pkg = tv.focused_package()
        label = _APP_NAMES.get(pkg) or pkg
        shot = capture(host, fmt="PNG", grab=grab)
        if shot.get("ok") and shot.get("blocked"):
            # State 3 at position 0 is Netflix's "Who's watching?" screen, not playback
            # (live: this said "playing" there). A moving position would cost 2.5s more.
            state, position, _ = tv.sessions().get(pkg, (0, 0, 0))
            doing = (" and playing" if state == 3 and position > 0
                     else " and paused" if state == 2 else "")
            return {"type": "tv", "ok": True,
                    "message": f"{label or 'An app'} is on the TV{doing}, but it blocks "
                               "screenshots (copy protection), so I can't see the picture."}
    except TvUnreachable as exc:
        return {"type": "tv", "ok": False, "message": str(exc)}
    if not shot.get("ok"):
        return {"type": "tv", "ok": False,
                "message": shot.get("why") or "I couldn't capture the TV's screen."}
    # The answer is spoken: an inventory of every tile on the home screen (what the
    # first live run read out) is noise. Reading text out is still verbatim.
    context = ("This is a screenshot of the user's TV (Android TV)"
               + (f", showing {label}" if label else "") + "."
               + (" Black areas are copy-protected video, which screenshots can't show."
                  if shot.get("partial") else "")
               + " Answer for someone talking to you, in one or two sentences — for a "
                 "general 'what's on' question, say which app or screen it is and what's "
                 "playing or highlighted — unless asked to read something out. ")
    return {"type": "tv", "needs_vision": True, "ok": None, "image": shot["image"],
            "context": context, "message": "Looking at the TV…",
            "question": question.strip() or "Describe what's on the TV screen."}


# ── Netflix title lookup ──────────────────────────────────────────────────────

def lookup_netflix_title(title: str, search=None, name_of=None) -> dict:
    """Web search finds the id; Netflix's own public title page then confirms which show
    it is, so a misremembered or invented id can't play the wrong thing under the right
    name. → {"id", "name"} or {"error"}."""
    t = (title or "").strip()
    if not t:
        return {"error": "Which show or film should I put on?"}
    ok, text = (search or _web_search)(
        f'What is the Netflix title ID of "{t}"? Reply with its '
        "https://www.netflix.com/title/<number> URL.")
    if not ok:
        return {"error": f"I couldn't look up “{t}” on Netflix: {text}"}
    ids = re.findall(r"netflix\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?title/(\d{5,10})", text, re.I)
    bare = re.search(r"\b(\d{8})\b", text)
    if bare:
        ids.append(bare.group(1))
    other = ""
    for nid in list(dict.fromkeys(ids))[:3]:
        name = (name_of or netflix_title_name)(nid)
        if name and same_title(name, t):
            return {"id": nid, "name": name}
        if name:
            other = name
    return {"error": (f"I couldn't find “{t}” on Netflix — the closest title I found was “{other}”."
                      if other else f"I couldn't find “{t}” on Netflix.")}


def _unescape_html(s: str) -> str:
    return (s.replace("&amp;", "&").replace("&quot;", '"')
            .replace("&#x27;", "'").replace("&#39;", "'"))


_JS_STR = r'"((?:[^"\\]|\\.)*)"'


def _js_unescape(s: str) -> str:
    """The page's data is a JS literal: \\x20-style escapes, then \\" and friends."""
    s = re.sub(r"\\x([0-9A-Fa-f]{2})", lambda m: chr(int(m.group(1), 16)), s)
    return re.sub(r"\\(.)", r"\1", s)


def _netflix_page(nid: str) -> "str | None":
    import httpx
    try:
        res = httpx.get(f"https://www.netflix.com/title/{nid}", follow_redirects=True, timeout=20)
    except Exception:  # noqa: BLE001
        return None
    return res.text if res.status_code == 200 else None


def netflix_title_name(nid: str) -> "str | None":
    html = _netflix_page(nid)
    return title_name_in(html, nid) if html else None


def title_name_in(html: str, nid: str) -> "str | None":
    """Netflix's name for a title, from its public page (~1 MB). Measured 2026-10-11 from
    the PC: <title> is just "Netflix" and the name sits in the embedded data near the end
    ("__typename":"Show"/"Movie","title":…,"videoId":<id>). The Android app measured
    "Watch X | Netflix" in <title> in Sept 2026 — kept as the fallback."""
    m = re.search(r'"__typename":"(?:Show|Movie)","title":' + _JS_STR + r',"videoId":'
                  + re.escape(nid) + r"\b", html)
    if m:
        return _js_unescape(m.group(1)).strip() or None
    m = re.search(r"<title[^>]*>([^<]*)</title>", html, re.I)
    name = _unescape_html(m.group(1) if m else "")
    name = re.sub(r"\s*\|\s*Netflix.*$", "", re.sub(r"^Watch\s+", "", name, flags=re.I),
                  flags=re.I).strip()
    return name if name and name.lower() != "netflix" else None


def netflix_episode(show_id: str, season: int, episode: int) -> dict:
    """A show's season/episode → the episode's Netflix id, from the show's public page."""
    html = _netflix_page(show_id)
    if html is None:
        return {"error": "I couldn't load that show's episode list from Netflix."}
    return find_episode(html, season, episode) or {
        "error": f"Netflix doesn't list a season {season}, episode {episode} for that show."}


def _find_episode_nested(html: str, season: int, episode: int) -> "dict | None":
    """Page shape measured 2026-10-11: "seasons":{…"edges":[{"node":{"__typename":"Season",
    "episodes":{…[{"node":{"__typename":"Episode","videoId":N,"number":1,…,"title":…}}…]}}…]}
    — seasons in order with no number label; episodes carry their number and title."""
    start = html.find('"__typename":"SeasonsConnection"')
    if start < 0:
        return None
    parts = re.split(r'"__typename":"Season","episodes"', html[start:])[1:]
    if not 0 < season <= len(parts):
        return None
    part = parts[season - 1]
    expected = 1
    for m in re.finditer(r'"__typename":"Episode","videoId":(\d+),"number":(\d+)', part):
        if int(m.group(2)) != expected:     # past this season's list (e.g. recommendations)
            break
        if expected == episode:
            t = re.search(r'"title":' + _JS_STR, part[m.end():m.end() + 6000])
            return {"id": m.group(1), "title": _js_unescape(t.group(1)) if t else ""}
        expected += 1
    return None


def find_episode(html: str, season: int, episode: int) -> "dict | None":
    """The page's embedded data lists each Season (labelled "Season N"), in order, with
    its episodes' ids in order; each Episode object carries its title. (The Sept 2026
    shape the Android app measured; the current one is _find_episode_nested.)"""
    nested = _find_episode_nested(html, season, episode)
    if nested:
        return nested
    seasons = []
    for m in re.finditer(r'"__typename":"Season","videoId":\d+([\s\S]*?)"edges":\[([^\]]*)\]', html):
        label = re.search(r'"Season (\d+)"', m.group(1))
        seasons.append((int(label.group(1)) if label else None,
                        re.findall(r'Episode:\{\\*"videoId\\*":(\d+)\}', m.group(2))))
    # Shows labelled "Part"/"Volume" instead of "Season" fall back to page order.
    s = next((x for x in seasons if x[0] == season), None)
    if s is None and not any(x[0] for x in seasons) and 0 < season <= len(seasons):
        s = seasons[season - 1]
    if not s or not 0 < episode <= len(s[1]):
        return None
    eid = s[1][episode - 1]
    t = re.search(r'"__typename":"Episode","videoId":' + eid + r',"title":"((?:[^"\\]|\\.)*)"', html)
    return {"id": eid, "title": re.sub(r"\\(.)", r"\1", t.group(1)) if t else ""}


def same_title(a: str, b: str) -> bool:
    """Loose title match: one contains the other once case, accents and punctuation go."""
    def norm(s: str) -> str:
        s = "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))
        return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()
    x, y = norm(a), norm(b)
    return bool(x and y) and (x in y or y in x)
