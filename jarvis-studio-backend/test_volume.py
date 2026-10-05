"""set_volume works on both pycaw APIs: the new AudioDevice wrapper
(.EndpointVolume, pycaw >= 20251023) and the old raw COM device (.Activate)."""

import sys
import types
import unittest
from unittest import mock

from actions import skills


class _Endpoint:
    def __init__(self):
        self.level = None

    def SetMasterVolumeLevelScalar(self, level, _ctx):
        self.level = level


def _fake_pycaw(speakers):
    pycaw_pkg = types.ModuleType("pycaw")
    pycaw_mod = types.ModuleType("pycaw.pycaw")
    pycaw_mod.AudioUtilities = types.SimpleNamespace(GetSpeakers=lambda: speakers)
    pycaw_mod.IAudioEndpointVolume = types.SimpleNamespace(_iid_="iid")
    comtypes = types.ModuleType("comtypes")
    comtypes.CLSCTX_ALL = 23
    return {"pycaw": pycaw_pkg, "pycaw.pycaw": pycaw_mod, "comtypes": comtypes}


class VolumeTests(unittest.TestCase):
    def test_new_pycaw_endpoint_volume(self):
        ep = _Endpoint()
        speakers = types.SimpleNamespace(EndpointVolume=ep)      # no .Activate at all
        with mock.patch.dict(sys.modules, _fake_pycaw(speakers)):
            self.assertEqual(skills.set_volume(35), (True, "Volume set to 35%."))
        self.assertAlmostEqual(ep.level, 0.35)

    def test_old_pycaw_activate(self):
        ep = _Endpoint()
        speakers = types.SimpleNamespace(Activate=lambda *_a: "iface")
        with mock.patch.dict(sys.modules, _fake_pycaw(speakers)), \
                mock.patch("ctypes.POINTER", return_value=object), \
                mock.patch("ctypes.cast", return_value=ep):
            self.assertTrue(skills.set_volume("150")[0])          # clamped to 100
        self.assertEqual(ep.level, 1.0)


if __name__ == "__main__":
    unittest.main()
