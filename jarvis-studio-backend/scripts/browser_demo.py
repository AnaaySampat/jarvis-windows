"""Run a complex browser autopilot task headlessly (no HUD) for debugging.

Usage (from jarvis-studio-backend):
    python scripts/browser_demo.py
    python scripts/browser_demo.py --goal "your custom goal"
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys

# Ensure backend root is on sys.path when invoked as a script.
_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

DEFAULT_GOAL = (
    "Go to Wikipedia, search for Alan Turing, open his article, scroll down twice, "
    "read the Early life section, and tell me the first sentence of that section."
)

COMPLEX_GOAL = (
    "Open DuckDuckGo, search for 'Python asyncio tutorial', open the first result "
    "from python.org or realpython.com, scroll down once, note the first three "
    "section headings you see, then summarize what the page is about in one sentence."
)

DEEP_GOAL = (
    "Research Alan Turing on Wikipedia in depth: search for Alan Turing and open his "
    "biography article. Read the opening lede and note his birth year, death year, "
    "and one field he is famous for. Scroll to the 'Early life and education' section "
    "and note where he studied as an undergraduate. Then scroll to the 'Cryptanalysis "
    "of the Enigma' section and note approximately when he worked at Bletchley Park. "
    "Finally scroll to the 'Legacy' or 'Honours' section and note one honour or award "
    "named after him. Use 'note' commands to record each fact as you find it, then "
    "deliver a structured 5-point briefing synthesizing everything — do not claim done "
    "until all five facts are gathered and stated in your final summary."
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Browser autopilot debug demo")
    parser.add_argument("--goal", default=COMPLEX_GOAL, help="Autopilot goal text")
    parser.add_argument("--simple", action="store_true", help="Use simpler Wikipedia goal")
    parser.add_argument("--deep", action="store_true",
                        help="Multi-section Wikipedia research with notes + synthesis")
    args = parser.parse_args()
    if args.deep:
        goal = DEEP_GOAL
    elif args.simple:
        goal = DEFAULT_GOAL
    else:
        goal = args.goal

    from llm import groq_bridge
    from actions import browser
    import autopilot

    await groq_bridge.initialize()
    if not groq_bridge.has_llm_credentials():
        print("[Demo] No LLM credentials — set Groq/Gemini key or Vertex ADC.", flush=True)
        return 1
    if not browser.available():
        print("[Demo] Playwright unavailable — pip install playwright && playwright install chromium",
              flush=True)
        return 1

    browser.approve()
    print(f"[Demo] Starting browser task:\n  {goal}\n", flush=True)

    def on_step(line: str) -> None:
        print(f"  → {line}", flush=True)

    res = await autopilot.run_task("browser", goal, on_step=on_step)
    ok = bool(res.get("ok"))
    print(f"\n[Demo] {'SUCCESS' if ok else 'FAILED'}: {res.get('summary')}", flush=True)
    steps = res.get("steps") or []
    if steps:
        print(f"[Demo] {len(steps)} steps logged.", flush=True)
    findings = res.get("findings") or []
    if findings:
        print("[Demo] Findings:", flush=True)
        for f in findings:
            print(f"  - {f}", flush=True)
    # #region agent log
    autopilot._agent_debug("E", "browser_demo.py:main", "task_finished", {
        "ok": ok, "summary": (res.get("summary") or "")[:300],
        "step_count": len(steps), "stopped": bool(res.get("stopped")),
        "findings_count": len(res.get("findings") or []),
        "deep": bool(args.deep),
    }, run_id="deep-demo" if args.deep else "demo")
    # #endregion
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
