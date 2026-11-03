"""Unavailable GPU counters back off, while a valid zero reading stays responsive."""

import unittest
from unittest import mock
import telemetry


class _Finished(BaseException):
    pass


class GpuPollingTests(unittest.TestCase):
    def poll(self, readings):
        delays = []

        def sleep(delay):
            delays.append(delay)
            if len(delays) == len(readings):
                raise _Finished()

        with mock.patch.object(telemetry, "_gpu_kind", "typeperf"), \
             mock.patch.object(telemetry, "_gpu_pct", None), \
             mock.patch.object(telemetry, "_typeperf_gpu", side_effect=readings), \
             mock.patch.object(telemetry.time, "sleep", sleep):
            with self.assertRaises(_Finished):
                telemetry._gpu_poller()
        return delays

    def test_failures_back_off_and_success_resets_the_poll_interval(self):
        self.assertEqual(self.poll([None, None, 42.0, None]), [30.0, 60.0, 15.0, 30.0])

    def test_retries_are_bounded_and_idle_gpu_zero_is_success(self):
        delays = self.poll([None] * 8 + [0.0])
        self.assertTrue(all(delay <= 300 for delay in delays))
        self.assertEqual(delays[-2:], [300.0, 15.0])


if __name__ == "__main__":
    unittest.main()
