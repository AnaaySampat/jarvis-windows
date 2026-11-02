"""A requested PC screenshot is a capture, never an operator clicking its own HUD."""
import re

_REQUEST = re.compile(
    r"^(?:(?:please|can you|could you|would you)\s+)?"
    r"(?:take|capture|get|send|show|give|share)\s+(?:me\s+)?(?:a\s+|the\s+|one\s+)?"
    r"(?:screenshot|screen\s*shot|screen capture)"
    r"(?:\s+(?:of|from)\s+(?:my\s+|the\s+|your\s+)?(?:pc|computer|laptop|desktop|screen))?"
    r"(?:\s+(?:(?:and\s+)?(?:send|show|share)\s+(?:it\s+)?)?to\s+(?:me|my phone))?"
    r"[.!?]*$", re.IGNORECASE,
)


def requested(goal):
    return bool(_REQUEST.fullmatch(" ".join(str(goal or "").split())))
