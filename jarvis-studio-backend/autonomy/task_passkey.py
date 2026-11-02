"""Laptop-owned, per-task passkey verifier. No plaintext passkey is persisted."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from autonomy.device_identity import _dpapi_protect, _dpapi_unprotect

ITERATIONS = 600_000
ATTEMPTS = 5
WINDOW_SECONDS = 60


@dataclass(frozen=True)
class Verification:
    accepted: bool
    error: str = ""
    revision: str = ""


class TaskPasskeyStore:
    def __init__(self, directory, *, clock=time.monotonic):
        self.path = Path(directory) / "task_passkey.dpapi"
        self._lock = threading.Lock()
        self._clock = clock
        self._attempts = {}
        self._record = None
        self._unavailable = False
        try:
            raw = self.path.read_bytes()
        except FileNotFoundError:
            return
        except OSError:
            self._unavailable = True
            return
        try:
            record = json.loads(_dpapi_unprotect(raw))
            if (record.get("version") != 1 or record.get("iterations") != ITERATIONS
                    or len(base64.b64decode(record["salt"], validate=True)) != 16
                    or len(base64.b64decode(record["digest"], validate=True)) != 32
                    or not isinstance(record.get("revision"), str) or not record["revision"]):
                raise ValueError("invalid verifier")
            self._record = record
        except Exception:
            self._unavailable = True  # fail closed; the laptop may save a replacement

    def state(self):
        with self._lock:
            return {"configured": self._record is not None, "storage_error": self._unavailable}

    def set(self, passkey):
        if (not isinstance(passkey, str) or not 8 <= len(passkey) <= 128
                or any(0xD800 <= ord(char) <= 0xDFFF for char in passkey)):
            raise ValueError("Use a passkey between 8 and 128 characters.")
        salt = secrets.token_bytes(16)
        digest = hashlib.pbkdf2_hmac("sha256", passkey.encode("utf-8"), salt, ITERATIONS)
        record = {"version": 1, "iterations": ITERATIONS,
                  "salt": base64.b64encode(salt).decode(), "digest": base64.b64encode(digest).decode(),
                  "revision": secrets.token_hex(16)}
        protected = _dpapi_protect(json.dumps(record).encode())
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(self.path.name + "." + secrets.token_hex(8) + ".tmp")
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(protected)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            self._record = record
            self._unavailable = False
            self._attempts.clear()

    def is_current(self, revision):
        with self._lock:
            return self._record is not None and self._record["revision"] == revision

    def verify(self, passkey, device_id):
        with self._lock:
            if self._unavailable:
                return Verification(False, "passkey_unavailable")
            record = self._record
            if record is None:
                return Verification(False, "passkey_not_configured")
            now = self._clock()
            attempts = [stamp for stamp in self._attempts.get(device_id, []) if now - stamp < WINDOW_SECONDS]
            if len(attempts) >= ATTEMPTS:
                return Verification(False, "passkey_rate_limited")
            self._attempts[device_id] = [*attempts, now]
        if not isinstance(passkey, str) or not 8 <= len(passkey) <= 128:
            return Verification(False, "passkey_incorrect")
        try:
            digest = hashlib.pbkdf2_hmac("sha256", passkey.encode("utf-8"),
                                         base64.b64decode(record["salt"]), ITERATIONS)
        except UnicodeError:
            return Verification(False, "passkey_incorrect")
        with self._lock:
            if self._record is None or self._record["revision"] != record["revision"]:
                return Verification(False, "passkey_changed")
            if not hmac.compare_digest(digest, base64.b64decode(record["digest"])):
                return Verification(False, "passkey_incorrect")
            self._attempts.pop(device_id, None)
            return Verification(True, revision=record["revision"])
