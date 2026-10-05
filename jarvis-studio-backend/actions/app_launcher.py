"""Windows app / URL / folder / system-action launcher.

JARVIS is Windows-only. This module resolves a fuzzy app name ("chrome",
"calculator", "vs code") to something Windows can actually start, and also
handles opening URLs, common folders, and a few safe system actions
(lock, sleep, volume, screenshot).

Everything here is best-effort: a failure to launch never raises — it returns
a (ok, message) tuple so the pipeline can tell the user what happened.
"""

from __future__ import annotations

import difflib
import os
import re
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional, Tuple

# CREATE_NO_WINDOW so we never flash a console when shelling out.
_NO_WINDOW = 0x08000000

# ── Known app aliases ─────────────────────────────────────────────────────────
# Maps spoken/typed names to a launch target. A target is either:
#   • an executable name resolvable via the App Paths registry (start "" name)
#   • an absolute path
#   • a shell: / ms-settings: URI
# The resolver tries these in order, so aliases just need to be "good enough".
_ALIASES: dict[str, str] = {
    # Browsers
    "chrome": "chrome", "google chrome": "chrome",
    "edge": "msedge", "microsoft edge": "msedge",
    "firefox": "firefox", "brave": "brave",
    # Microsoft / Office
    "word": "winword", "microsoft word": "winword",
    "excel": "excel", "powerpoint": "powerpnt",
    "outlook": "outlook", "onenote": "onenote",
    "teams": "ms-teams:",
    # System utilities
    "notepad": "notepad", "wordpad": "write",
    "calculator": "calc", "calc": "calc",
    "paint": "mspaint", "snipping tool": "snippingtool",
    "task manager": "taskmgr", "control panel": "control",
    "command prompt": "cmd", "cmd": "cmd", "terminal": "wt",
    "powershell": "powershell", "registry editor": "regedit",
    "file explorer": "explorer", "explorer": "explorer", "files": "explorer",
    "device manager": "devmgmt.msc", "services": "services.msc",
    "settings": "ms-settings:", "system settings": "ms-settings:",
    # Dev tools
    "vs code": "code", "vscode": "code", "visual studio code": "code",
    "notepad++": "notepad++", "git bash": "git-bash",
    # Media / chat
    "spotify": "spotify", "vlc": "vlc",
    "discord": "discord", "slack": "slack",
    "steam": "steam", "whatsapp": "whatsapp:",
    # Camera / photos / store
    "camera": "microsoft.windows.camera:",
    "photos": "ms-photos:", "store": "ms-windows-store:",
    "microsoft store": "ms-windows-store:",
    "calendar": "outlookcal:", "mail": "outlookmail:",
    "maps": "bingmaps:", "clock": "ms-clock:",
}

# ── Common folders ────────────────────────────────────────────────────────────
def _known_folders() -> dict[str, Path]:
    home = Path.home()
    return {
        "home": home, "user": home,
        "desktop": home / "Desktop",
        "documents": home / "Documents", "docs": home / "Documents",
        "downloads": home / "Downloads",
        "pictures": home / "Pictures", "photos folder": home / "Pictures",
        "music": home / "Music",
        "videos": home / "Videos",
    }


def real_known_folders() -> "dict[str, str]":
    """The user's REAL Desktop / Downloads / Documents / Pictures paths, resolved
    from the Windows 'User Shell Folders' registry — which correctly reflects
    OneDrive redirection, where the naive ``Home\\Desktop`` is often WRONG. Falls
    back to ``~/<name>`` per entry. Used to tell the desktop autopilot the exact
    full path to type into Save/Open dialogs (typing '%userprofile%\\Desktop' or a
    bare 'Desktop' left files in whatever folder the dialog defaulted to)."""
    home = str(Path.home())
    out = {
        "Desktop": os.path.join(home, "Desktop"),
        "Downloads": os.path.join(home, "Downloads"),
        "Documents": os.path.join(home, "Documents"),
        "Pictures": os.path.join(home, "Pictures"),
    }
    if os.name != "nt":
        return out
    reg_names = {
        "Desktop": "Desktop",
        "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
        "Documents": "Personal",
        "Pictures": "My Pictures",
    }
    try:
        import winreg
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
        ) as key:
            for label, val in reg_names.items():
                try:
                    raw, _ = winreg.QueryValueEx(key, val)
                    p = os.path.expandvars(raw)
                    if p and os.path.isdir(p):
                        out[label] = p
                except OSError:
                    pass
    except Exception:  # noqa: BLE001 — registry quirks: keep the ~/<name> fallback
        pass
    return out


# Windows Settings pages by what people call them → their ms-settings: page.
_SETTINGS_PAGES = {
    "bluetooth": "bluetooth", "devices": "devices", "wifi": "network-wifi",
    "wi-fi": "network-wifi", "network": "network-status", "internet": "network-status",
    "ethernet": "network-ethernet", "vpn": "network-vpn", "proxy": "network-proxy",
    "airplane mode": "network-airplanemode", "hotspot": "network-mobilehotspot",
    "display": "display", "screen": "display", "resolution": "display",
    "brightness": "display", "night light": "nightlight", "sound": "sound",
    "audio": "sound", "volume": "apps-volume", "notifications": "notifications",
    "focus": "quiethours", "do not disturb": "quiethours", "power": "powersleep",
    "sleep": "powersleep", "battery": "batterysaver", "storage": "storagesense",
    "multitasking": "multitasking", "clipboard": "clipboard", "about": "about",
    "personalization": "personalization", "background": "personalization-background",
    "wallpaper": "personalization-background", "colors": "colors", "colours": "colors",
    "dark mode": "colors", "light mode": "colors", "themes": "themes",
    "lock screen": "lockscreen", "start menu": "personalization-start",
    "taskbar": "taskbar", "fonts": "fonts", "apps": "appsfeatures",
    "installed apps": "appsfeatures", "default apps": "defaultapps",
    "startup apps": "startupapps", "startup": "startupapps", "account": "yourinfo",
    "accounts": "yourinfo", "sign-in options": "signinoptions",
    "sign in options": "signinoptions", "email accounts": "emailandaccounts",
    "date and time": "dateandtime", "date & time": "dateandtime", "time": "dateandtime",
    "date": "dateandtime", "language": "regionlanguage", "region": "regionlanguage",
    "keyboard": "typing", "typing": "typing", "mouse": "mousetouchpad",
    "touchpad": "devices-touchpad", "printers": "printers", "printer": "printers",
    "pen": "pen", "gaming": "gaming-gamebar", "game mode": "gaming-gamemode",
    "game bar": "gaming-gamebar", "accessibility": "easeofaccess-display",
    "narrator": "easeofaccess-narrator", "magnifier": "easeofaccess-magnifier",
    "privacy": "privacy", "camera": "privacy-webcam", "webcam": "privacy-webcam",
    "microphone": "privacy-microphone", "location": "privacy-location",
    "windows update": "windowsupdate", "update": "windowsupdate",
    "updates": "windowsupdate", "security": "windowsdefender",
    "recovery": "recovery", "activation": "activation", "troubleshoot": "troubleshoot",
    "backup": "backup",
}
_SETTINGS_NAME_RE = re.compile(
    r"^(?:the\s+|my\s+)?(?:windows\s+)?(.+?)\s+settings?(?:\s+(?:page|screen|menu))?$",
    re.IGNORECASE)


def settings_uri(name: str) -> str:
    """'display settings' / 'the bluetooth settings page' → 'ms-settings:display'
    — '' when it isn't a Settings page this knows (bare 'settings' is the app)."""
    m = _SETTINGS_NAME_RE.match((name or "").strip())
    page = _SETTINGS_PAGES.get(m.group(1).strip().lower()) if m else None
    return f"ms-settings:{page}" if page else ""


def known_folder(name: str) -> str:
    """The REAL path of a user folder named like 'Downloads' / 'my desktop' /
    'documents folder' (OneDrive-aware), or ''."""
    key = re.sub(r"^(?:my|the)\s+|\s+folder$", "", (name or "").strip().lower()).strip()
    real = {k.lower(): v for k, v in real_known_folders().items()}
    if key in real:
        return real[key]
    p = _known_folders().get(key)
    return str(p) if p is not None and p.is_dir() else ""


def _looks_like_path(value: str) -> bool:
    """True when the user/model supplied a filesystem path, not a plain app name."""
    raw = (value or "").strip().strip('"')
    if not raw:
        return False
    p = Path(raw).expanduser()
    return bool(p.drive or p.is_absolute() or "\\" in raw or "/" in raw or p.exists())


def open_app_needs_permission(name: str) -> bool:
    """Opening a known/bare app is routine; opening a local path needs consent."""
    raw = (name or "").strip()
    if not raw:
        return False
    target = _ALIASES.get(raw.lower(), raw)
    return raw.lower() not in _ALIASES and _looks_like_path(target)


def open_folder_needs_permission(name: str) -> bool:
    """Known user folders are routine; arbitrary folders reveal local contents."""
    raw = (name or "").strip()
    return bool(raw and raw.lower() not in _known_folders() and _looks_like_path(raw))


# ── Launchers ─────────────────────────────────────────────────────────────────
def _shell_start(target: str) -> None:
    """Launch via the shell `start` verb — resolves App Paths and URIs."""
    # The empty "" is the window title arg `start` expects before the target.
    subprocess.Popen(
        ["cmd", "/c", "start", "", target],
        creationflags=_NO_WINDOW,
        close_fds=True,
    )


def _in_app_paths(target: str) -> bool:
    """True if ``target`` (or ``target.exe``) is registered under the Windows
    ``App Paths`` registry key, i.e. the bare-name shell `start` will resolve it.
    Lets us avoid reporting a false "Opening …" for an app that doesn't exist."""
    try:
        import winreg  # Windows-only; absent elsewhere → treat as unknown.
    except Exception:  # noqa: BLE001
        return False
    name = target if target.lower().endswith(".exe") else target + ".exe"
    sub = r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths" + "\\" + name
    for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(root, sub):
                return True
        except OSError:
            continue
    return False


# ── The installed-app index ───────────────────────────────────────────────────
# The alias table + PATH + App Paths only cover apps that REGISTER themselves.
# Most real installs (BlueJ, game launchers, IDEs…) only drop a Start-Menu
# shortcut — which is exactly what Windows Search launches them by. So we build
# the same index Windows has: every Start-Menu .lnk, every entry the Store/
# system knows (Get-StartApps), and the uninstall registry's executables. Built
# lazily on the first name we can't resolve the fast way, then cached.

_INDEX_TTL = 600.0          # rebuild at most every 10 min (apps rarely change)
_index_lock = threading.Lock()
_index: "list[tuple[str, str, str]]" = []      # (display name, kind, target)
_index_built_at: float = 0.0


def _norm(name: str) -> str:
    """Loose matching key: lowercase, letters+digits only ('VS Code' → 'vscode')."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def _scan_start_menu() -> "list[tuple[str, str, str]]":
    """Every launchable Start-Menu shortcut (user + all-users)."""
    roots = []
    for env, sub in (("APPDATA", r"Microsoft\Windows\Start Menu\Programs"),
                     ("PROGRAMDATA", r"Microsoft\Windows\Start Menu\Programs")):
        base = os.environ.get(env)
        if base:
            roots.append(Path(base) / sub)
    skip = ("uninstall", "remove ", "repair", "readme", "documentation",
            "license", "website", "help")
    found: "list[tuple[str, str, str]]" = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for p in root.rglob("*"):
                if p.suffix.lower() not in (".lnk", ".url"):
                    continue
                name = p.stem.strip()
                if not name or any(s in name.lower() for s in skip):
                    continue
                found.append((name, "lnk", str(p)))
        except OSError:
            continue
    return found


def _scan_uninstall_registry() -> "list[tuple[str, str, str]]":
    """DisplayName → executable from the uninstall registry (covers installs
    whose shortcut name differs from what people call them)."""
    try:
        import winreg
    except Exception:  # noqa: BLE001
        return []
    found: "list[tuple[str, str, str]]" = []
    subkeys = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
               r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall")
    for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for sub in subkeys:
            try:
                key = winreg.OpenKey(root, sub)
            except OSError:
                continue
            with key:
                for i in range(0, 2048):
                    try:
                        child = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(key, child) as ck:
                            name = str(winreg.QueryValueEx(ck, "DisplayName")[0]).strip()
                            icon, loc = "", ""
                            try:
                                icon = str(winreg.QueryValueEx(ck, "DisplayIcon")[0])
                            except OSError:
                                pass
                            try:
                                loc = str(winreg.QueryValueEx(ck, "InstallLocation")[0])
                            except OSError:
                                pass
                    except OSError:
                        continue
                    if not name:
                        continue
                    exe = icon.split(",")[0].strip().strip('"')
                    if exe.lower().endswith(".exe") and Path(exe).exists() \
                            and "unins" not in Path(exe).name.lower():
                        found.append((name, "exe", exe))
                        continue
                    # No usable icon path → look for a matching exe where the
                    # app says it installed itself.
                    locdir = Path(loc.strip().strip('"')) if loc.strip() else None
                    if locdir and locdir.is_dir():
                        want = _norm(name)
                        try:
                            for p in locdir.glob("*.exe"):
                                en = _norm(p.stem)
                                if en and not any(b in p.stem.lower()
                                                  for b in _NON_APP_EXE) \
                                        and (en in want or want in en):
                                    found.append((name, "exe", str(p)))
                                    break
                        except OSError:
                            pass
    return found


def _scan_start_apps() -> "list[tuple[str, str, str]]":
    """Get-StartApps: the complete launchable-app list Windows itself uses —
    including Store/UWP apps that exist nowhere on disk. One PowerShell spawn
    (~1–2s), so it only runs inside the cached index build."""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-StartApps | ForEach-Object { $_.Name + '|' + $_.AppID }"],
            capture_output=True, text=True, timeout=15, creationflags=_NO_WINDOW,
        ).stdout
    except Exception:  # noqa: BLE001
        return []
    found: "list[tuple[str, str, str]]" = []
    for line in (out or "").splitlines():
        name, _, appid = line.partition("|")
        name, appid = name.strip(), appid.strip()
        if name and appid:
            found.append((name, "appid", appid))
    return found


_SKIP_PROGRAM_DIRS = {
    "common files", "windowsapps", "modifiablewindowsapps", "windows defender",
    "windows defender advanced threat protection", "windows nt", "windows mail",
    "windows media player", "windows photo viewer", "windows security",
    "windows sidebar", "windows multimedia platform", "internet explorer",
    "microsoft update health tools", "msbuild", "reference assemblies",
    "dotnet", "microsoft sdks", "microsoft.net", "uninstall information",
    "installshield installation information",
}
_NON_APP_EXE = ("unins", "setup", "install", "updater", "update", "repair",
                "crash", "report", "elevate", "helper", "service")


def _scan_program_dirs() -> "list[tuple[str, str, str]]":
    """Apps that exist ONLY as a folder with an exe — no Start-Menu shortcut, no
    StartApps entry, an MSI-only uninstall row. (BlueJ is the canonical case:
    even Windows' own Start search can't launch it by name.) Shallow scan: each
    child of the program roots, taking the top-level exe whose name matches the
    folder, or the folder's single exe."""
    roots: "list[Path]" = []
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        v = os.environ.get(env)
        if v:
            roots.append(Path(v))
    lap = os.environ.get("LOCALAPPDATA")
    if lap:
        roots.append(Path(lap) / "Programs")
    found: "list[tuple[str, str, str]]" = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            children = [d for d in root.iterdir() if d.is_dir()]
        except OSError:
            continue
        for d in children:
            if d.name.lower() in _SKIP_PROGRAM_DIRS:
                continue
            try:
                exes = [p for p in d.glob("*.exe")
                        if not any(b in p.stem.lower() for b in _NON_APP_EXE)]
            except OSError:
                continue
            if not exes:
                continue
            dn = _norm(d.name)
            best = None
            for p in exes:
                en = _norm(p.stem)
                if en and (en == dn or en in dn or dn in en):
                    best = p
                    break
            if best is None and len(exes) == 1:
                best = exes[0]
            if best is not None:
                found.append((d.name, "exe", str(best)))
    return found


def _build_index() -> "list[tuple[str, str, str]]":
    entries: "list[tuple[str, str, str]]" = []
    for scanner in (_scan_start_menu, _scan_start_apps, _scan_program_dirs,
                    _scan_uninstall_registry):
        try:
            entries.extend(scanner() or [])
        except Exception:  # noqa: BLE001 — one broken source never kills the index
            pass
    # Dedupe by normalized name, keeping the first hit (scan order = preference:
    # Start-Menu shortcut, then AppID, then bare exe).
    seen: "set[str]" = set()
    out: "list[tuple[str, str, str]]" = []
    for name, kind, target in entries:
        key = _norm(name)
        if key and key not in seen:
            seen.add(key)
            out.append((name, kind, target))
    return out


def _get_index(force: bool = False) -> "list[tuple[str, str, str]]":
    global _index, _index_built_at
    with _index_lock:
        if force or not _index or (time.monotonic() - _index_built_at) > _INDEX_TTL:
            _index = _build_index()
            _index_built_at = time.monotonic()
        return list(_index)


def prewarm_index() -> None:
    """Build the app index in the background so the first 'open <app>' doesn't
    pay the scan (Get-StartApps alone spawns PowerShell, ~2–8s). Best-effort."""
    threading.Thread(target=_get_index, daemon=True,
                     name="jarvis-app-index").start()


def resolve_app(name: str) -> "Optional[tuple[str, str, str]]":
    """Find the installed app best matching `name`: exact → prefix → substring →
    fuzzy. Returns (display name, kind, target) or None."""
    want = _norm(name)
    if not want:
        return None
    index = _get_index()
    by_key = {_norm(n): (n, k, t) for n, k, t in index}
    if want in by_key:
        return by_key[want]
    prefix = [e for e in index if _norm(e[0]).startswith(want)]
    if prefix:                                   # shortest name = least decorated
        return min(prefix, key=lambda e: len(e[0]))
    inside = [e for e in index if want in _norm(e[0])]
    if inside:
        return min(inside, key=lambda e: len(e[0]))
    close = difflib.get_close_matches(want, list(by_key), n=1, cutoff=0.78)
    return by_key[close[0]] if close else None


def installed_app_name(name: str) -> str:
    """The display name of an installed app matching `name` EXACTLY or by prefix,
    or "" — a deliberately stricter cousin of :func:`resolve_app`.

    resolve_app is built for "launch what the user probably meant" and will fall
    back to substring and fuzzy matches; that generosity is right when the user
    has already said "open X", and wrong when the question is "is X an app at
    all?". The task router asks the second question about arbitrary words pulled
    out of a sentence, where a fuzzy hit ("open" → "OpenSSH") would misroute the
    whole task. Prefix matching is kept because installed names are decorated
    ("Notepad", "Notepad++", "Visual Studio Code") far more often than they're
    truncated."""
    want = _norm(name)
    if len(want) < 3:                 # too short to identify anything reliably
        return ""
    index = _get_index()
    for display, _kind, _target in index:
        if _norm(display) == want:
            return display
    prefix = [e for e in index if _norm(e[0]).startswith(want)]
    return min(prefix, key=lambda e: len(e[0]))[0] if prefix else ""


def app_suggestions(name: str, n: int = 3) -> "list[str]":
    """Closest installed-app names — for a helpful 'did you mean' on a miss."""
    want = _norm(name)
    if not want:
        return []
    by_key = {_norm(nm): nm for nm, _k, _t in _get_index()}
    return [by_key[k] for k in difflib.get_close_matches(want, list(by_key),
                                                         n=n, cutoff=0.45)]


def _launch_entry(entry: "tuple[str, str, str]") -> None:
    """Start a resolved index entry. Raises on hard failure."""
    _display, kind, target = entry
    if kind == "appid":
        subprocess.Popen(["explorer.exe", "shell:AppsFolder\\" + target],
                         creationflags=_NO_WINDOW, close_fds=True)
    else:                       # lnk / exe — startfile == double-clicking it
        os.startfile(target)    # type: ignore[attr-defined]


# A REAL URI scheme is "scheme:rest" where the scheme is a short alpha token and
# the colon is followed IMMEDIATELY by the scheme-specific part (no whitespace) —
# ms-settings:, spotify:track:…, shell:AppsFolder, mailto:… . A window title like
# "BlueJ:  java (javaw)" also contains a colon, but it's followed by whitespace;
# treating it as a URI is what popped Windows' "get an app to open this 'bluej'
# link" dialog. This matcher accepts the former and rejects the latter.
_URI_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:(?!\s)")

# Trailing "(javaw)", "(64-bit)" style annotation a window title carries.
_PAREN_TAIL_RE = re.compile(r"\s*\([^)]*\)\s*$")


def clean_app_name(name: str) -> str:
    """Strip window-title decorations the operator sometimes passes in place of an
    app name. The autopilot occasionally launches by the WINDOW TITLE it sees —
    'BlueJ:  java', 'BlueJ:  java (javaw)' — instead of the app name 'BlueJ'.
    A window title's 'App:  document' separator is a colon followed by whitespace
    (distinct from a 'scheme:' URI, which has none), so we can recover the app
    token without harming real names or URIs. Dashes are left alone — 'Doc -
    Notepad' puts the app AFTER the dash, so splitting there would be wrong."""
    s = (name or "").strip().strip('"')
    if not s:
        return ""
    s = _PAREN_TAIL_RE.sub("", s)
    if not _URI_SCHEME_RE.match(s):          # never split a genuine URI
        parts = re.split(r":\s+", s, maxsplit=1)
        if len(parts) == 2 and parts[0].strip():
            s = parts[0]
    return s.strip() or (name or "").strip()


def open_app(name: str) -> Tuple[bool, str]:
    raw = clean_app_name(name)
    if not raw:
        return False, "No app name given."
    # "display settings" is a page of the Settings app, not an app of its own.
    page = settings_uri(raw)
    if page:
        try:
            _shell_start(page)
            return True, f"Opening {raw}."
        except Exception as exc:  # noqa: BLE001
            return False, f"Couldn't open {raw}: {exc}"
    key = raw.lower()

    target = _ALIASES.get(key, raw)

    # URI-scheme targets (ms-settings:, spotify:, etc.) → hand to the shell. Guard
    # with a real-scheme check so a stray window title with a colon never reaches
    # the shell as a bogus protocol link.
    if _URI_SCHEME_RE.match(target) and not Path(target).drive:
        try:
            _shell_start(target)
            return True, f"Opening {raw}."
        except Exception as exc:  # noqa: BLE001
            return False, f"Couldn't open {raw}: {exc}"

    # An absolute path that exists → start it directly.
    p = Path(target)
    if p.exists():
        try:
            os.startfile(str(p))  # type: ignore[attr-defined]
            return True, f"Opening {raw}."
        except Exception as exc:  # noqa: BLE001
            return False, f"Couldn't open {raw}: {exc}"

    # On PATH? Start it — unless it's a batch shim: VS Code's `code` is code.cmd,
    # and starting that opened a stray console window the launch then took for
    # the app. The installed-app index below finds the real program.
    found = shutil.which(target)
    shim = bool(found) and found.lower().endswith((".cmd", ".bat"))
    if found and not shim:
        try:
            _shell_start(target)
            return True, f"Opening {raw}."
        except Exception as exc:  # noqa: BLE001
            return False, f"Couldn't open {raw}: {exc}"

    # The App Paths registry (covers installed apps the steps above miss). Only
    # claim success when the key actually exists — a blind `start ""` returns
    # immediately even for a non-existent app, which is what made JARVIS report
    # "Opening …" for things it never launched. (Not past a shim: `start` would
    # find the same .cmd on PATH again.)
    if not shim and _in_app_paths(target):
        try:
            _shell_start(target)
            return True, f"Opening {raw}."
        except Exception:  # noqa: BLE001
            pass

    # The installed-app index: Start-Menu shortcuts, Get-StartApps (UWP/Store)
    # and the uninstall registry, fuzzy-matched — this is how "open BlueJ"
    # works even though BlueJ registers nothing the fast paths can see.
    entry = resolve_app(raw)
    if entry is not None:
        try:
            _launch_entry(entry)
            return True, f"Opening {entry[0]}."
        except Exception as exc:  # noqa: BLE001
            return False, f"I found {entry[0]} but couldn't start it: {exc}"

    hints = app_suggestions(raw)
    if hints:
        return False, (f"I couldn't find an app called {raw}. "
                       f"Closest installed matches: {', '.join(hints)}.")
    return False, f"I couldn't find an app called {raw}."


def open_url(url: str) -> Tuple[bool, str]:
    url = (url or "").strip()
    if not url:
        return False, "No URL given."
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        webbrowser.open(url)
        return True, f"Opening {url}."
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't open the link: {exc}"


def open_folder(name: str) -> Tuple[bool, str]:
    raw = (name or "").strip()
    folders = _known_folders()
    path = folders.get(raw.lower())
    if path is None:
        # Maybe an absolute path was given.
        candidate = Path(raw).expanduser()
        if candidate.exists():
            path = candidate
        else:
            return False, f"I don't know a folder called {raw}."
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
        return True, f"Opening your {raw} folder."
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't open {raw}: {exc}"


def system_command(command: str) -> Tuple[bool, str]:
    """A small, safe set of system actions."""
    cmd = (command or "").strip().lower()
    try:
        if cmd in ("lock", "lock screen"):
            subprocess.Popen(["rundll32.exe", "user32.dll,LockWorkStation"],
                             creationflags=_NO_WINDOW)
            return True, "Locking your screen."
        if cmd in ("sleep",):
            subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"],
                             creationflags=_NO_WINDOW)
            return True, "Putting the computer to sleep."
        if cmd in ("screenshot", "snip"):
            # Win+Shift+S opens the snip overlay; emulate via the Snipping Tool.
            _shell_start("ms-screenclip:")
            return True, "Opening the screenshot tool."
        if cmd in ("volume_up", "volume up"):
            return _media_key(0xAF, "Turning the volume up.")
        if cmd in ("volume_down", "volume down"):
            return _media_key(0xAE, "Turning the volume down.")
        if cmd in ("mute", "unmute"):
            return _media_key(0xAD, "Toggling mute.")
        if cmd in ("play", "pause", "play_pause", "playpause"):
            return _media_key(0xB3, "Toggling playback.")
        if cmd in ("next", "next_track"):
            return _media_key(0xB0, "Skipping to the next track.")
        if cmd in ("previous", "prev", "previous_track"):
            return _media_key(0xB1, "Going to the previous track.")
        return False, f"I don't know the system command '{command}'."
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't run that: {exc}"


def _media_key(vk_code: int, message: str) -> Tuple[bool, str]:
    """Tap a virtual key (media/volume) via the Win32 keybd_event API."""
    try:
        import ctypes
        user32 = ctypes.windll.user32  # type: ignore[attr-defined]
        KEYEVENTF_KEYUP = 0x0002
        user32.keybd_event(vk_code, 0, 0, 0)
        user32.keybd_event(vk_code, 0, KEYEVENTF_KEYUP, 0)
        return True, message
    except Exception as exc:  # noqa: BLE001
        return False, f"Couldn't send the media key: {exc}"
