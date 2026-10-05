"""Google Places API (New) — accurate place & brand lookups.

Used when configured: a Google Maps Platform **API key**, OR — for Vertex-tier
users — their existing **ADC login** (Places API (New) accepts OAuth tokens with
the cloud-platform scope `vertex_auth` already mints, so no extra key is needed;
they just enable "Places API (New)" on their GCP project). When neither is present
this module reports unavailable and the caller falls back to the free OpenStreetMap
path in :mod:`places`.

Why it exists: OSM has large coverage gaps for brand branches — the "nearest Blue
Tokai is 15 km away when one is 2 km away" problem. Google's data has them.

Best-effort by design: every function degrades to ``[]`` / unavailable rather than
raising, so a failed/again-rate-limited Google call transparently becomes an OSM
lookup.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import app_secrets
from llm import vertex_auth

_BASE = "https://places.googleapis.com/v1"


def _key() -> str:
    return app_secrets.get("google_maps_key")


def available() -> bool:
    """True when Places API (New) can be called — a Maps key, or Vertex ADC."""
    if _key():
        return True
    try:
        return vertex_auth.enabled()
    except Exception:  # noqa: BLE001
        return False


def source() -> str:
    """'google' when Places API (New) is usable, else 'osm' (for sysinfo/logging)."""
    return "google" if available() else "osm"


def _headers(field_mask: str) -> dict:
    h = {"Content-Type": "application/json", "X-Goog-FieldMask": field_mask}
    key = _key()
    if key:
        h["X-Goog-Api-Key"] = key
    else:
        # Vertex tier: reuse the ADC OAuth bearer token + project (for billing).
        h["Authorization"] = f"Bearer {vertex_auth.get_access_token()}"
        proj = vertex_auth.project()
        if proj:
            h["X-Goog-User-Project"] = proj
    return h


def _post(path: str, body: dict, field_mask: str, timeout: float = 10.0) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_BASE + path, data=data,
                                 headers=_headers(field_mask), method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def text_search(query: str, lat=None, lon=None, limit: int = 6,
                want_rating: bool = True) -> list:
    """Places API (New) Text Search — handles brands AND categories, biased to the
    user's coords. Returns ``[{name, lat, lon, address, rating}]`` (raw; the caller
    adds distances). Returns ``[]`` on ANY failure so the caller falls back to OSM."""
    query = (query or "").strip()
    if not query:
        return []
    body: dict = {"textQuery": query, "pageSize": min(max(limit, 1), 20)}
    if lat is not None and lon is not None:
        body["locationBias"] = {"circle": {
            "center": {"latitude": float(lat), "longitude": float(lon)},
            "radius": 25000.0}}
    # The field mask is what we PAY for. `location` is an Essentials field; adding
    # `rating`/`formattedAddress` bumps the request to the Pro SKU — kept minimal.
    fields = ["places.displayName", "places.location", "places.formattedAddress"]
    if want_rating:
        fields.append("places.rating")
    try:
        data = _post("/places:searchText", body, ",".join(fields))
    except urllib.error.HTTPError as exc:
        body_txt = ""
        try:
            body_txt = exc.read()[:200].decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        print(f"[GPlaces] HTTP {exc.code} on searchText: {body_txt!r}", flush=True)
        return []
    except Exception as exc:  # noqa: BLE001
        print(f"[GPlaces] searchText failed: {exc}", flush=True)
        return []
    out = []
    for p in data.get("places", []):
        loc = p.get("location") or {}
        la, lo = loc.get("latitude"), loc.get("longitude")
        if la is None or lo is None:
            continue
        out.append({"name": (p.get("displayName") or {}).get("text", "").strip(),
                    "lat": la, "lon": lo,
                    "address": p.get("formattedAddress", ""),
                    "rating": p.get("rating")})
    return out
