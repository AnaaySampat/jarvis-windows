<div align="center">

# J.A.R.V.I.S. for Windows

**A voice assistant that actually does things on your PC.**
Say *"Hey Jarvis"*, ask for something, and it answers out loud, drives a browser,
clicks through desktop apps, and remembers you. All from one home screen.

[![CI](https://github.com/AnaaySampat/jarvis-windows/actions/workflows/ci.yml/badge.svg)](https://github.com/AnaaySampat/jarvis-windows/actions/workflows/ci.yml)
![Windows 10/11](https://img.shields.io/badge/platform-Windows%2010%20%2F%2011-0078d4)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![Latest release](https://img.shields.io/github/v/release/AnaaySampat/jarvis-windows)](https://github.com/AnaaySampat/jarvis-windows/releases/latest)

![The JARVIS home screen: system, power and agenda cards on the left; the time, JARVIS's orb, its last reply and an ask box in the centre; weather and network on the right](docs/hud.png)

<sub>Screenshot shows demo data.</sub>

</div>

## Why it's different

Most desktop assistants stop at answering questions. JARVIS hands the task to an
**autopilot** that reads the screen, plans the steps, acts, checks each step
landed, and corrects itself when it didn't.

- **"Find a carbonara recipe and open it"**: it drives its own Chromium, reads the
  page, clicks, types and verifies.
- **"Open Notepad and write a shopping list"**: it reads the Windows accessibility
  tree and works real desktop apps, behind an explicit time-limited consent and a
  corner-of-the-screen abort.
- **From your phone**: pair it over Tailscale and send tasks, watch the PC live,
  and take over the mouse and keyboard, all end-to-end on your own network.

## Features

| | |
|---|---|
| 🎙️ **Hands-free voice** | Local wake word ("Hey Jarvis") and local Whisper transcription. Push-to-talk with **Ctrl+Space** from any app. Full-duplex voice mode via Gemini Live. |
| 🧠 **Any model, routed for you** | Bring keys for Groq, Gemini, OpenRouter, NVIDIA NIM, Mistral, Claude, OpenAI, Grok or Meta Llama. **Auto** ranks your models by benchmark and picks per request, falling back when one is rate-limited. Vertex AI (Google Cloud login) and offline Ollama work too. |
| 🌐 **Browser autopilot** | Goal in, result out. Sandboxed to a browser JARVIS owns, with approvals for anything risky. Watch every step and screenshot in the Agent Activity panel. |
| 🖥️ **Desktop control** | Opens, closes and operates Windows apps through UI Automation. Off until you arm it, with a live "JARVIS CONTROLLING" bar to pause or correct it. |
| 📱 **Phone remote** | QR + PIN pairing, signed messages, WebRTC screen streaming. A phone can run tasks but can never touch the PC's keys or settings. |
| 💬 **WhatsApp** | Optional: link JARVIS to your WhatsApp and message it from anywhere ("Jarvis, send me a screenshot"). Replies and approvals come back in the chat; voice notes work. A long task shows its steps as it goes, and "Jarvis, status" says what it's doing right now. |
| 🧭 **Agent mode** | Optional: a coordinator sends each request to a chat, browser or desktop agent, and plans a task that needs more than one ("find the cheapest flight to Goa and write it in Notepad"); the agents hand each other what they found. |
| 🔌 **Connectors** | Gmail (read, search, write drafts; never sends) with just an app password, plus one-form set-up for GitHub, Notion, Google Calendar, Todoist, Home Assistant, Brave Search, your folders and Zapier. |
| 📺 **TV** | Control an Android TV over its own network ADB: play a show on Netflix or YouTube, pause, volume, power, and multi-step tasks by sight. |
| 📊 **Rich answers** | Charts, tables, timelines and flowcharts render inline in the chat. |
| 💾 **Memory & playbooks** | "Remember that…" keeps facts across sessions. Teach a multi-step routine once and it reuses it. Everything is stored locally and viewable in the Memory panel. |
| 📚 **Skill files** | Write a how-to as a Markdown file in the `skills/` folder (OpenClaw `SKILL.md` files work too). JARVIS sees only its name and description, and reads it in full when a request needs it. |
| 💓 **Heartbeat** | Optional check-ins: every half hour JARVIS reads your checklist (`HEARTBEAT.md`) against the time, agenda, battery and weather, and speaks only when something needs you. |
| ⏰ **Everyday stuff** | Reminders, timers, routines you run by name or on a schedule (listed and editable in Memory ▸ Routines), a voice-editable agenda, weather, nearby places, directions, news, web search with citations (Gemini, with an optional Tavily backup). |
| 🛠️ **PC actions** | Volume, power, screenshots, screen/voice/video recording, QR codes, AI images, PDF read/write, speed test, camera OCR with translation. |
| 🎨 **Make it yours** | Restyle the HUD by voice: colours, background, density and panel layout. Voices: Piper (offline), ElevenLabs or Google Chirp 3 HD. |

## Download

Grab the installer from the **[latest release](https://github.com/AnaaySampat/jarvis-windows/releases/latest)**.

- It bundles everything (backend, Chromium, Whisper, the Piper voice), so it works
  offline right after install. No Python needed.
- On first launch, paste one API key ([Groq](https://console.groq.com/keys) and
  [Gemini](https://aistudio.google.com/apikey) both have free tiers) and choose where
  JARVIS saves its files.
- The installer isn't code-signed yet, so Windows SmartScreen will warn you. Click
  **More info → Run anyway**.

## Run from source

Requires Windows 10/11, Python 3.11+, Node.js 20+ and the Rust toolchain.

```powershell
git clone https://github.com/AnaaySampat/jarvis-windows.git
cd jarvis-windows

# Backend
cd jarvis-studio-backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium     # for the browser autopilot
cd ..

# GUI
cd jarvis-studio-gui
npm install
cd ..

# Launch both
python start.py
```

`python jarvis-studio-backend\main.py --text` runs the backend with typed input
instead of the mic.

Every optional package fails soft. If one is missing, only its feature turns off,
and JARVIS tells you what to `pip install`.

<details>
<summary>Which package powers which feature</summary>

| Feature | Package(s) |
|---|---|
| Vertex AI (Gemini / Imagen / Chirp / Vision) | `google-auth` + `gcloud auth application-default login` |
| Browser autopilot | `playwright` + `playwright install chromium` |
| Camera OCR | Cloud Vision via Vertex, or local `easyocr` + `opencv-python` + `pillow` |
| Live HUD telemetry | `psutil` (GPU gauge: `nvidia-ml-py` or `GPUtil`) |
| Exact volume control | `pycaw`, `comtypes` |
| Screenshots / QR codes | `mss` or `pillow` / `qrcode` |
| PDFs | `pypdf` (read), `fpdf2` (write) |
| Video recording | `opencv-python` |
| Speed test | `speedtest-cli` |
| Phone pairing | `cryptography` + Tailscale on both devices |
| Live remote desktop | `aiortc` |

</details>

## How it works

```
  Phone                                Desktop
  ─────────────────────                ─────────────────────────────────────
  signed envelopes  ──── Tailscale ──▶ +-------------------+
  WebRTC video      ◀───  (100.64/10)  |   JARVIS HUD      |
                                       |  (Tauri + React)  |
                                       +-------------------+
                                            │ WebSocket ws://127.0.0.1:8765
                                            ▼
  +------------------------------------------------------------------+
  |              Python backend (jarvis-studio-backend)               |
  |  openWakeWord → faster-whisper → LLM router → TTS                 |
  |  autopilot.py (browser + desktop operators) · task_router.py      |
  |  actions/ · llm/ · server/ (ws + webrtc) · autonomy/ (pairing)    |
  +------------------------------------------------------------------+
```

One WebSocket server serves both the local HUD (loopback, full access) and a paired
phone (signed, allowlisted). A deterministic router (`task_router.py`) decides
whether a goal needs the browser or the desktop before any model commits to one.

| Path | What's there |
|---|---|
| `jarvis-studio-backend/` | `main.py` (handlers + orchestration), `autopilot.py`, `actions/`, `llm/` (routing, model catalog), `server/`, `autonomy/` (pairing crypto), `transcription/`, `tts/`, `wake_word/` |
| `jarvis-studio-gui/` | Tauri 2 + React HUD: main window, status pill overlay, browser panel, control bar |
| `scripts/` | Installer build scripts |
| `start.py` | Launches backend + GUI for development |

## WhatsApp

Settings → Devices → WhatsApp → **Use WhatsApp**, then scan the code from your phone
(WhatsApp → Settings → Linked devices → Link a device), the same way you'd link
WhatsApp Web. After that, write to yourself in WhatsApp's **Message yourself** chat,
starting with JARVIS's name:

> Jarvis, what's on my agenda today?
> Jarvis, send me a screenshot
> Jarvis, open Spotify and play some jazz

- The reply comes back in that chat, and the PC stays quiet. Pictures (screenshots,
  generated images, QR codes) are sent as images, and voice notes are transcribed.
- Anything beyond a lookup (the time, weather, a web search…) asks you in the chat
  first, since nobody may be at the PC to see it: reply *yes* or *no*. An approval
  given at the PC doesn't carry over to WhatsApp.
- Attach a photo or a document with a "Jarvis, …" caption and it comes with the
  command ("Jarvis, what does this letter say?").
- Messages that don't start with "Jarvis" are left alone, unless they reply to one
  of JARVIS's messages. Messages to and from anyone else, and every group, are
  ignored. If JARVIS is linked to a second number, list your own number under it and
  message that number instead (the name rule applies there too).
- Commands sent while the PC was off aren't run when it comes back; JARVIS says so.
- A browser or desktop task gets one status message that JARVIS edits as its steps
  land, so you can watch it work. **"Jarvis, status"** (or "what are you doing?")
  is answered at once, even mid-task, with the last screen the task saw. A command
  sent while another is running is told what it's waiting for. Tasks sent from the
  paired phone show on the PC's home screen and in Agent Activity too.
- **Your phone, through the PC.** With a phone paired (below), "Jarvis, set an alarm
  on my phone for 7" works from the PC and from WhatsApp, including from another
  number you allowed: WhatsApp → this PC → the phone. The phone runs it with its
  alarms, timers, reminders and switches only, and JARVIS on it has to be open or
  recently used to answer. The phone can also answer WhatsApp itself while the PC is
  off: see the phone app's Settings ▸ PC & WhatsApp.

It needs the `neonize` package: `pip install -r requirements.txt` from source; the
installer includes it. It uses an unofficial
WhatsApp client, the WhatsApp Web protocol through
[neonize](https://github.com/krypton-byte/neonize)/whatsmeow, the same approach as
OpenClaw. That isn't covered by WhatsApp's terms, and WhatsApp can restrict accounts
that use one: a second number for JARVIS is the cautious choice. **Unlink** in the same
settings logs it out and deletes the session from this PC.

## One memory with the phone

What you ask JARVIS to remember, and your routines, are shared with the paired phone
app: tell either one, both know, and forgetting works the same way. The phone syncs
whenever it's connected (every minute, and right after a change). A timed routine still
goes off only on the device it was made on; the other lists it as "(on your phone)" /
"(on your PC)" and runs it when you say its name. Playbooks stay on each device.

Once a night (after 3 am) the **memory digest** reads what you said to JARVIS that day,
only your own words, and saves up to five new facts about you to Memory, plus a short
note of the day in `journal/` in your storage folder ("what did I do on Tuesday?").
Switch it off in Settings → Voice.

A routine can also run **when you get home** ("when I get home, turn on the TV"): make
it here or on the phone, and the phone, which knows when it joins your home Wi-Fi,
offers to run it as you arrive.

## MCP servers: more tools

JARVIS can use any **MCP server** (Model Context Protocol), the same ones Claude Desktop,
Cursor and others use: GitHub, Notion, databases, Home Assistant, your file system. Add
one under Settings → MCP Servers (a command such as
`npx -y @modelcontextprotocol/server-github`, or a server URL, plus its token), or paste
Claude Desktop's `mcpServers` block into `%APPDATA%\Jarvis\mcp.json`. Its tools load
when you name the server ("what's open on GitHub?"). A tool that changes something
asks you first, unless the server says the tool only reads or you mark the server
**trusted**. What a server returns is treated as data, never as instructions. It needs
`pip install mcp`; servers started with `npx` or `uvx` also need Node.js or uv. Each
server is a program running on your PC, so add only ones you trust.

## Connectors: Gmail, GitHub, Notion…

Settings → **Connectors** lists the services people connect most, each with its own
steps and one or two fields:

- **Gmail** (or Yahoo, iCloud, any IMAP mailbox) is built in. Make an app password
  (Google Account → 2-Step Verification → App passwords), paste it with your address,
  and ask "any new emails from Asha?", "read the one about the invoice", "draft a
  reply saying Friday works". JARVIS reads without marking anything read, and writes
  **drafts** in your Drafts folder; it never sends. The password stays in
  `%APPDATA%\Jarvis\secrets.json`.
- **GitHub, Notion, Google Calendar, Todoist, Home Assistant, Brave Search, Folders,
  Zapier** each build an MCP server entry for you (below) from a token or a link, so
  the same rules apply: tools load when you name the service, and one that changes
  something asks first. Most need Node.js; Google Calendar also needs its own Google
  Cloud OAuth client (the steps are on the card).

## Agent mode

Settings → Autopilot → **How JARVIS works** (or "Switch to agent mode" in Ctrl+K).
In agent mode each typed or spoken request goes to a specialist:

- the **chat agent** answers and does one-step things (timers, music, email, lookups) —
  it's JARVIS as usual, so anything unclear lands here;
- the **browser agent** does whole tasks on websites;
- the **desktop agent** does whole tasks in Windows apps and your files.

A request that needs both gets a short plan, shown in the chat and run in order. Each
agent works from what the earlier ones found, and when one needs something mid-task
("what's the price to write down?") it asks the other agent before it asks you. A step
that fails or that you don't approve stops the plan, and the reply says where. Every
step asks for browser or computer control exactly like a normal task. The live voice
session (Gemini Live) stays in simple mode.

## Two routers at home?

The PC, the phone and the TV reach each other directly, so they need to be on one
network. A second router plugged in by cable usually runs in **router mode**: its
Wi-Fi gets its own network (say `192.168.0.x` next to the main router's
`192.168.1.x`), and nothing on the main router's Wi-Fi can open a connection into it.
Two fixes, either one works:

1. **Switch the second router to Access Point mode** (on most TP-Link routers:
   Advanced → System → Operation Mode, or Operation Mode in Quick Setup; follow its
   cabling note). Its Wi-Fi names stay, but every device on every Wi-Fi joins the main
   router's network, so the phone, PC and TV see each other wherever they are.
2. **Tailscale.** The phone already reaches the PC over Tailscale from anywhere. For
   the TV, install Tailscale on it (it has an Android TV app) and add its `100.x`
   address after a comma in Settings → Devices → TV (`192.168.1.50, 100.64.1.2`).
   JARVIS uses whichever answers and remembers which one did.

If the TV can't be reached because it's on another router's network, JARVIS says so.

## Send this to the TV

Watching YouTube or Netflix in the browser? "Send this to the TV" plays it on the TV at
the same point and pauses it on the PC once the TV is playing (Netflix: paused here
first, so your profile resumes on the TV where it stopped). JARVIS reads what's playing
and where from Windows' media controls (the `winrt-Windows.Media.Control` package) and
which video it is from the browser's address bar, so the tab has to be the one in
front. A YouTube or Netflix link works too: "play this on the TV: <link>". The phone
app does the same for what's playing on the phone.

## Switching things on from off (Wake-on-LAN)

"Turn on the TV" works even when the TV is fully off (deep standby with its network
down): JARVIS sends its network card a Wake-on-LAN packet and waits for it to answer
before saying it's on. Turn on **Remote start** on the TV (Settings → Network; some
models call it "Turn on with Wi-Fi/LAN"). JARVIS reads the TV's MAC address the first
time the TV answers, or you can type it in Settings → Devices → TV. The phone app does
the same for the TV, and can switch **this PC** on ("turn on my PC"): the PC tells the
paired phone its network adapters when it connects. For that, enable Wake on LAN in the
PC's BIOS and in Device Manager → network adapter → Power Management ("Allow this
device to wake the computer"), and let JARVIS start with Windows. A wake-up packet
can't cross into another router's network, so the sender has to be on the same one.

## Privacy

- Wake word and transcription run **on your PC**. Mic audio stays local, except in
  the optional Gemini Live voice mode, which streams it to Google.
- What you say (as text), plus camera frames when you use OCR, goes to the AI
  provider you chose.
- API keys and settings live in `%APPDATA%\Jarvis\`, never in the project folder.
- Memory, chats and everything JARVIS creates stay on your disk (default `~/Jarvis`).
- Phone pairing stays on your own devices and Tailscale network. No relay, broker
  or account in between.

## Development

```powershell
# Backend tests: no network or API keys needed
cd jarvis-studio-backend
python -m unittest discover -s . -p "test_*.py" -t .

# GUI
cd jarvis-studio-gui
npm test
npm run lint
npm run build
```

CI runs all of these on every push. Build the Windows installer with
`.\scripts\build.ps1`. It freezes the backend with PyInstaller, bundles the runtime
assets, and writes the NSIS installer to
`jarvis-studio-gui\src-tauri\target\release\bundle\nsis\`. Add `-Lean` to
`scripts\package-backend.ps1` for a small installer that downloads assets on first run.

**Releasing.** Bump `version` in `jarvis-studio-gui/package.json` and
`jarvis-studio-gui/src-tauri/tauri.conf.json`, commit, then tag and push:
`git tag v0.3.0 && git push origin v0.3.0`. The Release workflow
(`.github/workflows/release.yml`) runs the tests, builds the installer and publishes it
as a GitHub release; installed copies then show the update notice. It refuses a tag
that doesn't match the version. "Run workflow" on the Actions tab does a dry run: the
same build, kept as a workflow artifact, nothing published.

## Contributing

Bug reports and pull requests are welcome. See [CONTRIBUTING.md](CONTRIBUTING.md)
and the [Code of Conduct](CODE_OF_CONDUCT.md). Found a security issue? Please
report it privately as described in [SECURITY.md](SECURITY.md), not in a public issue.

## License

MIT. See [LICENSE](LICENSE).

**Wake-word model:** the bundled `hey_jarvis` model comes from
[openWakeWord](https://github.com/dscripka/openWakeWord) by David Scripka. Its code
is Apache 2.0, but its pre-trained models are
**[CC BY-NC-SA 4.0](https://creativecommons.org/licenses/by-nc-sa/4.0/)**, which
forbids commercial use. For a commercial build, train your own model and put it
at `jarvis-studio-backend/wake_word/models/hey_jarvis.onnx`, or point
`JARVIS_WAKE_MODEL_PATH` at it.

*JARVIS and Iron Man are trademarks of Marvel. This is an unofficial fan project,
not affiliated with or endorsed by Marvel or Disney.*
