# App plugins — JARVIS's per-app control framework

This folder is how JARVIS controls a desktop app **through the app itself**
instead of moving the mouse. Each plugin is one small module that knows how to
read the app's state and drive it via its own interfaces (web API, CLI, window
messages, local socket — whatever the app offers).

The model emits:

```json
[ACTION]{"type":"app","app":"spotify","command":"play","query":"song name"}[/ACTION]
```

and `dispatch()` routes it to the registered handler.

## Adding a plugin

1. Create `actions/apps/<appname>.py` with a handler:

   ```python
   def handle(command: str, spec: dict) -> tuple[bool, str]:
       """command = lowercased verb ('play', 'pause', …); spec = the full
       action JSON. Return (ok, spoken_message). Never raise — dispatch()
       catches, but a friendly message beats a stack trace."""
   ```

2. Register it at the bottom of `actions/apps/__init__.py`:

   ```python
   from . import myapp
   register("myapp", myapp.handle)
   ```

3. Mention the new app + its commands in the `SYSTEM_PROMPT` app-control
   section (`llm/groq_bridge.py`) so the model knows it exists.

Nothing else in the codebase changes. If the app isn't registered, the `app`
action falls back to simply launching it.

## Tips

- Keep handlers **stateless** where possible; cache credentials/handles in
  module globals guarded by a fingerprint (see `spotify.py`'s `_sp_creds`).
- Secrets belong in the app-data `secrets.json` via `app_secrets`, never in
  source.
- If a command needs user consent, return a message saying so and add the
  action type to `needs_permission()` in `actions/__init__.py` — don't prompt
  from inside the handler.

## When NOT to write a plugin

- **Websites** → `actions/browser.py` (Playwright) already does it better.
- **One-off clicks in any desktop app** → the supervised computer control in
  `actions/computer.py` (UIA accessibility tree, armed-consent) covers it
  without code. A plugin is worth it when an app is used often enough that
  driving it via its own API is faster and more reliable than UI automation.
