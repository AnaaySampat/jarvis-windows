"""Direct file operations for the desktop autopilot — list, find, new folder,
rename, move, copy, new text file — done by the OS instead of by driving File
Explorer.

Explorer's UI is the slowest, most fragile route to these (2026-10-05 bench: F2
kept the extension selected-out, so a rename produced "x.txt.txt"; finding the
newest download took 27s of clicking column headers). Here each is one exact
step, and safe by construction:

  • only inside the user's own folders (their home, minus AppData) — never a
    system folder, another user's, or program files;
  • never overwrites: a destination that already exists is refused;
  • never deletes — there is no delete here (that stays a separately approved
    action, ``skills.delete_file``);
  • at most 200 items per call, so a loose pattern can't sweep a whole tree.

Gated by the same armed computer-control consent as every other actuation.
"""

from __future__ import annotations

import collections
import fnmatch
import os
import re
import shutil
import time
from pathlib import Path

from actions import app_launcher

_MAX_ITEMS = 200
_LIST_SHOWN = 60


def _home() -> Path:
    return Path.home().resolve()


def _resolve(raw) -> "tuple[Path | None, str]":
    """(path, "") or (None, why). A leading known-folder name ("Desktop\\a.txt",
    "Downloads") becomes the user's REAL folder (OneDrive-aware)."""
    s = os.path.expandvars(str(raw or "").strip().strip('"'))
    if not s:
        return None, "Which file or folder, sir? Give its full path."
    p = Path(s).expanduser()
    if not p.is_absolute():
        head, _, rest = s.replace("/", "\\").partition("\\")
        base = app_launcher.known_folder(head)
        if not base:
            return None, (f"“{s}” isn't a full path, sir — give it from the drive "
                          f"(C:\\…) or start it with Desktop, Documents or Downloads.")
        p = Path(base) / rest if rest else Path(base)
    try:
        p = p.resolve()
    except OSError as exc:
        return None, f"I can't use the path “{s}”: {exc}"
    home = _home()
    if not p.is_relative_to(home) or "appdata" in (
            part.lower() for part in p.relative_to(home).parts):
        return None, (f"“{p}” is outside your own folders, sir — I only move or create "
                      f"files under {home} (not AppData or system folders).")
    return p, ""


def _sources(raw) -> "tuple[list[Path], str]":
    """The paths a source names — one path, or a glob like Downloads\\*.pdf."""
    s = str(raw or "")
    if any(ch in s for ch in "*?["):
        folder, _, pattern = s.replace("/", "\\").rpartition("\\")
        base, err = _resolve(folder)
        if err:
            return [], err
        items = sorted(base.glob(pattern))
        if not items:
            return [], f"Nothing in {base} matches “{pattern}”, sir."
        if len(items) > _MAX_ITEMS:
            return [], (f"That matches {len(items)} items — more than the {_MAX_ITEMS} "
                        f"I'll move in one go, sir. Narrow the pattern.")
        return items, ""
    p, err = _resolve(s)
    if err:
        return [], err
    if not p.exists():
        return [], f"There's nothing at {p}, sir."
    return [p], ""


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def list_folder(path, pattern: str = "*", sort: str = "name") -> "tuple[bool, str, str]":
    """(ok, short message, listing) for a folder: name, size, modified — sorted by
    name, or "newest"/"oldest"/"largest". Hidden/system entries are skipped, as
    Explorer does."""
    base, err = _resolve(path)
    if err:
        return False, err, ""
    if not base.is_dir():
        return False, f"{base} isn't a folder, sir.", ""
    rows = []
    for e in base.glob(pattern or "*"):
        try:
            st = e.stat()
            if getattr(st, "st_file_attributes", 0) & 0x6:      # hidden | system
                continue
            rows.append((e.name, e.is_dir(), st.st_size, st.st_mtime))
        except OSError:
            continue
    key = (sort or "name").lower()
    if key.startswith(("new", "recent", "latest")):
        rows.sort(key=lambda r: -r[3])
    elif key.startswith("old"):
        rows.sort(key=lambda r: r[3])
    elif key.startswith(("large", "big", "size")):
        rows.sort(key=lambda r: -r[2])
    else:
        rows.sort(key=lambda r: (not r[1], r[0].lower()))
    lines = [f"{'[folder] ' if is_dir else ''}{name}"
             + ("" if is_dir else f"  {_human(size)}")
             + f"  modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}"
             for name, is_dir, size, mtime in rows[:_LIST_SHOWN]]
    more = f"\n…and {len(rows) - _LIST_SHOWN} more" if len(rows) > _LIST_SHOWN else ""
    # Counts by kind, so "how many PDFs are in Downloads?" is answered by this one
    # listing even when it shows only the first 60 rows.
    kinds = collections.Counter("folders" if is_dir else (os.path.splitext(name)[1].lower()
                                                          or "no extension")
                                for name, is_dir, _size, _mtime in rows)
    mix = ", ".join(f"{n} {kind}" for kind, n in kinds.most_common(8))
    listing = (f"{base} — {len(rows)} item{'s' if len(rows) != 1 else ''}"
               f"{'' if (pattern or '*') == '*' else f' matching {pattern}'}"
               f"{f' ({mix})' if mix else ''}, by {key}:\n" + "\n".join(lines) + more)
    return True, f"Listed {base.name} ({len(rows)} items).", listing


_FIND_SHOWN = 30
_FIND_BUDGET_S = 3.0
# Trees that are never what someone means by "my files" and can hold 100k entries.
_FIND_SKIP = {"node_modules", "__pycache__", "site-packages", "appdata", "$recycle.bin"}


def _junk(name: str) -> bool:
    low = name.lower()
    return low.startswith(".") or low in _FIND_SKIP or "venv" in low


def _is_glob(needle: str) -> bool:
    return any(ch in needle for ch in "*?[")


def _search_terms(needle: str) -> str:
    """Models wrap plain words in stars ('jarvis receipt*', '*jarvis*') — that's a
    word search, which also matches 'jarvis_receipt.txt'. Only an extension
    ('*.pdf') or an inner wildcard ('report*.pdf') stays a real pattern."""
    core = needle.strip("*").strip()
    return needle if needle.startswith("*.") or _is_glob(core) else core


def _words(needle: str) -> list:
    """The words a plain search names — split at spaces, _ and - like file names are."""
    return re.findall(r"[^\W_]+", needle)


def _matches(low: str, needle: str) -> bool:
    if _is_glob(needle):
        return fnmatch.fnmatch(low, needle)
    words = _words(needle)
    return bool(words) and all(w in low for w in words)


def _index_find(needle: str, scope: Path) -> "list | None":
    """Matches from the Windows Search index under ``scope`` — the whole home folder
    in ~0.1s — or None when it can't answer (search service off, comtypes missing,
    a pattern too slow for it)."""
    if "[" in needle:
        return None
    try:
        import calendar
        import comtypes
        import comtypes.client
    except ImportError:
        return None
    q = lambda s: s.replace("'", "''")                         # noqa: E731 — SQL quoting
    if _is_glob(needle):
        # LIKE scans the whole index: ~0.5s for '*.pdf', 13s once a digit is in it —
        # the command timeout hands those to the walk.
        where = f"System.FileName LIKE '{q(needle.replace('*', '%').replace('?', '_'))}'"
    else:
        # Word prefixes go through the full-text index instead: ~0.05s, digits or not.
        words = _words(needle)
        if not words:
            return None
        where = "CONTAINS(System.FileName, '" + " AND ".join(f'"{w}*"' for w in words) + "')"
    sql = ("SELECT TOP 300 System.ItemPathDisplay, System.ItemType, System.FileAttributes, "
           f"System.Size, System.DateModified FROM SystemIndex WHERE {where} "
           f"AND SCOPE='file:{q(scope.as_posix())}' ORDER BY System.DateModified DESC")
    try:
        comtypes.CoInitialize()
        uninit = True
    except OSError:
        uninit = False              # this thread already runs COM in another apartment
    try:
        conn = comtypes.client.CreateObject("ADODB.Connection")
        conn.CommandTimeout = 2
        conn.Open("Provider=Search.CollatorDSO;Extended Properties='Application=Windows';")
        rs = comtypes.client.CreateObject("ADODB.Recordset")
        rs.Open(sql, conn)
        rows = []
        while not rs.EOF:
            rows.append([rs.Fields.Item(i).Value for i in range(5)])
            rs.MoveNext()
        rs.Close()
        conn.Close()
    except Exception:  # noqa: BLE001 — service off, timeout, any COM failure: walk instead
        return None
    finally:
        if uninit:
            comtypes.CoUninitialize()
    hits = []
    for path, kind, attrs, size, modified in rows:
        p = Path(str(path or ""))
        try:
            rel = p.relative_to(scope).parts
        except ValueError:
            continue
        if (int(attrs or 0) & 0x6 or any(_junk(part) for part in rel)
                or not _matches(p.name.lower(), needle)):
            continue
        mtime = calendar.timegm(modified.timetuple()) if modified else 0  # index is UTC
        hits.append((str(p), kind == "Directory", int(size or 0), mtime))
    return hits


def _walk_find(needle: str, roots: list, budget_s: float) -> "tuple[list, bool]":
    """(matches, cut short) by walking ``roots`` breadth-first — shallow, likely
    matches first — until ``budget_s`` runs out."""
    hits, seen, end = [], set(), time.monotonic() + budget_s
    queue = collections.deque(roots)
    while queue:
        if time.monotonic() > end:
            return hits, True
        folder = queue.popleft()
        key = os.path.normcase(str(folder))
        if key in seen:
            continue
        seen.add(key)
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            low = e.name.lower()
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            # hidden | system | reparse point (junction loops), and dot folders
            if getattr(st, "st_file_attributes", 0) & 0x406 or low.startswith("."):
                continue
            is_dir = e.is_dir(follow_symlinks=False)
            if _matches(low, needle):
                hits.append((e.path, is_dir, st.st_size, st.st_mtime))
            if is_dir and not _junk(low):
                queue.append(Path(e.path))
    return hits, False


def find(pattern, path=None, budget_s: float = _FIND_BUDGET_S) -> "tuple[bool, str, str]":
    """(ok, short message, listing) of files/folders whose name matches ``pattern``
    — a glob (``*.pdf``) or words that must all appear in the name ("tax return")
    — under ``path``, else the whole home folder; newest first. The Windows Search
    index answers first; when it has nothing (or isn't running), a time-boxed walk
    of Desktop, Downloads, Documents, then the rest of home. Hidden/system and dot
    folders, AppData, junctions and dev junk (node_modules, venvs) are skipped."""
    needle = _search_terms(str(pattern or "").strip().lower())
    if not needle:
        return False, "What name should I look for, sir?", ""
    if path:
        base, err = _resolve(path)
        if err:
            return False, err, ""
        roots = [base]
    else:
        known = app_launcher.real_known_folders()
        roots = [Path(known[k]) for k in ("Desktop", "Downloads", "Documents")
                 if Path(known[k]).is_dir()] + [_home()]
    indexed = _index_find(needle, roots[-1])                # roots[-1]: home, or path
    hits, cut = indexed or [], False
    if not hits:
        # An index that answered leaves one blind spot — files made seconds ago,
        # which sit in the shallow user folders a short walk reaches first.
        hits, cut = _walk_find(needle, roots, budget_s if indexed is None else 1.0)
        cut = cut and indexed is None
    hits.sort(key=lambda h: -h[3])
    lines = [f"{'[folder] ' if is_dir else ''}{p}" + ("" if is_dir else f"  {_human(size)}")
             + f"  modified {time.strftime('%Y-%m-%d %H:%M', time.localtime(mtime))}"
             for p, is_dir, size, mtime in hits[:_FIND_SHOWN]]
    where = str(roots[0]) if path else "your Desktop, Downloads, Documents and home"
    more = f"\n…and {len(hits) - _FIND_SHOWN} more" if len(hits) > _FIND_SHOWN else ""
    partial = (f"\n(stopped after {budget_s:.0f}s — not every folder was searched; "
               f"give a folder to look deeper)") if cut else ""
    listing = (f"{len(hits)} match{'es' if len(hits) != 1 else ''} for “{pattern}” in "
               f"{where}, newest first:\n" + "\n".join(lines) + more + partial)
    if not hits:
        return True, f"Nothing named like “{pattern}” in {where}.", listing
    return True, f"Found {len(hits)} match{'es' if len(hits) != 1 else ''} for “{pattern}”.", listing


def make_folder(path) -> "tuple[bool, str]":
    p, err = _resolve(path)
    if err:
        return False, err
    if p.exists():
        return False, f"“{p.name}” already exists in {p.parent}, sir — nothing changed."
    p.mkdir(parents=True)
    return True, f"Created the folder {p}."


def write_text(path, text: str) -> "tuple[bool, str]":
    """Create a NEW text file holding ``text`` (never overwrites)."""
    p, err = _resolve(path)
    if err:
        return False, err
    if p.exists():
        return False, f"{p.name} already exists, sir — I won't overwrite it."
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(str(text or ""), encoding="utf-8")
    return True, f"Created {p} ({len(str(text or ''))} characters)."


def rename(path, new_name: str) -> "tuple[bool, str]":
    """Give one item a new name in the same folder — the FULL new name, extension
    included (``report.txt`` → ``final.txt``)."""
    name = str(new_name or "").strip()
    if not name or any(ch in name for ch in '\\/:*?"<>|'):
        return False, "Give just the new name (no folders or wildcards), sir."
    srcs, err = _sources(path)
    if err:
        return False, err
    if len(srcs) != 1:
        return False, "Rename one item at a time, sir."
    src = srcs[0]
    dst = src.with_name(name)
    if dst.exists():
        return False, f"Something called “{name}” already exists there, sir — nothing changed."
    src.rename(dst)
    return True, f"Renamed “{src.name}” to “{dst.name}”."


def transfer(src_raw, dst_raw, copy: bool = False) -> "tuple[bool, str]":
    """Move (or copy) items. An existing folder destination receives them; else the
    destination is the single item's new full path. Nothing is ever replaced: if
    any destination exists, nothing moves at all."""
    srcs, err = _sources(src_raw)
    if err:
        return False, err
    dst, err = _resolve(dst_raw)
    if err:
        return False, err
    if dst.is_dir():
        targets = [dst / s.name for s in srcs]
    elif len(srcs) == 1:
        targets = [dst]
    else:
        return False, f"{dst} isn't an existing folder to put {len(srcs)} items into, sir."
    clash = [t for t in targets if t.exists()]
    if clash:
        return False, (f"“{clash[0].name}” already exists in {clash[0].parent}, sir — I "
                       f"won't overwrite it, so nothing was {'copied' if copy else 'moved'}.")
    for s, t in zip(srcs, targets):
        if t == s or s in t.parents:
            return False, f"I can't put {s.name} inside itself, sir."
    for s, t in zip(srcs, targets):
        t.parent.mkdir(parents=True, exist_ok=True)
        if copy:
            (shutil.copytree if s.is_dir() else shutil.copy2)(s, t)
        else:
            shutil.move(str(s), str(t))
    verb = "Copied" if copy else "Moved"
    what = f"“{srcs[0].name}”" if len(srcs) == 1 else f"{len(srcs)} items"
    return True, f"{verb} {what} to {targets[0].parent if dst.is_dir() else targets[0]}."
