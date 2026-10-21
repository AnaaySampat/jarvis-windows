# Windows version — to-do

## 0. 2026-10-21 — seeing what JARVIS does, connectors, the phone through the PC, agent mode

Built and unit-tested here (backend `python -m unittest` → **508**, the Windows-only
`test_files` one aside; GUI `npm test` 14, `npm run lint` 0 errors / 22 warnings, `npm
run build`), Settings ▸ Connectors and ▸ Autopilot and the home screen checked in
Chromium against a stand-in backend. Nothing here has run against real WhatsApp, a real
mailbox, a paired phone or a live model yet.

- [ ] **A phone task on the HUD.** Send a task from the phone app: the home screen says
  "Working on it… From your phone: …" and Agent Activity shows its steps and
  screenshots, tagged *From your phone*. (Before, the HUD showed nothing.)
- [ ] **WhatsApp progress.** "Jarvis, open YouTube and play lofi": one ⏳ message that
  is edited as steps land (neonize `edit_message`), ending ✅/⚠️, then the reply.
  "typing…" stays up the whole time. Watch for `[WhatsApp] edit failed` in the log;
  if edits never take, progress falls back to a new message at most every 30 s.
- [ ] **"Jarvis, status"** mid-task: answered at once with the latest steps and a
  screenshot; and a second command sent mid-task gets "I'm busy with … Yours is next".
- [ ] **Gmail**: Settings ▸ Connectors ▸ Gmail with a real app password; "any unread
  email?", "read the one from …" (stays unread in Gmail), "draft a reply saying …"
  (lands in Drafts, threaded). From WhatsApp each asks first. A wrong password says
  to use an app password.
- [ ] **An MCP connector** (Notion or GitHub): connect from its card, check MCP
  Servers shows it Connected, ask about it by name; reconnect with a new token keeps
  *Trusted*. Google Calendar's one-time sign-in ("sign in to Google Calendar").
  Home Assistant with a long-lived token: its MCP endpoint is documented with OAuth;
  a Bearer token is expected to work like the rest of its API — check.
- [ ] **"Set an alarm on my phone for 7"** at the PC, with the phone app open: the
  alarm appears in the phone's Clock, the phone's chat shows "💻 From your PC: …",
  and the PC says what the phone reported. Again from an allowed WhatsApp number
  (asks yes/no in that chat first). With the phone app closed for a few minutes: the
  PC says the phone didn't answer in 45 s.
- [ ] **Agent mode**: turn it on; "what's the weather" (chat, no plan), "play lofi on
  YouTube" (browser agent straight away), "find the cheapest flight to Goa on Friday
  on Google Flights and write it in Notepad" (a two-step plan; the desktop agent gets
  the flight in its hint). Watch `[Agents]` in the log for how each was routed, and
  whether the planner over-plans simple requests on the live model.

## 0. 2026-10-20 menu overhaul — checked in Chromium against a mock backend, not in the app

Every menu was rebuilt and screenshotted in a browser (Vite + a stand-in WebSocket
server), not in the Tauri window on Windows. GUI `npm test` 12, `npm run lint`
0 errors / 30 warnings.

- [ ] **Ctrl+K** opens the command palette inside the WebView2 window (and isn't
  swallowed by it), Esc closes the topmost panel, Ctrl+, still opens Settings.
- [ ] **Settings** saves exactly as before: change a key, the voice, the heartbeat
  times and the floating-pill apps, Save, reopen. Closing with unsaved edits (Esc, ✕
  or a click outside) asks once before discarding.
- [ ] **Theme**: pick amber in Customize; Settings, Customize and the dialogs follow it.
- [ ] **Agenda add/edit** inline (the time field is a native time input in WebView2).
- [ ] **Skills ▸ QR code / Screenshot / List folder** open the chat with the result.
- [ ] **The new home screen** at the default 1100×720 window and maximised: the orb
  follows listening / thinking / speaking; clicking it talks when idle and stops
  otherwise; the ask box (or `/`) sends and the reply shows under the orb; "hide the
  weather panel", "collapse all the panels" and Customize's left/right order still apply.

## 0. 2026-10-19 catch-up with the phone — built, unit-tested, not yet run live

Baseline after it: `python -m unittest` → **370** (one Windows-only path test can only
pass on Windows); GUI `npm test` 8, `npm run lint` 0 errors / 33 warnings (unchanged).

- [ ] **Wake word keeps the first words.** Run `python start.py` and say "Hey Jarvis,
  open Notepad" in one breath; then "Hey Jarvis" … pause … "open Notepad"; then talk
  about Jarvis without the greeting (should be ignored: `Wake ignored` in the log).
  Watch `[Jarvis] Command recorded (…s, speech=…, room=…)`; tune with
  `JARVIS_WAKE_SILENCE_SECS` / `JARVIS_WAKE_MAX_SECS`, or `JARVIS_WAKE_CAPTURE=0` to
  turn it off. On local Whisper (no Groq) the greeting check may be stricter in
  practice: if real wakes get ignored, that's the first thing to look at.
- [ ] **Tool groups.** Watch for a turn where the model says it can't do something
  instead of calling `load_tools` (log line `[Tool] load_tools(…)`). Cue words live in
  `live_tools._CUES`.
- [ ] **Web search**: ground on 2.5 Flash with a free key; a Tavily key answering when
  Gemini is spent.
- [ ] **Heartbeat** with a one-line checklist ("tell me 10 minutes before an agenda
  item") and a 15-minute interval.
- [ ] **Routines**: Memory ▸ Routines add/edit/run/remove; "movie night" alone runs it.
- [ ] **TV with two addresses** (home IP + Tailscale IP), and the "different network"
  message from a PC on the other router's Wi-Fi.
  *2026-10-19:* the "different network" reply fired, but wrongly diagnosed: the TV had
  moved to a new address on the PC's own Wi-Fi; a changed TV address
  is never rediscovered. Re-pairing (Settings → Devices) fixed it.
  *2026-10-20:* three times within an hour this PC couldn't reach the TV for
  minutes while it was on and the phone could (Windows' own ping failed; the TV's ARP
  entry here sat at Unreachable/Stale 00-00-00-00-00-00; clearing it once brought it back
  at once). Each time the cast took it for "off", sent a wake-up packet and told the user
  to turn on Remote start, which was on. The failed-wake reply now names both causes
  (off, or on but unreachable from here). Cause of the drops not found yet.
- [ ] **Update notice**: needs a GitHub release newer than the installed version.
- [ ] **A false "done" is spoken before the claim check runs** (found by test_replay.py,
  2026-10-19): a streamed reply like "Resumed — back to Arcane." with no tool call is
  spoken while it streams; the recheck then runs the real action. The phone holds such
  a reply back. Fix idea: on an action-like request, hold speech until the step ends and
  it's known whether the model called a tool. Costs latency only on those turns.
  *2026-10-19:* not run live — the TV was in use.
  *2026-10-20:* "pause the TV" then "resume the TV" at once: the model called
  `tv_control resume` with no text before it (nothing streamed or spoken), "YouTube is
  playing." — not reproduced in one try; the replay fixture still shows the risk.
  Related, from the phone the same night: "what's on my TV right now?" got "nothing
  playing on the TV" with no tool call at all. The phone now rechecks a TV question
  answered without a tool (`answeredTvWithoutLooking`); this backend has no such check,
  and since it speaks while streaming it would need the same hold-speech fix first.
- [ ] **Nightly memory digest** (unit-tested with a fake model): leave the PC on
  overnight after a day of use; next morning check `[Digest]` in the log, Memory for new
  facts, and `journal/<date>.md`.
  *2026-10-19, first start after 03:00:* `[Digest] 2026-10-19: read 1 message(s),
  learned 0` and `journal/2026-10-19.md` written — but the note is in the third person
  with commentary ("This is a one-off request and not something to remember long-term"),
  not the second-person diary the prompt asks for. The phone's digest saved a guess
  ("My live stream is called Nebula…") from "Put Nebula live on the TV";
  gemini-2.5-flash-lite did that 6 times in 8 with this prompt, 0 in 8 after one added
  line (a command says nothing lasting about the user), now in both apps. Overnight
  run and turning it off: still to do.
  *2026-10-20:* the phone's morning digest brought back "My favourite colour is teal.",
  forgotten the night before in the sync test, and the sync carried it here: neither
  digest looked at what had been forgotten. Both now list the forgotten facts in the
  prompt and drop a fact whose voice-neutral key ("My X" = "The user's X") is remembered
  or forgotten (`test_memory_digest.py` fails without it). Real cheap-tier model: old
  prompt 8/8 brought them back, new 0/8.
- [x] **One memory with the phone** (unit-tested both sides, never run between the
  real devices): remember something on the phone, see it on the PC within a minute;
  forget it on the PC, gone from the phone; a timed routine made on the phone shows
  "(on your phone)" here and doesn't fire here.
  *Done 2026-10-19 (SM-E156B over Tailscale):* phone fact → `facts.json` in 6 s; forgotten
  here → gone from the phone within 30 s and still gone 15 min later; a timed phone
  routine listed as "Every day at <time> (on your phone)", fired on the phone, not here;
  removing it on the phone removed it here in 5 s (tombstone).
- [ ] **MCP servers** (unit-tested, and end to end against a real stdio server with
  mcp 1.30 and 2.2 on Linux; never on Windows): add
  `npx -y @modelcontextprotocol/server-filesystem C:\Users\<you>\Documents` in
  Settings → MCP Servers, check it shows Connected with its tools, then "use the
  filesystem server to list my Documents" (no prompt: read-only), and a write (asks
  first). Also an HTTP server with a header, and the frozen installer build.
  *2026-10-19 on Windows, mcp 2.3.0:* Connected — 14 tools; list ran with no prompt;
  a write asked first (deny → nothing written, allow → written); Trusted → no prompt;
  Remove → tools gone. Fixed on the way: a tool's answer was read aloud raw (label and
  listing, 16 s) and never given to the model — now silent and fed back, so the model
  answers in words (seen live); a denied call spoke its JSON arguments; `npx -y` takes
  ~34 s to answer even when cached, so the 45 s start limit failed twice on a busy PC
  (now 120 s). A weak model passed a relative path ("Documents") and got ENOENT.
  Left: an HTTP server with a header, the frozen build.
- [ ] **Send this to the TV** (built and unit-tested, never run): YouTube in Chrome,
  then Edge, at ~10 minutes in → "send this to the TV" (TV starts there? PC paused?);
  Netflix in Chrome (TV resumes the same episode?); with the video tab not in front
  (should say so, not send another video); Spotify playing (should refuse). Check the
  address bar is readable through UI Automation on the user's browser language.
  *2026-10-19:* not run — the TV was in use.
  *2026-10-20 (Chrome, YouTube ~10 min in):* first try started the TV 10:00 into the video while the
  PC was ~15:40 in, left the PC playing and said "couldn't confirm it started playing".
  Three causes, all fixed: (1) `start.py` runs the backend in `.venv-build`, which had
  neither winrt nor mcp (yesterday's run was from a worktree with no `.venv-build`, so
  system Python) — installed them there; without winrt the cast now says it went by the
  address bar; (2) YouTube's TV app reports its position only when its state changes
  (902099 for 95 s while playing), so the moving-position check never confirmed it —
  now a YouTube session counts once it has reported since the link (`updated=`); (3)
  "from the start" was claimed when a TV already playing the video just carried on.
  After: the TV played it and the PC paused ("Paused it on the PC"). In guest mode the
  TV's YouTube ignored `&t=` (0:02 for 44:39) and the reply said so; signed in it took
  `&t=600s`. A cold start shows YouTube's "Who's watching?" picker, which swallows the
  link (reply: couldn't confirm — honest). Tab not in front → "bring its tab to the
  front", nothing sent ✅. Spotify: not run (the TV dropped off the network first, then
  the laptop hung with Chrome + Spotify + a build). Netflix in Chrome: not run.
  **The frozen installer is built from `.venv-build`: check mcp and winrt are in it.**
  Later: neither app switched a TV in standby (its normal "off" with Remote start on —
  ADB still up) on before `play`/`open_app`/`cast`: apps opened on a dark screen. Now
  `Tv.screen_on()` wakes it first (`test_now_playing.py` fails without it). Live on the
  phone after the fix: the TV woke, then YouTube's "Who's watching?" picker swallowed
  the link (undetectable over ADB: `uiautomator` sees nothing in YouTube's TV app).
  The user chose: pick the highlighted account (like Netflix's gate). `youtube_gate`:
  after a wake or a cold start, if nothing started, one OK then the link/search again
  (tests fail without it). Live: TV in standby, "play lofi on the TV" → woke and
  "Playing the top YouTube result for “lofi” on the TV." in 29 s (TV playing it); the
  phone's shared link the same way played from 2:00.
- [ ] **Wake-on-LAN** (built and unit-tested, never on real hardware): with Remote
  start on, let the TV go fully off (deep standby), then "turn on the TV"
  (`[TV]` log: packet sent, then answered). Check Settings → Devices → TV shows the
  MAC it learned. From the phone: "turn on my PC" with the PC asleep, then shut down.
  *2026-10-19:* the TV's MAC was learned on pairing (its Wi-Fi adapter's —
  the TV isn't on Ethernet); the phone received this PC's adapters on connecting. Deep
  standby and a sleeping PC: not run yet.
  *2026-10-20:* with Remote start on the TV never went fully off: 15 minutes after
  switching off it still answered ping and ADB, so "turn on the TV" took the ADB path ("Turned the TV
  on.", 6 s; the phone the same). In that state a magic packet does nothing (Android is
  running): `wol.send`, the phone's `wol_send` and a unicast packet all left and the TV
  stayed Asleep. Waking a TV that's really unreachable: still untested.
  PC from the phone, this laptop asleep: no wake; the phone said so honestly (twice, no
  false claim). This laptop can't be woken over Wi-Fi: Windows doesn't offer the AX201
  as a wake device (`powercfg /devicequery wake_programmable`), so there's no Power
  Management tab to tick. Idea: send in `auth.ready` whether any adapter is wake-armed
  (`powercfg /devicequery wake_armed`), so the phone can say so instead of generic advice.
- [x] **Words glued together in streamed Gemini replies** (found 2026-10-19: "Ican't",
  "yourcomputer", "The`C:\…`" on screen, in history and spoken). `gemini_bridge` stripped
  every streamed chunk before joining them; chunks keep their edges now
  (`test_gemini_stream.py` fails without the fix).
- [ ] **WhatsApp channel** (never connected: the build sandbox's network blocks
  web.whatsapp.com, so only a failed dial was seen with the real library). On a PC:
  link by QR; "Jarvis, what time is it?" in Message yourself; a note without the name
  (ignored); "Jarvis, send me a screenshot" (arrives as an image); "Jarvis, shut down
  the PC" (asks yes/no in the chat; answer no); a voice note; a message from a number
  not on the list (ignored); unlink from the phone's Linked devices (status says so).
  Then the frozen installer build: the neonize and libmagic DLLs must be in
  `_internal/neonize` and `_internal/magic`.

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
