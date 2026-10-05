import base64
import tempfile
import unittest

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
