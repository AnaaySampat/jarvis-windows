"""What JARVIS is doing right now, for whoever isn't looking at the HUD.

A WhatsApp "Jarvis, status" (and the busy notice a command gets while another one runs)
answers from here, without waiting for the turn in progress. It is fed by a tap on
every HUD event (websocket_server.add_tap, installed in main), so it sees exactly what
the HUD sees: the ask, the thinking/speaking status, an autopilot task's goal, steps
and screenshots, and the reply. Commands waiting their turn are counted by main
(``queued``/``dequeued``).

Pure apart from the clock, which tests pass in.
"""

from __future__ import annotations

import re
import time

_STEPS_KEPT = 20          # per task; the status reply shows the last few
_STEPS_SHOWN = 4
_RECENT_S = 30 * 60       # a finished task older than this isn't "the last thing I did"

_KIND = {"browser": "browser task", "computer": "desktop task", "tv": "TV task"}
_SOURCE = {"phone": " from your phone", "whatsapp": " from WhatsApp"}


def _ago(seconds: float) -> str:
    s = max(0, int(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s} s" if s else f"{m} min"
    h, m = divmod(m, 60)
    return f"{h} h {m} min"


def _clip(text: str, n: int = 160) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


# A HUD bubble starts with where the ask came from (main._run_whatsapp_turn).
_ORIGIN = re.compile(r"^\s*(?:📱|💻)\s*(?:📎\s*\S+\s*)?")


class Activity:
    def __init__(self, clock=time.time):
        self.clock = clock
        self.status = "idle"
        self.ask, self.ask_at = "", 0.0
        self.reply = ""
        self.task: "dict | None" = None
        self.last_task: "dict | None" = None
        self.waiting: "list[str]" = []

    # ── The tap ───────────────────────────────────────────────────────────────

    def on_event(self, event: str, data) -> None:
        if event == "status" and isinstance(data, str):
            self.status = data
        elif event == "transcription":
            text = data.get("text") if isinstance(data, dict) else data
            if isinstance(text, str) and text.strip():
                self.ask, self.ask_at = _ORIGIN.sub("", text).strip(), self.clock()
        elif event == "agent_task" and isinstance(data, dict):
            self.task = {"id": data.get("id"), "kind": str(data.get("kind") or "browser"),
                         "goal": str(data.get("goal") or ""),
                         "source": str(data.get("source") or ""),
                         "started": self.clock(), "steps": [], "shot": ""}
        elif event == "agent_step" and isinstance(data, dict):
            task = self._task(data)
            if task is not None and data.get("line"):
                task["steps"] = (task["steps"] + [str(data["line"])])[-_STEPS_KEPT:]
        elif event == "agent_shot" and isinstance(data, dict):
            task = self._task(data)
            if task is not None and data.get("image"):
                task["shot"] = str(data["image"])
        elif event == "agent_task_end" and isinstance(data, dict):
            task = self._task(data)
            if task is not None:
                task.update(ended=self.clock(), ok=bool(data.get("ok")),
                            stopped=bool(data.get("stopped")),
                            summary=str(data.get("summary") or ""))
                self.last_task, self.task = task, None
        elif event == "response_end" and isinstance(data, dict):
            if str(data.get("text") or "").strip():
                self.reply = str(data["text"]).strip()
        elif event == "response" and isinstance(data, str) and data.strip():
            self.reply = data.strip()

    def _task(self, data: dict) -> "dict | None":
        t = self.task
        return t if t is not None and t.get("id") == data.get("id") else None

    # ── Commands waiting for the one in progress ──────────────────────────────

    def queued(self, text: str) -> None:
        self.waiting.append(_clip(text, 80))

    def dequeued(self, text: str) -> None:
        clipped = _clip(text, 80)
        if clipped in self.waiting:
            self.waiting.remove(clipped)

    # ── What to tell someone ──────────────────────────────────────────────────

    def busy(self) -> bool:
        return self.task is not None or self.status in ("thinking", "speaking", "working")

    def headline(self) -> str:
        """One line: what's running now ('' when nothing is)."""
        t = self.task
        if t is not None:
            return f"a {_KIND.get(t['kind'], 'task')}{_SOURCE.get(t['source'], '')}: “{_clip(t['goal'], 90)}”"
        if self.status in ("thinking", "speaking", "working") and self.ask:
            return f"answering “{_clip(self.ask, 90)}”"
        return ""

    def describe(self) -> str:
        now = self.clock()
        lines: "list[str]" = []
        t = self.task
        if t is not None:
            lines.append(f"Working on {self.headline()} — {_ago(now - t['started'])} so far.")
            if t["steps"]:
                lines.append("Latest steps:")
                lines += [f"→ {_clip(s, 120)}" for s in t["steps"][-_STEPS_SHOWN:]]
            else:
                lines.append("Getting started.")
        elif self.headline():
            lines.append(f"Busy {self.headline()}.")
        else:
            lines.append("Nothing running right now.")
            last = self.last_task
            if last is not None and now - last.get("ended", 0) < _RECENT_S:
                mark = "⏹" if last.get("stopped") else "✓" if last.get("ok") else "✗"
                lines.append(f"Last task, {_ago(now - last['ended'])} ago: “{_clip(last['goal'], 90)}” "
                             f"{mark} {_clip(last.get('summary'), 140)}".rstrip())
            elif self.ask and self.reply:
                lines.append(f"Last thing you asked: “{_clip(self.ask, 90)}” → {_clip(self.reply, 140)}")
        if self.waiting:
            n = len(self.waiting)
            lines.append(f"Waiting after it: {n} command{'s' if n > 1 else ''} "
                         f"(next: “{self.waiting[0]}”).")
        return "\n".join(lines)

    def screenshot(self) -> str:
        """The last screen the running task saw (a data URL), or ''."""
        return self.task["shot"] if self.task is not None else ""


tracker = Activity()
