"""App counters include WebViews without using whole-system usage as app usage."""
import os
from types import SimpleNamespace
import unittest
from unittest import mock

import telemetry as t


class AppTelemetryTests(unittest.TestCase):
    def setUp(self):
        for key, value in {"_app_root": None, "_app_processes": [], "_app_refresh_at": 0,
                           "_app_previous_cpu": {}, "_app_previous_at": 0}.items():
            patch = mock.patch.object(t, key, value)
            patch.start()
            self.addCleanup(patch.stop)

    def process(self, pid, memory, totals):
        p = mock.Mock(pid=pid)
        p.is_running.return_value = True
        p.memory_info.return_value = SimpleNamespace(private=memory * 2**20, rss=memory * 2**20)
        p.cpu_times.side_effect = [SimpleNamespace(user=x, system=0) for x in totals]
        return p

    def test_normalizes_tree_cpu_and_refreshes_children_infrequently(self):
        root = self.process(100, 10, [10, 12, 13])
        child = self.process(101, 20, [20, 22, 23])
        root.children.return_value = [child]
        with mock.patch.dict(os.environ, {"JARVIS_APP_ROOT_PID": "100"}), \
             mock.patch.object(t.psutil, "Process", return_value=root), \
             mock.patch.object(t.psutil, "cpu_count", return_value=8), \
             mock.patch.object(t.time, "monotonic", side_effect=[100, 102, 111]):
            first, second, _ = t.get_app_stats(), t.get_app_stats(), t.get_app_stats()
        self.assertEqual(first["cpu"], 0)
        self.assertEqual(second["cpu"], 25)
        self.assertEqual(second["privateMiB"], 30)
        self.assertEqual(second["processes"], 2)
        self.assertEqual(root.children.call_count, 2)

    def test_exited_child_is_omitted_without_breaking_telemetry(self):
        root = self.process(os.getpid(), 10, [10])
        child = self.process(101, 20, [20])
        child.memory_info.side_effect = t.psutil.NoSuchProcess(101)
        root.children.return_value = [child]
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(t.psutil, "Process", return_value=root):
            result = t.get_app_stats()
        self.assertEqual(result["privateMiB"], 10)
        self.assertEqual(result["scope"], "backend")

    def test_optional_dependency_absence_is_safe(self):
        with mock.patch.object(t, "psutil", None):
            self.assertIsNone(t.get_app_stats())


if __name__ == "__main__":
    unittest.main()
