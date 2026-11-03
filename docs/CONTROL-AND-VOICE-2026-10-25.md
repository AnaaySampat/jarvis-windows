# Computer control and Groq transcription — updated 26 October 2026

This records the control, screen-indicator and voice changes requested during the feature-verification task. The broader feature inventory, previous fixes, live service checks and remaining UI/account/device evidence are recorded in [FEATURE-VERIFICATION-2026-10-24.md](FEATURE-VERIFICATION-2026-10-24.md). Passing automated checks do not certify every feature on real hardware.

## Requests retained

1. Test the Windows app's features and fix reproducible failures.
2. Substantially improve computer control.
3. Show blue tint in screen corners while JARVIS controls the laptop.
4. Run Whisper transcription through the user's Groq key, without local Whisper fallback.

## Computer-control improvements

- Verify the actual foreground HWND before reporting successful focus; do not silently choose the first of several app documents.
- Bind input to a consistent observed window. A focus change, stale selector, replaced control, or ambiguous name requires another observation instead of sending input elsewhere.
- Require explicit typing targets to gain focus and verify the complete text before submitting Enter. Partial input failures cannot trigger an alternate backend that repeats the same text.
- Release modifiers after failed key delivery and clear observation bindings when control is armed or disarmed.
- Convert scroll amounts to wheel notches correctly, fixing excessive scrolling.
- Distinguish JARVIS's own process/overlay from documents that happen to be named JARVIS.
- Discard decisions made before a new user correction or pause. Re-check Stop, pause, consent and focus after UIA work yields, immediately before dispatch.
- Keep fast/leading launches paused until resume, and report focus failures honestly.

An actual native-app request opened a new Notepad tab, typed exactly **JARVIS desktop control verified 42**, and left it unsaved. Accessibility inspection verified the full text; the workflow journal recorded success with **four confirmed operations in 47,964 milliseconds**. This successful rerun followed the user's instruction to continue after an earlier attempt was stopped before inspection. No existing document was overwritten.

## Screen-corner indicator

Four small native blue gradient windows are positioned at the physical corners of every monitor, including displays with negative origins or mixed scaling. They do not activate, intercept clicks or create extra WebViews. Windows capture exclusion keeps them out of the operator's screenshots.

The main app renews the indicator only while connected, desktop control is armed, an actual computer task is running, and control is not paused. Pause, stop, completion, disarm, disconnect and application shutdown hide it. A four-second native lease also hides it if the UI stops renewing. An armed but idle permission window does not imply active work.

An actual Windows display test verified the corner windows, physical bounds, click-through/no-activate styles, hit testing, capture affinity, lease expiry, explicit hide/restart and shutdown cleanup. No mouse or keyboard input was used by that display test.

The user also confirmed the soft blue tint appeared in all four screen corners during the real Notepad task. A final status correction distinguishes **armed/ready**, **controlling/live**, **paused** and **waiting**; the backend now broadcasts automatic lease expiry rather than waiting for explicit disarm. Targeted tests and the frontend build passed. The final combined run and live status-expiry recheck are being completed separately.

## Groq-only transcription

The user's saved `stt` preference was set to `groq` through the normal app configuration writer. Microphone recordings, wake clips and voice notes use `whisper-large-v3-turbo` with the configured Groq key. Groq mode does not import, download, preload, cache or fall back to a local Whisper model. Missing keys and cloud failures produce an actionable message.

Local wake-word detection and Piper speech synthesis are separate from transcription. Explicit Local transcription remains an optional setting for other use cases; a missing Groq key never selects it automatically. Settings and the README describe which service receives speech.

A live synthetic 3.5-second Piper clip was transcribed by the existing Groq key as **JARVIS CLOUD TRANSCRIPTION TEST NUMBER 42** in 6.61 seconds. The probe confirmed that `faster_whisper` was not imported and no local Whisper model was cached. No personal microphone recording was used for this probe.

After the source app restarted with Groq selected, the user said “Hey Jarvis, what is twenty plus twenty-two?” and confirmed that the transcript and text answer **42** appeared. This establishes physical wake/microphone capture through the running app. Speakers stayed muted, so audible speech output is not certified.

## Other retained fixes

Earlier work repaired the missing camera OCR interface/handler, image-analysis fallback, provider-aware translation, multi-paragraph PDF output, recorder false-success reports and test-loop cleanup. Failed GPU counter queries now back off instead of repeatedly launching expensive subprocesses; actual GPU counter availability is still not certified.

## Validation

- Latest completed full backend discovery: **653 passed in 223.496 seconds** (`.test-tmp/latest-complete-backend-tests.log`). This supersedes the earlier 652-test run and precedes the final automatic-expiry addition.
- Final control-expiry/remote/voice targeted run: **22 passed in 6.117 seconds** (`.test-tmp/final-control-expiry-tests.log`).
- Final focused control/voice integration run: **102 passed** (`.test-tmp/last-control-voice-regressions.log`).
- Native computer-control tests: **40 passed**, including 20 new regressions (`.test-tmp/computer-reliability-tests.log`).
- Strict Groq transcription/startup tests: **19 passed** (`.test-tmp/groq-transcription-tests.log`).
- Latest completed full frontend suite: **28 passed**; full lint has zero errors and 22 existing warnings; production build passed. After the final status wording change, **six focused control tests passed**, focused lint had zero errors and seven existing warnings, and production build passed in **33.05 seconds** (`.test-tmp/control-readiness-*.log`).
- Native overlay geometry/gradient/lease tests: **4 passed**; real Windows overlay lifecycle test: **1 passed**.
- Earlier broad checks include nine scripted workflow evaluation cases, seven Chrome-extension checks, twelve actual local artifact/storage checks, a live browser task, and live synthetic vision/camera service checks.

New full backend and frontend runs are underway (`.test-tmp/final-complete-control-tests.log` and `.test-tmp/final-2026-10-26-frontend-tests.log`). Their results are pending and are not counted as passed here. The source-app startup log used for the successful Notepad/microphone checks contains no local Whisper loading/preload message in Groq mode; the final backend status-expiry live recheck is still being completed.

## Remaining live evidence

- The user cannot test the paired phone now; phone commands, pairing, remote tasks/control and memory exchange remain deferred. The TV is unavailable, and its read-only ADB connection probe timed out.
- Physical webcam OCR/recording, microphone recording-file quality, audible output, full-duplex/interruption and each voice/provider selector still need their specific checks. The physical wake/transcript/text-answer check already passed.
- Signed-in Chrome, WhatsApp request/reply/progress/send-to-self, real email and external MCP accounts are not certified by mocked or read-only connection tests. Successful AI image generation remains upstream-quota blocked.
- Local UI flows not individually recorded include conversation management/attachments, every rich-response variant, Memory/Skills/schedule editing, theme/background/HUD layout, push-to-talk/device/mute controls, first-run/tray/reset and release-notice links.
- Physical arrival/sleep/shutdown/wake, Spotify/external playback, GPU-counter recovery and speed-test UI remain separate checks. The broader report preserves the exact automated and local-smoke evidence for their related components.
