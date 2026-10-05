"""Central secret store for JARVIS.

API keys and credentials live outside the source tree so they do not sit in the
project folder or travel with source files. Resolution order for any key is:

1. environment variable
2. app-data secrets.json
3. legacy repo secrets.json / config.json, only for one-time migration

On startup, migrate_from_config() lifts old source-tree secrets into app data,
scrubs config.json, and removes the legacy secrets.json after the app-data write
succeeds.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import jarvis_paths

_DIR = Path(__file__).resolve().parent
_LEGACY_SECRETS_PATH = _DIR / "secrets.json"
_CONFIG_PATH = jarvis_paths.legacy_config_path()

_SECRETS_PATH = jarvis_paths.app_data_dir() / "secrets.json"

# Known secret keys -> the environment variables that may override them.
_KEYS: "dict[str, tuple[str, ...]]" = {
    "groq_api_key": ("GROQ_API_KEY",),
    "gemini_api_key": ("GEMINI_API_KEY", "GOOGLE_API_KEY"),
    "nvidia_api_key": ("NVIDIA_API_KEY",),
    "mistral_api_key": ("MISTRAL_API_KEY",),
    "openrouter_api_key": ("OPENROUTER_API_KEY",),
    # Paid providers: Settings only, deliberately no env-var fallback - a stray
    # OPENAI_API_KEY in a dev shell would otherwise put paid models into Auto
    # routing without the user ever choosing that.
    "openai_api_key": (),
    "anthropic_api_key": (),
    "xai_api_key": (),
    "meta_api_key": (),
    "picovoice_key": ("PICOVOICE_KEY", "PV_ACCESS_KEY"),
    "spotify_client_id": ("SPOTIFY_CLIENT_ID",),
    "spotify_client_secret": ("SPOTIFY_CLIENT_SECRET",),
    "spotify_redirect_uri": ("SPOTIFY_REDIRECT_URI",),
    "elevenlabs_api_key": ("ELEVENLABS_API_KEY", "ELEVEN_API_KEY"),
    "google_maps_key": ("GOOGLE_MAPS_API_KEY", "GOOGLE_MAPS_KEY", "MAPS_API_KEY"),
}

# Keys we treat as genuinely sensitive when deciding whether onboarding is needed.
PRIMARY_KEYS = ("groq_api_key", "gemini_api_key")


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


# The app-data secrets file changes only through this module (set_many /
# migrate_from_config), so cache the parsed dict instead of re-reading it on
# every get() — get_sysinfo() and the Settings panel call get() repeatedly.
_secrets_cache: "dict | None" = None


def _read_secrets() -> dict:
    global _secrets_cache
    if _secrets_cache is None:
        _secrets_cache = _read(_SECRETS_PATH)
    return _secrets_cache


def _invalidate_secrets_cache() -> None:
    global _secrets_cache
    _secrets_cache = None


def _write_json(path: Path, data: dict) -> bool:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[Secrets] Couldn't write {path}: {exc}", flush=True)
        return False


def get(name: str, default: str = "") -> str:
    """Resolve a single secret without logging or echoing its value."""
    for env in _KEYS.get(name, ()):
        v = os.environ.get(env)
        if v:
            return v.strip()

    current = _read_secrets()
    if current.get(name):
        return str(current[name]).strip()

    legacy = _read(_LEGACY_SECRETS_PATH)
    if legacy.get(name):
        return str(legacy[name]).strip()

    config = _read(_CONFIG_PATH)
    return str(config.get(name) or default).strip()


def get_list(name: str) -> "list[str]":
    """A secret that may hold SEVERAL credentials, newline- or comma-separated.

    Free-tier quota is per key, so pasting three Groq keys triples the headroom.
    The storage format is deliberately unchanged — still one string per secret —
    so no migration, no new app-data fields, and `get()` keeps working for the
    callers that only ever want one credential (STT, vision OCR).

    Order is preserved and duplicates are dropped: the route ladder walks these
    in order, so the user's first key stays their primary.
    """
    raw = get(name)
    if not raw:
        return []
    out: "list[str]" = []
    for part in raw.replace(",", "\n").split("\n"):
        val = part.strip()
        if val and val not in out:
            out.append(val)
    return out


def all_secrets() -> dict:
    return {k: get(k) for k in _KEYS}


def has(name: str) -> bool:
    return bool(get(name))


def set_many(updates: dict) -> dict:
    """Persist known non-empty secrets to app-data secrets.json."""
    current = _read(_SECRETS_PATH)
    for k, v in (updates or {}).items():
        if k not in _KEYS:
            continue
        val = ("" if v is None else str(v)).strip()
        if val:
            current[k] = val
    if _write_json(_SECRETS_PATH, current):
        _invalidate_secrets_cache()
        _scrub_config()
    return current


def remove(names) -> "list[str]":
    """Delete these secrets from app data. Returns the names that are still set
    afterwards because an environment variable provides them (can't be removed
    from here)."""
    names = [n for n in (names or []) if n in _KEYS]
    current = _read(_SECRETS_PATH)
    if any(n in current for n in names):
        for n in names:
            current.pop(n, None)
        if _write_json(_SECRETS_PATH, current):
            _invalidate_secrets_cache()
    return [n for n in names if any(os.environ.get(e) for e in _KEYS[n])]


def _migrate_legacy_app_data() -> None:
    """Copy secrets from the old %APPDATA%\\Aura folder into Jarvis."""
    if os.name != "nt":
        return
    base = os.environ.get("APPDATA") or os.environ.get("LOCALAPPDATA")
    if not base:
        return
    legacy = Path(base) / "Aura" / "secrets.json"
    current = _SECRETS_PATH
    if legacy.exists() and not current.exists():
        try:
            current.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(legacy, current)
            print(f"[Secrets] Migrated secrets from {legacy.parent} to {current.parent}.",
                  flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[Secrets] Couldn't migrate legacy Aura secrets: {exc}", flush=True)


def migrate_from_config() -> None:
    """Move secrets from old source-tree locations into app data."""
    _migrate_legacy_app_data()
    jarvis_paths.migrate_config_from_repo()
    config = _read(jarvis_paths.config_path())
    if not config:
        config = _read(_CONFIG_PATH)
    legacy = _read(_LEGACY_SECRETS_PATH)
    found = {k: legacy[k] for k in _KEYS if legacy.get(k)}
    found.update({k: config[k] for k in _KEYS if config.get(k)})
    if not found:
        return

    current = _read(_SECRETS_PATH)
    for k, v in found.items():
        current.setdefault(k, str(v).strip())

    if not _write_json(_SECRETS_PATH, current):
        return
    _invalidate_secrets_cache()
    _scrub_config()

    if _LEGACY_SECRETS_PATH.exists():
        try:
            _LEGACY_SECRETS_PATH.unlink()
            print(f"[Secrets] Moved API keys to {_SECRETS_PATH}.", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(
                "[Secrets] App-data migration succeeded, but couldn't remove "
                f"legacy secrets.json: {exc}",
                flush=True,
            )
    else:
        print(f"[Secrets] Migrated API keys to {_SECRETS_PATH}.", flush=True)


def _scrub_config() -> None:
    for path in (jarvis_paths.config_path(), _CONFIG_PATH):
        config = _read(path)
        removed = [k for k in list(config) if k in _KEYS]
        if not removed:
            continue
        for k in removed:
            config.pop(k, None)
        _write_json(path, config)


def path() -> Path:
    """Current app-data secrets path, for diagnostics only."""
    return _SECRETS_PATH
