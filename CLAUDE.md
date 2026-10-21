# JARVIS (aura) — agent orientation

Windows AI voice assistant: Tauri 2 + React HUD (`jarvis-studio-gui/`) talking to a
Python backend (`jarvis-studio-backend/`) over `ws://127.0.0.1:8765`. The same
WebSocket also serves a cryptographically paired phone over Tailscale. See
[README.md](README.md) for the feature set and architecture.

## Where things live

| Area | Start here |
|------|-----------|
| WS handlers, orchestration, startup | `jarvis-studio-backend/main.py` (large — grep, don't read whole) |
| Browser + desktop operators | `jarvis-studio-backend/autopilot.py` |
| Which operator runs a goal | `jarvis-studio-backend/task_router.py` |
| Model routing / tiers / fallback | `jarvis-studio-backend/llm/groq_bridge.py` |
| Tool palette, tool groups per turn (`load_tools`) | `jarvis-studio-backend/llm/live_tools.py` |
| Nightly memory digest (user's own words → ≤5 facts + journal note, after 03:00) | `jarvis-studio-backend/memory_digest.py`, `main.digest_loop` |
| One memory with the phone (facts + named routines, newest wins, tombstones, routine `origin`) | `jarvis-studio-backend/memory_sync.py`, `main.handle_memory_sync` (remote-allowed) |
| MCP servers (sessions on the main loop, tool group `mcp`, approval unless read-only/trusted) | `jarvis-studio-backend/mcp_client.py`, Settings ▸ MCP Servers |
| Skill files (`read_skill`), heartbeat, routines | `jarvis-studio-backend/skill_files.py`, `heartbeat.py`, `routines.py` |
| WhatsApp channel (neonize linked device; who may command; reply/approval routing; live progress message, "Jarvis, status") | `jarvis-studio-backend/whatsapp_channel.py`, `main._run_whatsapp_turn` |
| What JARVIS is doing right now (a tap on every HUD event; WhatsApp status and busy notice) | `jarvis-studio-backend/activity.py` |
| Agent mode (coordinator → chat / browser / desktop agent, plans, hand-over, agents asking each other) | `jarvis-studio-backend/agents.py`, `main._run_agent_plan` |
| Connectors (Gmail over IMAP, preset MCP servers) | `jarvis-studio-backend/connectors.py`, `actions/email_box.py`, Settings ▸ Connectors |
| Commands to the paired phone ("set an alarm on my phone", also from WhatsApp) | `jarvis-studio-backend/phone_link.py`, `phone.result` handler |
| Raw actions (apps, fs, computer, browser) | `jarvis-studio-backend/actions/` |
| Android TV (network ADB, screen capture) / TV operator by sight | `jarvis-studio-backend/actions/tv.py` / `tv_operator.py` — Netflix blocks every screenshot (DRM, by design) |
| "Send this to the TV" (what's playing: Windows media controls + the browser's address bar) | `jarvis-studio-backend/actions/now_playing.py`, `tv.cast` |
| Wake-on-LAN (TV from off; the PC's adapters sent to the phone in `auth.ready`) | `jarvis-studio-backend/actions/wol.py`, `tv.wake`, `websocket_server.set_ready_info` |
| Transport, auth, remote allowlist | `jarvis-studio-backend/server/websocket_server.py` |
| Phone pairing crypto + registry | `jarvis-studio-backend/autonomy/device_identity.py` |
| Home screen (header, orb + ask box, the cards) | `jarvis-studio-gui/src/hud/HudApp.jsx`, `widgets.jsx`, `Orb.jsx`, `home.css` |
| Panels over it (chat, Skills, Memory…), Settings, palette | `jarvis-studio-gui/src/hud/HudPanels.jsx`, `src/components/` |

## Knowledge graph (graphify)

`graphify-out/` is a generated AST graph, git-ignored. For codebase questions run
`graphify query "<question>"` first — it returns a scoped subgraph far smaller than
grep output. `graphify path "A" "B"` for relationships, `graphify explain "X"` for one
concept, `graphify-out/GRAPH_REPORT.md` only for broad architecture review.

Rebuild (no API key, ~1 min): `graphify extract . --code-only && graphify cluster-only .`
After code changes: `graphify update .`

## Verify before claiming done

```powershell
cd jarvis-studio-backend; python -m unittest discover -s . -p "test_*.py" -t .
cd jarvis-studio-gui; npm run build
```

508 backend tests, no network or API keys needed (one, in `test_files.py`, can only pass on Windows). GUI: `npm test` (vitest), `npm run lint`. CI runs them too (`.github/workflows/ci.yml`
installs a hand-picked minimum — add to that list if a new test needs a module).
For anything touching the live voice/WS wiring, run the app (`python start.py`),
don't stop at compiling.

**Replay tests** (`test_replay.py`, `replay_fixtures.json`) play real model replies back
through the real pipeline. To capture new ones, start JARVIS with
`JARVIS_RECORD_TURNS=1`: each turn lands in `%APPDATA%\Jarvis\recorded_turns.jsonl` in
fixture shape (turn_recorder.py); add `results` (what each action returned, with
`feed_to_model` where the real one has it) and `expect`, and it's a test. When a model
does something surprising, record it and keep it.

## Conventions

- Optional dependencies are lazy-imported and fail soft: a missing package disables
  exactly one feature and says what to `pip install`. Keep that pattern.
- Secrets live in `%APPDATA%\Jarvis\`, never in the tree (`app_secrets.py`).
- GUI menus are built from `components/controls.jsx` (Card, Row, Switch, Guide, Note,
  ConfirmButton for anything destructive) and the classes in `src/ui.css`. Colour with
  `var(--accent)` / `color-mix(…)`, never a literal cyan: App sets `--accent` from the
  user's theme. New menu actions also belong in App's command-palette list (Ctrl+K).
- A remote (phone) client is confined to the allowlist in `main.py`'s
  `set_remote_allowed({...})`. Adding a handler does **not** expose it remotely —
  and anything host-configuring must stay off that list.
- A chat turn advertises CORE tools plus the groups in `live_tools.GROUPS` its words
  (or the last two turns) point at. A new tool goes in a group unless nearly every
  turn needs it; add cue words for it to `_CUES`. A group only decides what's
  *advertised*: every call still runs through `to_spec` → `actions.run_specs`.
- An MCP tool call is `{"type": "mcp", …}` through `run_specs` like everything else.
  It is asked about unless the tool's own `readOnlyHint` says it only reads or the
  user marked the server trusted, and its answer goes back labelled as data.
- A WhatsApp command is an ordinary pipeline turn (`_run_pipeline(wa_chat=…)`): the
  PC's voice is quiet, every HUD event goes to a tap that builds the reply, and
  `request_permission` / `request_clarification` also ask in that chat. Every action
  not in `actions._REMOTE_FREE` asks first, open consent windows or not — a new
  read-only lookup belongs on that list. Never add a
  tool that messages anyone: replies only go to the chat a command came from.
- The email connector reads and writes drafts; it has no send, so JARVIS still has no
  tool that messages anyone. From WhatsApp, reading mail asks first, like reading a file. `phone_command` reaches only the paired phone, which runs
  it with its own short list of tools (`FROM_PC_TOOLS` in the phone app) and reports
  `ok` from its tool results.
- Agent mode is opt-in (`agent_mode` in config). The chat agent IS the ordinary
  pipeline turn: a coordinator change must leave anything it isn't sure about there,
  and every agent step goes through `run_specs`' consent gate like any other task.
- The phone app (`AnaaySampat/jarvis`) is a TypeScript port of this backend and now
  leads on some fixes; its `docs/STATUS.md` says what was verified on a device. When
  porting, keep the two in step (its `quota.ts` per-minute windows already are).
