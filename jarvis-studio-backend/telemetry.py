"""Live system telemetry for the JARVIS HUD.

Gathers fast-changing stats (CPU / RAM / disk / GPU / network / battery) with
``psutil`` and slow-changing connectivity facts (local + public IP, approximate
location). Follows the project's lazy-import / fail-soft convention: if a library
is missing, the affected fields are ``None`` and the rest still work.

Key names match what the HUD panels consume directly:
    cpu, ram, disk, gpu, net, down, up, ping, temp,
    batteryPct, charging, remaining, ramTotalGb, diskUsedGb, diskTotalGb, gpuName
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
import urllib.request

try:
    import psutil  # type: ignore
except ImportError:  # pragma: no cover - optional dep
    psutil = None

_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# ── module state for rate / ping / activity calculations ─────────────────────
_prev_net: "tuple[int, int] | None" = None     # (bytes_sent, bytes_recv)
_prev_t: float = 0.0
_prev_disk: "float | None" = None              # cumulative read_time+write_time (ms)
_prev_disk_t: float = 0.0
_last_ping: "float | None" = None
_gpu_handle = None
_gpu_kind: str = "none"               # "nvml" | "gputil" | "typeperf" | "none"
_gpu_inited = False
_gpu_pct: "float | None" = None       # cached util when read by the bg poller
_gpu_poller_started = False


def _system_drive() -> str:
    if os.name == "nt":
        return os.environ.get("SystemDrive", "C:") + "\\"
    return "/"


_gpu_name: str = ""


def _init_gpu() -> None:
    """Pick a GPU backend once: NVML → GPUtil → Windows typeperf → none.

    typeperf reads Windows GPU performance counters and works for ANY vendor
    (Intel/AMD/NVIDIA) with no extra pip install — it's just slow, so when it's
    the chosen backend we poll it on a background thread and cache the value.
    """
    global _gpu_handle, _gpu_kind, _gpu_inited, _gpu_name
    _gpu_inited = True
    try:
        import pynvml  # type: ignore
        pynvml.nvmlInit()
        _gpu_handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        name = pynvml.nvmlDeviceGetName(_gpu_handle)
        _gpu_name = name.decode("utf-8", "replace") if isinstance(name, bytes) else name
        _gpu_kind = "nvml"
        return
    except Exception:
        _gpu_handle = None
    try:
        import GPUtil  # type: ignore
        gpus = GPUtil.getGPUs()
        if gpus:
            _gpu_name = gpus[0].name
            _gpu_kind = "gputil"
            return
    except Exception:
        pass
    if os.name == "nt":
        _gpu_kind = "typeperf"
    else:
        _gpu_kind = "none"


def _typeperf_gpu() -> "float | None":
    """Windows: total GPU utilisation via perf counters (max busy engine)."""
    try:
        out = subprocess.run(
            ["typeperf", r"\GPU Engine(*)\Utilization Percentage", "-sc", "1"],
            capture_output=True, text=True, timeout=10, creationflags=_NO_WINDOW,
        ).stdout
    except Exception:
        return None
    best = 0.0
    found = False
    for line in out.splitlines():
        if not line.startswith('"') or "PDH" in line:
            continue
        parts = [p.strip().strip('"') for p in line.split(",")]
        for p in parts[1:]:                       # parts[0] is the timestamp
            try:
                best = max(best, float(p)); found = True
            except ValueError:
                pass
    return min(100.0, best) if found else None


def _gpu_poller() -> None:
    """Background sampler for the SLOW GPU backends so their cost stays OFF the
    ~1.5s telemetry tick. `typeperf` spawns a process; `GPUtil.getGPUs()` shells
    out to nvidia-smi (~100-500ms) — sampling here every 2.5s and caching into
    `_gpu_pct` means get_fast_stats() just reads the cached number."""
    global _gpu_pct, _gpu_name
    while True:
        try:
            if _gpu_kind == "gputil":
                import GPUtil  # type: ignore
                gpus = GPUtil.getGPUs()
                if gpus:
                    _gpu_pct = float(gpus[0].load * 100.0)
                    _gpu_name = gpus[0].name
                else:
                    _gpu_pct = None
            else:
                _gpu_pct = _typeperf_gpu()
        except Exception:
            _gpu_pct = None
        time.sleep(2.5)


def _read_gpu() -> "tuple[float | None, str]":
    global _gpu_poller_started
    if not _gpu_inited:
        _init_gpu()
    try:
        if _gpu_kind == "nvml":
            import pynvml  # type: ignore
            util = pynvml.nvmlDeviceGetUtilizationRates(_gpu_handle).gpu
            return float(util), _gpu_name
        # Both gputil and typeperf are sampled by a single background poller so
        # the per-tick read is just a cached lookup (no subprocess on the loop).
        if _gpu_kind in ("gputil", "typeperf"):
            if not _gpu_poller_started:
                _gpu_poller_started = True
                threading.Thread(target=_gpu_poller, daemon=True).start()
            return _gpu_pct, _gpu_name
    except Exception:
        pass
    return None, _gpu_name


def _read_temp() -> "float | None":
    """Best-effort CPU temperature. Usually unavailable on Windows → None."""
    if psutil is None or not hasattr(psutil, "sensors_temperatures"):
        return None
    try:
        temps = psutil.sensors_temperatures()
    except Exception:
        return None
    if not temps:
        return None
    # Prefer a CPU-ish sensor, else the first reading we find.
    for key in ("coretemp", "k10temp", "cpu_thermal", "acpitz"):
        if key in temps and temps[key]:
            return float(temps[key][0].current)
    for readings in temps.values():
        if readings:
            return float(readings[0].current)
    return None


def measure_ping(host: str = "1.1.1.1", port: int = 443, timeout: float = 1.5) -> "float | None":
    """TCP-connect latency in ms (no subprocess). Caches into _last_ping.

    Blocking — call via ``loop.run_in_executor``.
    """
    global _last_ping
    try:
        start = time.perf_counter()
        s = socket.create_connection((host, port), timeout=timeout)
        s.close()
        _last_ping = (time.perf_counter() - start) * 1000.0
    except OSError:
        _last_ping = None
    return _last_ping


def get_fast_stats() -> dict:
    """Snapshot of fast-changing system stats. Cheap / non-blocking."""
    global _prev_net, _prev_t, _prev_disk, _prev_disk_t

    if psutil is None:
        return {
            "cpu": 0, "ram": 0, "disk": 0, "diskActivity": 0, "gpu": None, "net": 0,
            "down": 0, "up": 0, "ping": _last_ping, "temp": None,
            "batteryPct": None, "charging": False, "remaining": "",
            "ramTotalGb": 0, "diskUsedGb": 0, "diskTotalGb": 0, "gpuName": "",
            "error": "psutil not installed (pip install psutil)",
        }

    cpu = psutil.cpu_percent(interval=None)
    vm = psutil.virtual_memory()
    du = psutil.disk_usage(_system_drive())
    now = time.monotonic()

    # network throughput from counter deltas
    io = psutil.net_io_counters()
    down = up = 0.0
    if _prev_net is not None and now > _prev_t:
        dt = now - _prev_t
        up = max(0.0, (io.bytes_sent - _prev_net[0]) * 8 / 1e6 / dt)     # Mbps
        down = max(0.0, (io.bytes_recv - _prev_net[1]) * 8 / 1e6 / dt)
    _prev_net = (io.bytes_sent, io.bytes_recv)

    # disk activity from byte throughput (read+write). 100 MB/s reads as 100%.
    # (psutil's read_time/write_time are unreliable on Windows; bytes are not.)
    disk_activity = 0.0
    try:
        dio = psutil.disk_io_counters()
        total_bytes = float(dio.read_bytes + dio.write_bytes)
        if _prev_disk is not None and now > _prev_disk_t:
            mbps = (total_bytes - _prev_disk) / 1e6 / (now - _prev_disk_t)
            disk_activity = min(100.0, max(0.0, mbps))     # 100 MB/s → 100%
        _prev_disk = total_bytes
        _prev_disk_t = now
    except Exception:
        pass

    _prev_t = now

    # network activity for the dial: 100 Mbps of combined throughput reads as
    # 100% so the gauge is responsive to real traffic (idle ≈ 0, downloads light
    # it up). Raw Mbps are reported separately in `down`/`up`.
    net_pct = min(100.0, down + up)

    gpu, gpu_name = _read_gpu()

    battery_pct = None
    charging = False
    remaining = ""
    try:
        bat = psutil.sensors_battery()
    except Exception:
        bat = None
    if bat is not None:
        battery_pct = float(bat.percent)
        charging = bool(bat.power_plugged)
        secs = bat.secsleft
        if secs is not None and secs >= 0 and not charging:
            remaining = f"{secs // 3600}h {(secs % 3600) // 60}m"
        elif charging:
            remaining = "On AC"

    return {
        "cpu": float(cpu),
        "ram": float(vm.percent),
        "ramTotalGb": vm.total / 1e9,
        "disk": float(du.percent),
        "diskUsedGb": du.used / 1e9,
        "diskTotalGb": du.total / 1e9,
        "gpu": gpu,
        "gpuName": gpu_name,
        "diskActivity": disk_activity,
        "net": net_pct,
        "down": down,
        "up": up,
        "ping": round(_last_ping) if _last_ping is not None else None,
        "temp": _read_temp(),
        "batteryPct": battery_pct,
        "charging": charging,
        "remaining": remaining,
    }


# ── connectivity facts (slow-changing; blocking → run in executor) ───────────

def _http_get(url: str, timeout: float = 6.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def get_net_info() -> dict:
    """Local IP, public IP, and approximate location (city/region + lat/lon)."""
    local_ip = "unknown"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass

    public_ip = "—"
    try:
        public_ip = _http_get("https://api.ipify.org").strip() or public_ip
    except Exception:
        pass

    location = ""
    lat = lon = None
    try:
        data = json.loads(_http_get(
            "http://ip-api.com/json/?fields=status,city,regionName,country,lat,lon"))
        if data.get("status") == "success":
            city = data.get("city", "")
            region = data.get("regionName", "")
            location = ", ".join(p for p in (city, region) if p)
            lat = data.get("lat")
            lon = data.get("lon")
    except Exception:
        pass

    return {
        "localIp": local_ip,
        "publicIp": public_ip,
        "location": location or "—",
        "lat": lat,
        "lon": lon,
    }


def reverse_geocode(lat, lon) -> str:
    """City/region name for precise GPS coords (BigDataCloud, free/no key)."""
    if lat is None or lon is None:
        return ""
    try:
        data = json.loads(_http_get(
            "https://api.bigdatacloud.net/data/reverse-geocode-client"
            f"?latitude={lat}&longitude={lon}&localityLanguage=en"))
        city = data.get("city") or data.get("locality") or ""
        region = data.get("principalSubdivision", "")
        return ", ".join(p for p in (city, region) if p)
    except Exception:
        return ""
