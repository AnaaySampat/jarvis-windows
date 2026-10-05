## What and why

<!-- What does this change, and why is it needed? Link the issue: Fixes #123 -->

## How it was tested

<!-- Tests added or run. For voice, WebSocket or autopilot changes: what you tried in the running app. -->

## Checklist

- [ ] Backend tests pass (`python -m unittest discover -s . -p "test_*.py" -t .`)
- [ ] GUI builds, lints and tests (`npm run build`, `npm run lint`, `npm test`)
- [ ] No keys, tokens or personal data in the diff or logs
- [ ] If a WebSocket handler was added, I checked whether it belongs on the remote allowlist (default: it doesn't)
- [ ] README updated if a feature, dependency or setting changed
