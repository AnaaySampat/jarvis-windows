"""Regression tests for the live remote-desktop control path: a WebRTC offer mints
a connection-bound screen session whose id is echoed in the answer, and direct
input only lands under a matching, unexpired, monotonic-seq control lease owned by
the same authenticated device."""

import asyncio
import contextlib
import unittest
from unittest.mock import patch

import main


class RemoteScreenTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._remote_screen_lock = asyncio.Lock()
        main._remote_input_lock = asyncio.Lock()
        main._remote_screen_lease.clear()
        main._remote_control_lease.clear()
        self.sent = []  # (event, payload) captured from send_to_current

    def _base_patches(self, *, client="c1", device="dev1", remote=True):
        async def fake_send_to_current(event, data):
            self.sent.append((event, data))
            return True

        async def fake_emit(event, data):
            return None

        return [
            patch.object(main, "is_current_sender_remote", return_value=remote),
            patch.object(main, "current_client_id", return_value=client),
            patch.object(main, "current_authenticated_device", return_value=device),
            patch.object(main, "send_to_current", fake_send_to_current),
            patch.object(main, "emit", fake_emit),
        ]

    async def test_offer_answers_with_a_connection_bound_session_id(self):
        async def fake_handle_offer(data, emit):
            await emit("webrtc_answer", {"sdp": "x", "type": "answer"})

        with contextlib.ExitStack() as es:
            for p in self._base_patches():
                es.enter_context(p)
            es.enter_context(patch.object(main.webrtc_screen, "is_available", return_value=True))
            es.enter_context(patch.object(main.webrtc_screen, "handle_offer", fake_handle_offer))
            await main.handle_webrtc_offer({"offer": {"sdp": "o", "type": "offer"}})

        answers = [d for e, d in self.sent if e == "webrtc_answer"]
        self.assertEqual(len(answers), 1, "exactly one answer expected")
        sid = answers[0].get("screen_session_id")
        self.assertTrue(sid and sid.startswith("screen-"), "answer must carry a screen_session_id")
        self.assertEqual(main._remote_screen_lease.get("session_id"), sid)
        self.assertTrue(main._remote_screen_lease.get("answered"))

    async def test_reconnected_phone_takes_over_its_screen_but_others_cannot(self):
        # After a network switch the server still holds the phone's dead socket; the
        # same device on a NEW connection must get its screen back, a foreign one not.
        async def fake_handle_offer(data, emit):
            await emit("webrtc_answer", {"sdp": "x", "type": "answer"})

        async def offer(client, device):
            with contextlib.ExitStack() as es:
                for p in self._base_patches(client=client, device=device):
                    es.enter_context(p)
                es.enter_context(patch.object(main.webrtc_screen, "is_available", return_value=True))
                es.enter_context(patch.object(main.webrtc_screen, "handle_offer", fake_handle_offer))
                es.enter_context(patch.object(main.webrtc_screen, "stop", lambda: asyncio.sleep(0)))
                await main.handle_webrtc_offer({"offer": {"sdp": "o", "type": "offer"}})

        await offer("c1", "dev1")
        await offer("c2", "dev1")
        self.assertEqual(main._remote_screen_lease.get("client_id"), "c2")
        self.sent.clear()
        await offer("c3", "intruder")
        self.assertEqual(main._remote_screen_lease.get("client_id"), "c2")
        self.assertTrue(any(e == "webrtc_error" for e, _ in self.sent))

    async def test_arm_then_input_is_lease_seq_and_owner_guarded(self):
        # Pretend the screen session is already up + answered for this connection.
        main._remote_screen_lease.update({
            "client_id": "c1", "device_id": "dev1",
            "session_id": "screen-abc", "answered": True,
        })
        clicks = []

        with contextlib.ExitStack() as es:
            for p in self._base_patches():
                es.enter_context(p)
            es.enter_context(patch.object(main.computer, "arm", lambda m=None: (True, "armed")))
            es.enter_context(patch.object(main.computer, "is_armed", lambda: True))
            es.enter_context(patch.object(main.computer, "disarm", lambda: (True, "")))
            es.enter_context(patch.object(main.computer, "release_inputs", lambda: None))
            es.enter_context(patch.object(
                main.computer, "click_xy",
                lambda x, y, double=False: (clicks.append((x, y)), (True, "clicked"))[1]))

            await main.handle_arm_control({"screen_session_id": "screen-abc", "minutes": 5})
            lease_id = main._remote_control_lease.get("lease_id")
            self.assertTrue(lease_id, "arming must mint a lease id")
            arm_acks = [d for e, d in self.sent
                        if e == "remote_input_ack" and d.get("action") == "arm"]
            self.assertTrue(arm_acks and arm_acks[-1]["ok"] and arm_acks[-1]["lease_id"] == lease_id)

            base = {"action": "click_xy", "x": 100, "y": 200,
                    "screen_session_id": "screen-abc", "lease_id": lease_id}
            # Valid, in-order input lands.
            await main.handle_remote_input({**base, "seq": 1})
            self.assertEqual(clicks, [(100, 200)])
            # Replayed seq is dropped.
            await main.handle_remote_input({**base, "seq": 1})
            self.assertEqual(len(clicks), 1, "replayed seq must be dropped")
            # Wrong lease id is dropped.
            await main.handle_remote_input({**base, "lease_id": "bogus", "seq": 2})
            self.assertEqual(len(clicks), 1, "stale lease id must be rejected")

            # A DIFFERENT device cannot drive this lease.
            with patch.object(main, "current_authenticated_device", return_value="intruder"):
                await main.handle_remote_input({**base, "seq": 3})
            self.assertEqual(len(clicks), 1, "another device must not drive the lease")

            # The rightful owner, next seq, still works.
            await main.handle_remote_input({**base, "seq": 4})
            self.assertEqual(clicks, [(100, 200), (100, 200)])

    async def test_input_without_a_lease_is_refused(self):
        main._remote_screen_lease.update({
            "client_id": "c1", "device_id": "dev1",
            "session_id": "screen-abc", "answered": True,
        })
        clicks = []
        with contextlib.ExitStack() as es:
            for p in self._base_patches():
                es.enter_context(p)
            es.enter_context(patch.object(
                main.computer, "click_xy",
                lambda x, y, double=False: (clicks.append((x, y)), (True, "clicked"))[1]))
            await main.handle_remote_input({
                "action": "click_xy", "x": 1, "y": 1,
                "screen_session_id": "screen-abc", "lease_id": "anything", "seq": 1})
        self.assertEqual(clicks, [], "no input may land before control is armed")
        nacks = [d for e, d in self.sent if e == "remote_input_ack"]
        self.assertTrue(nacks and nacks[-1]["ok"] is False)


if __name__ == "__main__":
    unittest.main()
