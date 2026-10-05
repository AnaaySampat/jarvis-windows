#!/usr/bin/python3
"""
start.py — Launch JARVIS with a single command.

    python start.py        (or double-click it)

Starts the Python backend and the Tauri GUI side-by-side,
streams their output with colour-coded prefixes, and shuts
both down cleanly on Ctrl+C.

The shebang is /usr/bin/python3, not /usr/bin/env python3: on a double-click
the Windows py launcher resolves "env python3" through PATH, where python3 is
the Microsoft Store alias it can't start — it exits with code 101 and the
console vanishes before anything is printed.
"""

import csv
import os
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT        = Path(__file__).parent
BACKEND_DIR = ROOT / "jarvis-studio-backend"
GUI_DIR     = ROOT / "jarvis-studio-gui"
ICONS_PATH  = GUI_DIR / "src-tauri" / "icons" / "tray_icon.rgba"
# Ports a start needs (both are strict) → what the JARVIS process holding one from
# an earlier start has in its command line.
PORTS       = {8765: ("backend", ("main.py", "run_backend")),
               1420: ("dev server", ("vite",))}

WIN = sys.platform == "win32"

# ── ANSI helpers ───────────────────────────────────────────────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
PURPLE = "\033[35m"
CYAN   = "\033[36m"
YELLOW = "\033[33m"
GREEN  = "\033[32m"
RED    = "\033[31m"


def _enable_ansi() -> None:
    if WIN:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleMode(
                ctypes.windll.kernel32.GetStdHandle(-11), 7
            )
        except Exception:
            pass


def _tag(label: str, color: str) -> str:
    return f"{color}{BOLD}{label}{RESET}"


# ── Subprocess helpers ─────────────────────────────────────────────────────

def _spawn(cmd, cwd: Path, env: "dict[str, str] | None" = None) -> subprocess.Popen:
    """Start a subprocess, merging stderr into stdout."""
    if WIN and isinstance(cmd, list) and cmd:
        exe = "npm.cmd" if cmd[0].lower() == "npm" else cmd[0]
        resolved = shutil.which(exe)
        cmd = [resolved or exe, *cmd[1:]]
    kwargs: dict = dict(
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    return subprocess.Popen(cmd, **kwargs)


def _stream(proc: subprocess.Popen, label: str, color: str) -> None:
    """Read stdout from proc and print each line with a coloured label."""
    prefix = _tag(label, color)
    try:
        for raw in iter(proc.stdout.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip()
            if line:
                print(f"{prefix} {line}", flush=True)
    except Exception:
        pass


def _kill(proc: subprocess.Popen, label: str) -> None:
    if proc.poll() is not None:
        return
    try:
        if WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
            )
        else:
            proc.terminate()
            proc.wait(timeout=6)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass
    print(f"{_tag(label, DIM)} stopped.", flush=True)


# ── One-time setup ─────────────────────────────────────────────────────────

def _ensure_icons() -> None:
    if ICONS_PATH.exists():
        return
    print(f"{_tag('[setup]', YELLOW)} icon.ico missing — running setup-icons.js…",
          flush=True)
    r = subprocess.run(
        ["node", "setup-icons.js"],
        cwd=str(GUI_DIR),
        capture_output=True,
        text=True,
    )
    if r.returncode == 0:
        print(f"{_tag('[setup]', GREEN)} {r.stdout.strip() or 'icons created.'}", flush=True)
    else:
        print(f"{_tag('[setup]', RED)} setup-icons.js failed:\n{r.stderr}", file=sys.stderr)
        sys.exit(1)


def _port_busy(port: int) -> bool:
    """Anything listening on localhost:port — IPv4 or IPv6 (Vite binds only ::1)."""
    try:
        with socket.create_connection(("localhost", port), timeout=0.5):
            return True
    except OSError:
        return False


def _port_owner(port: int) -> "int | None":
    """PID listening on TCP `port`, from netstat (no third-party deps)."""
    out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True,
                         errors="replace").stdout
    for parts in (line.split() for line in out.splitlines()):
        # Proto, Local, Foreign, State, PID — the state word is localized, but a
        # listener's foreign address is always 0.0.0.0:0 or [::]:0.
        if len(parts) >= 5 and parts[1].endswith(f":{port}") \
                and parts[2] in ("0.0.0.0:0", "[::]:0") and parts[4].isdigit():
            return int(parts[4])
    return None


def _command_line(pid: int) -> str:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
        capture_output=True, text=True, errors="replace").stdout.strip()


def _clear_old_jarvis() -> None:
    """Stop a JARVIS still running from an earlier start — usually hidden in the
    tray (the HUD's X only hides it) or a backend a closed console left behind.
    It holds port 8765 (or the dev server's 1420), so the new backend died on
    startup and took the GUI down with it: "JARVIS doesn't open". A port owner
    that isn't JARVIS is named instead of killed."""
    if not WIN:
        return
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq jarvis-studio-gui.exe",
                          "/FO", "CSV", "/NH"], capture_output=True, text=True,
                         errors="replace").stdout
    old = [(int(row[1]), "window") for row in csv.reader(out.splitlines())
           if len(row) > 1 and row[0].lower() == "jarvis-studio-gui.exe"]
    for port, (what, marks) in PORTS.items():
        if not _port_busy(port):
            continue
        owner = _port_owner(port)
        cmd = _command_line(owner) if owner else ""
        if owner and any(m in cmd for m in marks):
            old.append((owner, what))
            continue
        holder = ("the installed JARVIS app (quit it from its tray icon)"
                  if "jarvis-backend" in cmd.lower() else cmd or "another program")
        print(f"{_tag('[setup]', RED)} Port {port} is in use by {holder}"
              f"{f' (PID {owner})' if owner else ''}. JARVIS needs it — close that "
              f"and run start.py again.", flush=True)
        sys.exit(1)
    for pid, what in old:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
        print(f"{_tag('[setup]', YELLOW)} Stopped the JARVIS {what} still running from "
              f"before (PID {pid}).", flush=True)
    deadline = time.monotonic() + 8
    while old and any(map(_port_busy, PORTS)) and time.monotonic() < deadline:
        time.sleep(0.2)


def _backend_python() -> str:
    """Prefer the backend venv so Playwright and its Chromium revision match."""
    for rel in (".venv-build/Scripts/python.exe", ".venv/Scripts/python.exe",
                ".venv-build/bin/python", ".venv/bin/python"):
        exe = BACKEND_DIR / rel
        if exe.is_file():
            return str(exe)
    return sys.executable


# ── Entry point ────────────────────────────────────────────────────────────

def main() -> None:
    _enable_ansi()
    print(f"\n{CYAN}{BOLD}  J.A.R.V.I.S{RESET}  {DIM}— booting up…{RESET}\n")

    _ensure_icons()
    _clear_old_jarvis()
    ws_token = os.environ.get("JARVIS_WS_TOKEN") or secrets.token_urlsafe(32)
    backend_env = {**os.environ, "JARVIS_WS_TOKEN": ws_token}
    gui_env = {
        **backend_env,
        "VITE_JARVIS_WS_TOKEN": ws_token,
        "JARVIS_SKIP_BACKEND_SPAWN": "1",
    }

    # ── Backend ────────────────────────────────────────────────────────────
    print(f"{_tag('[backend]', PURPLE)} Starting Python backend…", flush=True)
    backend_py = _backend_python()
    if backend_py != sys.executable:
        print(f"{_tag('[backend]', DIM)} using {backend_py}", flush=True)
    backend = _spawn([backend_py, "main.py"], cwd=BACKEND_DIR, env=backend_env)

    # ── GUI ────────────────────────────────────────────────────────────────
    print(f"{_tag('[gui]', CYAN)} Starting Tauri GUI "
          f"{DIM}(first build may take ~1 min){RESET}…", flush=True)
    gui = _spawn(["npm", "run", "tauri", "dev"], cwd=GUI_DIR, env=gui_env)

    # ── Pipe output with colour-coded prefixes ─────────────────────────────
    for proc, label, color in [
        (backend, "[backend]", PURPLE),
        (gui,     "[gui]",     CYAN),
    ]:
        threading.Thread(target=_stream, args=(proc, label, color), daemon=True).start()

    print(f"\n{DIM}  Press Ctrl+C to stop both processes.{RESET}\n", flush=True)

    try:
        while True:
            # If either process dies unexpectedly, report and exit
            if backend.poll() is not None:
                print(f"\n{_tag('[backend]', RED)} exited (code {backend.returncode})",
                      flush=True)
                break
            if gui.poll() is not None:
                print(f"\n{_tag('[gui]', RED)} exited (code {gui.returncode})",
                      flush=True)
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{YELLOW}{BOLD}  Ctrl+C — shutting down…{RESET}", flush=True)
    finally:
        _kill(backend, "[backend]")
        _kill(gui,     "[gui]")
        print(f"\n{CYAN}{BOLD}  J.A.R.V.I.S offline.{RESET}\n", flush=True)


def _own_console() -> bool:
    """True when this script has a console window to itself (double-clicked),
    which Windows closes the moment we exit — taking any error with it."""
    if not WIN:
        return False
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        pids = (ctypes.c_uint * 8)()
        n = k32.GetConsoleProcessList(pids, 8)
        if n == 1:                       # python.exe alone
            return True
        if n != 2:                       # a shell (and maybe py.exe) shares it
            return False
        # Two: our py.exe launcher and us — or a shell and us (`python start.py`).
        other = next(p for p in pids[:n] if p != os.getpid())
        h = k32.OpenProcess(0x1000, False, other)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        buf, size = ctypes.create_unicode_buffer(260), ctypes.c_uint(260)
        ok = k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return bool(ok) and Path(buf.value).name.lower() in ("py.exe", "pyw.exe")
    except Exception:
        return False


if __name__ == "__main__":
    pause = _own_console()
    try:
        main()
    except SystemExit as exc:
        if exc.code not in (None, 0) and pause:
            input("\nPress Enter to close…")
        raise
    except KeyboardInterrupt:
        pass
    except BaseException:
        if not pause:
            raise
        import traceback
        traceback.print_exc()
        input("\nPress Enter to close…")
