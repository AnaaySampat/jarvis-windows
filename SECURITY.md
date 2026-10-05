# Security Policy

JARVIS can click, type, run apps and power off the PC it runs on, and a paired
phone can drive it over the network. Security bugs here have real consequences, so
please report them privately.

## Supported versions

Only the latest release and the `main` branch get security fixes.

| Version | Supported |
|---------|-----------|
| latest release / `main` | Yes |
| anything older | No |

## Reporting a vulnerability

**Do not open a public issue, discussion or pull request for a security bug.**

Report it through GitHub's private vulnerability reporting:
**[Report a vulnerability](../../security/advisories/new)** (repo → *Security* tab →
*Report a vulnerability*).

Please include:

- what an attacker can do, and from where (same PC, same LAN, Tailscale peer,
  malicious web page, crafted file…)
- steps to reproduce or a proof of concept
- the version or commit you tested, and your Windows version
- any logs, with API keys and personal data removed

What to expect:

- an acknowledgement within **7 days**
- an assessment and a fix plan within **30 days** for confirmed issues
- credit in the advisory and release notes, unless you'd rather stay anonymous

This is a small project maintained in spare time. These are goals, not a contract.

## Scope

In scope, for example:

- connecting to the backend WebSocket (`ws://127.0.0.1:8765`) without the session
  token, or from an origin that should be rejected
- getting past phone pairing, impersonating a paired device, or replaying signed
  envelopes
- a remote (phone) client calling a handler outside the remote allowlist
- running desktop or browser autopilot actions without the user's consent or
  approval gate, including through prompt injection from a web page, file or
  screen content
- API keys, ADC credentials or the host identity key leaking out of
  `%APPDATA%\Jarvis\`, into logs, or to a third party
- path traversal or arbitrary file write/execution through actions
- installer or update issues that let another user or process replace JARVIS
  binaries

Out of scope:

- attacks that already need admin rights or full control of the user's Windows
  account
- bugs in third-party services (Google, Groq, OpenAI, Anthropic, xAI, Meta,
  ElevenLabs, Tailscale). Report those upstream.
- LLM output that is wrong or rude, as long as it doesn't cause an unapproved action
- SmartScreen warnings on unsigned builds
- wake-word false triggers
- denial of service from a client that is already authenticated

## Hardening tips for users

- With phone pairing available, the backend listens on all network interfaces so
  a phone can reach it over Tailscale. Connections from other machines are refused
  unless they come from a verified Tailscale peer (or over TLS) and pass device
  authentication. If you don't use the phone remote, set
  `JARVIS_WS_BIND=127.0.0.1` to listen on localhost only.
- Revoke paired devices you no longer use.
- Arm desktop control only for as long as you need it.
- Use API keys with spending limits where the provider supports them. Paid keys
  in Auto routing can bill you.
