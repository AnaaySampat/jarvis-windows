# Ecosystem foundation integration (2026-10-31)

The canonical contract bundle is in the sibling Android repository at
`contracts/ecosystem-v1`. This repository vendors version 1.0.0 byte-for-byte.
`manifest.json` pins every schema and golden fixture by SHA-256. The existing
frontend, Rust and backend CI checks remain; backend discovery also runs
`test_ecosystem_contracts.py` (requires `jsonschema`).

Run `python -m unittest test_ecosystem_contracts` from the backend. Tests validate
all five schemas, common task states, partial results and SQLite interruption
recovery. They do not measure live model quality or open the foundation gate.
The signed auth.ready includes the installed source version when available,
contract support and `portable_workflows: false`. It never sends credentials.
Older phones ignore these optional fields and retain signed v2 functionality.

The Android Tasks and Health foundation preview is opt-in. New portable routing,
Google OAuth, background jobs, capture and deliverables remain gated on live
foundation acceptance. The Android `docs/ECOSYSTEM-IMPLEMENTATION.md` records the
program status and acceptance procedure. No installer or release was published.
