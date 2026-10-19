"""The once-a-day "a new version is available" check (updates.py)."""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import updates


class UpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(updates, "_stamp_path",
                              lambda: Path(self.tmp.name) / "update_check.json")
        p.start()
        self.addCleanup(p.stop)

    def test_version_order(self):
        self.assertTrue(updates.is_newer("v0.3.0", "0.2.0"))
        self.assertTrue(updates.is_newer("v0.2.10", "0.2.9"))
        self.assertFalse(updates.is_newer("v0.2.0", "0.2.0"))
        self.assertFalse(updates.is_newer("v0.1.9", "0.2.0"))

    async def test_checks_at_most_once_a_day(self):
        calls = []

        class Client:
            def __init__(self, **kw): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **kw):
                calls.append(url)
                return mock.Mock(status_code=200, json=lambda: {
                    "tag_name": "v0.3.0", "html_url": "https://github.com/x/releases/v0.3.0"})

        with mock.patch("httpx.AsyncClient", Client):
            found = await updates.check("0.2.0", now=1_000_000.0)
            again = await updates.check("0.2.0", now=1_000_000.0 + 3600)
            later = await updates.check("0.3.0", now=1_000_000.0 + 90_000)
        self.assertEqual(found, {"version": "0.3.0", "url": "https://github.com/x/releases/v0.3.0"})
        self.assertIsNone(again)          # within the day: no request at all
        self.assertIsNone(later)          # up to date
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
