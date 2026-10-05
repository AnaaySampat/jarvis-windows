"""Read-only filesystem access — JARVIS's "limited terminal".

SAFETY FIRST. This module can ONLY:
  • list the contents of directories the user has explicitly whitelisted, and
  • read the text of a file (which is additionally gated by an Approve/Deny
    prompt every single time — see actions.needs_permission).

It can NEVER execute commands, write, move, rename, or delete anything. There is
no shell here at all; listing is done with ``os.scandir`` and reading with a
plain file open in text mode. Both operations refuse any path that is not inside
one of the user-approved directories, so JARVIS can never wander outside the
folders you've allowed in Settings.

The allowlist itself is owned by the config (groq_bridge) and pushed in via
``set_allowed`` at startup and whenever the user edits it.
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Tuple

# Directories the user has approved. JARVIS may list these (and their
# sub-directories) and read files within them — nothing else.
_allowed: List[Path] = []

# Files larger than this are summarised by size rather than read wholesale, to
# avoid blowing the model's context window.
_MAX_READ_BYTES = 200_000
_MAX_RETURN_CHARS = 8_000
# Extensions we treat as plain text and are willing to read.
_TEXT_SUFFIXES = {
    "", ".txt", ".md", ".markdown", ".log", ".csv", ".tsv", ".json", ".yaml",
    ".yml", ".toml", ".ini", ".cfg", ".conf", ".py", ".js", ".jsx", ".ts",
    ".tsx", ".html", ".htm", ".css", ".xml", ".rs", ".go", ".java", ".c",
    ".h", ".cpp", ".hpp", ".sh", ".bat", ".ps1", ".rb", ".php", ".sql",
}


def set_allowed(dirs) -> None:
    """Replace the whitelist. ``dirs`` is an iterable of path strings."""
    global _allowed
    out: List[Path] = []
    for d in dirs or []:
        try:
            p = Path(str(d)).expanduser().resolve()
            if p.is_dir():
                out.append(p)
        except Exception:  # noqa: BLE001
            continue
    _allowed = out


def get_allowed() -> List[str]:
    return [str(p) for p in _allowed]


def _resolve(raw) -> "Path | None":
    try:
        return Path(str(raw).strip().strip('"')).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return None


def _is_within_allowed(p: Path) -> bool:
    for root in _allowed:
        try:
            p.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def is_allowed(path) -> bool:
    p = _resolve(path)
    return p is not None and _is_within_allowed(p)


def list_dir(path) -> "Tuple[bool, str, str | None]":
    """List a directory's contents (read-only). Refuses anything outside the
    whitelist. Returns (ok, short_message, listing_for_model)."""
    if not _allowed:
        return (False, "I don't have any approved directories yet — add one in "
                "Settings under 'Read-only Terminal' first, sir.", None)
    p = _resolve(path)
    if p is None:
        return False, "That doesn't look like a valid path.", None
    if not _is_within_allowed(p):
        return (False, f"'{p}' isn't in your approved directories, so I can't look "
                "there. Add it in Settings if you'd like me to.", None)
    if not p.exists():
        return False, f"There's nothing at {p}.", None
    if not p.is_dir():
        return False, f"{p.name} is a file, not a directory.", None
    try:
        entries = sorted(p.iterdir(), key=lambda e: (e.is_file(), e.name.lower()))
    except PermissionError:
        return False, f"Windows won't let me read {p}.", None
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't list {p}: {exc}", None

    lines: List[str] = []
    for e in entries[:200]:
        try:
            if e.is_dir():
                lines.append(f"[DIR]  {e.name}")
            else:
                size = e.stat().st_size
                lines.append(f"       {e.name}  ({_human_size(size)})")
        except Exception:  # noqa: BLE001
            lines.append(f"       {e.name}")
    extra = f"\n…and {len(entries) - 200} more" if len(entries) > 200 else ""
    listing = f"Contents of {p}:\n" + "\n".join(lines) + extra
    n = len(entries)
    return True, f"Listing {p} — {n} item{'s' if n != 1 else ''}.", listing


def read_file(path) -> "Tuple[bool, str, str | None]":
    """Read a text file (read-only). Refuses anything outside the whitelist and
    anything that isn't plain text. The Approve/Deny gate happens upstream in the
    dispatcher. Returns (ok, short_message, file_text)."""
    if not _allowed:
        return (False, "I have no approved directories — add one in Settings "
                "before I can read files, sir.", None)
    p = _resolve(path)
    if p is None:
        return False, "That doesn't look like a valid path.", None
    if not _is_within_allowed(p):
        return (False, f"'{p}' is outside your approved directories, so I won't "
                "read it. Add its folder in Settings if you want me to.", None)
    if not p.exists() or not p.is_file():
        return False, f"I couldn't find a file at {p}.", None
    # PDFs route through the dedicated extractor.
    if p.suffix.lower() == ".pdf":
        from . import skills
        return skills.read_pdf(str(p))
    if p.suffix.lower() not in _TEXT_SUFFIXES:
        return (False, f"{p.name} isn't a text file I can read "
                f"({p.suffix or 'no extension'}).", None)
    try:
        size = p.stat().st_size
        if size > _MAX_READ_BYTES:
            return (False, f"{p.name} is {_human_size(size)} — too large for me to "
                    "read into context safely.", None)
        text = p.read_text(encoding="utf-8", errors="replace").strip()
    except Exception as exc:  # noqa: BLE001
        return False, f"I couldn't read {p.name}: {exc}", None
    if not text:
        return False, f"{p.name} is empty.", None
    return True, f"Read {p.name} ({_human_size(size)}).", text[:_MAX_RETURN_CHARS]


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"
