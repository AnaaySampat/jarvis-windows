"""Application Default Credentials (ADC) provider for Vertex AI.

JARVIS can serve Gemini two ways:
  • the AI Studio "Developer API" (``generativelanguage.googleapis.com``) with an
    ``AIza…``/``AQ.…`` API key, or
  • **Vertex AI** (``{region}-aiplatform.googleapis.com``) authenticated with the
    user's ADC login (``gcloud auth application-default login``).

Vertex is the path that draws from a Google Cloud project's **billing / trial
credits** — and it needs no API key (handy when org policy blocks key creation).
This module is the thin glue: it discovers ADC, mints + caches short-lived OAuth
bearer tokens, and tells the Gemini bridge which project/region to target.

Everything here is best-effort and dependency-guarded. ``google-auth`` may not be
installed and ADC may not be configured; in either case :func:`enabled` returns
False and callers transparently fall back to the API-key path (or to Groq).

Config (app-data ``config.json``):
  • ``use_vertex``    — master switch (default True; only matters if ADC resolves)
  • ``vertex_project``— GCP project id ("" → use ADC's own default project)
  • ``vertex_region`` — Vertex location (default ``us-central1``)
"""

from __future__ import annotations

import asyncio
import threading

import jarvis_paths

# ADC needs the cloud-platform scope to call Vertex AI.
_SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
_DEFAULT_REGION = "us-central1"

# google-auth is an optional dependency. Guard the import so a machine without it
# (or a frozen build that didn't bundle it) simply has Vertex disabled.
try:
    import google.auth                          # type: ignore
    from google.auth.transport.requests import Request as _AuthRequest  # type: ignore
    _GOOGLE_AUTH_OK = True
except Exception:                               # noqa: BLE001
    google = None                               # type: ignore
    _AuthRequest = None                          # type: ignore
    _GOOGLE_AUTH_OK = False

# Cached ADC credentials + the project they resolved to. google-auth credentials
# track their own expiry, so we keep one object and refresh it in place. Guarded
# by a lock because token refresh can race across concurrent turns.
_creds = None
_adc_project: "str | None" = None
_lock = threading.Lock()
_resolve_failed = False     # don't re-attempt a hopeless resolve every single turn


def _ensure_creds():
    """Resolve ADC once and cache the credentials object. Returns it, or None if
    google-auth is missing or ADC isn't configured (cached so we don't retry a
    known-failed resolve on every turn)."""
    global _creds, _adc_project, _resolve_failed
    if _creds is not None:
        return _creds
    if not _GOOGLE_AUTH_OK or _resolve_failed:
        return None
    with _lock:
        if _creds is not None:
            return _creds
        try:
            creds, project = google.auth.default(scopes=_SCOPES)
            _creds = creds
            # User ("authorized_user") ADC usually resolves project=None — fall back
            # to the credential's quota project (set via `gcloud auth
            # application-default set-quota-project`), which is the project that
            # actually gets billed. vertex_project in config still overrides both.
            _adc_project = project or getattr(creds, "quota_project_id", "") or ""
        except Exception as exc:                # noqa: BLE001
            # No ADC file / not logged in / no quota project, etc. Disable quietly.
            print(f"[Vertex] ADC not available ({exc}); using API-key path.", flush=True)
            _resolve_failed = True
            return None
    return _creds


def _provider_mode(cfg: dict) -> str:
    """The provider edition (Phase 4), mirroring groq_bridge.provider_mode but read
    here from the config dict to avoid an import cycle. Empty migrates from the
    legacy ``use_vertex`` flag so existing installs are unaffected."""
    m = str(cfg.get("provider_mode") or "").strip().lower()
    if m in ("vertex", "gemini", "offline"):
        return m
    return "vertex" if cfg.get("use_vertex", True) else "gemini"


def enabled() -> bool:
    """True when Gemini calls should route through Vertex AI: google-auth present,
    the provider Mode is ``vertex``, AND ADC actually resolves. Cheap after the
    first call (credentials are cached); only the small config read happens each
    time. Any other Mode (gemini / offline) keeps Vertex off."""
    if not _GOOGLE_AUTH_OK:
        return False
    if _provider_mode(jarvis_paths.read_config()) != "vertex":
        return False
    return _ensure_creds() is not None


def get_access_token() -> str:
    """A valid OAuth2 bearer token for Vertex (``ya29.…``), refreshing if expired.
    Raises RuntimeError if ADC isn't available — callers gate on :func:`enabled`."""
    creds = _ensure_creds()
    if creds is None:
        raise RuntimeError("Vertex ADC is not available.")
    with _lock:
        if not creds.valid:
            creds.refresh(_AuthRequest())
    return creds.token


async def get_access_token_async() -> str:
    """Async wrapper: the refresh does blocking I/O, so run it off the event loop.
    The common (cached, still-valid) path returns near-instantly."""
    return await asyncio.get_running_loop().run_in_executor(None, get_access_token)


def project() -> str:
    """The GCP project id Vertex requests target: the configured ``vertex_project``
    if set, otherwise the project ADC itself resolved to (the quota project)."""
    cfg = jarvis_paths.read_config()
    p = (cfg.get("vertex_project") or "").strip()
    if p:
        return p
    _ensure_creds()
    return _adc_project or ""


def region() -> str:
    cfg = jarvis_paths.read_config()
    return (cfg.get("vertex_region") or _DEFAULT_REGION).strip() or _DEFAULT_REGION
