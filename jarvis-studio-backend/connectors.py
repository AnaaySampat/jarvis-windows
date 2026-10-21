"""Connectors: one-click set-up for the services people actually connect.

An MCP server (mcp_client.py) can already add any service's tools, but setting one up
meant knowing its package, its command line and which secret goes in which variable.
A connector is that knowledge, written down once: the user picks "Notion", pastes one
token, and this builds the MCP server entry for them. Gmail is built in instead
(actions/email_box.py, IMAP with an app password): Google's own MCP route needs a Cloud
project and an OAuth consent screen, which is exactly the set-up this is meant to spare.

Every preset here was checked against its package's own documentation (2026-10-21):
the command or URL, and the variable or header the secret goes in. Each MCP connector
is an ordinary entry in mcp.json under the connector's id, so it shows in Settings ▸
MCP Servers too, and the usual rules hold: tools load when a request names the server,
and a tool that changes something asks first unless the user trusts the server.
"""

from __future__ import annotations

import os
import re
import shutil

import mcp_client
from actions import email_box


def _field(key, label, *, secret=False, placeholder="", kind="text", options=None,
           optional=False):
    f = {"key": key, "label": label, "secret": secret, "placeholder": placeholder,
         "type": kind, "optional": optional}
    if options:
        f["options"] = options
    return f


def _npx(package: str, *args: str, env: "dict | None" = None) -> dict:
    cfg = {"command": "npx", "args": ["-y", package, *args]}
    if env:
        cfg["env"] = env
    return cfg


class Invalid(Exception):
    pass


def _need(values: dict, key: str, what: str) -> str:
    v = str(values.get(key) or "").strip()
    if not v:
        raise Invalid(f"Paste {what}.")
    return v


def _url(values: dict, key: str, what: str, https_only: bool = False) -> str:
    v = _need(values, key, what).rstrip()
    if not re.match(r"^https://" if https_only else r"^https?://", v, re.IGNORECASE):
        raise Invalid(f"{what[0].upper()}{what[1:]} has to start with "
                      f"{'https://' if https_only else 'http:// or https://'}.")
    return v


def _github(v):
    return {"url": "https://api.githubcopilot.com/mcp/",
            "headers": {"Authorization": f"Bearer {_need(v, 'token', 'a GitHub token')}"}}


def _notion(v):
    return _npx("@notionhq/notion-mcp-server",
                env={"NOTION_TOKEN": _need(v, "token", "the Notion integration secret")})


def _calendar(v):
    path = _need(v, "credentials", "the path of the OAuth client file you downloaded")
    path = path.strip().strip('"')
    if not os.path.isfile(path):
        raise Invalid(f"There's no file at {path}.")
    return _npx("@cocal/google-calendar-mcp", env={"GOOGLE_OAUTH_CREDENTIALS": path})


def _todoist(v):
    return _npx("@doist/todoist-mcp", env={"TODOIST_API_KEY": _need(v, "token", "the Todoist API token")})


def _home_assistant(v):
    url = _url(v, "url", "your Home Assistant address")
    if not url.rstrip("/").endswith("/api/mcp"):
        url = url.rstrip("/") + "/api/mcp"
    return {"url": url,
            "headers": {"Authorization": f"Bearer {_need(v, 'token', 'a long-lived access token')}"}}


def _brave(v):
    return _npx("@brave/brave-search-mcp-server",
                env={"BRAVE_API_KEY": _need(v, "token", "the Brave Search API key")})


def _filesystem(v):
    raw = _need(v, "folders", "at least one folder")
    folders = [f.strip().strip('"') for f in re.split(r"[;\n]+", raw) if f.strip()]
    missing = [f for f in folders if not os.path.isdir(f)]
    if missing:
        raise Invalid(f"There's no folder at {missing[0]}.")
    return _npx("@modelcontextprotocol/server-filesystem", *folders)


def _zapier(v):
    return {"url": _url(v, "url", "your Zapier MCP server URL", https_only=True)}


_GMAIL_STEPS = [
    "Turn on 2-Step Verification for your Google account, if it isn't already.",
    "Open myaccount.google.com/apppasswords, make an app password called “JARVIS”, "
    "and copy the 16 letters it shows.",
    "Paste them below with your Gmail address. JARVIS can then check, search and read "
    "your mail, and write drafts — it never sends anything; you send drafts yourself.",
]

CATALOG = [
    {"id": "gmail", "name": "Gmail", "icon": "message", "kind": "email", "provider": "gmail",
     "blurb": "Check, search and read your inbox; JARVIS writes drafts, you send them.",
     "link": "https://myaccount.google.com/apppasswords", "steps": _GMAIL_STEPS,
     "fields": [_field("address", "Gmail address", placeholder="you@gmail.com", kind="email"),
                _field("password", "App password", secret=True, placeholder="abcd efgh ijkl mnop")]},
    {"id": "email", "name": "Other email", "icon": "message", "kind": "email",
     "blurb": "Yahoo, iCloud or any IMAP mailbox, the same way as Gmail.",
     "link": "", "steps": [
         "Make an app password: Yahoo (Account security ▸ Generate app password) or iCloud "
         "(account.apple.com ▸ Sign-In and Security ▸ App-Specific Passwords). Another "
         "provider: its IMAP server name and a password it allows for mail apps.",
         "JARVIS reads and writes drafts only; it never sends."],
     "fields": [_field("provider", "Provider", kind="select",
                       options=[["yahoo", "Yahoo"], ["icloud", "iCloud"], ["custom", "Other (IMAP)"]]),
                _field("address", "Email address", placeholder="you@example.com", kind="email"),
                _field("password", "App password", secret=True),
                _field("host", "IMAP server", placeholder="imap.example.com", optional=True)]},
    {"id": "github", "name": "GitHub", "icon": "terminal", "kind": "mcp", "build": _github,
     "blurb": "Issues, pull requests, repositories and code search.",
     "link": "https://github.com/settings/personal-access-tokens", "steps": [
         "On GitHub: Settings ▸ Developer settings ▸ Personal access tokens ▸ Fine-grained "
         "tokens ▸ Generate new token. Pick the repositories and what it may do.",
         "Paste the token below. This uses GitHub's own hosted MCP server."],
     "fields": [_field("token", "Personal access token", secret=True, placeholder="github_pat_…")]},
    {"id": "notion", "name": "Notion", "icon": "note", "kind": "mcp", "build": _notion, "needs": "node",
     "blurb": "Search, read and add to your Notion pages and databases.",
     "link": "https://www.notion.so/profile/integrations", "steps": [
         "Open notion.so/profile/integrations ▸ New integration (internal), and copy its secret.",
         "In Notion, open each page JARVIS may use ▸ ••• ▸ Connections ▸ add the integration.",
         "Paste the secret below."],
     "fields": [_field("token", "Integration secret", secret=True, placeholder="ntn_…")]},
    {"id": "google-calendar", "name": "Google Calendar", "icon": "calendar", "kind": "mcp",
     "build": _calendar, "needs": "node",
     "blurb": "Your Google calendars: see, add and move events.",
     "link": "https://console.cloud.google.com/apis/credentials", "steps": [
         "Google Calendar needs its own sign-in: in Google Cloud Console make a project, "
         "turn on the Google Calendar API, and create an OAuth client of type “Desktop app”.",
         "On the Audience page add your own address as a test user, then download the "
         "client's JSON file.",
         "Give its path below. After connecting, ask “sign in to Google Calendar” once: "
         "a browser opens for you to allow it (again every 7 days while the app is in "
         "test mode)."],
     "fields": [_field("credentials", "OAuth client file",
                       placeholder="C:\\Users\\you\\Downloads\\gcp-oauth.keys.json")]},
    {"id": "todoist", "name": "Todoist", "icon": "check", "kind": "mcp", "build": _todoist,
     "needs": "node", "blurb": "Your to-do lists: add, find and complete tasks.",
     "link": "https://app.todoist.com/app/settings/integrations/developer", "steps": [
         "In Todoist: Settings ▸ Integrations ▸ Developer, copy the API token.",
         "Paste it below."],
     "fields": [_field("token", "API token", secret=True)]},
    {"id": "home-assistant", "name": "Home Assistant", "icon": "wifi", "kind": "mcp",
     "build": _home_assistant,
     "blurb": "Lights, switches and everything else in your smart home.",
     "link": "", "steps": [
         "In Home Assistant: Settings ▸ Devices & services ▸ Add integration ▸ “Model "
         "Context Protocol Server”, and choose what it exposes.",
         "Your profile ▸ Security ▸ Long-lived access tokens ▸ Create token.",
         "Give Home Assistant's address (e.g. http://homeassistant.local:8123) and the token."],
     "fields": [_field("url", "Home Assistant address", placeholder="http://homeassistant.local:8123"),
                _field("token", "Long-lived access token", secret=True)]},
    {"id": "brave-search", "name": "Brave Search", "icon": "search", "kind": "mcp",
     "build": _brave, "needs": "node",
     "blurb": "Web, news, image and local search through Brave's API.",
     "link": "https://api-dashboard.search.brave.com/app/keys", "steps": [
         "Make a free Brave Search API key at api-dashboard.search.brave.com.",
         "Paste it below."],
     "fields": [_field("token", "API key", secret=True)]},
    {"id": "filesystem", "name": "Folders", "icon": "folder", "kind": "mcp",
     "build": _filesystem, "needs": "node",
     "blurb": "Read, search and write files in the folders you choose (asks before writing).",
     "link": "", "steps": ["List the folders JARVIS may use, separated by ;."],
     "fields": [_field("folders", "Folders", placeholder="C:\\Users\\you\\Documents; D:\\Projects")]},
    {"id": "zapier", "name": "Zapier", "icon": "plug", "kind": "mcp", "build": _zapier,
     "blurb": "Thousands of apps — Slack, Sheets, Outlook, Trello… — through one Zapier link.",
     "link": "https://mcp.zapier.com", "steps": [
         "At mcp.zapier.com make a server, add the apps and actions JARVIS may use, and "
         "copy its server URL. Treat that URL like a password.",
         "Paste it below."],
     "fields": [_field("url", "Server URL", secret=True, placeholder="https://mcp.zapier.com/api/mcp/…")]},
]
_BY_ID = {c["id"]: c for c in CATALOG}
_last: "dict[str, dict]" = {}        # id → {ok, summary} of the last connect attempt


def _email_state(c: dict) -> dict:
    s = email_box.settings()
    mine = email_box.configured() and (s["provider"] == "gmail") == (c["id"] == "gmail")
    return {"status": "connected" if mine else "off", "detail": s["address"] if mine else ""}


def _mcp_state(c: dict, servers: dict) -> dict:
    srv = servers.get(c["id"])
    if srv is None:
        return {"status": "off", "detail": ""}
    status = {"ready": "connected", "starting": "connecting", "failed": "failed",
              "disabled": "off", "stopped": "off"}.get(srv["status"], srv["status"])
    n = len(srv.get("tools") or [])
    return {"status": status, "error": srv.get("error", ""),
            "detail": f"{n} tool{'s' if n != 1 else ''}" if status == "connected" else ""}


def state() -> dict:
    """For Settings ▸ Connectors: the catalog, each one's status, nothing secret."""
    servers = {s["name"]: s for s in mcp_client.state()["servers"]}
    node = shutil.which("npx") is not None
    out = []
    for c in CATALOG:
        row = {k: c[k] for k in ("id", "name", "icon", "kind", "blurb", "link", "steps", "fields")}
        row.update(_email_state(c) if c["kind"] == "email" else _mcp_state(c, servers))
        if c.get("needs") == "node" and not node:
            row["needs"] = "Needs Node.js (nodejs.org) on this PC, then restart JARVIS."
        if c["id"] in _last:
            row["result"] = _last[c["id"]]
        out.append(row)
    return {"connectors": out, "mcp_available": mcp_client.available()}


def mcp_config(cid: str, values: dict) -> dict:
    """The mcp.json entry a connector's form builds (raises Invalid with what's wrong)."""
    c = _BY_ID.get(cid)
    if c is None or c["kind"] != "mcp":
        raise Invalid("I don't know that connector.")
    cfg, why = mcp_client.clean_server(cid, c["build"](values or {}))
    if cfg is None:
        raise Invalid(why)
    return cfg


def connect_email(cid: str, values: dict) -> "tuple[bool, str]":
    """Sign in once, then save (blocking: run off the event loop)."""
    v = values or {}
    provider = "gmail" if cid == "gmail" else str(v.get("provider") or "custom")
    address = str(v.get("address") or "").strip()
    password = str(v.get("password") or "")
    host = str(v.get("host") or "").strip()
    ok, msg, server = email_box.test(provider, address, password, host)
    if ok:
        email_box.save(provider, address, password, server[0], server[1])
    _last[cid] = {"ok": ok, "summary": msg}
    return ok, msg


async def connect(cid: str, values: dict, run_blocking) -> "tuple[bool, str]":
    """Connect one connector. `run_blocking(fn, *args)` runs a blocking call off the
    loop (the email sign-in is network I/O)."""
    c = _BY_ID.get(cid)
    if c is None:
        return False, "I don't know that connector."
    if c["kind"] == "email":
        return await run_blocking(connect_email, cid, values)
    try:
        cfg = mcp_config(cid, values)
    except Invalid as exc:
        _last[cid] = {"ok": False, "summary": str(exc)}
        return False, str(exc)
    servers = mcp_client.load_config()
    if (servers.get(cid) or {}).get("trusted"):
        cfg["trusted"] = True          # a new token doesn't take back the user's trust
    servers[cid] = cfg
    mcp_client.save_config(servers)
    await mcp_client.restart(cid)
    msg = f"{c['name']} added — connecting now. Ask about it by name, e.g. “on {c['name']}, …”."
    _last[cid] = {"ok": True, "summary": msg}
    return True, msg


async def disconnect(cid: str) -> "tuple[bool, str]":
    c = _BY_ID.get(cid)
    if c is None:
        return False, "I don't know that connector."
    _last.pop(cid, None)
    if c["kind"] == "email":
        email_box.forget()
        return True, f"{c['name']} disconnected; the app password is deleted from this PC."
    servers = mcp_client.load_config()
    if servers.pop(cid, None) is not None:
        mcp_client.save_config(servers)
    await mcp_client.restart(cid)
    return True, f"{c['name']} disconnected."
