"""Reaching the TV from another router's network: several addresses for one TV (its
home IP, its Tailscale IP), the one that answered first next time, and a plain reason
when this PC and the TV are on different subnets (actions/tv.py)."""
import sys
import types
import unittest
from unittest import mock

from actions import tv


class FakeDev:
    def __init__(self, target):
        self.target = target

    def shell(self, command, **kw):
        return f"ran on {self.target[0]}".encode()


class ReachTests(unittest.TestCase):
    def setUp(self):
        tv._last_good = ""
        tv._drop()
        self.connects = []
        # adb_shell isn't installed in CI; _on_tv only checks it imports.
        p = mock.patch.dict(sys.modules, {"adb_shell": types.ModuleType("adb_shell")})
        p.start()
        self.addCleanup(p.stop)

    def _connect(self, reachable, refuse=()):
        def connection(target):
            self.connects.append(target[0])
            if target[0] in refuse:
                raise RuntimeError("device unauthorized")
            if target[0] not in reachable:
                raise TimeoutError("timed out")
            return FakeDev(target)
        return mock.patch.object(tv, "_connection", connection)

    def test_a_list_of_addresses_is_one_tv(self):
        self.assertEqual(tv._targets("192.168.1.20, 100.101.1.2:5555 ; nonsense!"),
                         [("192.168.1.20", 5555), ("100.101.1.2", 5555)])

    def test_falls_through_to_the_next_address_and_prefers_it_after(self):
        with self._connect({"100.101.1.2"}), \
             mock.patch.object(tv, "_same_subnet_hint", lambda t: ""):
            ok, out, _ = tv.shell("192.168.1.20, 100.101.1.2", "echo hi")
            self.assertEqual((ok, out), (True, "ran on 100.101.1.2"))
            self.connects.clear()
            tv._drop()
            tv.shell("192.168.1.20, 100.101.1.2", "echo hi")
        self.assertEqual(self.connects, ["100.101.1.2"])     # straight to what worked

    def test_a_tv_that_refused_us_is_not_tried_another_way(self):
        with self._connect({"192.168.1.20", "100.101.1.2"}, refuse={"192.168.1.20"}):
            ok, _, why = tv.shell("192.168.1.20, 100.101.1.2", "echo hi")
        self.assertFalse(ok)
        self.assertIn("didn't allow this PC", why)
        self.assertEqual(self.connects, ["192.168.1.20"])

    def test_another_routers_network_is_named_as_the_cause(self):
        class Sock:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def connect(self, addr): pass
            def getsockname(self): return ("192.168.0.105", 50000)

        with self._connect(set()), mock.patch.object(tv.socket, "socket", Sock):
            ok, _, why = tv.shell("192.168.1.20", "echo hi")
        self.assertFalse(ok)
        self.assertIn("This PC is on 192.168.0.105 and the TV is at 192.168.1.20", why)
        self.assertIn("Access Point mode", why)

    def test_same_network_timeout_keeps_the_old_advice(self):
        class Sock:
            def __init__(self, *a): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def connect(self, addr): pass
            def getsockname(self): return ("192.168.1.7", 50000)

        with self._connect(set()), mock.patch.object(tv.socket, "socket", Sock):
            _, _, why = tv.shell("192.168.1.20", "echo hi")
        self.assertIn("Allow USB debugging", why)
        self.assertNotIn("Access Point", why)


if __name__ == "__main__":
    unittest.main()
