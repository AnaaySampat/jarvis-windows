# Windows version — to-do

Last updated **2026-09-23**. Baseline: `python -m unittest` → **189 passed**; GUI
`npm run build`, `npm test` and `npm run lint` (0 errors, 33 warnings) clean.

Items marked *check* are suspicions carried over from bugs found in the Android port,
not confirmed on Windows yet. The phone's brain is a port of this backend, so its bugs
are good leads — but not proof: the phone's "stops on the 4th scroll" cycle-guard bug,
for example, does **not** exist in `autopilot.py`.

## 1. Known gaps — fix first

- [x] **Per-minute rate limits are benched like daily ones.** `llm/quota.py` `bench()`
  has no notion of a quota window, so a Groq tokens-per-minute 429 (resets in ~8s)
  climbs the 2m → 10m → 1h → day ladder, and a burst of autopilot steps can park every
  route. The phone fixed this on 2026-09-23 — port `errorClass.limitWindow()`, the
  minute-window bench in `quota.ts`, and chat's one short wait (≤15s) when every route
  is only briefly out.
  *Done: `quota.limit_window()` / `retry_after_in()`, minute bench 2–65s, transient
  90s → 20s, `groq_bridge._walk_routes` waits once (≤15s). Seen live: a Groq TPM 429
  was benched for seconds, JARVIS waited 3s and answered.*
- [x] **No retired-model remap.** Google retires Gemini ids and names the replacement
  in the 404 ("use models/…"). The phone reads it and remaps (`modelRemap.ts`); the
  desktop pins ids (`groq_bridge.py DEFAULT_MODEL`, `gemini_bridge.py`) and would bench
  a retired one for a day. This took the phone's whole Gemini tier down twice.
  *Done: `quota.retired_replacement()` + persisted `record_remap`/`live_model`, applied
  in `_model_ladder` and `gemini_bridge.rest_model`. Not seen live yet (needs a real
  retirement 404).*
- [x] *check* **Empty model replies.** On the phone, Groq `gpt-oss-120b` once answered
  HTTP 200 with empty content, and it was treated as an answer. See what
  `groq_bridge.py` / `gemini_bridge.py` do with an empty reply that has no tool calls.
  *Twin found: `gemini_send` turned an empty reply into a canned "could you rephrase?"
  that callers took as an answer. It now returns "", and an empty reply gets a short
  bench. Separate bug: a tool-call-only streamed reply counted as empty, so the next
  route ran the tool a second time and the user heard "quota used up". Fixed and
  verified live.*
- [x] *check* **Bugs from the phone's 2026-09-22 correctness pass that may have a
  Python twin:**
  - agenda times sorted and compared as raw strings (a 9am item sorts below every
    afternoon one)
  - agenda entries keyed by weekday only, so they recur weekly forever
  - removing a playbook deletes *every* name containing the query
  - `customize_screen`'s advertised actions not matching the handled ones
  - `ok: true` returned for failures (`forget` "Nothing matched", a record "stop"
    that isn't supported, a memory write that threw)
  *Found and fixed: agenda string sort, weekday-only keys (items now carry a date and
  are pruned; undated legacy items kept), and an edit that renamed an item to its match
  text. Playbook forget now refuses ambiguous matches. Memory, playbook, reminder and
  routine writes report failure. No twin: `customize_screen` (all 10 actions
  handled), `forget` with no match, recorder stop.*

## 2. Built but never tested live — *postponed: needs the phone*

- [ ] **Phone pairing on the device-key path**, from scratch: QR → pinned host key →
  single-use pairing challenge. Both sides are built; nobody has re-paired since.
- [ ] **Task protocol v2**: stable ids, idempotency, resubscribe with an event cursor
  after a reconnect (drop Wi-Fi mid-task).
- [ ] **Remote STOP from the phone actually stops the PC task** — it once said "hard
  stop sent" while nothing was sent.
- [ ] Live remote desktop (WebRTC) after the remote-channel hardening.
- [ ] LAN ↔ Tailscale switch mid-session.

## 3. Live bug hunt (like the phone's 2026-09-23 run)

- [x] Run `python start.py`, drive chat over `ws://127.0.0.1:8765` from a script,
  and read `server.log`, the same way the phone was driven over CDP + logcat.
  *Done with the backend alone (no GUI) and a WS driver that auto-approves consent.*
- [x] Desktop operator tasks — browser (Playwright) and computer (mouse/keyboard).
  **These move this PC's mouse and keyboard: hands off while they run.**
  *2026-09-23 runs, all on Groq (Gemini is down, see below):*
  - *"read the main heading on example.com": failed, then passed in 8.5s. A
    one-fact lookup was told to collect 3 facts, so it wandered after answering.*
  - *"work out 12 times 34 in Calculator": failed, then passed in 12s. The operator
    couldn't see the display (readouts are now in the observation), launched
    through the Start menu (a leading "open X, …" is now one deterministic launch),
    and a mid-task rate limit ended with "couldn't work out the next step" (it now
    waits up to 65s, then reports quota honestly).*
  - *"Alan Turing's birth year on Wikipedia": passed in 26s, 13s of it waiting on
    Groq's per-minute limit.*
- [ ] **Gemini is off: HTTP 403 "billing must be enabled" on project
  `gen-lang-client-0416689127`.** Everything runs on Groq's free tier, where
  nearly every autopilot step hits the tokens-per-minute limit. Enable billing (or
  use a key from a project that has it).
- [ ] Voice path: wake word, push-to-talk, a long spoken reply, stop mid-speech.
- [ ] Reminders, timers, routines, playbooks, file tools.
- [ ] The frozen installer build (`scripts\build.ps1`) on a clean Windows user
  account, not just `python start.py`.

## 4. Cleanup

- [x] Delete `backend/` (5 tracked files, dead prototype) and the empty
  `aura-studio-gui/` — CLAUDE.md already says neither is used.
- [x] `ANDROID_PLAN.md` belongs to the Android repo, which keeps its own (diverged)
  copy in `docs/archive/`. Delete it here.
- [x] Add `vitest` + `eslint` scripts to `jarvis-studio-gui` (the Android fork has them).
  Both run in CI. 33 lint warnings are left (hook deps, set-state-in-effect).

## 3b. Autopilot audit (2026-09-23) — all fixed

All 16 findings are fixed, each with a regression test: Stop during a model call;
the `see` prompt overwriting the `ask` callback; the hotkey blocklist bypass; the
wrong browser in personal-Chrome vision; "go back" false success; "Stopped" on a
correction; the substring done-gate; desktop offered a `see` it lacks; launch/focus
matching a window by title substring (now by owning process, so a "Wordle" tab is
never Word); pause, consent and a foreground-window change re-checked right before
acting; one autopilot at a time across PC and phone; do-nothing steps counted and
capped; personal mode never adopting the user's tab; the router ignoring typed
text; plan/done-check/`see` calls Stop-aware; the CDP connection closed on reset
(verified to leave Chrome running); no model call for the done-check once the
cheap gate already said no.

Not done: running the plan call alongside the first screenshot saves about 0.3s
per task.

## 5. Before publishing (GitHub, later)

- [x] Secret-scan the **whole git history**, not just the working tree. 17 commits are
  clean; the only hit is the test fixture `AIzaDefinitelySecret…`.
- [ ] Rename JARVIS → Aura (J.A.R.V.I.S. is a Marvel trademark).
- [x] Credit the `hey_jarvis` wake-word model: openWakeWord's pre-trained models are
  **CC BY-NC-SA 4.0 (non-commercial)**. The README's "Wake-word model" section doesn't
  say so. *README now says so. ⚠ This conflicts with selling the .exe: a paid build
  needs its own wake-word model.*
- [ ] Replace the placeholder icons (`setup-icon.py`, `npm run icons`).
- [ ] Sign the NSIS installer, or SmartScreen warns every user.
- [ ] Add a demo GIF/video to the top of the README.
