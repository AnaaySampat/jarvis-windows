"""A reply as it should sound: the words, without Markdown's punctuation.

The chat renders **bold** and pipe tables (utils/markdown.js in the GUI); read aloud
raw they came out as "asterisk asterisk" and "vertical bar". A table is read row by
row, its cells separated by pauses. Works line by line, so it also holds for the single
sentences main.py streams to the voice as they arrive. Port of the Android app's
brain/formatting.ts stripMarkdownForSpeech.
"""

from __future__ import annotations

import re

_DIVIDER = re.compile(r"^\s*\|?\s*:?-+:?\s*(?:\|\s*:?-+:?\s*)*\|?\s*$")
_EDGES = (
    (re.compile(r"!\[([^\]]*)\]\([^\s)]+(?:\s+[^)]*)?\)"), r"\1"),   # image → alt text
    (re.compile(r"\[([^\]]+)\]\([^\s)]+(?:\s+[^)]*)?\)"), r"\1"),    # link → its label
    (re.compile(r"`{1,3}([^`]+)`{1,3}"), r"\1"),
    (re.compile(r"~~(?=\S)([\s\S]*?\S)~~"), r"\1"),
    (re.compile(r"^\s{0,3}#{1,6}\s+", re.M), ""),                     # headings
    (re.compile(r"^(\s*)[-+*]\s+", re.M), r"\1"),                     # bullets
    (re.compile(r"^(\s*)\d+[.)]\s+", re.M), r"\1"),                   # numbered items
)
# Longest marker first so ***x*** goes as one unit; paired markers only, so 2*3*4 and
# snake_case survive.
_EMPHASIS = [re.compile(r"(^|[^\w\\])" + re.escape(m) + r"(?=\S)([\s\S]*?\S)" + re.escape(m),
                        re.M)
             for m in ("***", "___", "**", "__", "*", "_")]


def _row(line: str) -> str:
    cells = [c.strip().replace("\\|", "|")
             for c in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
    return ", ".join(c for c in cells if c) + "."


def speakable(text: str) -> str:
    lines = []
    for line in (text or "").split("\n"):
        stripped = line.strip()
        if "|" in line and _DIVIDER.match(line):
            continue                                   # a table's |---|---| row
        if stripped.startswith("|") and stripped.endswith("|") and stripped.count("|") >= 2:
            line = _row(stripped)
        lines.append(line)
    spoken = "\n".join(lines)
    for rx, sub in _EDGES:
        spoken = rx.sub(sub, spoken)
    for rx in _EMPHASIS:
        spoken = rx.sub(r"\1\2", spoken)
    return spoken
