""""Send this to the TV" (actions/now_playing.py + tv.cast): reading a YouTube/Netflix
link, finding the browser tab that's playing, and only reporting it moved once the TV
plays it. Windows' media controls and UI Automation are faked."""
import datetime as dt
import unittest
from types import SimpleNamespace as NS

from actions import now_playing as np
from actions import tv


class LinkTests(unittest.TestCase):
    def test_youtube_links_in_every_shape(self):
        for url, t in (("https://www.youtube.com/watch?v=dQw4w9WgXcQ", 0),
                       ("youtube.com/watch?v=dQw4w9WgXcQ&t=90", 90),
                       ("https://youtu.be/dQw4w9WgXcQ?t=1m30s", 90),
                       ("m.youtube.com/shorts/dQw4w9WgXcQ", 0),
                       ("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=x", 0)):
            self.assertEqual(np.parse_link(url), {"service": "youtube", "id": "dQw4w9WgXcQ", "t": t}, url)

    def test_netflix_links(self):
        self.assertEqual(np.parse_link("netflix.com/watch/80077209?trackId=1"),
                         {"service": "netflix", "kind": "watch", "id": "80077209"})
        self.assertEqual(np.parse_link("https://www.netflix.com/browse?jbv=80057281"),
                         {"service": "netflix", "kind": "title", "id": "80057281"})

    def test_anything_else_is_not_a_link(self):
        for url in ("", "google.com", "youtube.com/feed/subscriptions",
                    "youtube.com/watch?v=short", "netflix.com/browse", "javascript:alert(1)"):
            self.assertIsNone(np.parse_link(url), url)

    def test_position_counts_on_while_it_plays(self):
        then = dt.datetime(2026, 10, 19, 12, 0, 0, tzinfo=dt.timezone.utc)
        now = then + dt.timedelta(seconds=30)
        self.assertEqual(np.position_now(100.4, then, True, now), 130)
        self.assertEqual(np.position_now(100.4, then, False, now), 100)


class PickTests(unittest.TestCase):
    WINDOWS = [
        {"title": "Inbox - Gmail - Google Chrome", "url": "mail.google.com"},
        {"title": "(2) Lofi Girl - lofi hip hop radio - YouTube - Google Chrome",
         "url": "youtube.com/watch?v=jfKfPfyJRdk"},
        {"title": "Some other video - YouTube - Google Chrome",
         "url": "youtube.com/watch?v=dQw4w9WgXcQ"},
    ]

    def test_the_tab_named_after_what_plays(self):
        got = np.pick("lofi hip hop radio", self.WINDOWS)
        self.assertEqual(got["link"]["id"], "jfKfPfyJRdk")

    def test_a_youtube_tab_that_doesnt_name_it_is_another_video(self):
        self.assertIsNone(np.pick("Something else entirely", self.WINDOWS[:2]))

    def test_netflix_tabs_are_all_called_netflix(self):
        windows = [{"title": "Netflix - Google Chrome", "url": "netflix.com/watch/80077209"}]
        self.assertEqual(np.pick("Stranger Things", windows)["link"]["id"], "80077209")

    def test_with_no_title_only_a_single_page_will_do(self):
        self.assertEqual(np.pick("", self.WINDOWS[:2])["link"]["id"], "jfKfPfyJRdk")
        self.assertIsNone(np.pick("", self.WINDOWS))


class FakeTv:
    def __init__(self, ok=True):
        self.cast_args, self.ok, self.waited = [], ok, 0

    def cast(self, link, start=0, name=""):
        self.cast_args.append((link, start, name))
        return self.ok, f"Playing “{name}” on the TV."

    def wait(self, s):
        self.waited += s


def fake_np(found, windows, paused=True, media=True):
    log = []
    return NS(parse_link=np.parse_link, pick=np.pick, is_browser=np.is_browser,
              media_available=lambda: media, playing_on_pc=lambda: found,
              browser_windows=lambda: windows,
              pause=lambda f: log.append("pause") or paused), log


class CastTests(unittest.TestCase):
    YT = {"app": "Chrome", "title": "lofi hip hop radio", "artist": "Lofi Girl",
          "playing": True, "position": 754}
    WIN = [{"title": "Lofi Girl - lofi hip hop radio - YouTube - Google Chrome",
            "url": "youtube.com/watch?v=jfKfPfyJRdk"}]

    def test_youtube_moves_at_the_same_point_and_is_paused_only_after(self):
        t = FakeTv()
        fake, log = fake_np(self.YT, self.WIN)
        ok, msg = tv.cast(t, "", np=fake)
        self.assertTrue(ok)
        link, start, name = t.cast_args[0]
        self.assertEqual((link["id"], start, name), ("jfKfPfyJRdk", 754, "lofi hip hop radio"))
        self.assertEqual(log, ["pause"])
        self.assertIn("Paused it on the PC", msg)

    def test_a_tv_that_didnt_play_it_leaves_the_pc_playing(self):
        t = FakeTv(ok=False)
        fake, log = fake_np(self.YT, self.WIN)
        ok, _ = tv.cast(t, "", np=fake)
        self.assertFalse(ok)
        self.assertEqual(log, [])

    def test_netflix_is_paused_first_so_the_tv_resumes_there(self):
        t = FakeTv()
        found = {**self.YT, "title": "Stranger Things"}
        fake, log = fake_np(found, [{"title": "Netflix - Google Chrome",
                                     "url": "netflix.com/watch/80077209"}])
        ok, msg = tv.cast(t, "", np=fake)
        self.assertTrue(ok)
        self.assertEqual(log, ["pause"])
        self.assertGreater(t.waited, 0)
        self.assertEqual(t.cast_args[0][0], {"service": "netflix", "kind": "watch", "id": "80077209"})

    def test_honest_when_it_cant_tell_what_or_where(self):
        t = FakeTv()
        fake, _ = fake_np(None, self.WIN)
        self.assertEqual(tv.cast(t, "", np=fake), (False, "Nothing is playing on this PC right now."))
        fake, _ = fake_np(self.YT, [])
        ok, msg = tv.cast(t, "", np=fake)
        self.assertFalse(ok)
        self.assertIn("bring its tab to the front", msg)
        fake, _ = fake_np({**self.YT, "app": "Spotify.exe"}, self.WIN)
        ok, msg = tv.cast(t, "", np=fake)
        self.assertFalse(ok)
        self.assertIn("Spotify", msg)
        self.assertEqual(t.cast_args, [])

    def test_without_winrt_it_says_it_went_by_the_address_bar(self):
        # Seen 2026-10-20: the backend's venv had no winrt, so the TV started at the
        # page's stale &t=600s, 6 minutes behind, and the PC kept playing — unsaid.
        t = FakeTv()
        fake, log = fake_np(None, [{**self.WIN[0], "url": self.WIN[0]["url"] + "&t=600s"}],
                            media=False)
        ok, msg = tv.cast(t, "", np=fake)
        self.assertTrue(ok)
        self.assertEqual(t.cast_args[0][1], 600)
        self.assertEqual(log, [])
        self.assertIn("install winrt-Windows.Media.Control", msg)

    def test_a_link_goes_straight_to_the_tv(self):
        t = FakeTv()
        fake, log = fake_np(None, [])
        ok, _ = tv.cast(t, "https://youtu.be/dQw4w9WgXcQ?t=42", np=fake)
        self.assertTrue(ok)
        self.assertEqual(t.cast_args[0][:2], ({"service": "youtube", "id": "dQw4w9WgXcQ", "t": 42}, 42))
        self.assertEqual(log, [])
        self.assertFalse(tv.cast(t, "https://example.com", np=fake)[0])


class TvCastTests(unittest.TestCase):
    """Tv.cast on a scripted TV: the YouTube deep link, and the start time checked."""

    def tv(self, position_ms, step=2000):
        sent = []
        turns = {"n": 0}

        def sh(cmd):
            sent.append(cmd)
            if "mCurrentFocus" in cmd:
                return True, "mCurrentFocus=Window{1 u0 com.google.android.youtube.tv/x}", ""
            if "media_session" in cmd:
                turns["n"] += 1
                pos = position_ms + turns["n"] * step
                upd = 1 if turns["n"] > 1 else 0      # it reports once the link is sent
                return True, ("package=com.google.android.youtube.tv\n"
                              f"  state=PlaybackState {{state=3, position={pos}, buffered "
                              f"position=0, speed=1.0, updated={upd}, actions=379"), ""
            return True, "", ""
        return tv.Tv(sh, wait=lambda s: None), sent

    def test_starts_at_the_point_and_says_so(self):
        t, sent = self.tv(754_000)
        ok, msg = t.cast({"service": "youtube", "id": "jfKfPfyJRdk"}, 754, "lofi")
        self.assertTrue(ok)
        self.assertIn("watch?v=jfKfPfyJRdk&t=754s", next(c for c in sent if "am start" in c))
        self.assertIn("from 12:34", msg)

    def test_youtube_playing_with_a_frozen_position_is_playing(self):
        # Seen 2026-10-20: YouTube's TV app sat at position=902099 for 95 s while
        # playing, so "couldn't confirm it started playing" and the PC wasn't paused.
        t, _ = self.tv(600_503, step=0)
        ok, msg = t.cast({"service": "youtube", "id": "jfKfPfyJRdk"}, 600, "lofi")
        self.assertTrue(ok, msg)
        self.assertIn("from 10:00", msg)

    def test_a_tv_in_standby_is_switched_on_before_the_link(self):
        # Seen 2026-10-20: with Remote start on, "off" is standby with ADB up; the link
        # opened YouTube on a dark screen ("couldn't confirm it started playing").
        sent, awake = [], {"on": False}

        def sh(cmd):
            sent.append(cmd)
            if cmd == "input keyevent 224":
                awake["on"] = True
            if "mWakefulness" in cmd:
                return True, f"mWakefulness={'Awake' if awake['on'] else 'Asleep'}", ""
            if "mCurrentFocus" in cmd:
                return True, "mCurrentFocus=Window{1 u0 com.google.android.youtube.tv/x}", ""
            if "media_session" in cmd and awake["on"]:
                return True, ("package=com.google.android.youtube.tv\n  state=PlaybackState "
                              "{state=3, position=0, buffered position=0, speed=1.0, "
                              f"updated={len(sent)}, actions=379"), ""
            return True, "", ""
        ok, msg = tv.Tv(sh, wait=lambda s: None).cast({"service": "youtube", "id": "rru2passumI"})
        self.assertTrue(ok, msg)
        self.assertLess(sent.index("input keyevent 224"),
                        next(i for i, c in enumerate(sent) if "am start" in c))

    def test_youtubes_who_is_watching_gets_one_ok_then_the_link_again(self):
        # Seen 2026-10-20: after waking, YouTube asks "Who's watching?" and swallows the
        # link. The user chose: pick the highlighted account, as Netflix's gate does.
        sent, seen = [], {"ok": False, "links": 0}

        def sh(cmd):
            sent.append(cmd)
            seen["links"] += cmd.startswith("am start")
            seen["ok"] |= cmd == "input keyevent 23"
            if "mCurrentFocus" in cmd:
                return True, "mCurrentFocus=Window{1 u0 com.google.android.youtube.tv/x}", ""
            if "media_session" in cmd and seen["ok"] and seen["links"] >= 2:
                return True, ("package=com.google.android.youtube.tv\n  state=PlaybackState "
                              "{state=3, position=120000, buffered position=0, speed=1.0, "
                              "updated=5, actions=379"), ""
            return True, "", ""                 # pidof: YouTube not running, a cold start
        ok, msg = tv.Tv(sh, wait=lambda s: None).cast({"service": "youtube", "id": "rru2passumI"}, 120)
        self.assertTrue(ok, msg)
        links = [i for i, c in enumerate(sent) if c.startswith("am start")]
        self.assertEqual(len(links), 2)
        self.assertTrue(links[0] < sent.index("input keyevent 23") < links[1])

    def test_a_search_after_who_is_watching_is_sent_again(self):
        sent, seen = [], {"ok": 0, "links": 0}

        def sh(cmd):
            sent.append(cmd)
            seen["links"] += cmd.startswith("am start")
            seen["ok"] += cmd == "input keyevent 23"
            if "mCurrentFocus" in cmd:
                return True, "mCurrentFocus=Window{1 u0 com.google.android.youtube.tv/x}", ""
            if "media_session" in cmd and seen["ok"] >= 2 and seen["links"] >= 2:
                return True, ("package=com.google.android.youtube.tv\n  state=PlaybackState "
                              "{state=3, position=0, buffered position=0, speed=1.0, "
                              "updated=5, actions=379"), ""
            return True, "", ""                 # cold start: the first OK picks the account
        ok, msg = tv.Tv(sh, wait=lambda s: None).youtube_play("lofi")
        self.assertTrue(ok, msg)
        self.assertEqual(seen["links"], 2)

    def test_the_last_videos_leftover_session_isnt_the_new_one(self):
        # Seen 2026-10-20: the lofi video's state 3 (position 436, not reported since)
        # was taken for the shared link playing, so the reply said the TV ignored
        # ?t=120 while it was at 2:00.
        reads = iter([(436, 100)] * 3 + [(120_393, 200)] * 10)

        def sh(cmd):
            if "mCurrentFocus" in cmd:
                return True, "mCurrentFocus=Window{1 u0 com.google.android.youtube.tv/x}", ""
            if "media_session" in cmd:
                pos, upd = next(reads)
                return True, ("package=com.google.android.youtube.tv\n  state=PlaybackState "
                              f"{{state=3, position={pos}, buffered position=0, speed=1.0, "
                              f"updated={upd}, actions=379"), ""
            return True, "", ""
        ok, msg = tv.Tv(sh, wait=lambda s: None).cast({"service": "youtube", "id": "rru2passumI"}, 120)
        self.assertTrue(ok, msg)
        self.assertIn("from 2:00", msg)
        self.assertNotIn("not from", msg)

    def test_says_when_the_tv_ignored_the_start_time(self):
        t, _ = self.tv(0)
        ok, msg = t.cast({"service": "youtube", "id": "jfKfPfyJRdk"}, 754, "lofi")
        self.assertTrue(ok)
        self.assertIn("not from 12:34: it didn't take the start time", msg)

    def test_refuses_what_isnt_a_video_id(self):
        t, sent = self.tv(0)
        self.assertFalse(t.cast({"service": "youtube", "id": "x'; reboot"}, 0)[0])
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
