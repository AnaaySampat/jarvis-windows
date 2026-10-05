"""Single, user-chosen home for everything JARVIS creates.

Historically QR codes/screenshots went to ~/Pictures/Jarvis, PDFs to
~/Documents/Jarvis and recordings to ~/Videos/Jarvis — three different places,
none of which JARVIS actually knew about, so "open the file you just made" sent
it hunting in the wrong directory.

Now there is ONE root (default ``~/Jarvis``, chosen by the user at first run and
stored as ``storage_dir`` in config.json) with three subfolders:

    <root>/images        QR codes, screenshots, generated images
    <root>/recordings    audio / video / screen captures
    <root>/documents     PDFs and other generated docs

``groq_bridge`` owns the *write* side of config.json; this module only reads
``storage_dir`` from it (and writes are funnelled through ``set_root`` which
delegates back), so there's a single writer and no clobbering.
"""

from __future__ import annotations

from pathlib import Path

import jarvis_paths

DEFAULT_ROOT = Path.home() / "Jarvis"
_SUBDIRS = ("images", "recordings", "documents")


def _read_config() -> dict:
    return jarvis_paths.read_config()


def get_root() -> Path:
    raw = (_read_config().get("storage_dir") or "").strip().strip('"')
    return Path(raw).expanduser() if raw else DEFAULT_ROOT


def _points_at_other_windows_profile(path: Path) -> bool:
    """Detect stale packaged paths like C:\\Users\\someone-else\\Jarvis."""
    try:
        parts = [p.lower() for p in path.parts]
        home_parts = [p.lower() for p in Path.home().parts]
    except Exception:  # noqa: BLE001
        return False
    if len(parts) < 3 or len(home_parts) < 3:
        return False
    if parts[1] != "users" or home_parts[1] != "users":
        return False
    shared_profiles = {"public", "default", "default user", "all users"}
    return parts[2] not in shared_profiles and parts[2] != home_parts[2]


def is_configured() -> bool:
    """True once the user has explicitly chosen a storage location."""
    raw = (_read_config().get("storage_dir") or "").strip().strip('"')
    if not raw:
        return False
    root = Path(raw).expanduser()
    if _points_at_other_windows_profile(root):
        return False
    try:
        return root.exists() and root.is_dir()
    except Exception:  # noqa: BLE001
        return False


def ensure_dirs() -> Path:
    root = get_root()
    try:
        root.mkdir(parents=True, exist_ok=True)
        for sub in _SUBDIRS:
            (root / sub).mkdir(parents=True, exist_ok=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[Storage] Couldn't create {root}: {exc}", flush=True)
    return root


def _sub(name: str) -> Path:
    d = get_root() / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def images_dir() -> Path:
    return _sub("images")


def recordings_dir() -> Path:
    return _sub("recordings")


def documents_dir() -> Path:
    return _sub("documents")


def all_dirs() -> "list[str]":
    """Root + every subfolder, for auto-allowlisting the read-only terminal so
    JARVIS can list/read/open the files it created."""
    root = get_root()
    return [str(root)] + [str(root / s) for s in _SUBDIRS]


def is_in_storage(path) -> bool:
    """True when a resolved path is inside JARVIS's storage root."""
    try:
        Path(path).expanduser().resolve().relative_to(get_root().resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def describe() -> str:
    """One compact line telling JARVIS exactly where its own files live."""
    root = get_root()
    return (
        f"Your files are saved under {root}. Specifically: images, QR codes and "
        f"screenshots go in {root / 'images'}; audio/video/screen recordings in "
        f"{root / 'recordings'}; PDFs and documents in {root / 'documents'}. "
        f"When the user asks you to open a file you just made, use the open_file "
        f"action with just the file name — it's resolved inside these folders."
    )


# Keyword → subfolder, so a vague reference ("the screen recording", "my last
# screenshot") still lands in the right place.
_KEYWORD_SUBDIR = {
    "recordings": ("record", "recording", "screen-rec", "screencast", "video",
                   "webcam", "audio", "voice", "mic", "screen rec"),
    "images": ("screenshot", "screen shot", "qr", "image", "picture", "photo",
               "img", "snap"),
    "documents": ("pdf", "document", "doc", "report", "notes", "text_to_pdf"),
}


def _newest(paths) -> "Path | None":
    files = [p for p in paths if p.is_file()]
    return max(files, key=lambda x: x.stat().st_mtime) if files else None


def resolve(name_or_path) -> Path:
    """Turn a bare/approximate file reference into the actual file on disk.

    Tries, in order, the most-recently-modified match for: an existing path
    inside storage → exact name in a storage folder → ``*stem*`` wildcard →
    same file *extension* → a keyword that maps to a subfolder (so "open the screen recording" or a
    wrong guess like "recording_1.mp4" still opens the latest screen capture).
    Returns the original path (which may not exist) if nothing matches.
    """
    raw = str(name_or_path or "").strip().strip('"')
    p = Path(raw).expanduser()
    if p.exists() and is_in_storage(p):
        return p

    roots = [get_root()] + [get_root() / s for s in _SUBDIRS]
    name = p.name or raw
    low = name.lower()

    # 1) exact name in any storage folder
    hit = _newest(d / name for d in roots if (d / name).exists())
    # 2) wildcard on the stem. Skip absurdly short stems (e.g. "a", "qr") — a
    #    1–2 char `*stem*` glob matches almost everything and would resolve to a
    #    random recent file; let those fall through to extension/keyword matching.
    if not hit:
        stem = name.strip("*") or name
        if len(stem) >= 3:
            cands: "list[Path]" = []
            for d in roots:
                try:
                    cands += list(d.glob(f"*{stem}*"))
                except Exception:  # noqa: BLE001
                    pass
            hit = _newest(cands)
    # 3) same extension, newest
    if not hit and p.suffix:
        cands = []
        for d in roots:
            try:
                cands += list(d.glob(f"*{p.suffix.lower()}"))
            except Exception:  # noqa: BLE001
                pass
        hit = _newest(cands)
    # 4) keyword → newest file in the matching subfolder
    if not hit:
        for sub, words in _KEYWORD_SUBDIR.items():
            if any(w in low for w in words):
                d = get_root() / sub
                try:
                    hit = _newest(d.glob("*"))
                except Exception:  # noqa: BLE001
                    hit = None
                if hit:
                    break

    return hit or p
