"""End-to-end handshake probe: pairing → S1 pending → authorize → auth.response
→ signed task envelope, driven through the real _authenticate_remote with an
in-memory fake phone. Complements the crypto unit tests (test_device_identity)
and the transport gate tests (test_tailscale_transport)."""

import asyncio
import json
import tempfile
import unittest
from collections import deque

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from server import websocket_server as ws
from autonomy.device_identity import (
    DeviceIdentityRegistry, device_id_for_public_key, pairing_signing_bytes,
    auth_signing_bytes, envelope_signing_bytes, _b64,
)


class FakePhone:
    """Minimal in-memory websocket: queues host→phone sends, replays scripted
    phone→host messages, and signs like the real Android client will."""

    def __init__(self, peer="100.64.0.9"):
        self.remote_address = (peer, 55555)
        self._key = ec.generate_private_key(ec.SECP256R1())
        self.der = self._key.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.device_id = device_id_for_public_key(self.der)
        self.pkb64 = _b64(self.der)
        self.sent = []
        self._inbox = deque()

    def sign(self, material):
        return _b64(self._key.sign(material, ec.ECDSA(hashes.SHA256())))

    def queue(self, msg):
        self._inbox.append(json.dumps(msg))

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    async def recv(self):
        if not self._inbox:
            raise asyncio.TimeoutError
        return self._inbox.popleft()

    def last(self, event):
        return next((m for m in reversed(self.sent) if m.get("event") == event), None)


class RemoteHandshakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = DeviceIdentityRegistry(directory=tempfile.mkdtemp())
        ws.configure_device_identity(self.registry)

    def _pair(self, phone, challenge, nonce, pin=None):
        material = pairing_signing_bytes(
            self.registry.host_id, challenge["challenge_id"], phone.device_id,
            nonce, phone.pkb64, "Aura phone")
        return {"type": "pairing.claim", "data": {
            "challenge_id": challenge["challenge_id"], "device_id": phone.device_id,
            "public_key": phone.pkb64, "device_name": "Aura phone",
            "pin": pin if pin is not None else challenge["pin"],
            "signature": phone.sign(material)}}

    async def test_full_flow_pair_pending_authorize_auth_and_task_envelope(self):
        phone = FakePhone()
        challenge = self.registry.create_pairing_challenge(scopes=("tasks",))

        # 1. Pairing claim WITH the PIN → device registered AND trusted (the PIN
        #    is the human-present approval; no separate step).
        cid = "conn-1"
        conn = self.registry.make_connection_challenge(cid)  # nonce the phone signs against
        # Drive _authenticate_remote: it sends auth.challenge, reads our reply.
        phone.queue(self._pair(phone, challenge, conn["nonce"]))
        # Inject the SAME connection challenge the handshake will use.
        with _fixed_challenge(conn):
            device = await ws._authenticate_remote(phone, cid)
        self.assertIsNotNone(device)
        self.assertEqual(device.device_id, phone.device_id)
        self.assertFalse(self.registry.list_devices()[0]["pending"],
                         "a PIN-paired device is trusted immediately")

        # 2. Reconnect + auth.response with a signed monotonic counter.
        phone2 = FakePhone(peer="100.64.0.9")
        phone2._key = phone._key
        phone2.der, phone2.pkb64, phone2.device_id = phone.der, phone.pkb64, phone.device_id
        cid2 = "conn-2"
        conn2 = self.registry.make_connection_challenge(cid2)
        counter = 1
        sig = phone2.sign(auth_signing_bytes(
            self.registry.host_id, phone2.device_id, conn2["nonce"], counter))
        phone2.queue({"type": "auth.response", "data": {
            "device_id": phone2.device_id, "nonce": conn2["nonce"],
            "counter": counter, "signature": sig}})
        with _fixed_challenge(conn2):
            device2 = await ws._authenticate_remote(phone2, cid2)
        self.assertIsNotNone(device2)
        self.assertEqual(device2.scopes, ("tasks",))

        # 4. A signed task.submit envelope verifies with the 'tasks' scope.
        #    The server signs over _envelope_content(msg) (everything but
        #    auth/type/event/task_id), so build the message then sign that.
        ws._authenticated_devices[phone2] = device2
        ws._connection_challenges[phone2] = conn2
        counter2 = 2
        msg = {"type": "task.submit", "task_id": "task-1",
               "data": {"task_id": "task-1", "goal": "open notepad"}}
        env_sig = phone2.sign(envelope_signing_bytes(
            "device", phone2.device_id, conn2["nonce"], counter2,
            "task.submit", "task-1", ws._envelope_content(msg)))
        msg["auth"] = {"device_id": phone2.device_id,
                       "connection_nonce": conn2["nonce"],
                       "counter": counter2, "signature": env_sig}
        verified = ws._verify_remote_message(phone2, msg)
        self.assertEqual(verified.device_id, phone2.device_id)

        # 5. Scope-trim: screen/control map to no scope now.
        self.assertEqual(ws._scope_for_message("task.submit"), "tasks")
        self.assertIsNone(ws._scope_for_message("screen.open"))
        self.assertIsNone(ws._scope_for_message("arm_control"))

    async def test_s1_pairing_requires_the_pin_and_is_single_use(self):
        """The QR alone can't pair (S1): a claimant with the WRONG PIN is rejected,
        the correct PIN pairs + trusts immediately, and the challenge is single-use."""
        from autonomy.device_identity import PairingError
        challenge = self.registry.create_pairing_challenge(scopes=("tasks",))
        conn = self.registry.make_connection_challenge("c")
        racer, real = FakePhone(), FakePhone()

        def claim(phone, pin):
            return {
                "challenge_id": challenge["challenge_id"], "device_id": phone.device_id,
                "public_key": phone.pkb64, "device_name": "Aura phone", "pin": pin,
                "signature": phone.sign(pairing_signing_bytes(
                    self.registry.host_id, challenge["challenge_id"], phone.device_id,
                    conn["nonce"], phone.pkb64, "Aura phone")),
            }

        # A tailnet racer who grabbed the QR but not the on-screen PIN is rejected.
        with self.assertRaises(PairingError):
            self.registry.claim_pairing(claim(racer, "000000"), connection_nonce=conn["nonce"])
        # The real phone (correct PIN) pairs AND is trusted immediately.
        dev = self.registry.claim_pairing(claim(real, challenge["pin"]), connection_nonce=conn["nonce"])
        self.assertEqual(dev.device_id, real.device_id)
        self.assertFalse(self.registry.list_devices()[0]["pending"])
        # Single-use: the spent challenge can't be replayed even with the right PIN.
        with self.assertRaises(PairingError):
            self.registry.claim_pairing(claim(real, challenge["pin"]), connection_nonce=conn["nonce"])

    async def test_handshake_timeout_is_transient_not_an_identity_verdict(self):
        """A phone that never replies (slow Keystore) must NOT get the definitive
        None → 1008 close: the Android client treats 1008-after-challenge as
        terminal 'Identity rejected' and stops retrying. Silence returns the
        timeout sentinel (→ 4008), a wrong-type reply stays definitive."""
        silent = FakePhone()  # empty inbox → recv raises TimeoutError
        result = await ws._authenticate_remote(silent, "conn-1")
        self.assertIs(result, ws._HANDSHAKE_TIMEOUT)
        self.assertIsNotNone(silent.last("auth.challenge"))

        weird = FakePhone()
        weird.queue({"type": "task.submit", "data": {}})  # pre-auth nonsense
        self.assertIsNone(await ws._authenticate_remote(weird, "conn-2"))

    def test_revoked_then_removed_device_can_pair_again(self):
        """Revoke blocks re-pairing (by design), but remove() must unbrick the
        SAME phone key: revoke→remove→fresh QR+PIN pairs again. This is the HUD
        'Remove' flow — without remove(), one misclicked revoke bricked the
        phone forever."""
        from autonomy.device_identity import PairingError
        phone = FakePhone()

        def pair_once():
            challenge = self.registry.create_pairing_challenge(scopes=("tasks",))
            conn = self.registry.make_connection_challenge("c")
            return self.registry.claim_pairing({
                "challenge_id": challenge["challenge_id"], "device_id": phone.device_id,
                "public_key": phone.pkb64, "device_name": "Aura phone",
                "pin": challenge["pin"],
                "signature": phone.sign(pairing_signing_bytes(
                    self.registry.host_id, challenge["challenge_id"], phone.device_id,
                    conn["nonce"], phone.pkb64, "Aura phone")),
            }, connection_nonce=conn["nonce"])

        self.assertEqual(pair_once().device_id, phone.device_id)
        self.assertTrue(self.registry.revoke(phone.device_id))
        with self.assertRaises(PairingError):   # revoked row blocks re-pairing
            pair_once()
        self.assertTrue(self.registry.remove(phone.device_id))
        self.assertEqual(pair_once().device_id, phone.device_id)  # unbricked

    def test_c4_tasks_device_cannot_invoke_screen_or_control_verbs(self):
        """A tasks-only remote device is barred from screen/control verbs two ways:
        no scope maps to them AND the default-deny allowlist excludes them."""
        ws.set_remote_allowed({"task.submit"})
        for verb in ("arm_control", "remote_input", "screen.open", "webrtc_offer",
                     "control.acquire", "stop_screen"):
            self.assertIsNone(ws._scope_for_message(verb), verb)
            self.assertNotIn(verb, ws._remote_allowed, verb)
        self.assertIn("task.submit", ws._remote_allowed)


class _fixed_challenge:
    """Force make_connection_challenge to return a known challenge so the fake
    phone can pre-sign against its nonce."""

    def __init__(self, challenge):
        self.challenge = challenge

    def __enter__(self):
        self._orig = DeviceIdentityRegistry.make_connection_challenge
        challenge = self.challenge
        DeviceIdentityRegistry.make_connection_challenge = lambda self, cid, **k: challenge
        return self

    def __exit__(self, *a):
        DeviceIdentityRegistry.make_connection_challenge = self._orig


if __name__ == "__main__":
    unittest.main()
