# J.A.R.V.I.S

A JARVIS-style AI voice assistant desktop app for **Windows** with an Iron-Man
HUD interface. It passively listens for the wake word **"Hey Jarvis"**,
transcribes your speech, and answers via **Google Gemini** (served through
**Vertex AI**'s fast multi-region *global* endpoint) or **Groq** — pick the model
in Settings; the **Gemini 3.5 Flash / 2.5 Flash-Lite** auto-hybrid is the default
(flagship for rich/agentic turns, fastest model for quick chatter), with Groq as
an automatic fallback.
Rich responses (charts, tables, schedules, flowcharts) render inside the Tauri +
React HUD, Jarvis can take real actions on your PC, and a cryptographically
paired phone can drive the same PC remotely over Tailscale. Wake-word detection and
transcription (faster-whisper) run locally; TTS can be local (Piper) or cloud
(Chirp 3 HD / ElevenLabs); the LLM, translation, vision/OCR and image generation
run in the cloud.

For cloud access you can either sign in with **Google Cloud ADC**
(`gcloud auth application-default login`) to run Gemini / Imagen / Chirp / Vision
through **Vertex AI** on your own project — no API key needed — or provide a
**Groq** and/or **Gemini** API key on first launch. Either way, credentials live
outside the project in your app-data folder, never in the source tree.

## What Jarvis can do

**Talk & reason**
- Answer questions, explain things, give step-by-step **instructions** ("how to
  make a cake") as prose **and** a rendered flowchart.
- **Rich visual answers** — ask for a **chart** (bar/line/area/pie), **table**,
  **schedule/timeline**, or **flowchart** and it renders inline.
- Conversation memory across the last several turns.

**Camera OCR + translation**
- Click the **◉** button in the header to open the camera panel, point it at
  text (or upload an image), and Jarvis reads it with **Google Cloud Vision**
  (when signed in via Vertex/ADC — no heavy local models needed) and
  **translates** the result. If Cloud Vision isn't available it falls back to a
  local **EasyOCR** pipeline (image enhancement + reading-order clustering) when
  those optional packages are installed.

**Take action on your PC** (spoken or typed — handled as `[ACTION]` intents)
- Open & **close** apps, open URLs/folders, **search the web**.
- **System volume** — relative ("volume up") or exact ("set volume to 30").
- **Power** — shutdown / restart / cancel / sleep / hibernate / log off / lock.
- **Screenshots**, **QR codes**, and **AI-generated images** (**Imagen 4** on Vertex, or the Gemini flash-image model — needs Vertex/ADC or a Gemini key).
- **Open a file Jarvis just made** — it knows its own storage folders.
- **Internet speed** test, **IP address** (local + public), **location**.
- **Current time** & **current day**.
- **Schedule** — a persistent per-day agenda Jarvis can **add to, edit, reschedule
  and remove** items in (and you can edit it directly on the HUD).
- **Read PDFs** (extract text → summarise) and **convert text → PDF**.
- **Voice / video / screen recording** (until you say stop).
- **Be silent** for N seconds, or **sleep** until you say "wake up".

**Agentic control — Jarvis does things for you**
- **Browser autopilot** — give it a goal ("find a recipe for carbonara and open
  it") and it drives its **own** Chromium: reads the page, clicks, types, plans the
  steps, verifies each one landed, and self-corrects. Sandboxed to a browser it
  owns; actions are approval-gated.
- **Supervised desktop control** — reads the Windows accessibility tree and
  clicks/types into other apps, behind an explicit *arm-for-N-minutes* consent with
  a corner FAILSAFE abort. Disabled by default.
- **Task routing** — a deterministic check (`task_router.py`) decides *browser* vs
  *desktop* for a whole-task goal before any model commits to a surface, so
  "open Notepad" stops becoming a web search.
- **Live agent activity** — see the steps it takes and the page screenshots it
  captures while it works.

**Remote control from your phone**
- **Pair a phone** over **Tailscale** (Settings → *Remote Access*): the PC shows a
  two-minute QR plus a separate 6-digit PIN you type on the phone. Identity is an
  ECDSA P-256 host key (DPAPI-protected) + a per-device public key in a local
  SQLite registry; every message is a signed, counter-bounded envelope. The QR
  carries public information only, and the PIN — never in the QR — is what
  actually approves the pairing.
- **Submit tasks from the phone** — the same autopilot runs on the PC, with its
  approval/clarify prompts relayed to the phone and a working STOP.
- **Live remote desktop** — WebRTC screen streaming (DTLS-SRTP, P2P over the
  Tailscale overlay, no third-party broker) plus armed mouse/keyboard input.
- A paired phone is confined to a fixed allowlist of message types: it can drive
  tasks and remote desktop, but can never reach the host's own configuration
  (API keys, sandbox dirs, raw actions, conversation management).

**Remembers & learns**
- **Long-term memory** — tell it "remember that…" and it keeps durable facts about
  you across sessions (stored locally).
- **Playbooks** — teach it a multi-step routine in plain English once; it reuses
  the recipe afterward.

**Out in the world**
- **Weather** (current + forecast), **nearby places** (cafés, ATMs, pharmacies…),
  **driving directions**, and **news headlines** — via free, key-less services.
- **Grounded web search** — answers questions about current/real-time facts with
  citations.

**Reminders, timers & routines**
- One-shot **reminders/timers** ("remind me in 10 minutes") and recurring
  **routines** ("every weekday at 8am, give me a morning brief").

**Make it yours**
- **Restyle the HUD** by voice or from the UI — theme colour, background, density,
  and which panels show where.
- A **floating overlay pill** that appears over other apps with live status, plus a
  global **Ctrl+Space** push-to-talk that works even when Jarvis is backgrounded.
- **Premium voices** — Piper (offline), ElevenLabs, or Google **Chirp 3 HD**.
- **Native-audio voice mode** — full-duplex spoken conversation via Gemini Live.

Everything Jarvis creates is saved under one folder you choose at setup
(default `~/Jarvis`), in `images/`, `recordings/` and `documents/` subfolders.

## Architecture

```
  Phone (aura-android)                 Desktop
  ─────────────────────                ─────────────────────────────────────
  signed envelopes  ──── Tailscale ──▶ +-------------------+
  WebRTC video      ◀───  (100.64/10)  |      JARVIS       |
                                       |  (Tauri + React)  |
                                       +-------------------+
                                            │ WebSocket ws://127.0.0.1:8765
                                            │ text · actions · telemetry · agent activity
                                            ▼
  +------------------------------------------------------------------+
  |              Python backend (jarvis-studio-backend)               |
  |  openWakeWord → faster-whisper → Vertex Gemini → TTS  (Groq alt)  |
  |  autopilot.py (browser + desktop operators) · task_router.py      |
  |  actions/ · llm/ · server/ (ws + webrtc) · autonomy/ (pairing)    |
  +------------------------------------------------------------------+
```

One WebSocket server serves both the local GUI (loopback, full message surface)
and a paired phone (remote, signed + allowlisted). See
[`server/websocket_server.py`](jarvis-studio-backend/server/websocket_server.py)
and [`autonomy/device_identity.py`](jarvis-studio-backend/autonomy/device_identity.py).

### Repository layout

| Path | What it is |
|------|------------|
| `jarvis-studio-backend/` | The backend. `main.py` (WS handlers + orchestration), `autopilot.py` (browser/desktop operators), `actions/`, `llm/`, `server/`, `autonomy/`, `transcription/`, `tts/`, `wake_word/` |
| `jarvis-studio-gui/` | Tauri 2 + React HUD. Four windows: `main`, `overlay` (status pill), `browser-panel`, `control-overlay` |
| `scripts/` | `build.ps1`, `package-backend.ps1`, `prefetch_preload_assets.py` |
| `start.py` | Launch backend + GUI together for development |
| `graphify-out/` | Generated knowledge graph (git-ignored, see [Development](#development)) |

## Quick start

```
# 1. Backend
cd jarvis-studio-backend
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
python main.py            # or: python main.py --text   (typed input, no mic)

# 2. GUI (in another terminal)
cd jarvis-studio-gui
npm install
npm run tauri dev
```

…or launch both at once from the repo root with `python start.py`.

> **Cloud access (Vertex ADC, or a Groq/Gemini key).** The recommended path is
> **Vertex AI via ADC**: run `gcloud auth application-default login` and set a
> quota project, then everything (Gemini, Imagen, Chirp TTS, Cloud Vision) runs
> on your own Google Cloud project — no API key needed. Alternatively, on first
> launch the app prompts for a **Groq** and/or **Gemini** key, stored in
> `%APPDATA%\Jarvis\secrets.json` (or via the `GROQ_API_KEY` / `GEMINI_API_KEY`
> environment variables). Get keys at https://console.groq.com/keys and
> https://aistudio.google.com/apikey.

### Feature dependencies

The core voice loop only needs the base requirements. The new powers pull in a
few extra packages (all in `requirements.txt`); if one isn't installed, only
that single feature is disabled and Jarvis tells you what to `pip install`:

| Feature                  | Package(s)                  |
|--------------------------|-----------------------------|
| Vertex AI (Gemini / Imagen / Chirp / Vision) | `google-auth` + a one-time `gcloud auth application-default login` |
| Camera OCR + translate   | Cloud Vision (via Vertex/ADC) — or local `easyocr`, `opencv-python`, `pillow` |
| Screenshots              | `mss` (or `pillow`)         |
| QR codes                 | `qrcode`                    |
| Exact volume control     | `pycaw`, `comtypes`         |
| Read PDFs                | `pypdf`                     |
| Text → PDF               | `fpdf2`                     |
| Internet speed test      | `speedtest-cli`             |
| Video recording          | `opencv-python`             |
| Live HUD telemetry       | `psutil` (CPU/RAM/disk/net/battery) |
| HUD GPU gauge (optional) | `nvidia-ml-py` *or* `GPUtil` (NVIDIA) |
| Browser autopilot        | `playwright` + `playwright install chromium` |
| Phone pairing            | `cryptography` (bundled) + Tailscale on both devices |
| Live remote desktop      | `aiortc` (optional — feature reports unavailable without it) |

## Development

Run the backend test suite (unittest, no network, no API keys — 189 tests):

```powershell
cd jarvis-studio-backend
python -m unittest discover -s . -p "test_*.py" -t .
```

CI ([`.github/workflows/ci.yml`](.github/workflows/ci.yml)) builds the frontend,
`cargo check`s the Tauri shell, byte-compiles the Python sources and runs this
suite. The CI job installs a small verified subset of `requirements.txt` (no
torch, Playwright, aiortc or Windows-automation stack) — if a new test imports a
module at module scope, add it to that list.

### Codebase knowledge graph (graphify)

`graphify-out/` holds a generated AST knowledge graph used to answer
"where does X live / what talks to Y" without grepping the whole tree. It is
git-ignored; rebuild it in about a minute with no API key:

```powershell
graphify extract . --code-only
graphify cluster-only .
```

Then query it:

```powershell
graphify query "how does a remote task reach the autopilot"
graphify path "handle_task_submit" "run_task"
graphify explain "DeviceIdentityRegistry"
```

After changing code, `graphify update .` refreshes it (AST only, no API cost).
Community names stay as `Community N` placeholders unless an LLM backend key is
configured — the structure, hubs and queries work regardless.

## Privacy
- Mic audio is processed in-memory for transcription; transcribed text, OCR
  text and camera frames (only when you use the camera) are sent to your chosen
  cloud provider — Google Cloud (Vertex AI / Cloud Vision) or Groq.
- Screenshots, QR codes, generated images, recordings and PDFs are written to
  the single storage folder you pick at setup (default `~/Jarvis`).
- API keys (or your Google Cloud ADC credentials) are stored locally in your
  app-data folder, outside the project tree.
- Phone pairing never leaves your own devices: the host key, the paired-device
  registry and every remote frame stay on your machine and your Tailscale
  network — there is no relay, broker or account in the middle.

## Wake-word model
Jarvis uses openWakeWord's built-in **`hey_jarvis`** model — no extra training
needed.

**License:** the wake-word model comes from
[openWakeWord](https://github.com/dscripka/openWakeWord) by David Scripka. Its code
is Apache 2.0, but its pre-trained models (including `hey_jarvis`) are licensed
under **[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)**,
which forbids commercial use. A paid build needs its own wake-word model, for
example one trained with openWakeWord's training pipeline on data you're licensed
to use. Put it at `jarvis-studio-backend/wake_word/models/hey_jarvis.onnx`, or point
`JARVIS_WAKE_MODEL_PATH` at it, and the loader uses it instead of the bundled model.

## System requirements

| Component | Requirement |
|-----------|-------------|
| OS | Windows 10/11 (primary target) |
| Python | 3.11+ on `PATH` (backend) |
| Node.js | 20+ (build GUI only) |
| Rust | stable toolchain (build GUI only) |
| WebView2 | Installed automatically by the NSIS installer if missing |
| API keys | Groq and/or Gemini (free tiers available) |

## Publishing & release builds

### Open-source repo checklist

- MIT license — see [LICENSE](LICENSE)
- User settings and API keys live in `%APPDATA%\Jarvis\` (never in the repo)

### Build the Windows installer

```powershell
# From the repo root (requires Python 3.11+, Node 20+, Rust):
.\scripts\build.ps1
```

This runs two steps:

1. `scripts\package-backend.ps1` — PyInstaller **one-folder** bundle
   (`dist/jarvis-backend/` → staged under `src-tauri/resources/jarvis-backend/`)
2. `npm run tauri:build` — NSIS installer with GUI + frozen backend

Installer output:

`jarvis-studio-gui\src-tauri\target\release\bundle\nsis\JARVIS_*_x64-setup.exe`

To rebuild only the Python bundle:

```powershell
.\scripts\package-backend.ps1
```

### Runtime layout

| Mode | Backend |
|------|---------|
| **Installed app** | Frozen `jarvis-backend/jarvis-backend.exe` (auto-spawned) |
| **`python start.py`** | Live `jarvis-studio-backend/` via Python |
| **Dev GUI only** | `JARVIS_BACKEND_DIR` or sibling `jarvis-studio-backend/` |

End users need **API keys only** — no Python install.

### Branding

Replace placeholder icons before a public release:

```powershell
python setup-icon.py path\to\logo.png
cd jarvis-studio-gui && npm run icons
```

### Code signing

Unsigned Windows installers trigger SmartScreen warnings. Sign the NSIS output
with a trusted certificate before wide distribution.
