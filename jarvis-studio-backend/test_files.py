"""The autopilot's direct file operations stay inside the user's folders and never
overwrite — checked against a throwaway home folder."""

import os
import tempfile
import time
import unittest
from pathlib import Path

from actions import files


class FilesTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name).resolve()
        self._real_home = files._home
        files._home = lambda: self.home
        (self.home / "Desktop").mkdir()
        (self.home / "Desktop" / "a.txt").write_text("A")
        (self.home / "Desktop" / "b.pdf").write_text("B")
        (self.home / "Desktop" / "c.pdf").write_text("C")
        (self.home / "Docs").mkdir()

    def tearDown(self):
        files._home = self._real_home
        self._tmp.cleanup()

    def p(self, *parts):
        return str(self.home.joinpath(*parts))

    def test_rename_takes_the_full_new_name_and_never_clobbers(self):
        ok, msg = files.rename(self.p("Desktop", "a.txt"), "final.txt")
        self.assertTrue(ok, msg)
        self.assertTrue((self.home / "Desktop" / "final.txt").exists())
        self.assertFalse(files.rename(self.p("Desktop", "b.pdf"), "c.pdf")[0])
        self.assertEqual((self.home / "Desktop" / "c.pdf").read_text(), "C")
        self.assertFalse(files.rename(self.p("Desktop", "b.pdf"), r"..\x.pdf")[0])

    def test_a_pattern_moves_every_match_or_nothing(self):
        (self.home / "Docs" / "c.pdf").write_text("old C")
        ok, _ = files.transfer(self.p("Desktop", "*.pdf"), self.p("Docs"))
        self.assertFalse(ok, "one clash must stop the whole move")
        self.assertTrue((self.home / "Desktop" / "b.pdf").exists())
        self.assertEqual((self.home / "Docs" / "c.pdf").read_text(), "old C")
        (self.home / "Docs" / "c.pdf").unlink()
        ok, msg = files.transfer(self.p("Desktop", "*.pdf"), self.p("Docs"))
        self.assertTrue(ok, msg)
        self.assertEqual(sorted(x.name for x in (self.home / "Docs").iterdir()),
                         ["b.pdf", "c.pdf"])

    def test_copy_keeps_the_original(self):
        ok, _ = files.transfer(self.p("Desktop", "a.txt"), self.p("Docs"), copy=True)
        self.assertTrue(ok)
        self.assertTrue((self.home / "Desktop" / "a.txt").exists())
        self.assertTrue((self.home / "Docs" / "a.txt").exists())

    def test_new_folders_and_files_never_replace_anything(self):
        self.assertTrue(files.make_folder(self.p("Desktop", "New"))[0])
        self.assertFalse(files.make_folder(self.p("Desktop", "New"))[0])
        self.assertTrue(files.write_text(self.p("Desktop", "n.txt"), "hi")[0])
        self.assertFalse(files.write_text(self.p("Desktop", "n.txt"), "bye")[0])
        self.assertEqual((self.home / "Desktop" / "n.txt").read_text(), "hi")

    def test_nothing_outside_the_users_own_folders(self):
        outside = tempfile.gettempdir()
        self.assertFalse(files.make_folder(os.path.join(outside, "jarvis_never"))[0])
        self.assertFalse(files.make_folder(self.p("AppData", "Roaming", "x"))[0])
        self.assertFalse(files.transfer(self.p("Desktop", "a.txt"), r"C:\Windows")[0])
        self.assertFalse(files.make_folder("relative\\folder")[0])

    def test_listing_sorts_newest_first(self):
        now = time.time()
        os.utime(self.home / "Desktop" / "c.pdf", (now + 50, now + 50))
        ok, _msg, listing = files.list_folder(self.p("Desktop"), sort="newest")
        self.assertTrue(ok)
        self.assertEqual(listing.splitlines()[1].split()[0], "c.pdf")
        ok, _msg, listing = files.list_folder(self.p("Desktop"), pattern="*.pdf")
        self.assertIn("2 items", listing)
        self.assertIn("(2 .pdf, 1 .txt)", files.list_folder(self.p("Desktop"))[2])

    def test_find_searches_subfolders_by_name_and_skips_junk(self):
        deep = self.home / "Docs" / "2025" / "Taxes"
        deep.mkdir(parents=True)
        (deep / "Tax_Return_2025.pdf").write_text("T")
        for junk in ("node_modules", ".hidden"):
            (self.home / junk).mkdir()
            (self.home / junk / "tax return.txt").write_text("x")
        real = files.app_launcher.real_known_folders, files._index_find
        files.app_launcher.real_known_folders = lambda: {
            "Desktop": self.p("Desktop"), "Downloads": self.p("None"), "Documents": self.p("Docs")}
        files._index_find = lambda *_a: None          # the walk; the index is Windows'
        try:
            ok, msg, listing = files.find("tax return")
            self.assertTrue(files.find("*.pdf", self.p("Desktop"))[2].startswith("2 matches"))
            # a model's starred words are still words: '_' in the name matches ' '
            self.assertIn("Tax_Return_2025.pdf", files.find("tax return*")[2])
            self.assertIn("Tax_Return_2025.pdf", files.find("*return 2025*")[2])
        finally:
            files.app_launcher.real_known_folders, files._index_find = real
        self.assertTrue(ok, msg)
        self.assertIn("Tax_Return_2025.pdf", listing)
        self.assertIn("1 match ", listing)


class ReadOnlyListingTests(unittest.TestCase):
    """A refused read-only listing of the user's own folder hands the question to
    the autopilot instead of ending at "add it in Settings"; anything else stays
    refused."""

    def test_own_folders_go_to_the_autopilot_others_stay_refused(self):
        from actions import fs_access, run_action
        tmp = tempfile.TemporaryDirectory()
        home = Path(tmp.name).resolve()
        (home / "Downloads").mkdir()
        real = files._home, fs_access.get_allowed()
        files._home = lambda: home
        fs_access.set_allowed([])
        try:
            res = run_action({"type": "list_dir", "path": str(home / "Downloads")})
            self.assertTrue(res["ok"] and res.get("autopilot_fallback"), res)
            res = run_action({"type": "list_dir", "path": r"C:\Windows"})
            self.assertFalse(res["ok"])
            self.assertNotIn("autopilot_fallback", res)
        finally:
            files._home = real[0]
            fs_access.set_allowed(real[1])
            tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
