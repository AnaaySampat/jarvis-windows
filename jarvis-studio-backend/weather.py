"""Current weather + short hourly forecast via Open-Meteo (free, no API key).

Uses the lat/lon resolved from the public IP (see ``telemetry.get_net_info``).
Blocking HTTP → call via ``loop.run_in_executor``.
"""

from __future__ import annotations

import datetime as _dt
import json
import urllib.parse
import urllib.request

# WMO weather-code → (text, glyph) for the HUD.
_WMO = {
    0: ("Clear", "○"), 1: ("Mainly clear", "◔"), 2: ("Partly cloudy", "◑"),
    3: ("Overcast", "☁"), 45: ("Fog", "▒"), 48: ("Rime fog", "▒"),
    51: ("Light drizzle", "˙"), 53: ("Drizzle", "˙"), 55: ("Dense drizzle", "˙"),
    61: ("Light rain", "☂"), 63: ("Rain", "☂"), 65: ("Heavy rain", "☂"),
    66: ("Freezing rain", "☂"), 67: ("Freezing rain", "☂"),
    71: ("Light snow", "❄"), 73: ("Snow", "❄"), 75: ("Heavy snow", "❄"),
    77: ("Snow grains", "❄"), 80: ("Showers", "☂"), 81: ("Showers", "☂"),
    82: ("Violent showers", "☂"), 85: ("Snow showers", "❄"), 86: ("Snow showers", "❄"),
    95: ("Thunderstorm", "⚡"), 96: ("Thunderstorm", "⚡"), 99: ("Thunderstorm", "⚡"),
}


def _http_get(url: str, timeout: float = 7.0) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Jarvis/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _code_meta(code) -> "tuple[str, str]":
    try:
        return _WMO.get(int(code), ("—", "○"))
    except (TypeError, ValueError):
        return ("—", "○")


def get_weather(lat, lon, place: str = "") -> "dict | None":
    """Return current conditions + next-hours forecast, or None on failure."""
    if lat is None or lon is None:
        return None
    base = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m",
        "hourly": "temperature_2m,weather_code",
        "forecast_days": 1, "timezone": "auto", "wind_speed_unit": "mph",
    })
    try:
        data = json.loads(_http_get(base))
    except Exception:
        return None

    cur = data.get("current", {})
    cond, _glyph = _code_meta(cur.get("weather_code"))

    # next 6 hourly slots starting at the current hour
    hours = []
    try:
        times = data["hourly"]["time"]
        temps = data["hourly"]["temperature_2m"]
        codes = data["hourly"]["weather_code"]
        # "now" must be in the TARGET location's timezone, not the host's — with
        # timezone=auto the API stamps current.time in local time, so prefer it.
        # (Host clock would mis-place "NOW" for any remote location.)
        api_now = cur.get("time")
        now_hour = (api_now[:13] + ":00") if isinstance(api_now, str) and len(api_now) >= 13 \
            else _dt.datetime.now().strftime("%Y-%m-%dT%H:00")
        start = next((i for i, t in enumerate(times) if t >= now_hour), 0)
        for i in range(start, min(start + 6, len(times))):
            hh = times[i][11:13]
            _, glyph = _code_meta(codes[i])
            hours.append({"t": "NOW" if i == start else hh, "i": glyph, "c": temps[i]})
    except Exception:
        hours = []

    aqi = _get_aqi(lat, lon)

    return {
        "temp": cur.get("temperature_2m"),
        "condition": cond,
        "location": place or "—",
        "humidity": cur.get("relative_humidity_2m"),
        "wind": f"{round(cur.get('wind_speed_10m', 0))} mph" if cur.get("wind_speed_10m") is not None else "—",
        "aqi": aqi,
        "hours": hours,
    }


def forecast_summary(lat, lon, place: str = "", days: int = 3) -> "str | None":
    """A spoken-friendly current + multi-day forecast string for the model to read
    back, e.g. 'In London it's 12°C and overcast… Tomorrow: 8–14°C, light rain.'
    Returns None on failure so the caller can fall back gracefully."""
    if lat is None or lon is None:
        return None
    try:
        days = int(days or 3)
    except (TypeError, ValueError):
        days = 3
    days = max(1, min(7, days))
    url = "https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m,apparent_temperature",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "forecast_days": days, "timezone": "auto", "wind_speed_unit": "mph",
    })
    try:
        data = json.loads(_http_get(url))
    except Exception:
        return None
    cur = data.get("current", {})
    cond, _ = _code_meta(cur.get("weather_code"))
    where = f" in {place}" if place and place != "—" else ""
    parts = []
    if cur.get("temperature_2m") is not None:
        feels = cur.get("apparent_temperature")
        feels_s = f" (feels like {round(feels)}°C)" if feels is not None else ""
        parts.append(f"Right now{where} it's {round(cur['temperature_2m'])}°C{feels_s} and "
                     f"{cond.lower()}, humidity {cur.get('relative_humidity_2m', '—')}%, "
                     f"wind {round(cur.get('wind_speed_10m', 0))} mph.")
    try:
        d = data["daily"]
        # Anchor day labels to the API LOCATION's local date (timezone=auto), not
        # the host's — otherwise "Tomorrow"/weekday names are off by one when the
        # user and the queried place are in different timezones.
        cur_time = cur.get("time") or ""
        try:
            base_date = _dt.date.fromisoformat(cur_time[:10])
        except ValueError:
            base_date = _dt.date.today()
        labels = ["Today", "Tomorrow"] + [
            (base_date + _dt.timedelta(days=i)).strftime("%A") for i in range(2, days)
        ]
        for i in range(min(days, len(d.get("time", [])))):
            cc, _ = _code_meta(d["weather_code"][i])
            lo, hi = round(d["temperature_2m_min"][i]), round(d["temperature_2m_max"][i])
            pops = d.get("precipitation_probability_max") or []
            pop = pops[i] if i < len(pops) else None   # may be a legit 0
            rain = f", {pop}% chance of rain" if pop is not None else ""
            parts.append(f"{labels[i]}: {lo}–{hi}°C, {cc.lower()}{rain}.")
    except Exception:
        pass
    return " ".join(parts) if parts else None


def _get_aqi(lat, lon) -> "int | None":
    try:
        url = "https://air-quality-api.open-meteo.com/v1/air-quality?" + urllib.parse.urlencode({
            "latitude": lat, "longitude": lon, "current": "us_aqi",
        })
        data = json.loads(_http_get(url))
        val = data.get("current", {}).get("us_aqi")
        return int(val) if val is not None else None
    except Exception:
        return None
