"""Daily source launches reuse a current desktop build, never a stale GUI."""
import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

spec = importlib.util.spec_from_file_location("jarvis_source_launcher", Path(__file__).resolve().parents[1] / "start.py")
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class SourceLauncherTests(unittest.TestCase):
    def test_rebuilds_once_after_source_change_then_reuses_the_desktop_binary(self):
        with tempfile.TemporaryDirectory() as folder:
            gui = Path(folder)
            source = gui / "src/App.jsx"
            source.parent.mkdir()
            source.write_text("new UI", encoding="utf-8")
            binary = gui / "src-tauri/target/release/jarvis-studio-gui.exe"
            binary.parent.mkdir(parents=True)
            binary.write_bytes(b"old UI")
            os.utime(binary, (1000, 1000))
            os.utime(source, (2000, 2000))

            def build(*args, **kwargs):
                binary.write_bytes(b"current UI")
                os.utime(binary, (3000, 3000))
                return mock.Mock(returncode=0)

            with mock.patch.object(launcher, "GUI_DIR", gui), \
                 mock.patch.object(launcher, "WIN", True), \
                 mock.patch.object(launcher.shutil, "which", return_value="npm.cmd"), \
                 mock.patch.object(launcher.subprocess, "run", side_effect=build) as run:
                self.assertEqual(launcher._release_gui(), binary)
                self.assertEqual(launcher._release_gui(), binary)
            run.assert_called_once_with(["npm.cmd", "run", "tauri", "build", "--", "--no-bundle"], cwd=str(gui))

    def test_failed_build_does_not_launch_an_old_binary(self):
        with tempfile.TemporaryDirectory() as folder, \
             mock.patch.object(launcher, "GUI_DIR", Path(folder)), \
             mock.patch.object(launcher.subprocess, "run", return_value=mock.Mock(returncode=1)):
            with self.assertRaisesRegex(SystemExit, "Desktop build failed"):
                launcher._release_gui()


if __name__ == "__main__":
    unittest.main()
