import json
import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server import websocket_server as ws


class TailscaleTransportTests(unittest.TestCase):
    PEER = "100.101.102.103"

    def setUp(self):
        ws._clear_tailscale_peer_cache()

    def tearDown(self):
        ws._clear_tailscale_peer_cache()

    @staticmethod
    def _result(payload, *, returncode=0):
        stdout = payload if isinstance(payload, str) else json.dumps(payload)
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="")

    @classmethod
    def _user_peer(cls, address=None):
        return {
            "Node": {
                "ID": 42,
                "StableID": "node-stable-42",
                "Addresses": [f"{address or cls.PEER}/32"],
            },
            "UserProfile": {"ID": 7, "LoginName": "person@example.test"},
        }

    def test_non_tailscale_range_is_rejected_without_running_cli(self):
        with patch.object(ws.subprocess, "run") as run:
            self.assertFalse(ws._is_tailscale_peer("10.0.0.8"))
            self.assertFalse(ws._is_tailscale_peer("not-an-ip"))
        run.assert_not_called()

    def test_cgnat_range_alone_is_not_trusted_when_cli_is_missing(self):
        with patch.object(ws.subprocess, "run", side_effect=FileNotFoundError):
            self.assertFalse(ws._is_tailscale_peer(self.PEER))

    def test_verified_user_peer_is_accepted_with_bounded_shell_free_whois(self):
        result = self._result(self._user_peer())
        with patch.object(ws.subprocess, "run", return_value=result) as run:
            self.assertTrue(ws._is_tailscale_peer(self.PEER))

        args, kwargs = run.call_args
        self.assertEqual(
            args[0], ["tailscale", "whois", "--json", self.PEER]
        )
        self.assertEqual(kwargs["timeout"], ws._TAILSCALE_WHOIS_TIMEOUT_S)
        self.assertTrue(kwargs["capture_output"])
        self.assertNotIn("shell", kwargs)

    def test_verified_tagged_ipv6_peer_is_accepted_without_user_profile(self):
        peer = "fd7a:115c:a1e0::1234"
        payload = {
            "Node": {
                "StableID": "tagged-node",
                "Addresses": [f"{peer}/128"],
                "Tags": ["tag:aura-host"],
            }
        }
        with patch.object(
            ws.subprocess, "run", return_value=self._result(payload)
        ):
            self.assertTrue(ws._is_tailscale_peer(peer))

    def test_whois_address_must_match_exact_tcp_peer(self):
        payload = self._user_peer(address="100.101.102.104")
        with patch.object(
            ws.subprocess, "run", return_value=self._result(payload)
        ):
            self.assertFalse(ws._is_tailscale_peer(self.PEER))

    def test_whois_requires_durable_node_and_user_or_tag_identity(self):
        cases = [
            {
                "Node": {"Addresses": [f"{self.PEER}/32"]},
                "UserProfile": {"ID": 7},
            },
            {
                "Node": {
                    "StableID": "node-id",
                    "Addresses": [f"{self.PEER}/32"],
                }
            },
            {"UserProfile": {"ID": 7}},
        ]
        for payload in cases:
            with self.subTest(payload=payload):
                ws._clear_tailscale_peer_cache()
                with patch.object(
                    ws.subprocess, "run", return_value=self._result(payload)
                ):
                    self.assertFalse(ws._is_tailscale_peer(self.PEER))

    def test_timeout_nonzero_malformed_and_oversized_outputs_fail_closed(self):
        outcomes = [
            subprocess.TimeoutExpired("tailscale", 1.25),
            self._result("", returncode=1),
            self._result("{bad json"),
            self._result(" " * (ws._TAILSCALE_WHOIS_MAX_OUTPUT_BYTES + 1)),
        ]
        for outcome in outcomes:
            with self.subTest(outcome=type(outcome).__name__):
                ws._clear_tailscale_peer_cache()
                effect = outcome if isinstance(outcome, BaseException) else None
                with patch.object(
                    ws.subprocess,
                    "run",
                    side_effect=effect,
                    return_value=None if effect else outcome,
                ):
                    self.assertFalse(ws._is_tailscale_peer(self.PEER))

    def test_positive_result_is_cached_for_short_ttl(self):
        with (
            patch.object(ws.time, "monotonic", side_effect=[100.0, 101.0]),
            patch.object(
                ws.subprocess,
                "run",
                return_value=self._result(self._user_peer()),
            ) as run,
        ):
            self.assertTrue(ws._is_tailscale_peer(self.PEER))
            self.assertTrue(ws._is_tailscale_peer(self.PEER))
        self.assertEqual(run.call_count, 1)

    def test_negative_result_is_cached_but_rechecked_after_expiry(self):
        invalid = self._result("{bad json")
        valid = self._result(self._user_peer())
        with (
            patch.object(ws.time, "monotonic", side_effect=[100.0, 104.0, 106.0]),
            patch.object(ws.subprocess, "run", side_effect=[invalid, valid]) as run,
        ):
            self.assertFalse(ws._is_tailscale_peer(self.PEER))
            self.assertFalse(ws._is_tailscale_peer(self.PEER))
            self.assertTrue(ws._is_tailscale_peer(self.PEER))
        self.assertEqual(run.call_count, 2)


if __name__ == "__main__":
    unittest.main()
