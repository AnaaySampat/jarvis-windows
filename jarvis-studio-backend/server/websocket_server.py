import asyncio
import contextvars
import hmac
import ipaddress
import json
import logging
import os
import re
import ssl
import subprocess
import threading
import time
import uuid
from urllib.parse import parse_qs, urlparse
from typing import Awaitable, Callable

import websockets
from websockets.server import WebSocketServerProtocol

from autonomy.device_identity import (
    AuthenticatedDevice,
    AuthenticationError,
    DeviceIdentityRegistry,
    PairingError,
)

# Suppress spurious "opening handshake failed" tracebacks that fire when
# Vite HMR or browsers briefly probe the port with non-WebSocket requests.
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)

_clients: set[WebSocketServerProtocol] = set()
_client_ids: dict[WebSocketServerProtocol, str] = {}
_task_subscribers: dict[str, set[WebSocketServerProtocol]] = {}
_client_tasks: dict[WebSocketServerProtocol, set[str]] = {}
_task_sequences: dict[str, int] = {}
_incoming_handlers: dict[str, Callable[..., Awaitable]] = {}
_on_connect: "Callable[..., Awaitable] | None" = None
_on_disconnect: "Callable[..., Awaitable] | None" = None

# Remote sockets pass a cryptographic application handshake before they enter
# ``_clients`` or any application callback runs.  The maps are transport-local;
# the registry persists identities, scopes, revocation and replay counters.
_identity_registry: "DeviceIdentityRegistry | None" = None
_authenticated_devices: dict[WebSocketServerProtocol, AuthenticatedDevice] = {}
_connection_challenges: dict[WebSocketServerProtocol, dict] = {}
_host_out_counters: dict[WebSocketServerProtocol, int] = {}


def configure_device_identity(registry: DeviceIdentityRegistry) -> None:
    """Install the durable identity authority used for all remote sockets."""
    global _identity_registry
    _identity_registry = registry


def current_authenticated_device() -> str:
    """Cryptographically authenticated device ID for the current remote sender."""
    client = current_sender()
    if client is None:
        return ""
    device = _authenticated_devices.get(client)
    return device.device_id if device else ""


def current_authenticated_scopes() -> tuple[str, ...]:
    client = current_sender()
    if client is None:
        return ()
    device = _authenticated_devices.get(client)
    return device.scopes if device else ()


def connected_device_ids() -> "set[str]":
    """Device ids with a live authenticated socket right now (HUD roster UI)."""
    return {device.device_id for device in _authenticated_devices.values()}


async def disconnect_authenticated_device(device_id: str) -> int:
    """Immediately terminate every socket bound to a newly revoked device."""
    targets = [
        client for client, device in tuple(_authenticated_devices.items())
        if device.device_id == device_id
    ]
    if targets:
        await asyncio.gather(
            *[client.close(code=1008, reason="device revoked") for client in targets],
            return_exceptions=True,
        )
    return len(targets)

# SECURITY (audit reverify): message types a NON-loopback (remote / paired-phone /
# any token-holding) client is allowed to invoke. DEFAULT-DENY — a remote message
# whose type isn't in here is dropped before its handler runs. Populated by main.py
# via set_remote_allowed() with exactly the vocabulary the phone legitimately sends
# (task forwarding + mid-task answers + the remote-desktop screen/drive channel);
# every other handler (set_config → allowed_dirs/keys/provider, conversation wipes,
# direct run_action, setup re-runs, …) is local-GUI-only. Empty set until main.py
# registers it, which (fail-safe) means "trust no remote type yet".
_remote_allowed: "set[str]" = set()


def set_remote_allowed(types) -> None:
    """Declare which message types a remote client may invoke (see _remote_allowed)."""
    global _remote_allowed
    _remote_allowed = set(types)

# Which connection sent the message a handler is CURRENTLY processing — a
# ContextVar rather than a plain global because each incoming message is
# dispatched as its own asyncio Task (see _handler below); asyncio.create_task
# copies the current contextvars.Context at creation time, so this stays
# correctly scoped per in-flight handler even with several messages/clients
# in flight at once. Sole purpose: let a handler (e.g. the one that kicks off
# an autopilot task) tell "my own local GUI asked for this" apart from "a
# paired phone asked for this" — previously indistinguishable, since the
# phone's pc_task and the desktop's own chat box send the exact same
# {"type":"text_input",...} message with no source tag at all.
_current_is_remote: "contextvars.ContextVar[bool]" = contextvars.ContextVar(
    "current_is_remote", default=False)

# The concrete connection currently being handled.  Keeping this in ContextVars
# lets main.py subscribe the requesting connection to a durable task without
# passing websocket objects through every application-layer handler.
_current_client: "contextvars.ContextVar[WebSocketServerProtocol | None]" = (
    contextvars.ContextVar("current_client", default=None)
)
_current_client_id: "contextvars.ContextVar[str]" = contextvars.ContextVar(
    "current_client_id", default=""
)


def is_current_sender_remote() -> bool:
    """True while handling a message that arrived over a NON-loopback
    connection (a paired phone over LAN/tunnel) rather than the local GUI's
    own websocket. False outside of any handler."""
    return _current_is_remote.get()


def current_sender() -> "WebSocketServerProtocol | None":
    """Return the websocket whose handler is currently running, if any."""
    return _current_client.get()


def current_client_id() -> str:
    """Return this connection's opaque ID while inside a message handler."""
    return _current_client_id.get()


_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _valid_task_id(task_id) -> bool:
    return isinstance(task_id, str) and bool(_TASK_ID_RE.fullmatch(task_id))


def subscribe_task(
    task_id: str,
    client: "WebSocketServerProtocol | None" = None,
) -> bool:
    """Subscribe one authenticated connection to task-scoped events.

    With no explicit ``client`` this uses :func:`current_sender`, which is the
    normal form from a ``task.submit`` / ``task.subscribe`` handler.  The mapping
    is transport-local; the durable runtime remains responsible for authorization
    and replaying events after ``resume_after_seq``.
    """
    if not _valid_task_id(task_id):
        return False
    client = client or current_sender()
    if client is None or client not in _clients:
        return False
    _task_subscribers.setdefault(task_id, set()).add(client)
    _client_tasks.setdefault(client, set()).add(task_id)
    return True


def unsubscribe_task(
    task_id: str,
    client: "WebSocketServerProtocol | None" = None,
) -> bool:
    """Remove a connection's task subscription; returns whether one existed."""
    if not _valid_task_id(task_id):
        return False
    client = client or current_sender()
    if client is None:
        return False
    subscribers = _task_subscribers.get(task_id)
    existed = bool(subscribers and client in subscribers)
    if subscribers:
        subscribers.discard(client)
        if not subscribers:
            _task_subscribers.pop(task_id, None)
    tasks = _client_tasks.get(client)
    if tasks:
        tasks.discard(task_id)
        if not tasks:
            _client_tasks.pop(client, None)
    return existed


def subscribe_local_clients(task_id: str) -> int:
    """Subscribe every authenticated loopback HUD to a task.

    This lets the desktop Task Center observe phone-submitted work without
    broadcasting it to other remote phones. Returns the number subscribed.
    """
    if not _valid_task_id(task_id):
        return 0
    count = 0
    for client in tuple(_clients):
        if _is_loopback(client) and subscribe_task(task_id, client):
            count += 1
    return count


def _unsubscribe_all(client: WebSocketServerProtocol) -> None:
    for task_id in tuple(_client_tasks.pop(client, set())):
        subscribers = _task_subscribers.get(task_id)
        if subscribers:
            subscribers.discard(client)
            if not subscribers:
                _task_subscribers.pop(task_id, None)

_MAX_MESSAGE_BYTES = 12 * 1024 * 1024
_REQUIRED_TOKEN = os.environ.get("JARVIS_WS_TOKEN", "").strip()
_ALLOW_AUTHENTICATED_LAN_PLAINTEXT = (
    os.environ.get("AURA_ALLOW_AUTHENTICATED_LAN_PLAINTEXT", "").strip() == "1"
)
_ALLOW_LEGACY_REMOTE_BEARER = (
    os.environ.get("AURA_ALLOW_LEGACY_REMOTE_BEARER", "").strip() == "1"
)
# Bind address. "localhost" (default) = this PC only. Packaged/dev hosts may bind
# all interfaces for signed devices, but the transport gate still requires WSS,
# Tailscale, or an explicit development-only plaintext override.
_BIND_HOST = os.environ.get("JARVIS_WS_BIND", "localhost").strip() or "localhost"
_ALLOWED_ORIGINS = {
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "tauri://localhost",
    "http://tauri.localhost",
    "https://tauri.localhost",
}


def _serve_origins():
    # start.py supplies a per-session token for the normal app launch path. When
    # the backend is started directly for development there may be no token, so
    # do not allow originless websocket clients in that mode.
    return [None, *_ALLOWED_ORIGINS] if _REQUIRED_TOKEN else list(_ALLOWED_ORIGINS)


def register_handler(event_type: str, handler: Callable[..., Awaitable]) -> None:
    _incoming_handlers[event_type] = handler


def on_connect(handler: Callable[..., Awaitable]) -> None:
    """Register a coroutine run each time a new client connects (e.g. to push
    sysinfo/config so the Settings panel is populated immediately)."""
    global _on_connect
    _on_connect = handler


def on_disconnect(handler: Callable[..., Awaitable]) -> None:
    """Register cleanup invoked while the closing client's sender context is set."""
    global _on_disconnect
    _on_disconnect = handler


def _is_loopback(websocket: WebSocketServerProtocol) -> bool:
    try:
        peer = (websocket.remote_address or ("",))[0]
    except Exception:  # noqa: BLE001
        peer = ""
    return peer in ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _peer_host(websocket: WebSocketServerProtocol) -> str:
    try:
        return str((websocket.remote_address or ("",))[0] or "")
    except Exception:  # noqa: BLE001
        return ""


_TAILSCALE_IPV4_NETWORK = ipaddress.ip_network("100.64.0.0/10")
_TAILSCALE_IPV6_NETWORK = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_TAILSCALE_WHOIS_TIMEOUT_S = 1.25
_TAILSCALE_POSITIVE_CACHE_TTL_S = 60.0
_TAILSCALE_NEGATIVE_CACHE_TTL_S = 5.0
_TAILSCALE_WHOIS_MAX_OUTPUT_BYTES = 256 * 1024
_TAILSCALE_PEER_CACHE_MAX = 256
_tailscale_peer_cache: "dict[str, tuple[float, bool]]" = {}
_tailscale_peer_cache_lock = threading.Lock()


def _tailscale_range_address(peer: str):
    """Return the canonical Tailscale-range address, or ``None``.

    A range match is only a cheap prefilter. CGNAT space can be used outside
    Tailscale and must never be treated as proof of an authenticated route.
    """
    try:
        address = ipaddress.ip_address(peer)
    except (TypeError, ValueError):
        return None
    network = (
        _TAILSCALE_IPV4_NETWORK
        if address.version == 4
        else _TAILSCALE_IPV6_NETWORK
    )
    return address if address in network else None


def _tailscale_address_matches(value, expected) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        # ``tailscale whois --json`` normally returns CIDR addresses, while a
        # bare address is also accepted to remain compatible across CLI versions.
        observed = (
            ipaddress.ip_interface(value.strip()).ip
            if "/" in value
            else ipaddress.ip_address(value.strip())
        )
    except ValueError:
        return False
    return observed == expected


def _valid_tailscale_whois(payload, expected_address) -> bool:
    """Validate the minimal identity evidence returned by the local daemon."""
    if not isinstance(payload, dict):
        return False
    node = payload.get("Node")
    if not isinstance(node, dict):
        return False

    # A display name is mutable and therefore insufficient. Require a durable
    # node identifier plus an address binding to the exact TCP peer.
    node_id = node.get("StableID") or node.get("ID")
    if node_id is None or not str(node_id).strip():
        return False
    addresses = node.get("Addresses")
    if not isinstance(addresses, list) or not any(
        _tailscale_address_matches(value, expected_address) for value in addresses
    ):
        return False

    # Normal devices carry a user profile. Tagged devices may intentionally have
    # no user profile, so a non-empty daemon-issued tag is an acceptable principal.
    profile = payload.get("UserProfile")
    has_user = isinstance(profile, dict) and any(
        value is not None and bool(str(value).strip())
        for value in (profile.get("ID"), profile.get("LoginName"))
    )
    tags = node.get("Tags")
    has_tag = isinstance(tags, list) and any(
        isinstance(tag, str) and bool(tag.strip()) for tag in tags
    )
    return has_user or has_tag


def _run_tailscale_whois(peer: str, expected_address) -> bool:
    kwargs = {
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": _TAILSCALE_WHOIS_TIMEOUT_S,
        "check": False,
    }
    if os.name == "nt":
        # The host agent runs without opening a console window on every new peer.
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            ["tailscale", "whois", "--json", peer],
            **kwargs,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return False
    if len(result.stdout.encode("utf-8")) > _TAILSCALE_WHOIS_MAX_OUTPUT_BYTES:
        return False
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        return False
    return _valid_tailscale_whois(payload, expected_address)


def _clear_tailscale_peer_cache() -> None:
    """Clear transport evidence (kept public-to-module for isolated tests)."""
    with _tailscale_peer_cache_lock:
        _tailscale_peer_cache.clear()


def _is_tailscale_peer(peer: str) -> bool:
    """Verify a Tailscale peer using the local daemon, with a short TTL cache.

    The IP-range prefilter is never sufficient by itself. Any missing CLI,
    timeout, malformed output, identity omission, or address mismatch fails closed.
    Device-level signed authentication is still required after this transport gate.
    """
    address = _tailscale_range_address(peer)
    if address is None:
        return False
    canonical_peer = str(address)
    now = time.monotonic()
    with _tailscale_peer_cache_lock:
        cached = _tailscale_peer_cache.get(canonical_peer)
        if cached is not None:
            expires_at, allowed = cached
            if expires_at > now:
                return allowed
            _tailscale_peer_cache.pop(canonical_peer, None)

    allowed = _run_tailscale_whois(canonical_peer, address)
    ttl = (
        _TAILSCALE_POSITIVE_CACHE_TTL_S
        if allowed
        else _TAILSCALE_NEGATIVE_CACHE_TTL_S
    )
    with _tailscale_peer_cache_lock:
        # Keep attacker-controlled source addresses from creating an unbounded map.
        expired = [
            key for key, (expires_at, _value) in _tailscale_peer_cache.items()
            if expires_at <= now
        ]
        for key in expired:
            _tailscale_peer_cache.pop(key, None)
        if len(_tailscale_peer_cache) >= _TAILSCALE_PEER_CACHE_MAX:
            oldest = min(
                _tailscale_peer_cache,
                key=lambda key: _tailscale_peer_cache[key][0],
            )
            _tailscale_peer_cache.pop(oldest, None)
        _tailscale_peer_cache[canonical_peer] = (now + ttl, allowed)
    return allowed


def _has_tls(websocket: WebSocketServerProtocol) -> bool:
    try:
        return websocket.transport.get_extra_info("ssl_object") is not None
    except Exception:  # noqa: BLE001
        return False


def _remote_transport_allowed(websocket: WebSocketServerProtocol) -> bool:
    # WSS and Tailscale are production routes. Plain LAN is intentionally OFF by
    # default; the explicit environment switch exists only for authenticated-LAN
    # development while certificate pinning is provisioned.
    peer = _peer_host(websocket)
    return (
        _has_tls(websocket)
        or _is_tailscale_peer(peer)
        or _ALLOW_AUTHENTICATED_LAN_PLAINTEXT
    )


def _scope_for_message(message_type: str) -> str | None:
    # Only the 'tasks' scope is enforced here, and it covers the task protocol.
    # Live remote desktop (webrtc_* / arm_control / remote_input) is back and is
    # gated by the remote ALLOWLIST plus its own per-session arm/lease checks in
    # main.py rather than by a device scope — a paired phone that is not armed
    # still cannot send input.
    if (message_type.startswith("task.") or
            message_type in {"protocol.hello", "approval.response"}):
        return "tasks"
    return None


def _envelope_content(message: dict) -> dict:
    """All wire fields not already represented explicitly in signing material."""
    return {
        key: value
        for key, value in message.items()
        if key not in {"auth", "type", "event", "task_id"}
    }


# ── Connection rate limiting (audit M4) ──────────────────────────────────────
# The 256-bit token makes online brute force impractical, so this is purely a DoS
# blunt: cap how fast one REMOTE peer IP can open connections. Loopback (the local
# GUI, which reconnects freely during HMR/dev) is exempt.
# ponytail: naive per-IP timestamp list, pruned lazily — a personal backend never
# sees enough distinct IPs to need eviction. Widen _RATE_MAX if a legit reconnect
# storm (network flap) ever trips it.
_RATE_WINDOW_S = 10.0
_RATE_MAX = 20
# S3: with the socket on 0.0.0.0, a same-LAN attacker can self-assign many
# 100.64/10 source IPs and get a fresh per-IP budget for each, forcing a burst of
# `tailscale whois` subprocess spawns. An AGGREGATE cap across all remote peers
# blunts that — a single phone reconnecting never approaches it.
_RATE_AGG_MAX = 60
_recent_conns: "dict[str, list[float]]" = {}
_recent_conns_all: "list[float]" = []
_legacy_remote_clients: set[WebSocketServerProtocol] = set()


def _rate_limited(peer: str) -> bool:
    now = time.monotonic()
    # Aggregate window first (counts every remote attempt, even spoofed-IP ones).
    global _recent_conns_all
    _recent_conns_all = [t for t in _recent_conns_all if now - t < _RATE_WINDOW_S]
    _recent_conns_all.append(now)
    if len(_recent_conns_all) > _RATE_AGG_MAX:
        return True
    if not peer:
        return False
    hits = [t for t in _recent_conns.get(peer, []) if now - t < _RATE_WINDOW_S]
    hits.append(now)
    _recent_conns[peer] = hits
    return len(hits) > _RATE_MAX


def _request_parts(websocket: WebSocketServerProtocol):
    request = getattr(websocket, "request", None)
    if request is not None:
        return request.headers, request.path
    return websocket.request_headers, getattr(websocket, "path", "")


def _offered_bearer(websocket: WebSocketServerProtocol) -> str:
    headers, path = _request_parts(websocket)
    offered_protocols = headers.get("Sec-WebSocket-Protocol", "")
    for offered in offered_protocols.split(","):
        offered = offered.strip()
        if offered.startswith("aura-token."):
            return offered[len("aura-token."):]
    return parse_qs(urlparse(path).query).get("token", [""])[0]


def _legacy_remote_bearer_valid(websocket: WebSocketServerProtocol) -> bool:
    return bool(
        _ALLOW_LEGACY_REMOTE_BEARER
        and _REQUIRED_TOKEN
        and hmac.compare_digest(_offered_bearer(websocket), _REQUIRED_TOKEN)
    )


# Handshake outcome sentinel: the peer never finished in time (slow phone
# Keystore/native bridge). NOT an identity verdict — the phone treats close code
# 1008 after auth.challenge as terminal "Identity rejected" and stops retrying,
# so a timeout must close with a different (transient) code instead.
_HANDSHAKE_TIMEOUT = object()

# The phone's identity bridge can legitimately burn ~12.6s of native-keystore
# retries per call, and one handshake needs two calls (verify host + sign auth)
# before its reply, so a 20s window falsely rejected healthy slow phones.
_HANDSHAKE_REPLY_TIMEOUT_S = 45.0


async def _authenticate_remote(
    websocket: WebSocketServerProtocol, client_id: str
) -> "AuthenticatedDevice | None | object":
    """Run the signed remote handshake before exposing a socket to Aura."""
    if _legacy_remote_bearer_valid(websocket):
        # Explicit development-only compatibility. The untrusted claimed
        # ``device_id`` is still ignored; one deterministic identity is derived
        # from the configured dev bearer instead.
        legacy_id = "legacy-dev-" + hmac.new(
            _REQUIRED_TOKEN.encode("utf-8"), b"aura-legacy-device",
            "sha256",
        ).hexdigest()[:24]
        _legacy_remote_clients.add(websocket)
        return AuthenticatedDevice(
            legacy_id, "Legacy development client", "legacy-dev",
            ("tasks", "screen", "control"), 0,
        )
    registry = _identity_registry
    peer = _peer_host(websocket)
    if registry is None:
        print(f"[Auth] Rejected {peer or 'unknown'}: identity registry unavailable.", flush=True)
        return None
    challenge = registry.make_connection_challenge(client_id)
    _connection_challenges[websocket] = challenge
    await websocket.send(json.dumps({"event": "auth.challenge", "data": challenge}))
    for _attempt in range(3):
        try:
            raw = await asyncio.wait_for(
                websocket.recv(), timeout=_HANDSHAKE_REPLY_TIMEOUT_S
            )
            msg = json.loads(raw)
        except asyncio.TimeoutError:
            print(f"[Auth] {peer or 'unknown'}: handshake reply timed out — "
                  "transient, phone may retry.", flush=True)
            return _HANDSHAKE_TIMEOUT
        except (json.JSONDecodeError, TypeError) as exc:
            print(f"[Auth] Rejected {peer or 'unknown'}: no valid handshake reply "
                  f"({type(exc).__name__}).", flush=True)
            return None
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            print(f"[Auth] Rejected {peer or 'unknown'}: malformed handshake message.", flush=True)
            return None
        data = msg.get("data")
        if not isinstance(data, dict):
            print(f"[Auth] Rejected {peer or 'unknown'}: handshake message has no data.", flush=True)
            return None
        if time.time() > float(challenge.get("expires_at") or 0):
            # Same slow-phone shape as the recv timeout: nothing was judged, so
            # nothing was "rejected" — let the phone retry with a fresh challenge.
            print(f"[Auth] {peer or 'unknown'}: connection challenge expired "
                  "before a reply arrived — transient, phone may retry.", flush=True)
            return _HANDSHAKE_TIMEOUT
        try:
            if msg["type"] == "auth.response":
                if str(data.get("nonce") or "") != challenge["nonce"]:
                    raise AuthenticationError("authentication nonce mismatch")
                device = registry.authenticate(
                    str(data.get("device_id") or ""),
                    challenge["nonce"],
                    int(data.get("counter") or 0),
                    str(data.get("signature") or ""),
                )
                print(f"[Auth] {peer or 'unknown'} authenticated as "
                      f"{device.device_id} ({device.name}).", flush=True)
                return device
            if msg["type"] == "pairing.claim":
                device = registry.claim_pairing(
                    data, connection_nonce=challenge["nonce"]
                )
                print(f"[Auth] {peer or 'unknown'} PAIRED as "
                      f"{device.device_id} ({device.name}).", flush=True)
                return device
        except PairingError as exc:
            print(f"[Auth] Rejected {peer or 'unknown'} pairing.claim: {exc}", flush=True)
            await websocket.send(json.dumps({
                "event": "auth.denied",
                "data": {"reason": "pairing claim was invalid or expired"},
            }))
            return None
        except AuthenticationError as exc:
            # An unknown but otherwise well-formed key may pair only with the
            # public, one-use QR challenge. No bearer can elevate itself here.
            print(f"[Auth] {peer or 'unknown'} auth.response failed ({exc}); "
                  "asking it to pair instead.", flush=True)
            await websocket.send(json.dumps({
                "event": "auth.pairing_required",
                "data": {
                    "host_id": challenge["host_id"],
                    "nonce": challenge["nonce"],
                },
            }))
            continue
        print(f"[Auth] Rejected {peer or 'unknown'}: unexpected handshake message "
              f"type {msg['type']!r}.", flush=True)
        return None
    print(f"[Auth] Rejected {peer or 'unknown'}: exhausted pairing retries.", flush=True)
    return None


def _verify_remote_message(
    websocket: WebSocketServerProtocol, msg: dict
) -> AuthenticatedDevice:
    device = _authenticated_devices.get(websocket)
    challenge = _connection_challenges.get(websocket)
    registry = _identity_registry
    if device is None or challenge is None or registry is None:
        raise AuthenticationError("remote connection has no authenticated identity")
    auth = msg.get("auth")
    if not isinstance(auth, dict):
        raise AuthenticationError("signed envelope is required")
    if str(auth.get("device_id") or "") != device.device_id:
        raise AuthenticationError("envelope device does not match connection")
    if str(auth.get("connection_nonce") or "") != challenge["nonce"]:
        raise AuthenticationError("envelope nonce does not match connection")
    data = msg.get("data")
    mtype = str(msg.get("type") or "")
    task_id = ""
    if isinstance(data, dict):
        task_id = str(data.get("task_id") or "")
    task_id = str(msg.get("task_id") or task_id)
    return registry.verify_envelope(
        device_id=device.device_id,
        connection_nonce=challenge["nonce"],
        counter=auth.get("counter"),
        message_type=mtype,
        task_id=task_id,
        data=_envelope_content(msg),
        signature=str(auth.get("signature") or ""),
        required_scope=_scope_for_message(mtype),
    )


async def _handler(websocket: WebSocketServerProtocol):
    is_remote = not _is_loopback(websocket)
    if is_remote:
        try:
            peer = (websocket.remote_address or ("",))[0]
        except Exception:  # noqa: BLE001
            peer = ""
        if _rate_limited(peer):
            print(f"[WebSocket] Rate-limited {peer or 'unknown'} — too many "
                  "connections; backing off.", flush=True)
            await websocket.close(code=1013, reason="rate limited")
            return
    if not _authorized(websocket):
        await websocket.close(code=1008, reason="unauthorized")
        return
    client_id = uuid.uuid4().hex
    if is_remote:
        device = await _authenticate_remote(websocket, client_id)
        if device is None or device is _HANDSHAKE_TIMEOUT:
            _connection_challenges.pop(websocket, None)
            _legacy_remote_clients.discard(websocket)
            if device is _HANDSHAKE_TIMEOUT:
                # Private transient code: the phone only treats 1008-after-
                # challenge as a terminal identity verdict; 4008 lands in its
                # normal offline+auto-reconnect path.
                await websocket.close(code=4008, reason="handshake timeout")
            else:
                await websocket.close(code=1008, reason="device authentication failed")
            return
        # A device only reaches here after PIN-verified pairing (or a signed
        # auth.response from an already-paired device), so it is trusted — there is
        # no separate PC-side approval step (S1's PIN is the human-present gate).
        _authenticated_devices[websocket] = device
        _host_out_counters[websocket] = 0
        await _send_payload(websocket, {
            "event": "auth.ready",
            "data": {
                "device_id": device.device_id,
                "scopes": list(device.scopes),
                "host_fingerprint": (
                    _identity_registry.host_fingerprint if _identity_registry else ""
                ),
            },
        })
    _clients.add(websocket)
    _client_ids[websocket] = client_id
    keepalive = (asyncio.create_task(_link_keepalive(websocket))
                 if is_remote and websocket not in _legacy_remote_clients else None)
    if _on_connect is not None:
        remote_token = _current_is_remote.set(is_remote)
        client_token = _current_client.set(websocket)
        id_token = _current_client_id.set(client_id)
        try:
            await _on_connect(websocket)
        except Exception as exc:  # noqa: BLE001
            print(f"[WebSocket] on_connect handler failed: {exc}")
        finally:
            _current_client_id.reset(id_token)
            _current_client.reset(client_token)
            _current_is_remote.reset(remote_token)
    try:
        async for raw in websocket:
            try:
                msg = json.loads(raw)
                if not isinstance(msg, dict):
                    continue
                mtype = msg.get("type")
                if not isinstance(mtype, str):
                    continue
                if is_remote and websocket not in _legacy_remote_clients:
                    try:
                        verified = _verify_remote_message(websocket, msg)
                        _authenticated_devices[websocket] = verified
                    except AuthenticationError as exc:
                        print(f"[Auth] Rejected signed envelope "
                              f"(type={mtype!r}): {exc}", flush=True)
                        await websocket.close(code=1008, reason="invalid signed envelope")
                        return
                fn = _incoming_handlers.get(mtype)
                if fn:
                    # Default-deny for remote clients: only the declared vocabulary
                    # is honoured; anything else (config/keys/sandbox changes, data
                    # wipes, …) is local-GUI-only and silently dropped here.
                    if is_remote and mtype not in _remote_allowed:
                        print(f"[WebSocket] Ignored '{mtype}' from a remote client "
                              "— that action is local-only.", flush=True)
                        continue

                    data = msg.get("data")
                    task_id = data.get("task_id") if isinstance(data, dict) else None
                    # Unsubscription is always safe to apply at the transport
                    # boundary. Subscription is intentionally left to main.py
                    # *after* it verifies task ownership; pre-subscribing an
                    # arbitrary task ID would create a cross-client disclosure race.
                    if mtype == "task.unsubscribe" and task_id:
                        unsubscribe_task(task_id, websocket)

                    remote_token = _current_is_remote.set(is_remote)
                    client_token = _current_client.set(websocket)
                    id_token = _current_client_id.set(client_id)
                    try:
                        # create_task copies the current Context, keeping sender,
                        # client ID and remote/local provenance scoped to this call.
                        asyncio.create_task(_run_handler(fn, data, mtype))
                    finally:
                        _current_client_id.reset(id_token)
                        _current_client.reset(client_token)
                        _current_is_remote.reset(remote_token)
            except (json.JSONDecodeError, AttributeError):
                pass
    except websockets.exceptions.ConnectionClosed:
        pass
    finally:
        if keepalive is not None:
            keepalive.cancel()
        if _on_disconnect is not None:
            remote_token = _current_is_remote.set(is_remote)
            client_token = _current_client.set(websocket)
            id_token = _current_client_id.set(client_id)
            try:
                await _on_disconnect(websocket)
            except Exception as exc:  # noqa: BLE001
                print(f"[WebSocket] on_disconnect handler failed: {exc}", flush=True)
            finally:
                _current_client_id.reset(id_token)
                _current_client.reset(client_token)
                _current_is_remote.reset(remote_token)
        _unsubscribe_all(websocket)
        _clients.discard(websocket)
        _client_ids.pop(websocket, None)
        _authenticated_devices.pop(websocket, None)
        _connection_challenges.pop(websocket, None)
        _host_out_counters.pop(websocket, None)
        _legacy_remote_clients.discard(websocket)


# Signed app-level keepalive to each authenticated phone. Protocol pings are
# invisible to the phone's JS, so a half-open socket (Wi-Fi→cellular switch, NAT
# timeout) used to look "online" for minutes and swallow a task.submit. Once the
# phone has seen one link.ping it treats ~2.5 intervals of silence as a dead link
# and reconnects. Older phones just ignore the unknown event.
_LINK_PING_S = 20.0


async def _link_keepalive(websocket: WebSocketServerProtocol) -> None:
    try:
        while True:
            await asyncio.sleep(_LINK_PING_S)
            await send_to(websocket, "link.ping", {"t": int(time.time())})
    except Exception:  # noqa: BLE001 — closed socket / cancelled: the handler cleans up
        return


def _authorized(websocket: WebSocketServerProtocol) -> bool:
    # This is only the HTTP-upgrade gate. Remote authorization completes later
    # through `_authenticate_remote`; returning True here never grants dispatch.
    headers, path = _request_parts(websocket)

    is_loopback = _is_loopback(websocket)
    try:
        peer = (websocket.remote_address or ("",))[0]
    except Exception:  # noqa: BLE001
        peer = ""

    origin = headers.get("Origin")
    if origin and origin not in _ALLOWED_ORIGINS:
        print(f"[WebSocket] Rejected origin: {origin}", flush=True)
        return False

    if is_loopback:
        # PRESERVED VERBATIM from the known-working backend (do NOT tighten): an
        # allow-listed Origin is the bundled WebView (a browser forbids a web page
        # from forging Origin, so this already rules out cross-site abuse of the
        # local port) and is trusted WITHOUT also demanding the token — the token
        # IPC proved fragile in the packaged app, and requiring it here is exactly
        # what risks a HUD that silently can't connect. Originless local clients
        # (a native tool) still need the token.
        if origin:
            return True
        if not _REQUIRED_TOKEN:
            print("[WebSocket] Rejected originless local connection with no token configured.", flush=True)
            return False
        token = _offered_bearer(websocket)
        if not token or not hmac.compare_digest(token, _REQUIRED_TOKEN):
            print("[WebSocket] Rejected originless local connection: token mismatch.", flush=True)
            return False
        return True

    if _legacy_remote_bearer_valid(websocket):
        print("[WebSocket] WARNING: development-only legacy remote bearer enabled.", flush=True)
        return True
    if _identity_registry is None:
        print("[WebSocket] Rejected remote connection: device identity is unavailable.", flush=True)
        return False
    if not _remote_transport_allowed(websocket):
        print(
            "[WebSocket] Rejected remote plaintext LAN connection; use WSS, "
            "Tailscale, or explicitly enable authenticated-LAN development mode.",
            flush=True,
        )
        return False
    return True


async def _run_handler(fn: Callable[..., Awaitable], data, event_type: str) -> None:
    try:
        await fn(data)
    except Exception as exc:  # noqa: BLE001
        print(f"[WebSocket] Handler '{event_type}' failed: {exc}", flush=True)
        # A remote task failure must not leak to every other client.  Keep the
        # legacy broadcast only when no sender context exists (e.g. an internal
        # backend job invoked the helper directly).
        client = current_sender()
        if client is not None:
            try:
                await send_to(client, "warning", f"Backend handler '{event_type}' failed: {exc}")
                await send_to(client, "status", "idle")
            except Exception:  # noqa: BLE001
                pass
        else:
            await emit("warning", f"Backend handler '{event_type}' failed: {exc}")
            await emit("status", "idle")


async def emit(event: str, data):
    """Broadcast a legacy HUD event to authenticated loopback clients only.

    Durable phone work uses :func:`emit_task_event`, while remote-screen replies
    use :func:`send_to_current`.  Keeping this legacy bus local prevents a paired
    phone from passively receiving conversations, telemetry, permission prompts,
    provider configuration, or another user's activity merely because the HUD
    happens to broadcast them.
    """
    clients = [client for client in tuple(_clients) if _is_loopback(client)]
    if not clients:
        return
    message = json.dumps({"event": event, "data": data})
    await asyncio.gather(
        *[client.send(message) for client in clients],
        return_exceptions=True,
    )


async def _send_payload(client: WebSocketServerProtocol, payload: dict) -> None:
    """Serialize one payload, signing every authenticated remote host message."""
    wire = dict(payload)
    if (
        not _is_loopback(client)
        and client not in _legacy_remote_clients
        and client in _authenticated_devices
    ):
        registry = _identity_registry
        challenge = _connection_challenges.get(client)
        if registry is None or challenge is None:
            raise AuthenticationError("remote host envelope has no connection identity")
        counter = _host_out_counters.get(client, 0) + 1
        _host_out_counters[client] = counter
        event = str(wire.get("event") or "")
        data = wire.get("data")
        task_id = str(wire.get("task_id") or "")
        if not task_id and isinstance(data, dict):
            task_id = str(data.get("task_id") or "")
        wire["auth"] = registry.sign_host_envelope(
            connection_nonce=challenge["nonce"],
            counter=counter,
            message_type=event,
            task_id=task_id,
            data=_envelope_content(wire),
        )
    await client.send(json.dumps(wire))


async def send_to(client: WebSocketServerProtocol, event: str, data):
    """Send one event to one websocket client."""
    await _send_payload(client, {"event": event, "data": data})


async def send_to_current(event: str, data) -> bool:
    """Send a legacy event only to the current handler's connection."""
    client = current_sender()
    if client is None:
        return False
    try:
        await send_to(client, event, data)
        return True
    except Exception:  # noqa: BLE001
        return False


async def emit_task_event(
    task_id: str,
    event: str,
    data,
    *,
    seq: "int | None" = None,
) -> int:
    """Deliver a versioned event only to subscribers of ``task_id``.

    ``seq`` should come from the durable task journal.  It is optional to ease
    migration of existing handlers; an in-memory monotonically increasing value
    is assigned when omitted.  The returned sequence is the value sent.
    """
    if not _valid_task_id(task_id):
        raise ValueError("invalid task_id")
    previous = _task_sequences.get(task_id, 0)
    if seq is None:
        seq = previous + 1
    elif not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
        raise ValueError("seq must be a non-negative integer")
    _task_sequences[task_id] = max(previous, seq)
    payload = {
        "v": 2,
        "event": event,
        "task_id": task_id,
        "seq": seq,
        "data": data,
    }
    subscribers = [
        client
        for client in tuple(_task_subscribers.get(task_id, set()))
        if client in _clients
    ]
    if subscribers:
        await asyncio.gather(
            *[_send_payload(client, payload) for client in subscribers],
            return_exceptions=True,
        )
    return seq


def _server_tls_context() -> ssl.SSLContext | None:
    """Load an explicitly provisioned WSS certificate when configured.

    Aura does not generate a certificate that Android would have to trust
    blindly. Without an explicit certificate, Tailscale remains the production
    route and the transport gate rejects raw plaintext LAN peers.
    """
    cert = os.environ.get("AURA_WSS_CERT", "").strip()
    key = os.environ.get("AURA_WSS_KEY", "").strip()
    if not cert and not key:
        return None
    if not cert or not key:
        raise RuntimeError("AURA_WSS_CERT and AURA_WSS_KEY must be configured together")
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certfile=cert, keyfile=key)
    return context


def _select_subprotocol(connection, subprotocols):
    # Our "subprotocols" are bespoke wire markers, not real negotiated content
    # types: `aura-token.<token>` carries the local HUD's bearer credential;
    # `aura-v2`/`aura-v3` are bare version markers the remote (phone) client
    # sends with no credential at all (device-identity auth happens after
    # connect via the signed challenge/response). Either way, a client that
    # offers a Sec-WebSocket-Protocol header requires the server to echo ONE
    # back (RFC 6455 §4.1) — omitting it silently completes the handshake for
    # a lenient client (e.g. the `websockets` Python test client) but a strict
    # one (Chromium/WebView2 — the actual desktop HUD *and* the phone app,
    # both Chromium-based WebViews) fails client-side with "Sent non-empty
    # 'Sec-WebSocket-Protocol' header but no response was received" — which
    # from the phone's Remote PC screen just looks like "PC offline" forever,
    # since the socket never reaches JS's onopen to prove otherwise. Echo
    # whichever of our own "aura-*" markers the client offered; real
    # authorization still happens separately via _offered_bearer()/
    # _authenticate_remote(), so echoing the marker back grants nothing extra.
    for protocol in subprotocols:
        if protocol.startswith("aura-"):
            return protocol
    return None


async def start():
    tls = _server_tls_context()
    # Re-read the bind host HERE (not at import) so main.py can flip it to
    # 0.0.0.0 only when the user has enabled remote access, leaving the default
    # localhost-only posture of the working app untouched for everyone else.
    bind_host = os.environ.get("JARVIS_WS_BIND", "localhost").strip() or "localhost"
    server = await websockets.serve(
        _handler,
        bind_host,
        8765,
        origins=_serve_origins(),
        select_subprotocol=_select_subprotocol,
        max_size=_MAX_MESSAGE_BYTES,
        max_queue=16,
        ping_interval=20,
        ssl=tls,
    )
    auth = "local HUD credential + remote signed device identity"
    scheme = "wss" if tls else "ws"
    print(f"[WebSocket] Server listening on {scheme}://{bind_host}:8765 ({auth})")
    if bind_host not in ("localhost", "127.0.0.1", "::1"):
        route = ("authenticated WSS" if tls else
                 "Tailscale only; plaintext LAN is rejected")
        print(f"[WebSocket] Remote route: {route}.", flush=True)
    return server
