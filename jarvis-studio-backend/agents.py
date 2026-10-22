"""Agent mode: each request goes to the specialist that fits it, and specialists hand
each other what they found.

Simple mode (the default) is JARVIS as it always was: one chat model with every tool,
which decides for itself when to hand a whole task to the browser or desktop autopilot.
Agent mode puts a coordinator in front of it, with these agents:

    chat       answers, lookups and one-step actions — the ordinary pipeline turn
    computer   whole tasks in Windows apps (autopilot, desktop) — "Desktop agent"
    browser    whole tasks on websites, in the user's own Chrome (autopilot, browser)
    research   a topic across several sites, in a headless browser (autopilot, browser)
    files      going through the user's files and folders     ┐ chat turns with their
    scheduling the agenda, reminders, alarms, routines        │ tool groups always
    content    writing and images                             ┘ loaded and a brief

Agents are split by what they can reach, not by topic: the three operators each drive
something different, and the other three are the chat turn with a narrower focus
(TURN_AGENTS), which costs nothing extra to run.

The user can pick the agent: "@files …" anywhere in a request, a spoken "tell the
browser agent to …" (mention), or the agent row on the HUD (main's active agent) —
picked_plan. Otherwise the coordinator decides (Auto).

The coordinator (main._run_agent_plan) classifies each typed or spoken request: a
cheap look at its words first (route), one small model call only when that isn't
clear (plan_prompt / parse_plan). A request that spans the web and the desktop gets a
short plan of at most MAX_STEPS steps, run in order. Each step is told what the earlier
ones found (handover, through the operator's hint or the turn's context). When the
planned steps are done and the request had more to it (may_continue), the coordinator
looks once more and may hand the rest to another agent (NEXT_SYSTEM / parse_next) —
an agent's work can show what's needed next. An operator that needs to know
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

AGENTS = ("chat", "computer", "browser", "research", "files", "scheduling", "content")
NAMES = {"chat": "Chat agent", "computer": "Desktop agent", "browser": "Browser agent",
         "research": "Research agent", "files": "Files agent",
         "scheduling": "Scheduling agent", "content": "Content agent"}
ICONS = {"chat": "💬", "computer": "🖱", "browser": "🌐", "research": "🔎",
         "files": "📁", "scheduling": "📅", "content": "✍"}
# Whole tasks the autopilot does by looking and clicking. Every other agent is a chat
# turn: (the tool groups it always has, its brief).
OPERATORS = ("computer", "browser", "research")
TURN_AGENTS = {
    "chat": ((), ""),
    "files": (("files",),
              "You are JARVIS's Files agent: you work in the user's files and folders. To "
              "go through a folder, list it first, then read every file that matters (PDFs "
              "with read_pdf) — never describe a file you didn't read. A summary names each "
              "file. If part of the request needs an app or a website, do your part and say "
              "plainly what's left."),
    "scheduling": (("planner", "phone"),
                   "You are JARVIS's Scheduling agent: timers, reminders, alarms, the agenda "
                   "and routines. Turn the times the user gives into exact ones, look at the "
                   "agenda before adding something that could clash, and confirm what you set "
                   "in one sentence."),
    "content": (("media", "files", "email"),
                "You are JARVIS's Content agent: you write — messages, email drafts, posts, "
                "summaries, documents (saved as a PDF when asked) — and make images. Match the "
                "tone asked for and give the finished piece itself, not a description of it."),
}
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


# A topic to look into across several sites, not one fact (that's chat's web search).
_RESEARCH = re.compile(
    r"\b(research|deep[- ]dive|investigate|look into|find out (?:everything|all|more) about|"
    r"(?:write|make|give) (?:me )?a (?:report|brief(?:ing)?) (?:on|about)|with sources|"
    r"pros and cons of|compare (?:the )?(?:reviews|options))\b", re.IGNORECASE)

# Going through the user's files, not doing a chore on them (that's the desktop agent).
_FILE_READ = re.compile(
    r"\b(summar(?:y|i[sz]e)|read|go through|look through|what'?s in|contents of)\b.*"
    r"\b(files|folders?|downloads|my documents|documents folder|pdfs|desktop)\b",
    re.IGNORECASE | re.DOTALL)


def route(text: str, installed=_default_installed) -> str:
    """"chat", "browser", "computer", "research", "files", "multi" (more than one
    surface), or "unsure" (an action whose surface the words don't show: the model
    decides)."""
    t = (text or "").strip()
    if _RESEARCH.search(t):
        return "research"
    if _FILE_READ.search(t):
        return "multi" if _web(t) else "files"
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

_AGENT_LIST = (
    "- chat: answers questions and does one-step things itself — timers, reminders, "
    "weather, news, facts from a web search, music, volume, the TV, the user's phone, "
    "email drafts, a screenshot, sending something to the user themself (their own "
    "WhatsApp or email — never anyone else).\n"
    "- files: goes through the user's files and folders — lists and reads them (PDFs "
    "too), summarises them, saves text as a PDF.\n"
    "- research: researches a topic across several websites in its own background "
    "browser and reports what it found, with sources.\n"
    "- browser: does a whole task on websites in the user's own Chrome, where they're "
    "signed in — open a site, search on it, play a video, fill a form, compare prices.\n"
    "- computer: does a whole task in Windows apps — open an app, type into it, save, "
    "rename, move or copy files.\n"
    "- scheduling: several reminders, alarms, agenda entries or routines at once.\n"
    "- content: writes — posts, letters, documents — and makes images.\n")
_AGENT_ENUM = "|".join(AGENTS)

PLAN_SYSTEM = (
    "You route one request for JARVIS, a Windows assistant with these agents:\n"
    + _AGENT_LIST +
    "Reply with ONLY JSON: {\"steps\":[{\"agent\":\"" + _AGENT_ENUM + "\",\"task\":\""
    "a complete instruction for that agent\"}]}\n"
    "One step whenever one agent can do all of it — most requests are one chat step. "
    "Several steps only when the work really needs different agents, in the order "
    "they must happen; a later task may refer to \"what the previous step found\". "
    "At most 4 steps. Keep every detail the user gave (names, numbers, file names, "
    "addresses). Never add a step the user didn't ask for.")


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
        agent = _ALIASES.get(agent, agent)
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
    if r in ("browser", "computer", "research"):
        return Plan([Step(r, text.strip()[:_TASK_MAX])], "words")
    if r == "files":
        return Plan([Step(r, text.strip())], "words")
    system, user = plan_prompt(text)
    try:
        raw = await complete(system, user)
    except Exception:  # noqa: BLE001 — no plan: the chat agent answers
        return None
    return parse_plan(raw)


# ── The user picks the agent (pure; unit-tested) ─────────────────────────────

_ALIASES = {"desktop": "computer", "computer": "computer", "pc": "computer",
            "browser": "browser", "web": "browser", "chrome": "browser",
            "research": "research", "researcher": "research",
            "files": "files", "file": "files",
            "scheduling": "scheduling", "schedule": "scheduling", "scheduler": "scheduling",
            "calendar": "scheduling", "content": "content", "writer": "content",
            "writing": "content", "chat": "chat", "jarvis": "chat", "auto": "auto"}
_AT = re.compile(r"(?:^|\s)@([a-z]+)\b[,:]?", re.IGNORECASE)    # not inside an address
_SPOKEN = re.compile(r"^\s*(?:(?:ask|tell|use|get) (?:the |my |your )?)?([a-z]+) agent\b"
                     r"(?:\s*[,:]|\s+to\b)?\s*", re.IGNORECASE)


def mention(text: str) -> "tuple[str, str]":
    """(agent, the request without the mention) for "@files …" anywhere in it, or a
    spoken "tell the browser agent to …" / "research agent, …"; ("", text) otherwise.
    "auto" sends a request back to the coordinator from a picked agent."""
    t = text or ""
    m = _AT.search(t)
    if m and m.group(1).lower() in _ALIASES:
        rest = " ".join((t[:m.start()] + " " + t[m.end():]).split())
        return (_ALIASES[m.group(1).lower()], rest) if rest else ("", t)
    m = _SPOKEN.match(t)
    if m and m.group(1).lower() in _ALIASES and t[m.end():].strip():
        return _ALIASES[m.group(1).lower()], t[m.end():].strip()
    return "", t


def picked_plan(agent: str, text: str) -> "Plan | None":
    """The user chose the agent: one step for it, in their own words (never cut)."""
    if agent not in AGENTS or not (text or "").strip():
        return None
    return Plan([Step(agent, text.strip())], "picked")


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


def message(frm: str, to: str, kind: str, text: str, items=(), task: str = "") -> dict:
    """An `agent_message` event: one agent talking to another — "handover" (what it
    passes the next step), "next" (the coordinator gave it the rest), "ask" / "answer"
    (a question mid-task). The chat shows it as a card and WhatsApp as its `line`, so
    the user sees what the agents give each other, not just their results."""
    text = " ".join(str(text or "").split())[:300]
    return {"from": frm, "to": to, "kind": kind, "text": text,
            "items": [" ".join(str(f).split())[:200] for f in items][:5],
            "task": " ".join(str(task or "").split())[:200],
            "line": f"{ICONS[frm]} {NAMES[frm]} → {ICONS[to]} {NAMES[to]}: "
                    f"{'❓ ' if kind == 'ask' else ''}{text}"}


NEXT_SYSTEM = (
    "JARVIS's agents are working through a user's request. Given the request and what "
    "they have done so far, is any part of it still not done? Agents:\n" + _AGENT_LIST +
    "If everything asked for is done, reply {\"next\":null}. Otherwise reply with the ONE "
    "next step: {\"next\":{\"agent\":\"" + _AGENT_ENUM + "\",\"task\":\"a complete "
    "instruction, including what the earlier agents found that it needs\"}}. Reply with "
    "ONLY JSON. Never repeat work that was done; never add work the user didn't ask for.")

# A second thing to DO ("… and email it to me"), not a two-part question ("when was it
# built and how tall is it"): worth one look at what's left once the plan is done.
_MORE = re.compile(
    r"\b(?:and|then|after(?:wards| that)?|also|plus)\s+(?:then\s+)?(?:send|e-?mail|mail|"
    r"whatsapp|message|text|share|post|upload|save|write|put|paste|note|open|play|book|"
    r"order|buy|add|fill|download|create|make|draft|reply|remind|schedule|set|summari[sz]e|"
    r"rename|move|copy|print|compare|research|find|look up|search)\b", re.IGNORECASE)


def may_continue(text: str, results: "list[StepResult]") -> bool:
    return bool(_MORE.search(text or "")) and len(results) < MAX_STEPS \
        and all(r.ok for r in results)


def next_prompt(text: str, results: "list[StepResult]") -> "tuple[str, str]":
    return NEXT_SYSTEM, f"Request: {text.strip()[:1000]}\n\n{handover(results)}"


def parse_next(raw: str, results: "list[StepResult]") -> "Step | None":
    """The step to hand the rest to, or None (all done, junk, or a repeat)."""
    nxt = (_json_object(raw) or {}).get("next")
    if not isinstance(nxt, dict):
        return None
    agent = _ALIASES.get(str(nxt.get("agent") or "").strip().lower(), "")
    task = " ".join(str(nxt.get("task") or "").split())[:_TASK_MAX]
    if agent not in AGENTS or not task:
        return None
    if any(r.agent == agent and r.task.lower() == task.lower() for r in results):
        return None
    return Step(agent, task)


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


def research_goal(topic: str) -> str:
    """The research agent's goal for the browser operator (in a headless browser)."""
    return ("Research this and report back: " + topic.strip()[:600] + "\nSearch the web, "
            "open two or three good sources (not only the results page) and read them — "
            "more only when they disagree. "
            "Put each key fact in your findings with the page it came from. Change nothing "
            "and sign in nowhere; your summary answers the question.")


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
