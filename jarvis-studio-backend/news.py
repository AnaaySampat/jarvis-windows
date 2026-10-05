"""Top news headlines via free RSS feeds — no API key.

``get_headlines(topic, n)`` fetches a handful of current headlines (optionally on
a topic, via Google News search RSS) and returns a spoken-friendly summary plus
the structured list, for the model to read back. Best-effort; never raises.
"""

from __future__ import annotations

import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

_UA = {"User-Agent": "Jarvis/1.0"}

# Topic → Google News RSS section (no key). Falls back to a keyword search.
_SECTIONS = {
    "world": "WORLD", "business": "BUSINESS", "tech": "TECHNOLOGY",
    "technology": "TECHNOLOGY", "science": "SCIENCE", "sport": "SPORTS",
    "sports": "SPORTS", "health": "HEALTH", "entertainment": "ENTERTAINMENT",
}


def _http_get(url: str, timeout: float = 9.0) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _feed_url(topic: str) -> str:
    t = (topic or "").lower().strip()
    base = "https://news.google.com/rss"
    params = "?hl=en&gl=US&ceid=US:en"
    if not t or t in ("news", "top", "headlines", "latest"):
        return base + params
    section = _SECTIONS.get(t)
    if section:
        return f"{base}/headlines/section/topic/{section}{params}"
    return f"{base}/search?q={urllib.parse.quote(t)}&hl=en&gl=US&ceid=US:en"


def get_headlines(topic: str = "", n: int = 5) -> "tuple[bool, str, list]":
    """Return (ok, spoken_summary, items) for the latest headlines."""
    try:
        n = int(n or 5)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(10, n))
    try:
        raw = _http_get(_feed_url(topic))
        root = ET.fromstring(raw)
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't fetch the news just now: {exc}", []
    items = []
    for item in root.iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        # Google News titles end with " - Source"; split it out.
        source = ""
        if " - " in title:
            title, source = title.rsplit(" - ", 1)
        items.append({"title": title.strip(), "source": source.strip(),
                      "link": (item.findtext("link") or "").strip()})
        if len(items) >= n:
            break
    if not items:
        return True, "I couldn't find any headlines right now, sir.", []
    label = f" on {topic}" if topic and topic.lower() not in ("news", "top", "latest") else ""
    spoken = (f"Here are the top headlines{label}, sir: "
              + " ".join(f"{i + 1}. {h['title']}." for i, h in enumerate(items)))
    return True, spoken, items
