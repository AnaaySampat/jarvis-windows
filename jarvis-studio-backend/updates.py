""""A new version is available." Asks GitHub for the latest release at most once a day
and reports it when it's newer than the running HUD (which sends its version on
connect: the frozen backend has no version of its own). Notify-only: the user gets the
release page and runs the installer the way they ran this one. Port of the Android
app's platform/update.ts.
"""

from __future__ import annotations

import json
import time

import jarvis_paths

RELEASES_URL = "https://github.com/AnaaySampat/jarvis-windows/releases/latest"
_API = "https://api.github.com/repos/AnaaySampat/jarvis-windows/releases/latest"
_DAY_S = 24 * 3600


def _stamp_path():
    return jarvis_paths.app_data_dir() / "update_check.json"


def is_newer(latest: str, current: str) -> bool:
    """True when release tag `latest` ("v0.3.0") is a higher x.y.z than `current`."""
    def parse(v: str) -> "list[int]":
        parts = (v or "").strip().lstrip("vV").replace("-", ".").split(".")[:3]
        out = []
        for p in parts:
            digits = "".join(ch for ch in p if ch.isdigit())
            out.append(int(digits) if digits else 0)
        return out + [0] * (3 - len(out))
    return parse(latest) > parse(current)


def _due(now: float) -> bool:
    """Once a day: claim today's check, or say it was already done."""
    path = _stamp_path()
    try:
        last = float(json.loads(path.read_text(encoding="utf-8")).get("checked_at") or 0)
    except Exception:  # noqa: BLE001 — no file yet / corrupt: check now
        last = 0.0
    if now - last < _DAY_S:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"checked_at": now}), encoding="utf-8")
    except OSError:
        pass                      # can't remember the check: still check this launch
    return True


async def check(current: str, now: "float | None" = None) -> "dict | None":
    """{"version", "url"} for a newer release, else None — up to date, already checked
    today, or the check failed (an update notice is never worth an error)."""
    if not (current or "").strip() or not _due(time.time() if now is None else now):
        return None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(_API, headers={"Accept": "application/vnd.github+json"})
        if resp.status_code != 200:
            return None
        rel = resp.json()
    except Exception:  # noqa: BLE001
        return None
    tag = str(rel.get("tag_name") or "")
    if not tag or rel.get("draft") or rel.get("prerelease") or not is_newer(tag, current):
        return None
    return {"version": tag.lstrip("vV"), "url": str(rel.get("html_url") or RELEASES_URL)}
