# Contributing

Thanks for helping. Bug reports, fixes and focused features are welcome.

Found a security issue? Don't file it here. Follow [SECURITY.md](SECURITY.md).

## Before you start

- For anything bigger than a bug fix, open an issue first so we can agree on the
  approach before you write it.
- Keep each PR to one change. Small PRs get reviewed faster.

## Setup

Requirements: Windows 10/11, Python 3.11+, Node 20+, Rust stable. See the
[Run from source](README.md#run-from-source) for the full setup. In short:

```powershell
cd jarvis-studio-backend
python -m venv .venv; .venv\Scripts\activate
pip install -r requirements.txt

cd ..\jarvis-studio-gui
npm install
```

Run both together from the repo root with `python start.py`.

## Checks

CI runs all of these. Run them before you open a PR:

```powershell
cd jarvis-studio-backend
python -m unittest discover -s . -p "test_*.py" -t .

cd ..\jarvis-studio-gui
npm run build
npm run lint
npm test
```

The backend tests need no network and no API keys. If a new test imports a module
at module scope, add that package to the install step in
[`.github/workflows/ci.yml`](.github/workflows/ci.yml).

If you change voice, WebSocket or autopilot wiring, run the app (`python start.py`)
and try it. Passing tests aren't enough for those parts.

## Conventions

- **Optional dependencies fail soft.** Import them lazily. A missing package should
  disable one feature and tell the user what to `pip install`, not crash startup.
- **No secrets in the tree.** Keys and credentials live in `%APPDATA%\Jarvis\`
  (see `app_secrets.py`). Never commit keys, tokens, `.env` files or logs that
  contain them.
- **Remote access is default-deny.** A new WebSocket handler is not reachable from
  the phone unless it's added to `set_remote_allowed({...})` in `main.py`. Never
  add handlers that change host configuration (keys, settings, pairing) to that list.
- **Actions that touch the PC need consent.** Desktop and browser autopilot steps
  go through the existing approval and arming gates. Don't add paths around them.
- Match the style of the code around your change.

## Pull requests

- Describe what changed and why, and how you tested it.
- Add or update tests for behaviour changes.
- Update the README if you add a feature, a dependency or a setting.
- By contributing, you agree your work is licensed under the [MIT License](LICENSE).
