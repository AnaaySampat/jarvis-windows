# Feature verification — 24–26 October 2026

This report tracks the current Windows app source, including the pre-existing working-tree changes. It distinguishes automated checks, live checks and checks that require connected accounts or user/device participation. A listed feature is not marked as passed merely because its code or dependency exists.

## Current result — 26 October

- The latest completed combined backend suite passed **653 tests in 223.496 seconds** (`.test-tmp/latest-complete-backend-tests.log`). After the final control-status expiry change, **22 targeted tests passed** (`.test-tmp/final-control-expiry-tests.log`). A new complete run is underway; it is not counted as passed here.
- The latest completed full frontend suite passed **28 tests**, with **zero lint errors and 22 existing warnings**. After the final armed/active/paused status wording change, **six focused control tests passed**, focused lint had zero errors and seven existing warnings, and the production build completed in 33.05 seconds (`.test-tmp/control-readiness-*.log`). A new full frontend run is underway. Scripted workflow evaluation passed **9/9** and Chrome-extension tests passed **7/7**.
- Twelve actual local artifact/storage checks passed, including PDF round trips, QR decoding, screenshots, and recording with decoded video frames. Details and isolated fixture paths are below.
- The native Windows app launched from source. Its live dashboard displayed system, battery, weather, network and agenda data. A prior typed chat returned the correct answer, **42**, for 17 + 25.
- **Live browser task passed:** from the native app, `@browser Open https://example.com in your browser and tell me the page title.` opened JARVIS's own browser and returned **“The page title is Example Domain, sir.”** The user approved the app's control dialog.
- **Live cloud vision and camera-service checks passed:** only the synthetic “JARVIS TEST 42” image was used. The user explicitly authorized image fallback between configured Gemini and Groq. No personal screenshot was sent by those probes. Image generation was correctly reported unavailable when its upstream service returned HTTP 429 quota exhaustion.
- **Live desktop task passed:** after the user resumed testing and approved control, JARVIS opened a new Notepad tab and left exactly `JARVIS desktop control verified 42` unsaved. Accessibility inspection verified the text, and the workflow journal recorded success with four confirmed operations in **47,964 milliseconds**. This successful rerun supersedes the earlier attempt stopped with physical Escape before inspection.
- **Blue corners and microphone passed with user confirmation:** the user saw the blue tint in all four corners during the Notepad task. They later said “Hey Jarvis, what is twenty plus twenty-two?” and confirmed that the transcript and text answer **42** appeared. Speakers remained muted, so this does not certify audible output. Whisper uses the saved Groq preference without local fallback; a separate synthetic live Groq check also passed.
- The user cannot test the phone now, and the TV is unavailable. Email, external MCP accounts and additional AI-provider credentials are not configured for live testing. Nightly/arrival/sleep/wake behavior and the other explicitly listed UI/device checks remain unverified live.

The feature-family matrix below records the evidence for each family and the remaining limits. Historical run counts and initial failures are retained in later sections and labeled where superseded. **This report does not certify that every feature has passed on real hardware or that every UI interaction has been exercised.**

## Status key

- **Automated pass**: the stated tests completed successfully; this does not establish real account or hardware behavior.
- **Live pass**: a stated operation completed against the running app or real service.
- **Setup needed**: a required account, credential, connection or device is unavailable.
- **User/device check needed**: a physical or consequential operation still needs participation.
- **Not individually verified live**: no recorded check establishes the specific UI/device interaction, even if related automated tests passed.

## Runtime and configuration evidence

A sanitized probe using the default Python 3.11 runtime found the packages for voice, transcription, TTS, browser control, desktop control, camera OCR, PDF, QR, screenshots, recording, network speed tests, Spotify, WebRTC, TV ADB, WhatsApp, MCP and Windows media control. This checks discoverability, not successful operation. The optional GPU telemetry packages `pynvml` and `GPUtil` were absent.

Configured AI services: **Groq, Gemini and ElevenLabs**. Agent mode is enabled. **WhatsApp is enabled and has a saved linked-device session. A TV is configured. Email is not configured. No MCP servers are configured.** No secret values, account addresses, device addresses or phone numbers are included in this report.

The sandboxed Windows Python event loop hung while creating its loopback socket pair, before the first async test could execute. Its temporary-directory check succeeded. The affected test processes were stopped, and the integration batch was restarted with permission to use local loopback outside the sandbox. This is an execution-environment restriction, not a confirmed app defect.

## Complete feature coverage matrix

| Feature family | Features covered | Status and evidence |
| --- | --- | --- |
| Home screen / HUD | Clock/date, assistant orb/status, ask box, reply preview, system/power/agenda/weather/network cards | Live source app/dashboard displayed; every widget interaction is not individually verified |
| Chat | Typed conversation, voice requests, new/open/delete conversations, saved history, attachments | Automated chat/memory tests; live typed and microphone answers of 42; conversation-management/attachment UI not individually verified |
| Rich responses | Markdown, code, links/citations, inline charts, tables, schedules/timelines and flowcharts | Automated Markdown/response-renderer tests; every rich-response variant not individually verified in native UI |
| Voice input | Local Hey Jarvis wake detection, Groq Whisper transcription, optional explicit Local mode, Ctrl+Space, microphone selection/mute | 19 strict transcription/startup tests; synthetic Groq and user microphone transcript/42 passed; hotkey/device-switch/mute interactions not individually verified |
| Continuous/live voice | Conversation/listening modes, interruption/stop speech, Gemini Live full duplex | Not live-certified; full-duplex, interruption and audible-response checks remain |
| Speech output | Offline Piper/system speech, Edge TTS, ElevenLabs, Google Chirp and voice selection | Synthetic Piper audio generated successfully; physical audible output and each provider/voice selector not live-certified; speakers remained muted |
| AI providers | Groq, Gemini, OpenRouter, NVIDIA NIM, Mistral, Claude, OpenAI, Grok, Meta Llama, Vertex ADC, Ollama | Live Groq and Gemini fallback chat passed; other providers need their configured credentials/runtime for live checks |
| Model routing | Auto selection, tier/capability routing, catalog refresh/rank, quota/rate-limit fallback | Automated routing/quota/ranking tests; live catalog/chat and approved vision recovery passed |
| Browser automation | Own Chromium and signed-in Chrome extension, navigate/read/click/type, observation and verification, approvals, screenshots | Automated operator/relay/observation tests and seven extension checks; own-browser Example Domain task passed; signed-in Chrome task not individually verified live |
| Desktop automation | Open/close Windows apps, accessibility inspection, click/type/keys/scroll, time-limited arm, pause/correct/stop/failsafe | 40 focused native-control tests plus intervention/operator regressions; live Notepad task succeeded with four confirmed operations; each input/stop variant not individually exercised live |
| Agent mode | Chat/desktop/browser/research/files/scheduler/content specialists, @agent selection, coordinator plans, hand-offs and inter-agent questions | Automated agent/router/workflow tests and nine scripted evaluations; live browser/desktop agent requests passed; each specialist UI flow not individually verified |
| Durable workflows | Recorded task intent, outcomes, artifacts/sources, agent progress and interrupted-run handling | Automated workflow tests/evaluation; live Notepad journal confirmed success, operations and elapsed time |
| Activity views | Agent Activity steps/screenshots, browser panel, controlling overlay, status pill and stop controls | Automated activity/tint tests; live done card and user-confirmed four blue corners; final status-expiry integration recheck underway |
| File operations | Allowed folders, search/list/read/write/create/move/copy/rename/recoverable delete, open outputs | Automated file tests and actual isolated CRUD/search/copy/move/overwrite-protection smoke pass; file-opening UI suppressed during smoke checks |
| Memory | Remember/forget/list facts, persistent personal context and Memory panel | Automated memory/chat tests and actual isolated remember/forget/archive/restore pass; full Memory panel interaction not individually verified live |
| Memory digest / journal | Nightly extraction of facts from the user's words and daily journal notes | Automated memory-digest tests; actual nightly clock-triggered behavior not observed |
| Phone memory sharing | Facts and named routines sync, edits/deletions, device origin avoids double firing | Automated pass; real paired-phone exchange needed |
| Playbooks and skill files | Teach/reuse multi-step routines; Markdown/OpenClaw-style SKILL.md loading | Actual isolated taught/learned recipes and Markdown discovery/read passed; full Skills UI not individually verified live |
| Heartbeat | Optional periodic checklist using time/agenda/battery/weather; speaks on actionable findings | Automated tests and scripted alert/repeat-suppression smoke passed; live proactive audible delivery not verified |
| Timers, reminders and routines | Create/list/cancel timers/reminders; named/manual/scheduled routines and home-arrival triggers | Automated routines tests and isolated once-only timer/reminder/schedule smoke passed; physical arrival and full scheduling UI need live checks |
| Agenda | Voice-editable agenda / schedules | Live agenda dashboard displayed; schedule-editing UI/voice flow not individually verified |
| Information lookups | Time, weather, nearby places, directions, news, web answers and citations | Live weather, forecast, news, translation and cited web search passed; places/directions not individually verified live |
| PC/media actions | System volume, playback controls, power actions, launch apps and Spotify | Automated volume/media coverage; live Notepad launch passed; Spotify/external playback and power/sleep/shutdown not exercised |
| Capture and recording | Screenshots, screen recording, voice recording, video/camera recording | Actual screenshot and screen recording decoded successfully; simulated audio/camera/error regressions passed; physical webcam/audio recording remains |
| Camera OCR | Camera text recognition and translation | 17 service/handler tests and frontend capture/request tests; live synthetic vision/camera-service pass; physical webcam permission/quality/text target still needed |
| Generated artifacts | QR codes, AI images, PDF reading/writing | Actual QR decoding and PDF round trips passed; image-generation failure handled correctly on upstream HTTP 429; successful generation remains quota blocked |
| Network/telemetry | CPU/RAM/disk/network/battery/GPU readings, network speed test | Live dashboard and independent CPU/memory observations; GPU failure-backoff regressions passed; actual GPU reading and speed-test UI not individually verified |
| Personalization | Theme/accent/background, HUD panel layout/density/style, voice commands to restyle, settings | Automated screen-style coverage; actual theme/layout/settings interactions not individually verified live |
| Onboarding / maintenance | First-run provider/storage setup, asset setup progress/repair, startup/tray/window behavior, reset | Source app startup and Groq setup/preload/asset-filter regressions passed; first-run/tray/reset UI not individually verified; real user settings were not reset |
| Updates | Daily release check, newer-version notice and release link | Automated version-order and once-daily check tests passed; actual newer-release notice/link UI not verified |
| Phone pairing | QR plus separate PIN, signed devices/Tailscale, list connected devices, revoke/re-pair | Automated pass; live phone pairing needs user participation |
| Remote tasks | Phone submit/cancel/progress/status, approvals/questions, reconnect/replay and task ownership | Automated pass; live paired-phone task needed |
| Remote screen/control | WebRTC screen stream, click/right/double click, drag, scroll, typing/keys, short lease and disconnect cleanup | Automated pass; live phone viewing/control needs participation |
| PC-to-phone commands | Phone alarms/timers/reminders/flashlight/ringer/DND/volume/media via PC or WhatsApp | Automated pass; connected active phone needed; no physical action sent |
| WhatsApp | QR link/unlink, self-chat/allowed-number commands, voice notes, photos/docs, in-chat approval and responses | Enabled/saved session; automated pass; incoming/outgoing live messages not sent by this lane |
| WhatsApp progress | Edited task progress, immediate status, busy notices and offline-command handling | Automated pass; real chat test needs user participation |
| Send to self | Text/files/screenshots to owner's own WhatsApp chat or own mailbox | Automated pass; no message sent; email needs setup |
| Email | Gmail/Yahoo/iCloud/custom IMAP inbox search/read, drafts/replies without marking read | Automated pass against fake IMAP; setup needed for live check: no mailbox configured |
| Connector presets | GitHub, Notion, Google Calendar, Todoist, Home Assistant, Brave Search, Folders and Zapier setup/status/disconnect | Automated pass; setup needed: no MCP services configured |
| MCP extension tools | stdio/HTTP servers, tool discovery, restart/disable/remove/trust, read-only/change approval | Automated pass including a real local MCP 2.x stdio server; external servers need setup |
| TV basic control | Network ADB, multiple addresses, app launch, Netflix titles/episodes, YouTube, pause/resume, volume/mute, power/status/remote keys | Automated pass against fake TV; live read-only ADB echo failed to connect |
| TV visual tasks | Inspect screen, visual question and multi-step screenshot-driven operation, pause/correct/stop | Automated pass against fake TV; real visual operation needs reachable TV and user participation; Netflix DRM blocks screenshots |
| TV handoff | Send YouTube/Netflix browser playback/link to TV, retain position where available and pause PC | Automated pass against fake TV; no playback changed automatically |
| Wake-on-LAN | Wake compatible TV; expose PC network adapters to paired phone for PC wake | Automated pass; no wake/power command sent; physical deep-off/sleep check needed |
| Privacy and access gates | Local settings/secrets/storage, allowed file roots, consent gates, phone host-configuration allowlist, signed remote identity/replay protection | Automated integration/guard tests; unauthorized local WebSocket refused live; user handled browser/desktop Allow dialogs; no credentials exposed |

## Retained requests and remaining live evidence — 26 October

The original feature-test/fix request and all later additions remain in scope: improve computer control, show blue screen corners during active control, and use the user's Groq key for Whisper without a local fallback. Implementation details and completed control/voice checks are in [CONTROL-AND-VOICE-2026-10-25.md](CONTROL-AND-VOICE-2026-10-25.md).

The remaining live evidence is explicitly limited as follows; related passing unit tests do not close these items:

- Phone pairing/re-pairing, PC-to-phone commands, memory exchange, task submission and WebRTC input: the user cannot test the phone now.
- TV control, visual tasks, playback handoff and Wake-on-LAN: the TV is unavailable and the read-only connection probe timed out.
- Physical webcam OCR/recording and microphone recording-file quality; audible speech and Gemini Live full-duplex/interruption. The physical wake/transcript/text-answer check passed, but speakers stayed muted.
- Signed-in Chrome operation, WhatsApp request/reply/progress/send-to-self, real email inbox/drafts, external MCP accounts and unconfigured AI/voice providers. Read-only connection checks and mocks do not establish those end-to-end operations. Successful AI image generation remains blocked by upstream quota.
- Local UI interactions not individually recorded: conversation create/open/delete and attachments; every rich-response variant; Memory/Skills and schedule editing; theme/background/HUD layout and voice selection; push-to-talk/device/mute controls; first-run setup, tray behavior, reset and release-notice links. The dashboard, typed/wake chat, browser request and Notepad task have live evidence.
- Physical home arrival, sleep/shutdown/wake, external media/Spotify behavior, actual GPU-counter recovery and the speed-test UI. These have not been inferred from packages, mocks or telemetry counters.

The final complete backend/frontend runs and final status-expiry live recheck are being completed separately. Running checks are not recorded as passed in this report.

## Integration lane automated checks

Command (run from `jarvis-studio-backend`):

```powershell
python -m unittest test_connectors test_mcp test_tv test_tv_reach test_now_playing test_wol test_phone_link test_whatsapp test_send_me test_memory_sync test_remote_screen test_remote_shim server.test_tailscale_transport server.test_remote_handshake autonomy.test_device_identity -v
```

**210 tests passed in 60.521 seconds**, outside the sandbox after diagnosing the Windows asyncio loopback restriction. This includes the real MCP 2.x stdio server, dispatcher result, fake IMAP drafts/read flags, TV state verification/DRM/network cases, phone replies, WhatsApp routing/progress/approvals, send-to-self restrictions, memory sync/deletion, remote leases/reconnect and device signatures/identity.

The original real-MCP test failed because its readiness deadline was 15 seconds. A diagnostic observed the real server still initializing at 15.2 seconds, ready by 20.2 seconds, then returning `5` for `add(2, 3)` through the dispatcher. `test_mcp.py` now waits up to the application's existing bounded startup budget, exits early on a failed-server state and reliably stops the temporary server/closes the loop on either success or assertion failure. No production MCP change was indicated.

Non-fatal output included an installed `requests` dependency-version warning and an unclosed-event-loop `ResourceWarning` elsewhere in the batch. These did not fail the tests and do not establish live account/device success.

## Live integration checks and remaining setup

- TV: configuration exists, but its read-only ADB echo timed out. The returned message asks the user to accept “Allow USB debugging?” on the TV if shown, with “Always allow.” No power, wake, volume, playback or app-launch action has been sent.
- Phone: the user cannot test it now. A later connected companion-app check is needed for real task submission, memory sync and WebRTC/control.
- WhatsApp: read-only authentication/session evidence was obtained, but no live request/reply or send-to-self message was sent. Those end-to-end checks remain separate.
- Email: connect a mailbox before live inbox/draft tests. Sending a draft to anyone is outside this verification lane; send-to-self needs a separate explicit live check.
- MCP/connector services: configure intended accounts/servers to verify their actual permissions and external data. A temporary local MCP server can verify the transport without external account writes.
- TV wake / PC wake / home arrival: require physical device/network state changes. Follow `docs/TESTING-2026-10-19.md`; do not claim these passed from fake-device tests.

## Frontend and camera OCR verification

- **Automated pass:** the initial full frontend run completed with **20 tests across five files**. The default sandbox temporary directory denied a Vitest cache rename; running with `TEMP` and `TMP` set to the workspace's `.test-tmp/vitest` resolved it. This was an execution-environment issue.
- **Automated pass:** the initial production frontend build completed. Full lint returned **zero errors** with 22 pre-existing warnings. Focused lint of the original new camera implementation was clean.
- **Automated pass:** after the camera edge review, **six focused camera capture/request tests** pass. These cover frame-size bounds, camera warm-up failure, stale replies, stale timers, late success, disconnect/retry, and clearing a closed request. Focused lint remains clean, and the updated production build completed with 753 modules; the existing large-bundle size warning remains.
- **Automated pass:** **15 camera service tests and one targeted-handler test** pass across focused runs. They cover actual PNG/JPEG decoding, rejected corrupt/truncated images, compressed-image dimension limits, language validation, explicit-language local fallback, provider failures, partial OCR retention, no-text results, translation shortcuts, request correlation and requester-only delivery. No cloud calls or real webcam access were used by these tests.
- **Fixed:** advertised camera OCR had no accessible panel or backend handler. Camera text recognition and translation now open from Skills, the command palette, and `open_camera`/`close_camera` voice UI actions. The panel handles absent/denied/busy cameras and disconnection, captures a still image up to 1280 pixels on its longest side, offers 16 translation languages, displays recognized text and translation, and supports copying either result. Camera tracks are released on close or retry.
- **Fixed:** the service originally recognized only a file signature. It now verifies and decodes actual PNG/JPEG images before using any provider, with limits of 4 MB compressed data, 16 million pixels and 8192 pixels on either side. Image validation runs off the WebSocket event loop. Tests include truncated/corrupt images and compressed images declaring excessive dimensions.
- **Fixed:** the panel now retains the response belonging to its current capture. Late replies and old timers cannot hide a newer successful result; disconnects and late successful responses are recoverable. Its timeout covers the service's cloud/local/translation fallback budget.
- **Local OCR boundary:** EasyOCR is optional; its explicit source-language combinations were checked against the installed library. Local automatic language detection is not available, so a failed cloud request asks the user to select the text's language instead of silently treating all text as English. At most two local language readers remain cached.
- **Privacy review:** the handler returns OCR text only to the requesting local window, and is absent from the remote-handler allowlist. Camera frames are not written to storage or conversation history. Captured images can go to the configured vision provider, and recognized text can go to the configured translation provider; the camera panel states the image-provider behavior before capture.
- **User/device check needed:** actual webcam image quality, Windows/WebView2 camera permission prompts, and OCR accuracy on a real text target still require the live device check. Unit tests do not establish physical-camera or cloud-provider quality.

## Live integration probes

- **Tailscale live pass**: CLI reports `Running`, with one online peer. This establishes overlay health, not that the JARVIS phone is connected.
- **Local WebSocket authorization pass**: an originless client without the private UI session token was refused. Connection status must be read through the authorized app UI/session; no origin was forged to bypass the gate.
- **Weather live pass**: a public London test coordinate returned current temperature, six hourly slots and AQI. A separate three-day forecast returned Today and Tomorrow labels. No user location was used.
- **News live pass**: the science RSS request returned two headlines, both with source links.
- Sanitized live device/weather/news results: `jarvis-studio-backend/.test-tmp/live-device-info-probe-2026-10-24.json`.
- **Groq live pass**: the model catalog returned HTTP 200; the catalog-selected `openai/gpt-oss-20b` returned the required synthetic check token. An initial probe pinned the older `llama-3.3-70b-versatile` and got HTTP 404; that ID is absent from the current catalog, so it is not evidence that the app's catalog-selected chat path fails.
- **Gemini live pass on a fallback model**: `gemini-2.5-flash-lite` returned the required token. A strict call to the app's default `gemini-3.5-flash` returned HTTP 429. Credentials and the service work, but that particular route is quota limited.
- **Translation live pass**: the app's translation function returned Hindi text for a synthetic English sentence, through its provider routing.
- **Grounded web search live pass**: a query for official Python JSON documentation returned an answer and one source. User history and location were explicitly excluded.
- **Historical vision failure, superseded:** a synthetic image containing “JARVIS TEST 42” and a red square initially returned no answer through `vision_query` when the selected Gemini route was quota limited; another strict image route returned HTTP 503. The early error return was repaired after the user approved Gemini/Groq image fallback. The later live recovery and camera-service passes below supersede this initial result. No personal screen/image was used by these probes.
- Sanitized AI evidence: `jarvis-studio-backend/.test-tmp/live-ai-probe-2026-10-24.json`, `live-supported-model-probe-2026-10-24.json` and `live-gemini-supported-probe-2026-10-24.json`. The synthetic image is `synthetic-vision-fixture-2026-10-24.png` in the same directory.


## Backend and local artifact verification

The initial backend baseline completed successfully with **569 tests in 87.475 seconds** (exit 0). It ran with `jarvis-studio-backend/.venv-build/Scripts/python.exe`, an isolated `JARVIS_CONFIG_DIR`, and Windows local socket access. The sandboxed attempt stalled in the Windows asyncio event loop's internal `socket.socketpair()` / `socket.accept()` before tests could progress; the same suite passed outside that restriction. Evidence: `.test-tmp/backend-regression.log`. One event-loop ResourceWarning appeared during teardown, without a failed test.

Additional offline checks passed: **9/9** scripted workflow evaluation cases (`.test-tmp/workflow-evaluation.log`) and **7/7** Chrome extension Node tests (`.test-tmp/chrome-extension-tests.log`). These use scripted models and mocked devices, rather than proving live provider or phone/TV connectivity.

Actual local feature smoke checks initially reproduced two PDF failures: a titled document with multiple paragraphs failed with `Not enough horizontal space to render a single character`, and a title containing an em dash failed core-font encoding. The fixes reset the PDF cursor after each multiline cell, normalize common smart punctuation in both title and body, and sanitize Windows filename characters in titles. The original failing evidence remains in `.test-tmp/local-feature-smoke-asp3k9ii/results.json`.

Recorder failure checks also reproduced incorrect success replies when a camera or encoder could not open, an audio device failed to initialize, a worker had already exited with an error, or stop timed out while a file was still being written. Recorders now wait for their first frame before confirming startup, capture device and encoder errors, preserve failures after worker exit, release resources on failure, and confirm that the worker has finished before saying output was saved.

The new targeted PDF and recorder regression suite passed **12/12 tests** (exit 0; `.test-tmp/pdf-recorder-tests.log`): four actual PDF round trips and eight recording tests, including a readable WAV produced from a simulated PCM input. CI installs `fpdf2` and `pypdf` so the PDF regression checks run there too.

After the fixes, **12/12 actual local smoke checks passed** (exit 0; `.test-tmp/local-feature-smoke.log`; structured evidence: `.test-tmp/local-feature-smoke-9p2og9qd/results.json`):

- QR generation with exact payload recovery through OpenCV's QR decoder.
- PDF creation and text extraction across title and multiple paragraphs.
- PDF title/body containing smart punctuation.
- Image persistence and a correctly resized JPEG thumbnail.
- Actual PNG/JPEG screen capture and a saved 1920 by 1080 screenshot.
- Actual screen recording start/stop and decoding of three recorded video frames.
- File create/read/list/search/rename/copy/move, with overwrite prevention.
- Durable remember/forget and conversation archive/restore.
- Reminder and timer delivery exactly once.
- Named, scheduled and home-arrival routine storage/trigger behavior.
- Taught and learned playbooks plus Markdown `SKILL.md` discovery and reading.
- Heartbeat alert generation and repeat suppression with a scripted model.

All smoke artifacts, configuration and memory data were created beneath disposable `.test-tmp/local-feature-smoke-*` directories. File-opening side effects were suppressed. User configuration and storage were not changed. Real microphone and webcam capture were not part of this lane; their startup/error behavior was tested with simulated devices. PDF output retains the existing core-font limitation for characters outside Latin-1, while common smart punctuation now has readable substitutes. The final combined backend suite is recorded separately after all parallel fixes settle.

## Earlier combined backend verification — 25 October 2026

- **Automated pass:** the complete backend discovery run passed **607 tests in 156.795 seconds**, exit 0. Evidence: `.test-tmp/backend-final-regression.log`. It used the existing `.venv-build` Python runtime, isolated configuration and temporary files under `.test-tmp/final-backend-2026-10-25`, and Windows loopback access. This includes the vision-route, translation, camera-handler/service, PDF and recorder regressions, plus the actual local MCP stdio server. No live model, mailbox, phone or TV operation was claimed from these automated tests.
- **Automated pass:** the scripted workflow evaluation passed **9/9 cases in 2,064 milliseconds**, exit 0 (`.test-tmp/workflow-final-evaluation.log`). Chrome extension checks passed **7/7**, exit 0 (`.test-tmp/chrome-extension-final-tests.log`).
- **Automated pass after the final camera change:** the current camera service and requester-only handler passed **17/17 tests in 70.404 seconds**, exit 0 (`.test-tmp/backend-final-camera-tests.log`). This targeted run includes the newly added regression for valid cold vision recovery exceeding the former 45-second outer timeout. It ran after the full suite had already imported the earlier camera source; the separate targeted evidence verifies the final allowance.
- The full suite emitted non-fatal installed-dependency and unclosed-event-loop warnings. The latter came from test fixtures that stopped background event-loop threads without closing their loops. `test_mcp.RunTests` and `test_send_me.SendToMeTests` now join/stop their own test threads and close the loops. Their targeted MCP/send-me run passed **20/20 tests in 228.043 seconds**, exit 0 (`.test-tmp/backend-final-loop-cleanup-tests.log`). These are test-harness changes, not a production feature change.

## Earlier camera/frontend verification — 25 October 2026

- **Automated pass:** the complete frontend suite passed **23 tests across five files** (exit 0, 62.67 seconds). Evidence: `.test-tmp/frontend-final-tests.log`. This includes the six camera capture/request lifecycle checks; the request reducer is implemented in `cameraCapture.js`.
- **Automated pass:** full frontend lint returned **zero errors and 22 existing warnings** (exit 0). Evidence: `.test-tmp/frontend-final-lint.log`.
- **Build pass:** the latest production build transformed **753 modules** and completed with exit 0 in 3 minutes 27 seconds under machine load. Its bundled JavaScript was checked for the current camera disclosure naming the configured Gemini/Groq fallback services. Evidence: `.test-tmp/frontend-final-build.log`. The existing large-chunk warning remains.
- **Fixed:** the camera service's former 45-second outer vision timeout could cancel valid recovery by the shared 60-second vision router. The actual synthetic recovery took **54.64 seconds**, so the service now allows 65 seconds for that router and the panel allows 185 seconds for the complete vision/local-OCR/translation path. Evidence for the observed recovery: `jarvis-studio-backend/.test-tmp/live-vision-recovery-probe-2026-10-25.json`.
- **Automated pass:** the latest camera service and targeted-handler run passed **17/17 tests** (exit 0, 30.815 seconds). Evidence: `.test-tmp/camera-final-regression.log`. A new regression simulates the observed cold-recovery deadline without adding a minute to every run and verifies that a valid vision response is retained rather than incorrectly attempting local OCR.

## Live image recovery checks — 25 October 2026

The approved vision fallback was tested against configured live services using only the existing synthetic PNG containing `JARVIS TEST 42` and a red square. No user screenshot, camera frame, conversation history or location was included. The probe used isolated quota state and did not change user credentials or settings.

- **Vision live pass:** the updated `vision_query` returned exactly `JARVIS TEST 42` after 54.64 seconds. This verifies that the actual configured image-analysis path can recover and produce the fixture's real text.
- **Camera service live pass:** a subsequent `camera_ocr.process_frame` request returned the same recognized text, a translation result, `ok: true`, and the matching request ID in 3.56 seconds. This establishes the synthetic image-to-service path; it does not establish webcam permissions or optical quality on physical camera input.
- **AI image generation remains quota blocked:** one bounded generation request for a plain red circle on white returned upstream HTTP 429, `ok: false`, and no image. The application correctly reported failure. No paid-plan setting or credential was changed to bypass the provider limit.
- **Timing finding:** cold vision recovery took longer than the camera service's then-current 45-second wrapper. The frontend/camera lane was asked to align its timeout with the vision function's 60-second budget and extend the panel's total timeout accordingly.

Sanitized evidence: `jarvis-studio-backend/.test-tmp/live-vision-recovery-probe-2026-10-25.json`. The corresponding probe script uses only synthetic fixture input and keeps API values out of its output.

## Runtime load observations — 25 October 2026

Read-only process sampling confirmed that the HUD's high CPU and memory readings reflected actual host load. Independent `psutil` samples reported 100% system CPU and approximately 85–87% memory use. A separate valid process-delta sample measured each process over its own elapsed interval (approximately 4.8–5.0 seconds), divided by eight logical processors. No process command lines or secrets are included in the evidence.

- JARVIS's main WebView renderer used 14.41% of whole-machine CPU and approximately 328 MB resident memory.
- Another JARVIS WebView used 2.99% and approximately 141 MB.
- The active Python backend used 3.40% and approximately 762 MB; it did not dominate this sample. The data does not isolate wake-word processing from Whisper within that process.
- Other measured load included ChatGPT processes and the test/tooling PowerShell processes. The measurements do not establish that all host load was caused by JARVIS.
- A preceding valid three-second sample observed `typeperf` using 4.97% of whole-machine CPU while the GPU gauge remained unavailable.

One bounded read-only GPU counter query timed out after 12.39 seconds and produced 9118 bytes of partial standard output. This does not prove missing GPU hardware or unavailable counters. At that point the telemetry source retried failed ten-second `typeperf` queries after 2.5 seconds. **This implementation finding was subsequently fixed:** failed reads now back off through 5, 10, 20, 40 and 60 seconds; a valid reading, including zero utilization, restores the normal 2.5-second interval. Two backoff/bounds/reset regressions are included in the completed backend suite. Actual GPU-counter recovery remains unverified.

Valid process evidence: `.test-tmp/runtime-cpu-valid-deltas-2026-10-25.json`. GPU-query metadata: `.test-tmp/typeperf_gpu_probe_2026_10_25.json`. The earlier experimental `runtime_cpu_probe_2026_10_25.json` had an incorrect denominator for process percentages and must not be used for process CPU conclusions; its separate system CPU/memory observations were valid.

## Strict Groq Whisper transcription — 25 October 2026

The user requested Groq Whisper with their Groq API key instead of transcription on the laptop. Groq mode now uses that service exclusively for microphone recordings, wake-word command clips and WhatsApp voice notes. A missing key, network error or rejected/quota-limited request raises an actionable `TranscriptionError`; an empty successful transcript stays empty. Neither case starts local Whisper. An explicitly selected Local mode remains available as a separate preference.

- Local `faster_whisper` imports and model construction are lazy and guarded by the explicit Local selection. Selecting Groq releases any cached local transcription model, and Groq startup never schedules its preload.
- First-run setup and repair filter out the local speech-model asset before presence checks or downloads whenever Groq is selected. Existing bundled model files are not deleted; they are simply not loaded or used in that mode.
- Settings → Voice now exposes the Speech-to-text selector and states that Groq mode sends audio to Groq, without a local fallback. Local wake-word detection and optional local Piper speech output remain separate from transcription.
- **Automated pass:** `test_groq_transcription`, `test_whisper_vad` and `test_voice_startup` passed **19/19 tests in 4.397 seconds**, exit 0. These cover all three input paths, missing keys, cloud failures, empty transcripts, transport errors, local-import/preload guards, startup mode selection, explicit Local behavior, and setup/repair asset filtering. Evidence: `.test-tmp/groq-transcription-tests.log`.
- **Live Groq pass:** a synthetic 3.5-second speech fixture generated with Piper was submitted through the actual `transcribe_clip` path using the configured Groq key. Groq `whisper-large-v3-turbo` returned **`JARVIS CLOUD TRANSCRIPTION TEST NUMBER 42`** in **6.61 seconds**. The probe confirmed `faster_whisper` was not imported and no local Whisper model was cached. No personal microphone audio, conversation history or location was used.

Sanitized live evidence: `.test-tmp/groq-whisper-live-result.json` and `.test-tmp/groq-whisper-live.log`. The local Piper synthesis in this probe produced test audio; it was not local speech recognition. After restarting the source app with the Groq preference, the user confirmed a physical “Hey Jarvis, what is twenty plus twenty-two?” request displayed the transcript and text answer 42. Speakers remained muted; audible output is not certified by this check.

## Native desktop-control corner tint — 25 October 2026

The app now renders a soft blue radial tint at all four physical corners of every connected monitor during a connected, armed, unpaused desktop-agent task. These are small native Win32 layered windows, not additional WebViews or a full-screen backdrop. They are click-through, do not activate or take keyboard focus, stay out of the taskbar, and are excluded from screen capture. The existing interactive control bar remains separate.

The main window renews a four-second native lease while the desktop task is active. Pause, stop, task completion, disarm, backend disconnection and component cleanup send hide; expiry also hides the corners if the webview stops renewing. Show/hide IPC is serialized and coalesced so a backlog of old heartbeats cannot delay the latest hide request. Monitor topology is checked while active; physical coordinates support negative monitor origins and mixed-DPI displays. Only the main Tauri window can invoke the native tint command.

- **Automated pass:** all **28 frontend tests across six files** passed in 5.54 seconds, including five tint gating, heartbeat, cleanup and IPC-order/coalescing regressions. Evidence: `.test-tmp/control-tint-current-js-tests.log`.
- **Lint pass:** zero errors and the same 22 existing warnings. Evidence: `.test-tmp/control-tint-all-lint.log`.
- **Build pass:** current production frontend build succeeded in 20.71 seconds, including the Groq transcription selector added by the audio lane. The existing large-chunk warning remains. Evidence: `.test-tmp/control-tint-frontend-build.log`.
- **Rust pass:** four pure geometry, gradient and lease tests passed. The explicit display test is ignored by default so CI does not create visible windows. Evidence: `.test-tmp/control-tint-rust-tests.log`.
- **Native Windows live pass:** the opt-in integration test passed in 4.59 seconds. It created the real corner windows on the available monitor(s), checked their exact physical bounds, click-through/no-activate/tool-window styles, transparent hit testing, capture-exclusion affinity, expiry hiding without renewal, explicit stop/restart and destruction on shutdown. It sent no mouse or keyboard input. Evidence: `.test-tmp/control-tint-native-smoke.log`.

The native display test establishes actual window creation and lifecycle behavior. The user also confirmed the blue tint in all four screen corners during the successful real Notepad task. An armed-but-idle permission lease deliberately does not show a controlling tint. Isolated one-step input without a running desktop-task event does not prolong the tint. The later status-wording change distinguishes armed idle, active, paused and waiting states; its six focused frontend tests and production build passed, and the final combined/live verification is still being completed.
