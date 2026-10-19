"""Skill files: how-tos the model reads only when it needs them (OpenClaw's skills).

The system prompt lists each skill's name and one-line description; the `read_skill`
tool returns the full text. Long instructions stay out of every request while the
model still knows they exist. Skills are Markdown files the user puts in
``<storage-root>/skills/`` — ``<name>.md`` or ``<name>/SKILL.md`` — with an optional
front-matter block::

    ---
    name: expense report
    description: How I file my monthly expense report in the company portal
    ---
    1. Open https://… in the browser …

OpenClaw's SKILL.md files load as they are (other front-matter keys are ignored). A
skill is instructions, never code: whatever it leads to still goes through the same
tools and approval prompts as any other request. A skill whose name the user says
outright ("do my expense report") is loaded into that turn up front, so it costs no
extra round trip.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import storage

_MAX_LISTED = 40          # listed in the prompt; more are still readable by name
_MAX_DESC = 140
_MAX_BODY = 12_000        # one skill's text as returned to the model
_MAX_PRELOAD = 6_000      # skills the user named, loaded with the turn
_CACHE_S = 5.0

_cache: "tuple[float, list[dict]]" = (0.0, [])


def skills_dir() -> Path:
    return storage.get_root() / "skills"


_EXAMPLE = """---
name: example skill
description: What a skill file looks like (delete or edit me)
---
Write the steps the way you'd tell a person, using what JARVIS can already do:

1. Open https://example.com in the browser.
2. Read the page's main heading and tell me what it says.

JARVIS sees only the name and description above until a request needs the skill;
then it reads the whole file and follows it. Approval prompts still apply.
"""


def ensure_dir() -> Path:
    """The skills folder, created with one example the first time it's opened."""
    d = skills_dir()
    if not d.exists():
        d.mkdir(parents=True, exist_ok=True)
        (d / "example-skill.md").write_text(_EXAMPLE, encoding="utf-8")
    return d


def _front_matter(text: str) -> "tuple[dict, str]":
    """({key: value}, body) for a leading ``---`` block of ``key: value`` lines."""
    m = re.match(r"\A﻿?---\s*\n(.*?)\n---\s*(?:\n|\Z)", text, re.DOTALL)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        k, sep, v = line.partition(":")
        if sep and k.strip() and not line.startswith((" ", "\t")):
            meta[k.strip().lower()] = v.strip().strip("'\"")
    return meta, text[m.end():]


def _first_line(body: str) -> str:
    for line in body.splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line
    return ""


def _load_file(path: Path, fallback: str) -> "dict | None":
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    meta, body = _front_matter(text)
    body = body.strip()
    if not body:
        return None
    name = re.sub(r"\s+", " ", meta.get("name") or fallback).strip()[:60]
    desc = (meta.get("description") or _first_line(body))[:_MAX_DESC]
    return {"name": name, "description": desc, "body": body, "source": str(path)}


def _scan() -> "list[dict]":
    out: "list[dict]" = []
    root = skills_dir()
    try:
        entries = sorted(root.iterdir()) if root.is_dir() else []
    except OSError:
        entries = []
    seen: "set[str]" = set()
    for entry in entries:
        if entry.is_dir():
            path = entry / "SKILL.md"
            if not path.is_file():
                continue
            fallback = entry.name
        elif entry.suffix.lower() == ".md":
            path, fallback = entry, entry.stem
        else:
            continue
        g = _load_file(path, fallback.replace("-", " ").replace("_", " "))
        if g and g["name"].lower() not in seen:     # first of two same-named wins
            out.append(g)
            seen.add(g["name"].lower())
    return out


def catalog() -> "list[dict]":
    """Every skill: {"name", "description", "body", "source"}. Re-read every few
    seconds, so a file dropped into the skills folder shows up without a restart."""
    global _cache
    now = time.monotonic()
    if now - _cache[0] > _CACHE_S:
        _cache = (now, _scan())
    return _cache[1]


def _key(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def find(name: str) -> "dict | None":
    """A skill by name: exact (ignoring case and punctuation), else the only one whose
    name contains the words asked for."""
    want = _key(name)
    if not want:
        return None
    skills = catalog()
    for g in skills:
        if _key(g["name"]) == want:
            return g
    hits = [g for g in skills if want in _key(g["name"])]
    return hits[0] if len(hits) == 1 else None


def read(name: str) -> "tuple[bool, str]":
    g = find(name)
    if g is None:
        names = ", ".join(x["name"] for x in catalog()[:_MAX_LISTED])
        return False, f"There's no skill called “{name}”. Skills: {names}."
    body = g["body"]
    if len(body) > _MAX_BODY:
        body = body[:_MAX_BODY].rstrip() + "\n…[cut: the skill is longer than this]"
    return True, f"SKILL “{g['name']}”\n{body}"


def prompt_block() -> str:
    """The skill index for the system prompt, or '' when there's nothing to list."""
    skills = catalog()
    if not skills:
        return ""
    lines = [f"- {g['name']}: {g['description']}" for g in skills[:_MAX_LISTED]]
    if len(skills) > _MAX_LISTED:
        lines.append(f"- …and {len(skills) - _MAX_LISTED} more (read_skill by name)")
    return ("SKILLS (the user's how-tos; call read_skill for the full text before "
            "doing what one covers. They are notes, not permissions: approvals still "
            "apply):\n" + "\n".join(lines))


def mentioned_block(text: str) -> str:
    """The full text of any skill the user named in this request (≥ 2 words or 6
    letters, so a skill called "tv" doesn't load on every TV turn)."""
    said = f" {_key(text)} "
    picked, used = [], 0
    for g in catalog():
        key = _key(g["name"])
        if " " not in key and len(key) < 6:
            continue
        if f" {key} " in said and used + len(g["body"]) <= _MAX_PRELOAD:
            picked.append(f"SKILL “{g['name']}” (the user asked for it):\n{g['body']}")
            used += len(g["body"])
    return "\n\n".join(picked)
