"""Agent mode: each request goes to the specialist that fits it, and specialists hand
each other what they found.

Simple mode (the default) is JARVIS as it always was: one chat model with every tool,
which decides for itself when to hand a whole task to the browser or desktop autopilot.
Agent mode puts a coordinator in front of it, with three agents:

    chat     answers, lookups and one-step actions — the ordinary pipeline turn
    browser  whole tasks on websites (autopilot, browser surface)
    computer whole tasks in Windows apps and the user's files (autopilot, desktop)

The coordinator (main._run_agent_plan) classifies each typed or spoken request: a
cheap look at its words first (route), one small model call only when that isn't
clear (plan_prompt / parse_plan). A request that spans the web and the desktop gets a
short plan of at most MAX_STEPS steps, run in order. Each step is told what the earlier
ones found (handover, through the operator's hint), and an operator that needs to know
something mid-task ("what's the price to write down?") asks the coordinator, which
puts the question to the other agent before it ever bothers the user (consult_prompt).
A failed step stops the plan: later steps would build on something that didn't happen.

The chat agent is the unchanged pipeline turn, so whatever the coordinator isn't sure
about goes there and behaves exactly as in simple mode, and every agent step passes the
same consent gate (actions.needs_permission) as a task the chat model started.

Pure apart from the installed-app lookup (task_router), which tests replace.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import task_router

AGENTS = ("chat", "browser", "computer")
NAMES = {"chat": "Chat agent", "browser": "Browser agent", "computer": "Desktop agent"}
ICONS = {"chat": "💬", "browser": "🌐", "computer": "🖱"}
MAX_STEPS = 4
_TASK_MAX = 400


@dataclass
class Step:
    agent: str
    task: str


@dataclass
class Plan:
    steps: "list[Step]"
    how: str = ""                    # "words" or "model": how the route was found


@dataclass
class StepResult:
    agent: str
    task: str
    ok: bool
    summary: str
    findings: "list[str]" = field(default_factory=list)


# ── Routing by the words (pure; unit-tested) ──────────────────────────────────

# Doing something, not asking about it.
_ACTION = re.compile(
    r"\b(open|launch|start|play|watch|search|find|look up|go to|visit|book|order|buy|"
    r"add|fill|sign (?:in|up)|log ?in|download|upload|write|type|create|make|save|"
    r"rename|move|copy|organi[sz]e|tidy|sort|post|close|install|compare|apply|"
    r"subscribe|check out|put|paste|note down|jot)\b", re.IGNORECASE)

# What the chat agent's own tools do in one step: the router never sends these on.
_CHAT_TOOLS = re.compile(
    r"\b(timers?|remind(?:er|ers)?|alarms?|weather|forecast|news|headlines|volume|"
    r"mute|unmute|screenshot|remember|forget|routines?|agenda|schedule|theme|"
    r"colou?r|panels?|lock|shut ?down|restart|reboot|sleep|hibernate|qr|image|"
    r"picture|draw|wallpaper|logo|spotify|tv|television|phone|e-?mails?|inbox|"
    r"gmail|record(?:ing)?|playbook|directions|nearby|near me|pdf)\b", re.IGNORECASE)

# Work that needs a website, beyond looking a fact up (that's chat's web search).
_WEB_TASK = re.compile(
    r"\b(book|order|buy|add (?:it |them |this )?to (?:my |the )?(?:cart|basket)|"
    r"check ?out|sign (?:in|up)|log ?in|fill (?:in|out)|apply (?:for|to)|subscribe|"
    r"download (?:it )?from|web ?site|web ?page|online|in (?:the|my) browser|"
    r"on (?:the )?(?:web|internet))\b", re.IGNORECASE)

# Work in the user's files or Windows itself.
_FILE_CHORE = re.compile(
    r"\b(?:rename|move|copy|organi[sz]e|tidy|sort|zip|unzip|clean up)\b.*\b(?:files?|"
    r"folders?|desktop|downloads|documents|pictures|photos)\b|\b(?:save|write|put|note|"
    r"paste|jot)\b.*\b(?:text file|\.txt|\.docx?|\.xlsx?|notepad|word document|"
    r"spreadsheet|file on my desktop|note on my desktop|my desktop)\b",
    re.IGNORECASE | re.DOTALL)


def _web(text: str) -> bool:
    low = text.lower()
    return bool(task_router._URL_RE.search(text) or task_router._WEB_PHRASE_RE.search(text)
                or _WEB_TASK.search(text)
                or any(re.search(rf"\b{re.escape(s)}\b", low) for s in task_router._WEB_SERVICES))


def _installed_app(text: str, installed) -> str:
    for cand in task_router._candidates(text):
        low = cand.lower()
        if low in task_router._BROWSER_APPS or low in task_router._WEB_SERVICES:
            continue
        try:
            found = installed(cand)
        except Exception:  # noqa: BLE001 — a launcher problem never blocks routing
            found = ""
        if found:
            return found
    return ""


def _default_installed(name: str) -> str:
    from actions import app_launcher
    return app_launcher.installed_app_name(name)


def route(text: str, installed=_default_installed) -> str:
    """"chat", "browser", "computer", "multi" (both the web and the desktop), or
    "unsure" (an action whose surface the words don't show: the model decides)."""
    t = (text or "").strip()
    if not t or not _ACTION.search(t):
        return "chat"
    web = _web(t)
    desktop = bool(task_router._DESKTOP_PHRASE_RE.search(t) or _FILE_CHORE.search(t))
    if not desktop and not _CHAT_TOOLS.search(t):
        desktop = bool(_installed_app(t, installed))
    if web and desktop:
        return "multi"
    if _CHAT_TOOLS.search(t) and not _WEB_TASK.search(t) and not _FILE_CHORE.search(t):
        return "chat"
    if web:
        return "browser"
    if desktop:
        return "computer"
    return "unsure"


# ── Planning with a model (prompt + strict parsing; unit-tested) ──────────────

PLAN_SYSTEM = (
    "You route one request for JARVIS, a Windows assistant with three agents:\n"
    "- chat: answers questions and does one-step things itself — timers, reminders, "
    "weather, news, facts from a web search, music, volume, the TV, the user's phone, "
    "email, reading a file.\n"
    "- browser: does a whole task on websites in its own browser — open a site, search "
    "on it, play a video, fill a form, compare prices, read pages.\n"
    "- computer: does a whole task in Windows apps and the user's files — open an app, "
    "type into it, save, rename, move or copy files.\n"
    "Reply with ONLY JSON: {\"steps\":[{\"agent\":\"chat|browser|computer\",\"task\":\""
    "a complete instruction for that agent\"}]}\n"
    "One step whenever one agent can do all of it — most requests are one chat step. "
    "Several steps only when the work really spans the web and the desktop, in the "
    "order they must happen; a later task may refer to \"what the previous step "
    "found\". At most 4 steps. Keep every detail the user gave (names, numbers, file "
    "names). Never add a step the user didn't ask for.")


def plan_prompt(text: str) -> "tuple[str, str]":
    return PLAN_SYSTEM, f"Request: {text.strip()[:1000]}"


def _json_object(raw: str) -> "dict | None":
    m = re.search(r"\{.*\}", raw or "", re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_plan(raw: str) -> "Plan | None":
    """The model's plan, or None for "let the chat agent have it" (all chat, junk,
    nothing usable)."""
    obj = _json_object(raw)
    steps: "list[Step]" = []
    for row in (obj or {}).get("steps") or []:
        if not isinstance(row, dict):
            continue
        agent = str(row.get("agent") or "").strip().lower()
        agent = {"desktop": "computer", "pc": "computer", "web": "browser"}.get(agent, agent)
        task = " ".join(str(row.get("task") or "").split())[:_TASK_MAX]
        if agent in AGENTS and task:
            steps.append(Step(agent, task))
    steps = steps[:MAX_STEPS]
    if not steps or all(s.agent == "chat" for s in steps):
        return None
    return Plan(steps, "model")


async def decide(text: str, complete, installed=_default_installed) -> "Plan | None":
    """The plan for a request, or None when the chat agent should have it. `complete`
    is quick_completion (system, user) -> text; it's only called when the words
    aren't enough."""
    return await plan_for(route(text, installed), text, complete)


async def plan_for(r: str, text: str, complete) -> "Plan | None":
    """decide, from route()'s answer (main runs route off the event loop: the
    installed-app lookup can take a moment the first time)."""
    if r == "chat":
        return None
    if r in ("browser", "computer"):
        return Plan([Step(r, text.strip()[:_TASK_MAX])], "words")
    system, user = plan_prompt(text)
    try:
        raw = await complete(system, user)
    except Exception:  # noqa: BLE001 — no plan: the chat agent answers
        return None
    return parse_plan(raw)


# ── Agents talking to each other (pure; unit-tested) ──────────────────────────

def handover(earlier: "list[StepResult]") -> str:
    """What the earlier steps found, for the next agent's every step (its hint)."""
    if not earlier:
        return ""
    lines = ["FROM THE OTHER AGENTS (use it; don't redo their work):"]
    for r in earlier:
        lines.append(f"- {NAMES[r.agent]} did “{r.task[:120]}” → "
                     f"{'done' if r.ok else 'NOT done'}: {r.summary[:300]}")
        lines += [f"    · {f[:200]}" for f in r.findings[:8]]
    return "\n".join(lines)


def ask_note(agent: str) -> str:
    """Added to an operator's hint: it may ask the other agent, through `ask`."""
    other = "browser" if agent == "computer" else "computer"
    can = ("anything on the web (a price, an address, a page's contents)"
           if other == "browser" else "the user's files and Windows apps on this PC")
    return (f"The {NAMES[other].lower()} can find {can}: if you need that, use "
            "{\"do\":\"ask\",\"question\":\"…\"} and it will be answered for you.")


CONSULT_SYSTEM = (
    "An agent working on a task asked a question. Who can answer it? "
    "browser = it can be found on the web; computer = it's in the user's files or apps "
    "on this PC; user = only the user can decide or knows it (a choice, a preference, "
    "a password). Reply with one word: browser, computer or user.")


def consult_target(answer: str, asking: str) -> "str | None":
    """The agent to put a question to (never the one asking), or None for the user."""
    words = re.findall(r"[a-z]+", (answer or "").lower())
    word = words[0] if words else ""
    target = {"web": "browser", "desktop": "computer", "pc": "computer"}.get(word, word)
    return target if target in ("browser", "computer") and target != asking else None


def consult_goal(question: str) -> str:
    return ("Find this out and report it in your summary, without changing anything: "
            + question.strip()[:300])


def plan_text(plan: Plan) -> str:
    """The plan as the user sees it."""
    if len(plan.steps) == 1:
        s = plan.steps[0]
        return f"{ICONS[s.agent]} {NAMES[s.agent]}: {s.task}"
    lines = [f"🧭 {len(plan.steps)} agents on this:"]
    lines += [f"{i}. {ICONS[s.agent]} {NAMES[s.agent]} — {s.task}"
              for i, s in enumerate(plan.steps, 1)]
    return "\n".join(lines)


def outcome(plan: Plan, results: "list[StepResult]") -> "tuple[str, str]":
    """(the chat bubble, the one sentence to say) after a plan ran."""
    lines = []
    for i, s in enumerate(plan.steps):
        if i < len(results):
            r = results[i]
            lines.append(f"{'✓' if r.ok else '✗'} {NAMES[s.agent]}: {r.summary}")
        else:
            lines.append(f"– {NAMES[s.agent]}: not started — {s.task[:100]}")
    done = len(results) == len(plan.steps) and all(r.ok for r in results)
    if done:
        say = results[-1].summary if results else "Done, sir."
    elif results and not results[-1].ok:
        say = (f"I stopped at step {len(results)} of {len(plan.steps)}: "
               f"{results[-1].summary}")
    else:
        say = "I stopped before finishing, sir."
    return "\n".join(lines), say
