"""Nearby places + directions — all free, no API key.

Three capabilities, each best-effort and never raising:

  • ``find_nearby(query, lat, lon)`` — restaurants, cafés, shops, ATMs, fuel,
    pharmacies… around the user, via the OpenStreetMap **Overpass** API. We map a
    plain-English query ("food", "coffee", "pharmacy") to OSM tags, search a
    growing radius, and sort by distance.
  • ``geocode(query)`` — turn a place name/address into coordinates + a tidy
    label, via OSM **Nominatim**.
  • ``directions(dest, lat, lon)`` — driving distance + time from the user to a
    destination, via the public **OSRM** routing server.

These return spoken-friendly strings (and a structured list, for places) so the
model can read results back naturally. Blocking HTTP → call from an executor.
"""

from __future__ import annotations

import json
import math
import re
import urllib.parse
import urllib.request

import places_google   # Google Places API (New) — accurate; falls back to OSM here

# Leading question phrasing the model sometimes passes verbatim as the destination
# ("how far is the nearest blue tokai") — strip it down to the actual place name.
_GEO_QUESTION_RE = re.compile(
    r"^\s*(how\s+far(\s+away)?(\s+is|\s+are)?|how\s+long(\s+to|\s+does\s+it\s+take"
    r"(\s+to\s+(get|reach))?)?|distance\s+(to|from\s+me\s+to)|where\s+is|"
    r"how\s+do\s+i\s+(get|reach)(\s+to)?|directions?\s+to|navigate\s+to|route\s+to|"
    r"get\s+to|take\s+me\s+to)\s+", re.IGNORECASE)
# Leading filler that breaks geocoding ("nearest Starbucks" finds nothing; the
# place is just "Starbucks"). Stripped before we hand the name to Nominatim.
_GEO_FILLER_RE = re.compile(
    r"^(the\s+)?(nearest|closest|nearby|local|a|an|some|any|my\s+local)\s+", re.IGNORECASE)

_UA = {"User-Agent": "Jarvis/1.0 (personal voice assistant)"}

# The user's current coordinates + place label, pushed in by main.py whenever
# they change (browser GPS or IP fallback). Read by the places/directions/weather
# actions, which run synchronously and have no other access to location.
_USER = {"lat": None, "lon": None, "place": ""}


def set_user_coords(lat, lon, place: str = "") -> None:
    _USER["lat"], _USER["lon"] = lat, lon
    if place:
        _USER["place"] = place


def get_user_coords() -> "tuple":
    return _USER["lat"], _USER["lon"], _USER["place"]

# Plain-English category → Overpass tag filters. First match wins; the catch-all
# is amenity=restaurant. Keep the keys lowercase.
_CATEGORIES = [
    (("restaurant", "food", "eat", "dinner", "lunch", "place to eat", "diner"),
     '["amenity"="restaurant"]', "restaurants"),
    (("cafe", "coffee", "café", "tea"), '["amenity"="cafe"]', "cafés"),
    (("bakery", "bakeries", "croissant", "pastry", "patisserie", "bread", "cake",
      "donut", "doughnut", "baked"), '["shop"~"bakery|pastry|confectionery"]', "bakeries"),
    (("fast food", "burger", "mcdonald", "pizza", "takeaway", "take out"),
     '["amenity"="fast_food"]', "fast-food spots"),
    (("bar", "pub", "drink", "beer"), '["amenity"~"bar|pub"]', "bars & pubs"),
    (("pharmacy", "chemist", "drugstore", "medicine"),
     '["amenity"="pharmacy"]', "pharmacies"),
    (("hospital", "clinic", "doctor", "emergency", "urgent care"),
     '["amenity"~"hospital|clinic|doctors"]', "medical facilities"),
    (("atm", "cash machine"), '["amenity"="atm"]', "ATMs"),
    (("bank",), '["amenity"="bank"]', "banks"),
    (("fuel", "gas station", "petrol", "gas"), '["amenity"="fuel"]', "fuel stations"),
    (("supermarket", "grocery", "groceries", "store"),
     '["shop"~"supermarket|convenience"]', "shops"),
    (("hotel", "motel", "stay", "lodging"),
     '["tourism"~"hotel|motel|guest_house"]', "places to stay"),
    (("park", "garden"), '["leisure"="park"]', "parks"),
    (("gym", "fitness"), '["leisure"~"fitness_centre|sports_centre"]', "gyms"),
    (("parking",), '["amenity"="parking"]', "parking"),
    (("school",), '["amenity"="school"]', "schools"),
    (("hospital",), '["amenity"="hospital"]', "hospitals"),
]


def _http_get(url: str, timeout: float = 12.0) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _http_post(url: str, body: str, timeout: float = 18.0) -> str:
    req = urllib.request.Request(url, data=body.encode("utf-8"), headers=_UA, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# Overpass mirrors, tried IN ORDER until one answers. The plain overpass-api.de is
# frequently overloaded — that's the "read operation timed out" the maps feature
# kept hitting. We lead with the fast lz4 mirror, keep the main as second, and end
# with an INDEPENDENT mirror (mail.ru) so an overload of the shared .de infra still
# resolves. (kumi.systems was dropped: its DNS often fails to resolve and wastes
# ~11s before erroring.) A tight per-mirror timeout fails a slow server over fast.
_OVERPASS_ENDPOINTS = (
    "https://lz4.overpass-api.de/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
)


def _overpass(query_body: str, timeout: float = 12.0) -> dict:
    """Run an Overpass QL query, RACING all mirrors at once and returning the first
    successful response. Overpass load is wildly variable (a mirror that answers in
    1s now may stall for 20s a minute later), so racing them gives the fastest
    AVAILABLE server every time instead of waiting on a sequential chain. Raises
    only if every mirror fails/times out."""
    import concurrent.futures
    body = "data=" + urllib.parse.quote(query_body)

    def _fetch(url: str) -> dict:
        return json.loads(_http_post(url, body, timeout=timeout))

    last_exc: "Exception | None" = None
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(_OVERPASS_ENDPOINTS)) as ex:
        futures = [ex.submit(_fetch, url) for url in _OVERPASS_ENDPOINTS]
        try:
            for fut in concurrent.futures.as_completed(futures, timeout=timeout + 2):
                try:
                    return fut.result()       # first mirror to answer successfully wins
                except Exception as exc:      # noqa: BLE001 — this mirror failed; wait for others
                    last_exc = exc
        except concurrent.futures.TimeoutError as exc:
            last_exc = exc
    raise last_exc or RuntimeError("all Overpass mirrors timed out")


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _pretty_dist(m: float) -> str:
    if m < 950:
        return f"{int(round(m / 10) * 10)} m"
    return f"{m / 1000:.1f} km"


# Specific-venue / entertainment qualifiers that OSM has no clean category for.
# A query containing one of these is a SPECIFIC place, not a generic category —
# so it must NOT be hijacked by a loose substring match (e.g. "trampoline park"
# catching the generic "park" category and returning playgrounds). These fall to
# a name search here; the prompt steers such asks to web_search in the first place.
_SPECIFIC_VENUE_HINTS = (
    "trampoline", "theme park", "water park", "amusement", "skate park",
    "go kart", "go-kart", "bowling", "arcade", "escape room", "laser tag",
    "aquarium", "zoo", "stadium", "cinema", "movie theat", "play area",
    "play centre", "play center", "indoor play",
)


def _classify(query: str) -> "tuple[str, str]":
    q = (query or "").lower().strip()
    # A specific named/entertainment venue → name search, never a generic category.
    specific = any(h in q for h in _SPECIFIC_VENUE_HINTS)
    if not specific:
        for keys, filt, label in _CATEGORIES:
            if any(k in q for k in keys):
                return filt, label
    # Unknown or specific query → fuzzy name search across the map's named places.
    if q:
        safe = q.replace('"', "")
        return f'["name"~"{safe}",i]', f"places matching “{query}”"
    return '["amenity"="restaurant"]', "restaurants"


def _google_rows(query: str, lat, lon, limit: int) -> "list | None":
    """Google Places (New) results in the same shape find_nearby returns, sorted by
    distance. None when Google returned nothing (so the caller tries OSM)."""
    raw = places_google.text_search(query, lat, lon, limit)
    if not raw:
        return None
    rows = []
    for r in raw:
        dist = _haversine_m(lat, lon, r["lat"], r["lon"])
        extra = f"{r['rating']}★" if r.get("rating") else ""
        rows.append({"name": r["name"], "dist": dist, "distance": _pretty_dist(dist),
                     "extra": extra, "lat": r["lat"], "lon": r["lon"],
                     "address": r.get("address", "")})
    rows.sort(key=lambda x: x["dist"])
    return rows[:limit]


def find_nearby(query: str, lat, lon, limit: int = 6) -> "tuple[bool, str, list]":
    """Find places near (lat, lon). Uses Google Places (New) when configured (best
    brand/POI coverage), else free OpenStreetMap. Returns (ok, spoken_text, items)."""
    if lat is None or lon is None:
        return (False, "I don't know where you are yet, sir — allow location access "
                       "and try again.", [])
    _, label = _classify(query)
    # Google first when available — it has the brand branches OSM is missing.
    if places_google.available():
        g = _google_rows(query, lat, lon, limit)
        if g:
            spoken = f"The nearest {label}: " + "; ".join(
                f"{r['name']} ({r['distance']}{', ' + r['extra'] if r['extra'] else ''})"
                for r in g) + "."
            return True, spoken, g
    filt, label = _classify(query)
    items = []
    # Grow the search only ONCE if the near radius is empty. The old 3-tier loop
    # could fire three sequential 18s Overpass calls (~54s worst case) in a sparse
    # area; two tiers + a 16s cap (just above Overpass's own 15s query timeout)
    # bounds that to ~32s while a populated first tier still resolves in one call.
    for radius in (2000, 9000):                        # near first, then wider
        q = (f"[out:json][timeout:12];"
             f"(node{filt}(around:{radius},{lat},{lon});"
             f"way{filt}(around:{radius},{lat},{lon}););"
             f"out center {max(limit * 4, 20)};")
        try:
            data = _overpass(q)                        # races all mirrors, fastest wins
        except Exception as exc:  # noqa: BLE001
            return False, f"I couldn't reach the maps service: {exc}", []
        seen = set()
        rows = []
        for el in data.get("elements", []):
            tags = el.get("tags", {})
            name = (tags.get("name") or "").strip()
            if not name or name.lower() in seen:
                continue
            elat = el.get("lat") or el.get("center", {}).get("lat")
            elon = el.get("lon") or el.get("center", {}).get("lon")
            if elat is None or elon is None:
                continue
            seen.add(name.lower())
            dist = _haversine_m(lat, lon, elat, elon)
            extra = tags.get("cuisine") or tags.get("amenity") or tags.get("shop") or ""
            rows.append({"name": name, "dist": dist, "distance": _pretty_dist(dist),
                         "extra": extra.replace("_", " "), "lat": elat, "lon": elon,
                         "address": tags.get("addr:street", "")})
        if rows:
            rows.sort(key=lambda r: r["dist"])
            items = rows[:limit]
            break
    if not items:
        return True, f"I couldn't find any {label} near you, sir.", []
    spoken = f"The nearest {label}: " + "; ".join(
        f"{r['name']} ({r['distance']}{', ' + r['extra'] if r['extra'] else ''})"
        for r in items
    ) + "."
    return True, spoken, items


def geocode(query: str, lat=None, lon=None, limit: int = 1) -> "dict | None":
    """Resolve a place name/address → {name, lat, lon}. None on failure.

    When the user's (lat, lon) is given, bias the search to their area (a brand
    like 'Blue Tokai' has branches everywhere — without a bias, bare 'Blue Tokai'
    resolved to Delhi for a Mumbai user) and return the NEAREST candidate, since
    Nominatim ranks by relevance, not distance."""
    q = _GEO_QUESTION_RE.sub("", (query or "").strip()).strip()
    q = _GEO_FILLER_RE.sub("", q).strip().rstrip("?.").strip()
    if not q:
        return None
    have_loc = lat is not None and lon is not None

    # Google Places (New) first when configured — resolves brands/addresses far more
    # reliably than Nominatim. Returns the NEAREST match (text_search is relevance-
    # ranked, so sort by distance when we know where the user is).
    if places_google.available():
        gr = places_google.text_search(q, lat, lon, 5, want_rating=False)
        if gr:
            if have_loc:
                gr.sort(key=lambda r: _haversine_m(lat, lon, r["lat"], r["lon"]))
            top = gr[0]
            return {"name": top.get("address") or top.get("name") or q,
                    "address": {}, "lat": top["lat"], "lon": top["lon"]}

    def _fetch(bounded: bool) -> list:
        params = {"q": q, "format": "json", "limit": max(limit, 1), "addressdetails": 1}
        if have_loc:
            d = 0.6   # ~60 km box around the user: west,north,east,south
            params["viewbox"] = f"{lon - d},{lat + d},{lon + d},{lat - d}"
            if bounded:
                params["bounded"] = 1       # RESTRICT to the box (a real local match)
        url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(params)
        try:
            data = json.loads(_http_get(url))
        except Exception:
            return []
        out = []
        for top in (data or []):
            try:
                out.append({"name": top.get("display_name", q),
                            "address": top.get("address") or {},
                            "lat": float(top["lat"]), "lon": float(top["lon"])})
            except (KeyError, TypeError, ValueError):
                continue        # skip malformed Nominatim rows
        return out

    # Prefer a result inside the user's area; only if none exists there fall back to
    # the best global match (so 'Blue Tokai'/'Starbucks' resolve LOCALLY, not to a
    # same-named place across the country/world).
    rows = _fetch(bounded=True) if have_loc else _fetch(bounded=False)
    if not rows and have_loc:
        rows = _fetch(bounded=False)
    if not rows:
        return None
    if have_loc and len(rows) > 1:
        rows.sort(key=lambda r: _haversine_m(lat, lon, r["lat"], r["lon"]))
    return rows[0]


def concise_label(geo: "dict | None") -> str:
    """A short 'Neighbourhood, City' label from a geocode result — instead of the
    full Nominatim display_name, which dumped a 15-part address (clinic name,
    apartment, road, ward, zone, postcode, country…) into the HUD's LOCATION field."""
    a = (geo or {}).get("address") or {}
    local = (a.get("suburb") or a.get("neighbourhood") or a.get("quarter")
             or a.get("city_district") or a.get("town") or a.get("village")
             or a.get("hamlet") or a.get("road") or "")
    city = (a.get("city") or a.get("town") or a.get("municipality")
            or a.get("county") or a.get("state_district") or a.get("state") or "")
    uniq = []
    for p in (local, city):
        if p and p.lower() not in {u.lower() for u in uniq}:
            uniq.append(p)
    if uniq:
        return ", ".join(uniq[:2])
    comps = [c.strip() for c in str((geo or {}).get("name") or "").split(",") if c.strip()]
    return ", ".join(comps[:2])


def directions(dest: str, lat, lon) -> "tuple[bool, str]":
    """Driving distance + time from (lat, lon) to a named destination."""
    if lat is None or lon is None:
        return False, ("I don't know your current location yet, sir — allow location "
                       "access and try again.")
    target = geocode(dest, lat, lon, limit=5)          # bias to user's area, pick nearest
    if not target:
        return False, f"I couldn't find “{dest}” on the map, sir."
    url = (f"https://router.project-osrm.org/route/v1/driving/"
           f"{lon},{lat};{target['lon']},{target['lat']}"
           f"?overview=false")
    try:
        data = json.loads(_http_get(url))
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't reach the routing service: {exc}"
    routes = data.get("routes") or []
    if not routes:
        return False, f"I couldn't find a driving route to “{dest}”, sir."
    r = routes[0]
    # OSRM normally returns numeric distance/duration, but guard against a
    # malformed route (missing/non-numeric fields) so a bad response degrades
    # gracefully instead of raising out of the action dispatcher.
    try:
        km = float(r["distance"]) / 1000.0
        mins = float(r["duration"]) / 60.0
    except (KeyError, TypeError, ValueError):
        return False, f"I got an odd route to “{dest}”, sir — try again in a moment."
    if mins >= 60:
        dur = f"{int(mins // 60)} h {int(mins % 60)} min"
    else:
        dur = f"{int(round(mins))} min"
    short = target["name"].split(",")[0]
    return True, (f"{short} is about {km:.1f} km away — roughly a {dur} drive, sir.")
