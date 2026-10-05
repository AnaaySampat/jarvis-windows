"""Cryptographic host/device identity for Aura's remote-control protocol.

This module deliberately does not depend on the websocket server.  It owns the
durable security state and exposes small, deterministic primitives which the
transport can enforce before dispatching a message:

* one ECDSA P-256 Windows-host identity (PKCS#8 private key protected with
  current-user DPAPI on Windows),
* an SQLite registry of paired phone public keys, scopes, revocation and the
  last accepted monotonic counter,
* expiring, single-use pairing challenges whose QR payload contains public
  information only, and
* canonical signed authentication and protocol-envelope verification.

No goal, bearer token, private phone key, or reusable pairing secret is stored
here.  Pairing challenge IDs are capabilities only while visibly presented to
the user, expire quickly, and are consumed atomically.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec


PROTOCOL_VERSION = 1
KNOWN_SCOPES = frozenset({"tasks", "screen", "control"})
DEFAULT_SCOPES = ("tasks",)
MAX_PAIRING_TTL_SECONDS = 300


class IdentityError(RuntimeError):
    """Base class for fail-closed identity errors."""


class PairingError(IdentityError):
    """A pairing claim was expired, consumed, malformed, or invalid."""


class AuthenticationError(IdentityError):
    """A device authentication or signed envelope was invalid."""


class ScopeDenied(AuthenticationError):
    """A valid device lacks the required capability scope."""


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _unb64(text: str) -> bytes:
    raw = str(text or "").strip().encode("ascii")
    return base64.urlsafe_b64decode(raw + b"=" * (-len(raw) % 4))


def _normalize_numbers(value):
    """Fold integral floats to ints so canonical JSON survives a JS round-trip.

    Python renders 1.0 as ``1.0`` but JSON.parse → Number → canonical JS emits
    ``1``: the phone would compute a different digest for byte-identical wire
    data and reject a perfectly valid host signature (live-debugged 2026-07-19:
    a journal payload's ``backoff_seconds: 1.0`` broke every replayed
    task.event envelope). JS prints a float64's exact integer digits up to
    1e21, and ``int(float)`` is exact, so folding matches JS for every value
    Aura actually sends (timestamps, budgets, coordinates).
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _normalize_numbers(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_numbers(item) for item in value]
    return value


def _canonical_json(value) -> bytes:
    return json.dumps(
        _normalize_numbers(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def payload_digest(data) -> str:
    """Base64url SHA-256 over the canonical JSON representation of ``data``."""
    return _b64(hashlib.sha256(_canonical_json(data)).digest())


def public_key_fingerprint(public_der: bytes) -> str:
    return "sha256:" + _b64(hashlib.sha256(public_der).digest())


def device_id_for_public_key(public_der: bytes) -> str:
    return "phone-" + hashlib.sha256(public_der).hexdigest()[:32]


def host_id_for_public_key(public_der: bytes) -> str:
    return "host-" + hashlib.sha256(public_der).hexdigest()[:32]


def auth_signing_bytes(host_id: str, device_id: str, nonce: str, counter: int) -> bytes:
    return (
        f"aura-auth-v1\n{host_id}\n{device_id}\n{nonce}\n{int(counter)}"
    ).encode("utf-8")


def pairing_signing_bytes(
    host_id: str,
    challenge_id: str,
    device_id: str,
    connection_nonce: str,
    public_key_b64: str,
    device_name: str,
) -> bytes:
    return (
        "aura-pair-v1\n"
        f"{host_id}\n{challenge_id}\n{device_id}\n{connection_nonce}\n"
        f"{public_key_b64}\n{device_name}"
    ).encode("utf-8")


def envelope_signing_bytes(
    role: str,
    identity_id: str,
    connection_nonce: str,
    counter: int,
    message_type: str,
    task_id: str,
    data,
) -> bytes:
    """Canonical bytes signed by phones and the host for every post-auth message."""
    return (
        "aura-envelope-v1\n"
        f"{role}\n{identity_id}\n{connection_nonce}\n{int(counter)}\n"
        f"{message_type}\n{task_id}\n{payload_digest(data)}"
    ).encode("utf-8")


def host_challenge_signing_bytes(
    host_id: str, nonce: str, expires_at: int, connection_id: str
) -> bytes:
    return (
        "aura-host-challenge-v1\n"
        f"{host_id}\n{nonce}\n{int(expires_at)}\n{connection_id}"
    ).encode("utf-8")


@dataclass(frozen=True)
class AuthenticatedDevice:
    device_id: str
    name: str
    fingerprint: str
    scopes: tuple[str, ...]
    counter: int


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _blob(data: bytes) -> tuple[_DataBlob, object]:
    buf = ctypes.create_string_buffer(data)
    return _DataBlob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))), buf


def _dpapi_protect(data: bytes) -> bytes:
    if os.name != "nt":
        # Portable storage exists solely for development/tests on non-Windows
        # hosts. Production Aura is Windows-only and never takes this branch.
        return b"portable-test-v1\0" + data
    in_blob, keepalive = _blob(data)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob), "Aura host identity", None, None, None,
        0x1, ctypes.byref(out_blob),  # CRYPTPROTECT_UI_FORBIDDEN
    )
    del keepalive
    if not ok:
        raise IdentityError("Windows DPAPI could not protect the host identity")
    try:
        return bytes(ctypes.string_at(out_blob.pbData, out_blob.cbData))
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    prefix = b"portable-test-v1\0"
    if os.name != "nt":
        if not data.startswith(prefix):
            raise IdentityError("host identity is not a portable test key")
        return data[len(prefix):]
    in_blob, keepalive = _blob(data)
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        0x1, ctypes.byref(out_blob),
    )
    del keepalive
    if not ok:
        raise IdentityError("Windows DPAPI could not unlock the host identity")
    try:
        return bytes(ctypes.string_at(out_blob.pbData, out_blob.cbData))
    finally:
        kernel32.LocalFree(out_blob.pbData)


class DeviceIdentityRegistry:
    """Durable Windows identity and paired-phone authorization registry."""

    def __init__(self, directory: str | os.PathLike | None = None, *, clock=time.time):
        if directory is None:
            configured = os.environ.get("AURA_IDENTITY_DIR", "").strip()
            base = Path(configured) if configured else Path(
                os.environ.get("APPDATA") or Path.home() / ".aura"
            ) / "Aura" / "identity"
        else:
            base = Path(directory)
        base.mkdir(parents=True, exist_ok=True)
        self.directory = base
        self._clock = clock
        self._lock = threading.RLock()
        self._private_key = self._load_or_create_host_key(base / "host_identity.dpapi")
        self._public_der = self._private_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.host_id = host_id_for_public_key(self._public_der)
        self.host_fingerprint = public_key_fingerprint(self._public_der)
        self.host_public_key = _b64(self._public_der)
        self._db = sqlite3.connect(
            str(base / "devices.sqlite3"),
            isolation_level=None,
            check_same_thread=False,
            timeout=10.0,
        )
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.executescript(
                """
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS devices (
                    device_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    public_key TEXT NOT NULL,
                    fingerprint TEXT NOT NULL UNIQUE,
                    scopes TEXT NOT NULL,
                    paired_at REAL NOT NULL,
                    last_seen REAL,
                    last_counter INTEGER NOT NULL DEFAULT 0,
                    revoked_at REAL,
                    authorized_at REAL
                );
                CREATE TABLE IF NOT EXISTS pairing_challenges (
                    challenge_id TEXT PRIMARY KEY,
                    created_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    used_at REAL,
                    device_name TEXT NOT NULL,
                    scopes TEXT NOT NULL,
                    pin TEXT
                );
                """
            )
            # S1: pairing requires a 6-digit PIN shown on the PC screen (NOT in the
            # QR) and typed into the phone — proof the pairer actually saw the
            # screen, so a tailnet peer who only grabbed the QR can't claim it. A
            # PIN-verified device is trusted immediately (no separate approval).
            # Migrate any older devices.sqlite3 / pre-PIN challenges.
            for table, column, coltype in (
                ("devices", "authorized_at", "REAL"),
                ("pairing_challenges", "pin", "TEXT"),
            ):
                try:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
                except sqlite3.OperationalError:
                    pass  # column already exists

    @staticmethod
    def _load_or_create_host_key(path: Path) -> ec.EllipticCurvePrivateKey:
        if path.exists():
            raw = _dpapi_unprotect(path.read_bytes())
            key = serialization.load_der_private_key(raw, password=None)
            if not isinstance(key, ec.EllipticCurvePrivateKey):
                raise IdentityError("stored Aura host identity is not an EC key")
            return key
        key = ec.generate_private_key(ec.SECP256R1())
        raw = key.private_bytes(
            serialization.Encoding.DER,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        protected = _dpapi_protect(raw)
        temp = path.with_suffix(".tmp")
        with open(temp, "xb") as handle:
            handle.write(protected)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temp, path)
        return key

    @staticmethod
    def _normalize_scopes(scopes: Iterable[str] | None) -> tuple[str, ...]:
        selected = tuple(sorted({str(scope) for scope in (scopes or DEFAULT_SCOPES)}))
        if not selected or any(scope not in KNOWN_SCOPES for scope in selected):
            raise PairingError("pairing requested an unknown or empty scope set")
        return selected

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def create_pairing_challenge(
        self,
        *,
        device_name: str = "Aura phone",
        scopes: Iterable[str] = DEFAULT_SCOPES,
        ttl_seconds: int = 120,
    ) -> dict:
        ttl = max(15, min(MAX_PAIRING_TTL_SECONDS, int(ttl_seconds)))
        now = float(self._clock())
        challenge_id = "pair-" + secrets.token_urlsafe(24)
        clean_name = " ".join(str(device_name or "Aura phone").split())[:80]
        selected = self._normalize_scopes(scopes)
        pin = f"{secrets.randbelow(1_000_000):06d}"
        with self._lock:
            self._db.execute(
                "INSERT INTO pairing_challenges VALUES (?, ?, ?, NULL, ?, ?, ?)",
                (challenge_id, now, now + ttl, clean_name, json.dumps(selected), pin),
            )
        # The "pin" is shown on the PC screen and typed into the phone; it is the
        # ONE field the caller must keep OUT of the QR. Everything else is public.
        return {
            "version": PROTOCOL_VERSION,
            "kind": "aura.pairing",
            "host_id": self.host_id,
            "host_fingerprint": self.host_fingerprint,
            "host_public_key": self.host_public_key,
            "challenge_id": challenge_id,
            "expires_at": int(now + ttl),
            "scopes": list(selected),
            "pin": pin,
        }

    def make_connection_challenge(self, connection_id: str, *, ttl_seconds: int = 60) -> dict:
        # 60s: the phone's Keystore bridge can burn ~12.6s of retries per call
        # and needs two calls (verify + sign) before it can reply; a 20s TTL
        # expired healthy-but-slow phones mid-handshake.
        now = int(self._clock())
        expires_at = now + max(5, min(90, int(ttl_seconds)))
        nonce = secrets.token_urlsafe(32)
        signature = self._private_key.sign(
            host_challenge_signing_bytes(self.host_id, nonce, expires_at, connection_id),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "version": PROTOCOL_VERSION,
            "host_id": self.host_id,
            "host_fingerprint": self.host_fingerprint,
            "host_public_key": self.host_public_key,
            "connection_id": connection_id,
            "nonce": nonce,
            "expires_at": expires_at,
            "signature": _b64(signature),
        }

    @staticmethod
    def _load_phone_public_key(encoded: str) -> tuple[ec.EllipticCurvePublicKey, bytes]:
        try:
            raw = _unb64(encoded)
            key = serialization.load_der_public_key(raw)
        except Exception as exc:  # noqa: BLE001 - malformed input is authentication failure
            raise AuthenticationError("invalid phone public key") from exc
        if not isinstance(key, ec.EllipticCurvePublicKey) or not isinstance(
            key.curve, ec.SECP256R1
        ):
            raise AuthenticationError("phone identity must use ECDSA P-256")
        return key, raw

    @staticmethod
    def _verify(key: ec.EllipticCurvePublicKey, signature_b64: str, material: bytes) -> None:
        try:
            key.verify(_unb64(signature_b64), material, ec.ECDSA(hashes.SHA256()))
        except (InvalidSignature, ValueError, TypeError) as exc:
            raise AuthenticationError("signature verification failed") from exc

    def claim_pairing(
        self,
        claim: Mapping,
        *,
        connection_nonce: str,
    ) -> AuthenticatedDevice:
        challenge_id = str(claim.get("challenge_id") or "")
        device_id = str(claim.get("device_id") or "")
        public_key_b64 = str(claim.get("public_key") or "")
        name = " ".join(str(claim.get("device_name") or "Aura phone").split())[:80]
        signature = str(claim.get("signature") or "")
        if not all((challenge_id, device_id, public_key_b64, name, signature, connection_nonce)):
            raise PairingError("incomplete pairing claim")
        key, public_der = self._load_phone_public_key(public_key_b64)
        expected_id = device_id_for_public_key(public_der)
        if device_id != expected_id:
            raise PairingError("device id does not match its public key")
        self._verify(
            key,
            signature,
            pairing_signing_bytes(
                self.host_id, challenge_id, device_id, connection_nonce,
                public_key_b64, name,
            ),
        )
        now = float(self._clock())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._db.execute(
                    "SELECT * FROM pairing_challenges WHERE challenge_id = ?",
                    (challenge_id,),
                ).fetchone()
                if row is None:
                    raise PairingError("unknown pairing challenge")
                if row["used_at"] is not None:
                    raise PairingError("pairing challenge was already used")
                if now > float(row["expires_at"]):
                    raise PairingError("pairing challenge expired")
                stored_pin = row["pin"] if "pin" in row.keys() else None
                if not stored_pin or not hmac.compare_digest(
                    str(claim.get("pin") or ""), str(stored_pin)
                ):
                    got = str(claim.get("pin") or "")
                    mask = lambda p: f"{p[:1]}****{p[-1:]}(len={len(p)})" if p else "EMPTY"
                    print(f"[Pairing DIAG] challenge={challenge_id[:18]}… "
                          f"claim_pin={mask(got)} stored_pin={mask(str(stored_pin or ''))} "
                          f"challenge_age={now - float(row['created_at']):.0f}s",
                          flush=True)
                    raise PairingError("pairing PIN is missing or incorrect")
                scopes = tuple(json.loads(row["scopes"]))
                fingerprint = public_key_fingerprint(public_der)
                existing = self._db.execute(
                    "SELECT public_key, revoked_at FROM devices WHERE device_id = ?",
                    (device_id,),
                ).fetchone()
                if existing and existing["public_key"] != public_key_b64:
                    raise PairingError("device id is already bound to another key")
                if existing and existing["revoked_at"] is not None:
                    raise PairingError("a revoked device must be explicitly removed before re-pairing")
                # PIN verified above ⇒ the device is trusted immediately (the PIN
                # IS the human-present approval; there is no separate PC click).
                self._db.execute(
                    """INSERT INTO devices
                       (device_id,name,public_key,fingerprint,scopes,paired_at,last_seen,last_counter,revoked_at,authorized_at)
                       VALUES (?,?,?,?,?,?,?,0,NULL,?)
                       ON CONFLICT(device_id) DO UPDATE SET
                         name=excluded.name, scopes=excluded.scopes, last_seen=excluded.last_seen,
                         authorized_at=excluded.authorized_at""",
                    (device_id, name, public_key_b64, fingerprint,
                     json.dumps(scopes), now, now, now),
                )
                self._db.execute(
                    "UPDATE pairing_challenges SET used_at = ? WHERE challenge_id = ?",
                    (now, challenge_id),
                )
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return AuthenticatedDevice(device_id, name, fingerprint, scopes, 0)

    def _device_row(self, device_id: str):
        row = self._db.execute(
            "SELECT * FROM devices WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise AuthenticationError("unknown device")
        if row["revoked_at"] is not None:
            raise AuthenticationError("device is revoked")
        return row

    def _accept_counter_and_signature(
        self, device_id: str, counter: int, signature: str, material: bytes
    ) -> AuthenticatedDevice:
        if isinstance(counter, bool) or not isinstance(counter, int) or counter <= 0:
            raise AuthenticationError("counter must be a positive integer")
        now = float(self._clock())
        with self._lock:
            self._db.execute("BEGIN IMMEDIATE")
            try:
                row = self._device_row(device_id)
                if counter <= int(row["last_counter"]):
                    raise AuthenticationError("replayed or stale device counter")
                key, _ = self._load_phone_public_key(row["public_key"])
                self._verify(key, signature, material)
                changed = self._db.execute(
                    """UPDATE devices SET last_counter = ?, last_seen = ?
                       WHERE device_id = ? AND revoked_at IS NULL AND last_counter < ?""",
                    (counter, now, device_id, counter),
                ).rowcount
                if changed != 1:
                    raise AuthenticationError("counter raced another connection")
                self._db.execute("COMMIT")
            except Exception:
                self._db.execute("ROLLBACK")
                raise
        return AuthenticatedDevice(
            device_id=device_id,
            name=row["name"],
            fingerprint=row["fingerprint"],
            scopes=tuple(json.loads(row["scopes"])),
            counter=counter,
        )

    def authenticate(
        self, device_id: str, nonce: str, counter: int, signature: str
    ) -> AuthenticatedDevice:
        return self._accept_counter_and_signature(
            device_id,
            counter,
            signature,
            auth_signing_bytes(self.host_id, device_id, nonce, counter),
        )

    def verify_envelope(
        self,
        *,
        device_id: str,
        connection_nonce: str,
        counter: int,
        message_type: str,
        task_id: str,
        data,
        signature: str,
        required_scope: str | None = None,
    ) -> AuthenticatedDevice:
        material = envelope_signing_bytes(
            "device", device_id, connection_nonce, counter,
            message_type, task_id, data,
        )
        device = self._accept_counter_and_signature(
            device_id, counter, signature, material
        )
        if required_scope and required_scope not in device.scopes:
            raise ScopeDenied(f"device lacks {required_scope!r} scope")
        return device

    def sign_host_envelope(
        self,
        *,
        connection_nonce: str,
        counter: int,
        message_type: str,
        task_id: str,
        data,
    ) -> dict:
        signature = self._private_key.sign(
            envelope_signing_bytes(
                "host", self.host_id, connection_nonce, counter,
                message_type, task_id, data,
            ),
            ec.ECDSA(hashes.SHA256()),
        )
        return {
            "host_id": self.host_id,
            "connection_nonce": connection_nonce,
            "counter": int(counter),
            "signature": _b64(signature),
        }

    def revoke(self, device_id: str) -> bool:
        with self._lock:
            return self._db.execute(
                "UPDATE devices SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                (float(self._clock()), device_id),
            ).rowcount == 1

    def remove(self, device_id: str) -> bool:
        """Delete a device row entirely, allowing the same phone key to pair
        again (fresh QR + PIN — the human-present gate — is still required).
        Without this, a revoked device was bricked forever: claim_pairing
        refuses revoked rows and no removal path existed. The audit trail in
        remote_audit.jsonl is append-only and survives the row's deletion."""
        with self._lock:
            return self._db.execute(
                "DELETE FROM devices WHERE device_id = ?", (device_id,)
            ).rowcount == 1

    def record_event(self, entry: Mapping) -> None:
        """Append-only audit trail (device_id, task, goal, outcome) with a server
        timestamp. The registry's write side (replay counters, revocation, expiring
        challenges) is thorough; this is the read side — what ran, from which
        device — for forensics after a stolen-phone or mis-approved-pairing case.
        Best-effort: auditing must never break the task path."""
        try:
            record = {"ts": float(self._clock()), **dict(entry)}
            line = json.dumps(record, separators=(",", ":"), default=str)
            with self._lock:
                with open(self.directory / "remote_audit.jsonl", "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass

    def has_scope(self, device_id: str, scope: str) -> bool:
        with self._lock:
            try:
                row = self._device_row(device_id)
            except AuthenticationError:
                return False
            return scope in tuple(json.loads(row["scopes"]))

    def list_devices(self, *, include_revoked: bool = True) -> list[dict]:
        query = "SELECT * FROM devices"
        if not include_revoked:
            query += " WHERE revoked_at IS NULL"
        query += " ORDER BY paired_at DESC"
        with self._lock:
            rows = self._db.execute(query).fetchall()
        return [
            {
                "device_id": row["device_id"],
                "name": row["name"],
                "fingerprint": row["fingerprint"],
                "scopes": json.loads(row["scopes"]),
                "paired_at": row["paired_at"],
                "last_seen": row["last_seen"],
                "revoked": row["revoked_at"] is not None,
                "pending": row["authorized_at"] is None and row["revoked_at"] is None,
            }
            for row in rows
        ]


__all__ = [
    "AuthenticatedDevice",
    "AuthenticationError",
    "DeviceIdentityRegistry",
    "IdentityError",
    "KNOWN_SCOPES",
    "PairingError",
    "ScopeDenied",
    "auth_signing_bytes",
    "device_id_for_public_key",
    "envelope_signing_bytes",
    "host_challenge_signing_bytes",
    "pairing_signing_bytes",
    "payload_digest",
    "public_key_fingerprint",
]
