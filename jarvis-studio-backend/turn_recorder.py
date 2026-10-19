"""Record real chat turns as replay fixtures (test_replay.py).

Off unless JARVIS_RECORD_TURNS=1 is set when JARVIS starts. Then every pipeline turn is
appended to ``%APPDATA%\\Jarvis\\recorded_turns.jsonl`` in the shape of
replay_fixtures.json: the user's words and each model step in order — the tool calls it
made, or the text it answered. Add "results" (what each action returned) and "expect"
(what must happen) by hand, and the turn becomes a test that replays what a real model
did. It records what you said, so it's a developer switch, never on by default.
"""

from __future__ import annotations

import json
import os
import time

import jarvis_paths

_turn: "dict | None" = None


def enabled() -> bool:
    return os.environ.get("JARVIS_RECORD_TURNS") == "1"


def path():
    return jarvis_paths.app_data_dir() / "recorded_turns.jsonl"


def begin(user: str) -> None:
    global _turn
    _turn = {"user": user, "model": [], "recorded": time.strftime("%Y-%m-%d %H:%M")} \
        if enabled() else None


def step(tool_calls, text: str) -> None:
    """One model step of the current turn: its tool calls, or else its text."""
    if _turn is None:
        return
    calls = [{"name": c.get("name", ""), "args": c.get("args") or {}}
             for c in tool_calls or [] if isinstance(c, dict)]
    _turn["model"].append({"toolCalls": calls} if calls else {"text": text or ""})


def end() -> None:
    global _turn
    turn, _turn = _turn, None
    if not turn or not turn["model"]:
        return
    try:
        p = path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(turn, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"[Recorder] couldn't write {exc}", flush=True)
