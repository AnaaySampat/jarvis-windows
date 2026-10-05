"""Pluggable per-app control framework.

JARVIS controls external desktop apps by emitting:
    [ACTION]{"type":"app","app":"spotify","command":"play","query":"song name"}[/ACTION]

Each app is a small module that registers a handler ``(command, spec) -> (ok, msg)``.
**Adding a new app = drop a module in this folder + register it** — no other part
of the codebase changes. (This is the safe, extensible alternative to letting the
model write its own integration code at runtime.)
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

# app name → handler(command, spec) -> (ok, message)
_APPS: Dict[str, Callable[[str, dict], Tuple[bool, str]]] = {}


def register(name: str, handler: Callable[[str, dict], Tuple[bool, str]]) -> None:
    _APPS[name.lower().strip()] = handler


def available() -> List[str]:
    return sorted(_APPS)


def dispatch(spec: dict) -> Tuple[bool, str]:
    """Route an `app` action to the right integration."""
    app = str(spec.get("app") or spec.get("target") or "").lower().strip()
    if not app:
        return False, "Which app should I control?"
    handler = _APPS.get(app)
    if handler is None:
        have = ", ".join(available()) or "none yet"
        return False, f"I don't have a {app} integration yet. Available: {have}."
    command = str(spec.get("command") or spec.get("do") or spec.get("action") or "").lower().strip()
    try:
        return handler(command, spec)
    except Exception as exc:  # noqa: BLE001
        return False, f"The {app} integration errored: {exc}"


# ── Register built-in integrations ────────────────────────────────────────────
from . import spotify  # noqa: E402
register("spotify", spotify.handle)
