import base64
import tempfile
import unittest
from unittest import mock

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from autonomy.device_identity import (
    AuthenticationError,
    DeviceIdentityRegistry,
    PairingError,
    ScopeDenied,
    auth_signing_bytes,
    device_id_for_public_key,
    envelope_signing_bytes,
    pairing_signing_bytes,
    _canonical_json,
)


def b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


class DeviceIdentityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.now = [1_700_000_000.0]
        self.registry = DeviceIdentityRegistry(
            self.temp.name, clock=lambda: self.now[0]
        )
        self.phone_key = ec.generate_private_key(ec.SECP256R1())
        self.public_der = self.phone_key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        self.public_b64 = b64(self.public_der)
        self.device_id = device_id_for_public_key(self.public_der)

    def tearDown(self):
        self.registry.close()
        self.temp.cleanup()

    def claim(self, challenge, *, nonce="connection-nonce", name="My phone"):
        material = pairing_signing_bytes(
            self.registry.host_id,
            challenge["challenge_id"],
            self.device_id,
            nonce,
            self.public_b64,
            name,
        )
        return self.registry.claim_pairing(
            {
                "challenge_id": challenge["challenge_id"],
                "device_id": self.device_id,
                "public_key": self.public_b64,
                "device_name": name,
                "pin": challenge["pin"],
                "signature": b64(
                    self.phone_key.sign(material, ec.ECDSA(hashes.SHA256()))
                ),
            },
            connection_nonce=nonce,
        )

    def pair(self, scopes=("tasks",)):
        challenge = self.registry.create_pairing_challenge(scopes=scopes)
        return self.claim(challenge)

    def auth(self, counter=1, nonce="connection-nonce"):
        signature = self.phone_key.sign(
            auth_signing_bytes(
                self.registry.host_id, self.device_id, nonce, counter
            ),
            ec.ECDSA(hashes.SHA256()),
        )
        return self.registry.authenticate(self.device_id, nonce, counter, b64(signature))

    def envelope(self, counter, data, *, message_type="task.submit", task_id="t-1"):
        material = envelope_signing_bytes(
            "device",
            self.device_id,
            "connection-nonce",
            counter,
            message_type,
            task_id,
            data,
        )
        signature = self.phone_key.sign(material, ec.ECDSA(hashes.SHA256()))
        return self.registry.verify_envelope(
            device_id=self.device_id,
            connection_nonce="connection-nonce",
            counter=counter,
            message_type=message_type,
            task_id=task_id,
            data=data,
            signature=b64(signature),
            required_scope="tasks",
        )

    def test_pairing_payload_has_only_public_expiring_material_and_is_single_use(self):
        challenge = self.registry.create_pairing_challenge(
            scopes=("tasks", "screen"), ttl_seconds=60
        )
        self.assertNotIn("secret", challenge)
        self.assertNotIn("token", challenge)
        paired = self.claim(challenge)
        self.assertEqual(paired.device_id, self.device_id)
        self.assertEqual(set(paired.scopes), {"tasks", "screen"})
        with self.assertRaisesRegex(PairingError, "already used"):
            self.claim(challenge)

    def test_expired_pairing_challenge_fails_closed(self):
        challenge = self.registry.create_pairing_challenge(ttl_seconds=15)
        self.now[0] += 16
        with self.assertRaisesRegex(PairingError, "expired"):
            self.claim(challenge)

    def test_canonical_payloads_match_javascript_numbers_and_utf16_key_order(self):
        vectors = [
            (1.0, b"1"), (-0.0, b"0"), (1e-6, b"0.000001"),
            (1e-7, b"1e-7"), (1e21, b"1e+21"), (1e20, b"100000000000000000000"),
            (10**20, b"100000000000000000000"), (2**53 + 1, b"9007199254740992"),
            ({"\ue000": "a", "\U0001f680": "b"}, '{"🚀":"b","\ue000":"a"}'.encode()),
            ({"data": [1.0, {"x": 1e-6}]}, b'{"data":[1,{"x":0.000001}]}'),
        ]
        for value, expected in vectors:
            with self.subTest(value=value):
                self.assertEqual(_canonical_json(value), expected)
        for invalid in (float("nan"), float("inf"), 10**500):
            with self.assertRaises(AuthenticationError):
                _canonical_json(invalid)

    def test_malformed_counters_fail_as_authentication_errors(self):
        self.pair()
        for counter in (None, "1", True, 0, -1, 1.5, 2**63):
            with self.subTest(counter=counter):
                with self.assertRaises(AuthenticationError):
                    self.registry.authenticate(self.device_id, "nonce", counter, "signature")
                with self.assertRaises(AuthenticationError):
                    self.registry.verify_envelope(
                        device_id=self.device_id, connection_nonce="nonce", counter=counter,
                        message_type="task.submit", task_id="t", data={}, signature="signature",
                    )

    def test_pin_guess_budget_survives_restart_and_requires_a_fresh_code(self):
        challenge = self.registry.create_pairing_challenge()
        wrong = {**challenge, "pin": "000000" if challenge["pin"] != "000000" else "111111"}
        with mock.patch("builtins.print") as output:
            for _ in range(4):
                with self.assertRaisesRegex(PairingError, "incorrect"):
                    self.claim(wrong)
            self.registry.close()
            self.registry = DeviceIdentityRegistry(self.temp.name, clock=lambda: self.now[0])
            with self.assertRaisesRegex(PairingError, "incorrect"):
                self.claim(wrong)
            # Even the correct PIN cannot revive a challenge after five guesses.
            with self.assertRaisesRegex(PairingError, "attempt limit"):
                self.claim(challenge)
        output.assert_not_called()
        row = self.registry._db.execute(
            "SELECT pin FROM pairing_challenges WHERE challenge_id = ?",
            (challenge["challenge_id"],),
        ).fetchone()
        self.assertIsNone(row["pin"])
        self.assertEqual(self.claim(self.registry.create_pairing_challenge()).device_id, self.device_id)

    def test_successful_and_expired_pairing_pins_are_removed_from_storage(self):
        expired = self.registry.create_pairing_challenge(ttl_seconds=15)
        self.now[0] += 16
        fresh = self.registry.create_pairing_challenge()
        self.claim(fresh)
        for challenge in (expired, fresh):
            row = self.registry._db.execute(
                "SELECT pin FROM pairing_challenges WHERE challenge_id = ?",
                (challenge["challenge_id"],),
            ).fetchone()
            self.assertIsNone(row["pin"])

    def test_non_ascii_pin_is_a_counted_pairing_failure(self):
        challenge = self.registry.create_pairing_challenge()
        with self.assertRaisesRegex(PairingError, "incorrect"):
            self.claim({**challenge, "pin": "１２３４５６"})
        row = self.registry._db.execute(
            "SELECT failed_attempts FROM pairing_challenges WHERE challenge_id = ?",
            (challenge["challenge_id"],),
        ).fetchone()
        self.assertEqual(row["failed_attempts"], 1)

    def test_device_id_cannot_impersonate_another_public_key(self):
        challenge = self.registry.create_pairing_challenge()
        bad_id = "phone-" + "0" * 32
        material = pairing_signing_bytes(
            self.registry.host_id,
            challenge["challenge_id"],
            bad_id,
            "connection-nonce",
            self.public_b64,
            "My phone",
        )
        with self.assertRaisesRegex(PairingError, "does not match"):
            self.registry.claim_pairing(
                {
                    "challenge_id": challenge["challenge_id"],
                    "device_id": bad_id,
                    "public_key": self.public_b64,
                    "device_name": "My phone",
                    "signature": b64(
                        self.phone_key.sign(material, ec.ECDSA(hashes.SHA256()))
                    ),
                },
                connection_nonce="connection-nonce",
            )

    def test_signature_tampering_is_rejected(self):
        challenge = self.registry.create_pairing_challenge()
        material = pairing_signing_bytes(
            self.registry.host_id,
            challenge["challenge_id"],
            self.device_id,
            "connection-nonce",
            self.public_b64,
            "My phone",
        )
        signature = bytearray(
            self.phone_key.sign(material, ec.ECDSA(hashes.SHA256()))
        )
        signature[-1] ^= 1
        with self.assertRaises(AuthenticationError):
            self.registry.claim_pairing(
                {
                    "challenge_id": challenge["challenge_id"],
                    "device_id": self.device_id,
                    "public_key": self.public_b64,
                    "device_name": "My phone",
                    "signature": b64(bytes(signature)),
                },
                connection_nonce="connection-nonce",
            )

    def test_authentication_counter_is_persistent_and_replay_safe(self):
        self.pair()
        authenticated = self.auth(counter=7)
        self.assertEqual(authenticated.counter, 7)
        with self.assertRaisesRegex(AuthenticationError, "replayed or stale"):
            self.auth(counter=7)

        # Reopen the SQLite registry: the anti-replay boundary survives restart.
        self.registry.close()
        self.registry = DeviceIdentityRegistry(
            self.temp.name, clock=lambda: self.now[0]
        )
        with self.assertRaisesRegex(AuthenticationError, "replayed or stale"):
            self.auth(counter=6)
        self.assertEqual(self.auth(counter=8).counter, 8)

    def test_signed_envelope_binds_type_task_data_nonce_and_counter(self):
        self.pair()
        self.auth(1)
        data = {"task_id": "t-1", "goal": "safe research"}
        self.assertEqual(self.envelope(2, data).device_id, self.device_id)

        # A signature for different content cannot authorize modified content.
        material = envelope_signing_bytes(
            "device", self.device_id, "connection-nonce", 3,
            "task.submit", "t-1", data,
        )
        signature = b64(self.phone_key.sign(material, ec.ECDSA(hashes.SHA256())))
        with self.assertRaises(AuthenticationError):
            self.registry.verify_envelope(
                device_id=self.device_id,
                connection_nonce="connection-nonce",
                counter=3,
                message_type="task.cancel",
                task_id="t-1",
                data=data,
                signature=signature,
                required_scope="tasks",
            )

    def test_revocation_and_scope_checks_are_enforced(self):
        self.pair(scopes=("tasks",))
        self.auth(1)
        data = {"task_id": "t-1"}
        material = envelope_signing_bytes(
            "device", self.device_id, "connection-nonce", 2,
            "webrtc_offer", "t-1", data,
        )
        with self.assertRaises(ScopeDenied):
            self.registry.verify_envelope(
                device_id=self.device_id,
                connection_nonce="connection-nonce",
                counter=2,
                message_type="webrtc_offer",
                task_id="t-1",
                data=data,
                signature=b64(self.phone_key.sign(material, ec.ECDSA(hashes.SHA256()))),
                required_scope="screen",
            )
        self.assertTrue(self.registry.revoke(self.device_id))
        with self.assertRaisesRegex(AuthenticationError, "revoked"):
            self.auth(3)


if __name__ == "__main__":
    unittest.main()
