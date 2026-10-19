"""Wake-on-LAN (actions/wol.py) and waking the TV with it (actions/tv.py `wake`): the
packet, where it goes, which of this PC's adapters a phone is told about, and that the
TV is only reported awake once it answers again."""
import socket
import unittest
from types import SimpleNamespace as NS
from unittest import mock

from actions import tv, wol


class FakeSocket:
    sent = []
    fail = False

    def __init__(self, *_a):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def setsockopt(self, *_a):
        pass

    def sendto(self, data, addr):
        if FakeSocket.fail:
            raise OSError("Network is unreachable")
        FakeSocket.sent.append((data, addr))


class PacketTests(unittest.TestCase):
    def setUp(self):
        FakeSocket.sent, FakeSocket.fail = [], False

    def test_macs_in_any_spelling(self):
        self.assertEqual(wol.norm_mac("AA-BB-CC-DD-EE-FF"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(wol.norm_mac("aabbccddeeff"), "aa:bb:cc:dd:ee:ff")
        self.assertEqual(wol.norm_mac("ff:ff:ff:ff:ff:ff"), "")
        self.assertEqual(wol.norm_mac("hello"), "")

    def test_the_magic_packet(self):
        p = wol.magic_packet("01:02:03:04:05:06")
        self.assertEqual(p, b"\xff" * 6 + bytes([1, 2, 3, 4, 5, 6]) * 16)
        with self.assertRaises(ValueError):
            wol.magic_packet("nope")

    def test_sent_to_the_broadcast_and_the_devices_subnet(self):
        self.assertEqual(wol.targets("192.168.1.20"), ["255.255.255.255", "192.168.1.255"])
        self.assertEqual(wol.targets("100.101.1.2"), ["255.255.255.255"])
        ok, msg = wol.send("01:02:03:04:05:06", "192.168.1.20", sock_factory=FakeSocket)
        self.assertTrue(ok, msg)
        self.assertEqual({a for _, a in FakeSocket.sent},
                         {(h, p) for h in ("255.255.255.255", "192.168.1.255") for p in (9, 7)})
        self.assertEqual(len(FakeSocket.sent), 12)

    def test_nothing_sent_is_not_success(self):
        FakeSocket.fail = True
        ok, msg = wol.send("01:02:03:04:05:06", sock_factory=FakeSocket)
        self.assertFalse(ok)
        self.assertIn("Network is unreachable", msg)

    def test_the_tvs_connected_interface(self):
        link = ("3: wlan0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc mq "
                "link/ether 11:22:33:44:55:66 brd ff:ff:ff:ff:ff:ff\n"
                "4: eth0: <NO-CARRIER,BROADCAST,MULTICAST,UP> mtu 1500 "
                "link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff")
        self.assertEqual(wol.mac_from_ip_link(link), "11:22:33:44:55:66")
        self.assertEqual(wol.mac_from_ip_link(link.replace("NO-CARRIER,", "LOWER_UP,")),
                         "aa:bb:cc:dd:ee:ff")

    def test_only_real_adapters_on_the_home_network_wired_first(self):
        AF_LINK = -1
        addr = lambda fam, a: NS(family=fam, address=a)
        fake = NS(
            AF_LINK=AF_LINK,
            net_if_stats=lambda: {n: NS(isup=True) for n in
                                  ("Wi-Fi", "Ethernet", "vEthernet (WSL)", "Tailscale")},
            net_if_addrs=lambda: {
                "Wi-Fi": [addr(AF_LINK, "11-22-33-44-55-66"), addr(socket.AF_INET, "192.168.1.5")],
                "vEthernet (WSL)": [addr(AF_LINK, "00-15-5D-00-00-01"),
                                    addr(socket.AF_INET, "172.20.0.1")],
                "Tailscale": [addr(socket.AF_INET, "100.101.1.3")],
                "Ethernet": [addr(AF_LINK, "AA-BB-CC-DD-EE-FF"), addr(socket.AF_INET, "192.168.1.6")],
            })
        self.assertEqual(wol.local_macs(fake), [
            {"name": "Ethernet", "mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.1.6"},
            {"name": "Wi-Fi", "mac": "11:22:33:44:55:66", "ip": "192.168.1.5"},
        ])


class TvWakeTests(unittest.TestCase):
    CFG = {"host": "192.168.1.20", "model": "KD-55X8500D", "mac": "aa:bb:cc:dd:ee:ff"}
    DOWN = (False, "", "I can't reach the TV at 192.168.1.20:5555. Check it's on…")

    def setUp(self):
        self.t = 0.0
        self.sent = []
        p = mock.patch.object(wol, "send", lambda mac, near: (self.sent.append((mac, near)) or
                                                              (True, "sent")))
        p.start()
        self.addCleanup(p.stop)

    def wake(self, replies):
        def sh(_host, _cmd):
            return replies.pop(0) if replies else (True, "ok", "")

        def wait(s):
            self.t += s
        with mock.patch.object(tv, "shell", sh):
            return tv.wake(self.CFG, wait=wait, now=lambda: self.t)

    def test_a_tv_that_answers_is_left_alone(self):
        self.assertIsNone(self.wake([(True, "ok", "")]))
        self.assertEqual(self.sent, [])

    def test_an_unreachable_tv_is_woken_and_only_then_reported_awake(self):
        r = self.wake([self.DOWN, self.DOWN, (True, "ok", "")])
        self.assertEqual(r, (True, "Woke the TV over the network."))
        self.assertEqual(self.sent, [("aa:bb:cc:dd:ee:ff", "192.168.1.20")])

    def test_the_packet_goes_to_the_home_address_not_the_tailscale_one(self):
        self.CFG = {**self.CFG, "host": "100.101.1.2, 192.168.1.20"}
        self.wake([self.DOWN, (True, "ok", "")])
        self.assertEqual(self.sent, [("aa:bb:cc:dd:ee:ff", "192.168.1.20")])

    def test_one_that_never_answers_says_what_to_turn_on(self):
        ok, msg = self.wake([self.DOWN] * 100)
        self.assertFalse(ok)
        self.assertIn("Remote start", msg)

    def test_refused_or_on_another_network_sends_nothing(self):
        refused = (False, "", "The TV didn't allow this PC. Accept \"Allow USB debugging?\"")
        self.assertIsNone(self.wake([refused]))
        other = (False, "", "I can't reach the TV at 192.168.0.20:5555. This PC is on "
                            "192.168.1.5 and the TV is at 192.168.0.20: different networks…")
        ok, msg = self.wake([other])
        self.assertFalse(ok)
        self.assertIn("different networks", msg)
        self.assertEqual(self.sent, [])

    def test_power_on_says_it_woke_the_tv(self):
        with mock.patch.object(tv, "config", lambda: dict(self.CFG)), \
             mock.patch.object(tv, "wake", lambda cfg: (True, "Woke the TV over the network.")), \
             mock.patch.object(tv, "run_tv", lambda *a: (True, "The TV is already on.")):
            r = tv.run({"type": "tv", "do": "power_on"})
        self.assertEqual(r["message"], "Turned the TV on (woke it over the network).")

    def test_the_mac_is_learned_while_the_tv_answers(self):
        saved = []
        cfg = {**self.CFG, "mac": ""}
        with mock.patch.object(tv, "config", lambda: dict(cfg)), \
             mock.patch.object(tv, "run_tv", lambda *a: (True, "Paused.")), \
             mock.patch.object(tv, "_read_mac", lambda host: "11:22:33:44:55:66"), \
             mock.patch.object(tv, "_save", saved.append):
            tv.run({"type": "tv", "do": "pause"})
        self.assertEqual(saved, [{**cfg, "mac": "11:22:33:44:55:66"}])


class ReadyInfoTests(unittest.TestCase):
    def test_extras_never_override_the_handshake_or_break_it(self):
        from server import websocket_server as ws
        self.addCleanup(ws.set_ready_info, None)
        ws.set_ready_info(lambda: {"wake": [{"mac": "aa:bb:cc:dd:ee:ff"}], "device_id": "x"})
        self.assertEqual(ws._ready_extras(), {"wake": [{"mac": "aa:bb:cc:dd:ee:ff"}]})
        ws.set_ready_info(lambda: 1 / 0)
        self.assertEqual(ws._ready_extras(), {})


if __name__ == "__main__":
    unittest.main()
