import tempfile
import unittest

from autonomy.task_passkey import TaskPasskeyStore


class TaskPasskeyTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name
        self.now = 1000.0
        self.store = TaskPasskeyStore(directory.name, clock=lambda: self.now)

    def test_unset_or_unreadable_storage_never_authorizes_a_task(self):
        self.assertEqual(self.store.verify("test-only-passkey", "phone").error, "passkey_not_configured")
        self.store.path.write_bytes(b"invalid protected record")
        broken = TaskPasskeyStore(self.directory)
        self.assertFalse(broken.verify("test-only-passkey", "phone").accepted)
        broken.set("replacement-test-passkey")
        self.assertTrue(broken.verify("replacement-test-passkey", "phone").accepted)

    def test_set_restart_and_edit_without_persisting_plaintext(self):
        self.store.set("first-test-only-passkey")
        first = self.store.verify("first-test-only-passkey", "phone")
        self.assertTrue(first.accepted)
        self.assertNotIn(b"first-test-only-passkey", self.store.path.read_bytes())
        self.assertEqual(self.store.state(), {"configured": True, "storage_error": False})
        restarted = TaskPasskeyStore(self.directory)
        self.assertTrue(restarted.verify("first-test-only-passkey", "phone").accepted)
        restarted.set("second-test-only-passkey")
        self.assertFalse(restarted.verify("first-test-only-passkey", "phone").accepted)
        self.assertTrue(restarted.verify("second-test-only-passkey", "phone").accepted)
        self.assertFalse(restarted.is_current(first.revision))

    def test_wrong_guesses_are_limited_and_success_is_not_cached(self):
        self.store.set("test-only-passkey")
        for _ in range(5):
            self.assertEqual(self.store.verify("incorrect-test-key", "phone").error, "passkey_incorrect")
        self.assertEqual(self.store.verify("test-only-passkey", "phone").error, "passkey_rate_limited")
        self.now += 61
        self.assertTrue(self.store.verify("test-only-passkey", "phone").accepted)
        self.assertFalse(self.store.verify(None, "phone").accepted)
        self.assertFalse(self.store.verify("incorrect-test-key", "phone").accepted)

    def test_validation_does_not_replace_a_working_passkey(self):
        self.store.set("test-only-passkey")
        for invalid in (None, "", "short", "x" * 129, "\ud800" * 8):
            with self.assertRaises(ValueError):
                self.store.set(invalid)
        self.assertTrue(self.store.verify("test-only-passkey", "phone").accepted)

    def test_whitespace_and_unicode_are_checked_exactly(self):
        self.store.set("  test-only-🔐  ")
        self.assertFalse(self.store.verify("test-only-🔐", "phone").accepted)
        self.assertTrue(self.store.verify("  test-only-🔐  ", "phone").accepted)
