"""Run offline workflow regressions and emit machine-readable measured results.

Latency is test runtime, not a live-provider benchmark. No API calls/cost estimates.
"""
import io
import json
import time
import unittest
from contextlib import redirect_stdout

import test_agent_workflows


if __name__ == "__main__":
    started = time.perf_counter()
    suite = unittest.defaultTestLoader.loadTestsFromModule(test_agent_workflows)
    with redirect_stdout(io.StringIO()):
        report = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
    print(json.dumps({"mode": "offline_scripted", "cases": report.testsRun,
                      "passed": report.testsRun - len(report.failures) - len(report.errors),
                      "failed": len(report.failures) + len(report.errors),
                      "elapsed_ms": round((time.perf_counter() - started) * 1000),
                      "live_model_cost": None,
                      "failures": [str(case) for case, _ in report.failures + report.errors]}))
    raise SystemExit(not report.wasSuccessful())
