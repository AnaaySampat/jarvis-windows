"""App-data paths shared across the JARVIS backend."""

from __future__ import annotations

import copy
import json
import os
import shutil
import sys
from pathlib import Path


def bundle_root() -> Path:
    """Source tree root, or the PyInstaller one-folder bundle directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


_DIR = bundle_root()
_LEGACY_CONFIG_PATH = _DIR / "config.json"


def app_data_dir() -> Path:
    override = os.environ.get("JARVIS_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "Jarvis"
    return Path.home() / ".jarvis"


def config_path() -> Path:
    return app_data_dir() / "config.json"


def legacy_config_path() -> Path:
    return _LEGACY_CONFIG_PATH


def migrate_config_from_repo() -> None:
    """Move config.json from the source tree into app data on first run."""
    target = config_path()
    if target.exists():
        return
    legacy = legacy_config_path()
    if not legacy.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy, target)
        print(f"[Config] Migrated settings to {target}.", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[Config] Couldn't migrate legacy config.json: {exc}", flush=True)


# Parsed-config cache, keyed on the file's (mtime_ns, size). Several hot paths
# read the config many times per voice turn — vertex_auth.enabled()/region()/
# project() alone hit it ~4× per Gemini turn, plus TTS and routing. Re-statting,
# re-reading and re-parsing the JSON each time added avoidable per-turn latency.
# The signature changes whenever write_config()/_save_config() does its atomic
# os.replace(), so a fresh write is always picked up on the next read.
_cfg_cache: "dict | None" = None
_cfg_cache_sig: "tuple | None" = None


def read_config() -> dict:
    global _cfg_cache, _cfg_cache_sig
    migrate_config_from_repo()
    path = config_path()
    try:
        st = path.stat()
        sig = (st.st_mtime_ns, st.st_size)
    except OSError:                       # missing file → no cached config
        _cfg_cache = None
        _cfg_cache_sig = None
        return {}
    if _cfg_cache is not None and sig == _cfg_cache_sig:
        # Hand back a deep copy so callers that mutate nested dicts (screen/
        # overlay) can't corrupt the shared cache.
        return copy.deepcopy(_cfg_cache)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(data, dict):
        return {}
    _cfg_cache = data
    _cfg_cache_sig = sig
    return copy.deepcopy(data)


def write_config(data: dict) -> bool:
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[Config] Couldn't write {path}: {exc}", flush=True)
        return False
