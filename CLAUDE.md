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
| Skill files (`read_skill`), heartbeat, routines | `jarvis-studio-backend/skill_files.py`, `heartbeat.py`, `routines.py` |
| Raw actions (apps, fs, computer, browser) | `jarvis-studio-backend/actions/` |
| Android TV (network ADB, screen capture) / TV operator by sight | `jarvis-studio-backend/actions/tv.py` / `tv_operator.py` — Netflix blocks every screenshot (DRM, by design) |
| Transport, auth, remote allowlist | `jarvis-studio-backend/server/websocket_server.py` |
| Phone pairing crypto + registry | `jarvis-studio-backend/autonomy/device_identity.py` |
| HUD windows/components | `jarvis-studio-gui/src/` (`hud/`, `components/`) |

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

352 backend tests, no network or API keys needed. GUI: `npm test` (vitest), `npm run lint`. CI runs them too (`.github/workflows/ci.yml`
installs a hand-picked minimum — add to that list if a new test needs a module).
For anything touching the live voice/WS wiring, run the app (`python start.py`),
don't stop at compiling.

## Conventions

- Optional dependencies are lazy-imported and fail soft: a missing package disables
  exactly one feature and says what to `pip install`. Keep that pattern.
- Secrets live in `%APPDATA%\Jarvis\`, never in the tree (`app_secrets.py`).
- A remote (phone) client is confined to the allowlist in `main.py`'s
  `set_remote_allowed({...})`. Adding a handler does **not** expose it remotely —
  and anything host-configuring must stay off that list.
- A chat turn advertises CORE tools plus the groups in `live_tools.GROUPS` its words
  (or the last two turns) point at. A new tool goes in a group unless nearly every
  turn needs it; add cue words for it to `_CUES`. A group only decides what's
  *advertised*: every call still runs through `to_spec` → `actions.run_specs`.
- The phone app (`AnaaySampat/jarvis`) is a TypeScript port of this backend and now
  leads on some fixes; its `docs/STATUS.md` says what was verified on a device. When
  porting, keep the two in step (its `quota.ts` per-minute windows already are).
