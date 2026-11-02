# Windows / Android companion connection pass — 2026-11-02

The reported screenshot failure now has an application-level fix: a phone request
asks for screen-capture approval on that phone, waits for its signed answer, then
captures the PC screen in memory and returns the image to phone chat. It does not
drive JARVIS's desktop interface to click its capture controls. Denial and timeout
prevent capture; STOP prevents subsequent delivery. Generic JARVIS permission and
clarification requests inside phone tasks use the same originating-phone route.

## Changes

### Follow-up: a laptop-owned passkey for each new phone task

Set/change it in **Windows Settings → Devices → Phone → Phone-task passkey**.
Choose 8–128 characters; this is separate from the one-time pairing PIN. The phone
asks for it for each new PC task. The host enforces this even for a client that
bypasses the phone UI. Laptop-originated tasks remain exempt.

Only a salted PBKDF2-HMAC-SHA256 verifier (600,000 iterations) is persisted, protected
by current-user Windows DPAPI. Setting/status handlers are local-only and return no
passkey or verifier. The transient passkey is removed before routing/journaling/model
calls, and the phone neither saves it nor reuses it for another task. Reconnect may
resume the same accepted task without another entry. Missing/wrong/unset/corrupt
credentials fail closed, repeated incorrect entries are rate-limited, and an edit
invalidates unfinished checks of the old passkey. STOP during a check cancels the
request before action. Existing phone-side approval routing remains in place.

| Area | Result |
| --- | --- |
| Sending work | The paired-PC panel has Send to PC and Get a screenshot actions. They reuse fingerprint verification and the task protocol without a phone model call or live-video setup. Chat/voice also has `pc_screenshot`, lowering through the existing dispatcher. |
| Approvals | Windows now registers `approval.response`. Answers must match the authenticated requesting device, task, step, action digest, approval id and expiry; they are one-shot. A desktop approval cannot authorize a phone task. Reconnect replays the pending challenge. |
| Task recovery | Missing protocol negotiation, task list/status and unsubscribe handlers are wired. Task indexes and results use authenticated ownership; listing does not bundle all saved screenshot bytes. |
| Reconnects | A socket that never opens times out after 8 seconds. Failed local closes force recovery even without `onclose`. Stale native callbacks and queues cannot block or overwrite replacement connection state. Known plaintext LAN fallback is skipped when a Tailscale address is present. IPv6 URLs are bracketed. |
| Task submission | Concurrent callers waiting for reconnect cannot overwrite a pending task. Cancelling while waiting prevents a later submission. Accepted work resumes with its original task identity instead of executing twice. |
| Message integrity | RFC 8785 canonicalization matches JavaScript numbers and UTF-16 key ordering. Tiny exponents, large floating values and Unicode keys no longer produce false signature failures. Authentication counters must be positive safe integers. |
| Pairing and logs | Five wrong PIN attempts exhaust a challenge across reconnects/restarts. Consumed PIN fields are cleared, the phone drops the transient PIN after ready, and PIN fragments and phone command/reply contents are removed from those console logs. |
| Memory | One exchange runs at a time. A reply acknowledges only its own request cursor; stale replies cannot skip later changes. Older hosts safely receive local resends. |
| Screenshot privacy | Only embedded raster images from a signed, successful task with matching evidence enter chat. Remote URLs/SVG are rejected. Image pixels stay out of model tool results and WebView persistence. |

## Verification

The repeatable probe runs the actual Android TypeScript `RemotePC` client against
the Python WebSocket transport and registry with real P-256 signatures. It uses
fresh temporary identities and synthetic data. Its screenshot test runs the
production task and approval handlers with synthetic image capture. The fixture
binds only loopback and substitutes the network identity gate; it does not establish
real Tailscale health or real Windows screen capture.

From this Windows repository, with the Android repository alongside it:

```sh
node scripts/check-companion-link.mjs ../jarvis /path/to/backend/python
```

Node 22+ and the Android checkout's installed npm dependencies are needed. The Python
environment needs the backend test dependencies, including the new
`rfc8785>=0.1.4,<1`. Both normal and installer requirements and Windows CI include it.

The probe passes eight groups: pairing/pinned host verification, bidirectional signed
numeric/Unicode payloads, memory merge, PC-to-phone command/result correlation,
reconnect without re-pairing, phone-side screenshot approval/image return, broadcast
privacy/no duplicated execution, and immediate revocation. The production backend
also started with isolated app data and served the local HUD's device-list request;
audio, asset provisioning and unrelated background feeds were disabled for that smoke check.

Final checks:

| Check | Result |
| --- | --- |
| Passkey and remote task regression subset | 30 passed |
| Real client/server probe | 9 groups passed, including wrong-passkey rejection and disconnecting while phone approval is pending |
| Android suite | 578 passed, 9 skipped |
| Android typecheck, lint, web build | Passed; lint has zero errors/warnings |
| Windows GUI suite | 49 passed |
| Windows GUI typecheck, web build | Passed; lint has zero errors and 22 existing warnings |
| Python backend suite | 571 passed, 2 skipped, 1 existing Windows-path test failure on Linux |
| Python compilation and diff whitespace checks | Passed |

The full backend suite has one pre-existing Linux failure in
`test_files.FilesTests.test_a_pattern_moves_every_match_or_nothing`: it expects Windows
drive-path semantics. Other backend checks pass, including the new approval,
ownership, cancellation, signature, PIN-budget, memory and privacy regressions.
The Windows GUI tests, typecheck and build pass; lint reports 22 existing warnings
and zero errors. Android tests, typecheck, lint and web build pass. Native Android
and Windows packaging were not run here. `graphify` is not installed in this environment.

## Device verification and rollout

Install builds from both updated repositories. Existing device-key pairings remain
compatible; a fresh QR is required only if its PIN challenge was exhausted or the
pairing was revoked. A source-based Windows install must install the updated backend
requirements before launching.

On the physical devices, verify that a screenshot request opens the approval on the
phone, produces no JARVIS desktop permission modal, and returns the current image only
after phone approval. Repeat with denial and STOP. Then check phone-to-PC tasks across
Wi-Fi/mobile-data changes, a PC app restart, phone background/resume, real biometric
prompts and remote video/control recovery. These checks remain open: this environment
has neither the Windows app nor an attached Android phone/ADB or VPN.

Tailscale or provisioned TLS still supplies transport encryption; signatures supply
identity and message integrity. Memory intentionally syncs to the paired PC, and
ordinary assistant tasks may send requested content to the configured model provider.
Android can still suspend the phone WebView, so PC-to-phone commands require the app
to be awake. This pass does not add a native background connection service or redirect
Windows OS permission/UAC dialogs.
