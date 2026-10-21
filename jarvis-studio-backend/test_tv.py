"""actions/tv.py against a scripted TV (port of aura-android's remote/tv.test.ts)."""

import re
import unittest

from actions import tv
from llm import live_tools


def fake_tv(rules):
    """The first rule whose pattern matches answers the command; a list answers in
    turn (its last entry repeats). Every command sent is recorded."""
    sent, turns = [], {}

    def shell(command):
        sent.append(command)
        out = ""
        for i, (pat, answer) in enumerate(rules):
            if re.search(pat, command):
                if isinstance(answer, list):
                    n = turns.get(i, 0)
                    out = answer[min(n, len(answer) - 1)]
                    turns[i] = n + 1
                else:
                    out = answer
                break
        return True, out, ""

    return tv.Tv(shell, wait=lambda s: None, now=lambda: 0), sent


def lookup(_title):
    return {"id": "80057281", "name": "Stranger Things"}


def episode_of(_show, _s, _e):
    return {"id": "80077368", "title": "Chapter One: The Vanishing of Will Byers"}


def run(t, action, target="", app="", season=0, episode=0, lk=lookup):
    return tv.run_tv(t, action, target, app, season, episode, lookup=lk, episode_of=episode_of)


def focus(pkg):
    return f"  mCurrentFocus=Window{{6838c77 u0 {pkg}/{pkg}.MainActivity}}"


def nf(state, position):
    return ("    package=com.netflix.ninja\n      active=true\n"
            f"      state=PlaybackState {{state={state}, position={position}, buffered position=0}}")


def vol(n):
    return f"[v] Connecting to AudioService\n[v] volume is {n} in range [0..100]"


class TvControlTest(unittest.TestCase):
    def test_opens_app_and_confirms_focus(self):
        t, sent = fake_tv([(r"mCurrentFocus", focus("com.netflix.ninja"))])
        self.assertEqual(run(t, "open_app", "Netflix"), (True, "Opened Netflix on the TV."))
        self.assertIn("monkey -p com.netflix.ninja -c android.intent.category.LEANBACK_LAUNCHER 1", sent)

    def test_no_claim_when_something_else_in_front(self):
        t, _ = fake_tv([(r"mCurrentFocus", focus("com.google.android.tvlauncher"))])
        ok, msg = run(t, "open_app", "netflix")
        self.assertFalse(ok)
        self.assertIn("the home screen", msg)

    def test_netflix_source30_link_and_moving_position(self):
        t, sent = fake_tv([(r"^pidof", "9812"),
                           (r"media_session", [nf(2, 1000), nf(1, 0), nf(3, 5000), nf(3, 5000), nf(3, 8000)])])
        self.assertEqual(run(t, "play", "stranger things", "netflix"),
                         (True, "Playing Stranger Things on Netflix."))
        link = next(c for c in sent if c.startswith("am start"))
        self.assertIn("-d 'https://www.netflix.com/title/80057281'", link)
        self.assertIn("-e source 30", link)
        self.assertNotIn("input keyevent 23", sent)

    def test_cold_start_profile_gate_one_ok(self):
        t, sent = fake_tv([(r"^pidof", ""),
                           (r"media_session", [nf(3, 0)] * 4 + [nf(3, 203014), nf(3, 203014), nf(3, 207108)])])
        self.assertTrue(run(t, "play", "Stranger Things", "netflix")[0])
        self.assertEqual(sent.count("input keyevent 23"), 1)

    def test_a_show_page_that_doesnt_autoplay_gets_one_ok(self):
        # Live 2026-10-20: "play The Big Bang Theory" (never started on that profile)
        # opened the show's page, Play focused, and waited: Netflix stayed stopped.
        t, sent = fake_tv([(r"^pidof", "9812"), (r"mCurrentFocus", focus("com.netflix.ninja")),
                           (r"media_session", [nf(1, 0)] * 5 + [nf(3, 4000), nf(3, 4000), nf(3, 6500)])])
        self.assertEqual(run(t, "play", "stranger things", "netflix"),
                         (True, "Playing Stranger Things on Netflix."))
        self.assertEqual(sent.count("input keyevent 23"), 1)

    def test_no_ok_unless_netflix_is_in_front(self):
        t, sent = fake_tv([(r"^pidof", "9812"), (r"mCurrentFocus", focus("com.google.android.tvlauncher")),
                           (r"media_session", nf(1, 0))])
        self.assertFalse(run(t, "play", "stranger things", "netflix")[0])
        self.assertNotIn("input keyevent 23", sent)

    def test_netflix_never_starts(self):
        t, _ = fake_tv([(r"^pidof", ""), (r"media_session", nf(3, 0))])
        ok, msg = run(t, "play", "Stranger Things", "netflix")
        self.assertFalse(ok)
        self.assertIn("couldn't confirm it started playing", msg)

    def test_named_episode_uses_watch_link(self):
        t, sent = fake_tv([(r"^pidof", "9812"),
                           (r"media_session", [nf(2, 1000), nf(3, 0), nf(3, 4000), nf(3, 4000), nf(3, 7000)])])
        ok, msg = run(t, "play", "Stranger Things", "netflix", season=1, episode=1)
        self.assertTrue(ok)
        self.assertIn("season 1, episode 1", msg)
        self.assertIn("-d 'https://www.netflix.com/watch/80077368'",
                      next(c for c in sent if c.startswith("am start")))

    def test_no_switch_claim_when_same_playback_continues(self):
        t, _ = fake_tv([(r"^pidof", "9812"),
                        (r"media_session", [nf(3, 100_000), nf(3, 102_500), nf(3, 103_000),
                                            nf(3, 103_000), nf(3, 105_500)])])
        ok, msg = run(t, "play", "Stranger Things", "netflix")
        self.assertFalse(ok)
        self.assertIn("couldn't confirm it switched", msg)

    def test_failed_lookup_never_touches_tv(self):
        t, sent = fake_tv([])
        r = run(t, "play", "Nope", "netflix", lk=lambda _t: {"error": "I couldn't find “Nope” on Netflix."})
        self.assertEqual(r, (False, "I couldn't find “Nope” on Netflix."))
        self.assertEqual(sent, [])

    def test_pause_one_toggle_confirmed(self):
        t, sent = fake_tv([(r"mCurrentFocus", focus("com.netflix.ninja")),
                           (r"media_session", [nf(3, 1000), nf(3, 1000), nf(3, 3500), nf(2, 3600)])])
        self.assertEqual(run(t, "pause"), (True, "Paused Netflix."))
        self.assertEqual([c for c in sent if c.startswith("input keyevent")], ["input keyevent 85"])

    def test_already_in_state_presses_nothing(self):
        t, sent = fake_tv([(r"mCurrentFocus", focus("com.netflix.ninja")), (r"media_session", nf(2, 500))])
        self.assertEqual(run(t, "pause"), (True, "Netflix is already paused."))
        self.assertFalse(any(c.startswith("input keyevent") for c in sent))

    def test_volume_verified_with_key_fallback(self):
        t, _ = fake_tv([(r"--get", [vol(20), vol(30)])])
        self.assertEqual(run(t, "set_volume", "30"), (True, "TV volume is 30."))
        t, sent = fake_tv([(r"--get", [vol(20), vol(20), vol(23)])])
        self.assertTrue(run(t, "set_volume", "23")[0])
        self.assertIn("input keyevent 24 24 24", sent)

    def test_mute_from_tv_state(self):
        t, _ = fake_tv([(r"dumpsys audio", ["   Muted: false", "   Muted: true"])])
        self.assertEqual(run(t, "mute"), (True, "The TV is muted."))

    def test_remote_buttons(self):
        t, sent = fake_tv([])
        self.assertEqual(run(t, "key", "down 3"), (True, "Sent down ×3 to the TV."))
        self.assertIn("input keyevent 20 20 20", sent)
        self.assertFalse(run(t, "key", "jump")[0])

    def test_unreachable_tv_is_a_clear_failure(self):
        t = tv.Tv(lambda c: (False, "", "I can't reach the TV at 192.168.1.50:5555."), wait=lambda s: None)
        self.assertEqual(run(t, "power_off"), (False, "I can't reach the TV at 192.168.1.50:5555."))

    def test_every_advertised_action_has_a_handler(self):
        decl = next(d for d in live_tools._tv_tools(True) if d["name"] == "tv_control")
        for action in decl["parameters"]["properties"]["action"]["enum"]:
            if action in ("look", "task", "cast"):  # tv.run's own (SeeingTest, test_now_playing)
                continue
            t, _ = fake_tv([(r"mCurrentFocus", focus("com.netflix.ninja"))])
            self.assertNotRegex(run(t, action, "1", "netflix")[1], "don't know the TV action", action)

    def test_tool_only_advertised_when_paired(self):
        self.assertEqual(live_tools._tv_tools(False), [])
        spec = live_tools.to_spec("tv_control", {"action": "play", "target": "Dark", "season": 2})
        self.assertEqual((spec["type"], spec["do"], spec["target"], spec["season"]), ("tv", "play", "Dark", 2))


class RunawayReplyTest(unittest.TestCase):
    def test_a_runaway_reply_runs_only_a_few_of_its_repeats(self):
        import actions
        up = {"type": "tv", "do": "key", "target": "up"}
        replay = [{"type": "tv", "do": "open_app", "target": "youtube"},
                  {"type": "tv", "do": "key", "target": "home"}] + [dict(up) for _ in range(213)]
        kept = actions._drop_runaway(replay)
        self.assertEqual(len(kept), 5)
        self.assertEqual(kept.count(up), actions._MAX_SAME_SPEC)
        many = [{"type": "time", "n": i} for i in range(30)]
        self.assertEqual(len(actions._drop_runaway(many)), actions._MAX_SPECS_PER_REPLY)
        normal = [{"type": "time"}, {"type": "weather"}, {"type": "news", "topic": "tech"}]
        self.assertEqual(actions._drop_runaway(normal), normal)


class HelpersTest(unittest.TestCase):
    def test_find_episode(self):
        page = (r'"Season:{\\"videoId\\":1}":{"__typename":"Season","videoId":1,"numberLabelV2({\\"label\\":\\"LONG\\"})":"Season 1","episodes":{"__typename":"EpisodesConnection","edges":[{"node":{"__ref":"Episode:{\\"videoId\\":11}"}},{"node":{"__ref":"Episode:{\\"videoId\\":12}"}}]}},'
                r'"Season:{\\"videoId\\":2}":{"__typename":"Season","videoId":2,"numberLabelV2({\\"label\\":\\"LONG\\"})":"Season 2","episodes":{"__typename":"EpisodesConnection","edges":[{"node":{"__ref":"Episode:{\\"videoId\\":21}"}}]}},'
                r'"Episode:{\\"videoId\\":12}":{"__typename":"Episode","videoId":12,"title":"Chapter Two: The Weirdo on Maple Street","number":2}')
        self.assertEqual(tv.find_episode(page, 1, 2), {"id": "12", "title": "Chapter Two: The Weirdo on Maple Street"})
        self.assertEqual(tv.find_episode(page, 2, 1), {"id": "21", "title": ""})
        self.assertIsNone(tv.find_episode(page, 2, 2))
        self.assertIsNone(tv.find_episode(page, 3, 1))

    def test_find_episode_nested_shape(self):
        # The Oct 2026 page: seasons in order, unlabelled; \x-escaped JS strings; a
        # recommendations tail after the last season that must not be read as episodes.
        ep = lambda vid, n, title: ('{"__typename":"EpisodeEdge","node":{"__typename":"Episode","videoId":%d,'
                                    '"number":%d,"runtimeSec":1,"shortSynopsis":"x","title":"%s"}}' % (vid, n, title))
        page = ('"__typename":"Show","title":"Stranger\\x20Things","videoId":80057281,'
                '"seasons":{"__typename":"SeasonsConnection","edges":['
                '{"node":{"__typename":"Season","episodes":{"edges":[' + ep(11, 1, "One") + "," + ep(12, 2, "Two\\x21")
                + ']},"title":"Stranger\\x20Things"}},'
                '{"node":{"__typename":"Season","episodes":{"edges":[' + ep(21, 1, "Uno")
                + ']},"title":"Stranger\\x20Things\\x202"}}]},"more":[' + ep(99, 7, "Other show") + "]")
        self.assertEqual(tv.find_episode(page, 1, 2), {"id": "12", "title": "Two!"})
        self.assertEqual(tv.find_episode(page, 2, 1), {"id": "21", "title": "Uno"})
        self.assertIsNone(tv.find_episode(page, 2, 2))
        self.assertIsNone(tv.find_episode(page, 3, 1))
        self.assertEqual(tv.title_name_in(page, "80057281"), "Stranger Things")
        self.assertEqual(tv.title_name_in("<title>Watch Dark | Netflix Official Site</title>", "1"), "Dark")
        self.assertIsNone(tv.title_name_in("<title>Netflix</title>", "1"))

    def test_shell_quote(self):
        self.assertEqual(tv.shell_quote("it's"), "'it'\\''s'")

    def test_same_title(self):
        self.assertTrue(tv.same_title("Stranger Things", "stranger things season 4"))
        self.assertTrue(tv.same_title("Amélie", "amelie"))
        self.assertFalse(tv.same_title("Money Heist", "La Casa de Papel"))

    def test_lookup_confirms_id_against_netflix_page(self):
        search = lambda q: (True, "It's https://www.netflix.com/in/title/80057281 — enjoy.")
        self.assertEqual(tv.lookup_netflix_title("stranger things", search, lambda i: "Stranger Things"),
                         {"id": "80057281", "name": "Stranger Things"})
        r = tv.lookup_netflix_title("stranger things", search, lambda i: "Dark")
        self.assertIn("closest title I found was “Dark”", r["error"])

    def test_parse_target(self):
        self.assertEqual(tv._parse_target("192.168.1.50"), ("192.168.1.50", 5555))
        self.assertEqual(tv._parse_target(" tv.local:5556 "), ("tv.local", 5556))
        self.assertIsNone(tv._parse_target("1.2.3.4; rm -rf /"))
        self.assertIsNone(tv._parse_target("1.2.3.4:70000"))


def png_bytes(color=(40, 90, 200), size=(1920, 1080)):
    import io
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, format="PNG")
    return buf.getvalue()


def frame_bytes(color=(0, 0, 0), w=1280, h=720):
    import struct
    body = bytes(color) * (w * h)
    return struct.pack("<5I", 16 + len(body), w, h, w * 3, 3) + body


def gz_screencap(color=(40, 90, 200), w=320, h=180, fmt=1, header=12):
    """What `screencap | gzip -1` prints: gzip of (w, h, format[, dataspace]) + RGBA."""
    import gzip
    import struct
    head = struct.pack("<3I", w, h, fmt) + (b"\0\0\0\0" if header == 16 else b"")
    return gzip.compress(head + bytes((*color, 255)) * (w * h))


def grabs(png=b"", frame=b"", ok=True, gz=b"NOGZIP\n"):
    """capture()'s (exec_out, first_frame) pair, scripted. By default the TV has no
    gzip, so capture() falls through to `screencap -p`."""
    def run_exec(host, cmd):
        if not ok:
            return False, b"", "I can't reach the TV."
        return True, (gz if "gzip" in cmd else png), ""
    return run_exec, (lambda host, size: (True, frame, ""))


class SeeingTest(unittest.TestCase):
    def test_capture_takes_screencap_when_allowed(self):
        shot = tv.capture("tv", grab=grabs(png=png_bytes()))
        self.assertTrue(shot["ok"])
        self.assertFalse(shot["blocked"] or shot["partial"])
        self.assertTrue(shot["image"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(len(shot["sig"]), 64 * 36)

    def test_capture_prefers_the_gzipped_raw_screencap(self):
        shot = tv.capture("tv", grab=grabs(gz=gz_screencap((200, 10, 10)), png=b"unused"))
        self.assertTrue(shot["ok"] and not shot["partial"])
        img, refused = tv.raw_screencap_image(gz_screencap((200, 10, 10), header=16, fmt=5))
        self.assertEqual((img.size, img.getpixel((0, 0)), refused), ((320, 180), (10, 10, 200), False))
        self.assertEqual(tv.raw_screencap_image(gz_screencap(fmt=99)), (None, None))
        self.assertEqual(tv.raw_screencap_image(b"NOGZIP\n"), (None, None))

    def test_a_refused_gzipped_screencap_goes_to_the_frame(self):
        import gzip
        shot = tv.capture("tv", grab=grabs(gz=gzip.compress(b""), frame=frame_bytes()))
        self.assertEqual(shot, {"ok": True, "blocked": True})

    def test_capture_falls_back_to_a_frame_with_protected_holes(self):
        # screencap refused (a secure layer is up) but the frame shows the rest.
        frame = bytearray(frame_bytes((0, 0, 0)))
        frame[20:20 + 3 * 5000] = bytes((200, 200, 200)) * 5000      # a lit UI strip
        shot = tv.capture("tv", fmt="PNG", grab=grabs(frame=bytes(frame)))
        self.assertTrue(shot["ok"] and shot["partial"])
        self.assertFalse(shot["blocked"])
        self.assertTrue(shot["image"].startswith("data:image/png;base64,"))

    def test_capture_reports_a_fully_protected_screen_as_blocked(self):
        self.assertEqual(tv.capture("tv", grab=grabs(frame=frame_bytes())),
                         {"ok": True, "blocked": True})

    def test_capture_passes_an_unreachable_tv_through(self):
        self.assertEqual(tv.capture("tv", grab=grabs(ok=False)),
                         {"ok": False, "why": "I can't reach the TV."})

    def test_look_defers_to_vision_with_the_tv_image(self):
        t, _ = fake_tv([(r"mWakefulness", "  mWakefulness=Awake"),
                        (r"mCurrentFocus", focus("com.google.android.youtube.tv"))])
        r = tv.look("tv", "", tv=t, grab=grabs(png=png_bytes()))
        self.assertTrue(r["needs_vision"])
        self.assertTrue(r["image"].startswith("data:image/png;base64,"))
        self.assertIn("showing YouTube", r["context"])
        self.assertEqual(r["question"], "Describe what's on the TV screen.")

    def test_look_says_why_it_cant_see_netflix(self):
        t, _ = fake_tv([(r"mWakefulness", "  mWakefulness=Awake"),
                        (r"mCurrentFocus", focus("com.netflix.ninja")),
                        (r"media_session", nf(3, 5000))])
        r = tv.look("tv", "what's on?", tv=t, grab=grabs(frame=frame_bytes()))
        self.assertNotIn("needs_vision", r)
        self.assertEqual(r["message"], "Netflix is on the TV and playing, but it blocks "
                                       "screenshots (copy protection), so I can't see the picture.")

    def test_look_doesnt_call_the_profile_screen_playing(self):
        t, _ = fake_tv([(r"mWakefulness", "  mWakefulness=Awake"),
                        (r"mCurrentFocus", focus("com.netflix.ninja")),
                        (r"media_session", nf(3, 0))])
        r = tv.look("tv", "", tv=t, grab=grabs(frame=frame_bytes()))
        self.assertTrue(r["message"].startswith("Netflix is on the TV, but it blocks"))

    def test_look_at_a_tv_in_standby(self):
        t, sent = fake_tv([(r"mWakefulness", "  mWakefulness=Asleep")])
        self.assertEqual(tv.look("tv", "", tv=t)["message"], "The TV is off (in standby).")
        self.assertEqual(len(sent), 1)

    def test_a_flat_screen_is_flagged_blank(self):
        self.assertTrue(tv.capture("tv", grab=grabs(png=png_bytes((40, 40, 40))))["blank"])
        from PIL import Image, ImageDraw
        import io
        img = Image.new("RGB", (1920, 1080), (40, 40, 40))
        ImageDraw.Draw(img).rectangle((200, 200, 900, 600), fill=(240, 240, 240))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        self.assertFalse(tv.capture("tv", grab=grabs(png=buf.getvalue()))["blank"])

    def test_open_app_takes_the_name_from_app_too(self):
        # Live 2026-10-11: tv_control({'action': 'open_app', 'app': 'youtube'}).
        real, real_shell = tv.config, tv.shell
        tv.config = lambda: {"host": "tv", "model": "x"}
        sent = []

        def shell(host, cmd):
            sent.append(cmd)
            return True, focus("com.google.android.youtube.tv") if "mCurrentFocus" in cmd else "", ""
        tv.shell = shell
        try:
            spec = live_tools.to_spec("tv_control", {"action": "open_app", "app": "youtube"})
            self.assertEqual(tv.run(spec)["message"], "Opened YouTube on the TV.")
            self.assertIn("monkey -p com.google.android.youtube.tv -c "
                          "android.intent.category.LEANBACK_LAUNCHER 1", sent)
        finally:
            tv.config, tv.shell = real, real_shell

    def test_run_defers_a_task_to_the_operator(self):
        real = tv.config
        tv.config = lambda: {"host": "tv", "model": "x"}
        try:
            r = tv.run({"do": "task", "target": "open the second YouTube result"})
            self.assertEqual((r["needs_autopilot"], r["kind"], r["goal"]),
                             (True, "tv", "open the second YouTube result"))
            self.assertFalse(tv.run({"do": "task", "target": " "})["ok"])
            tv.config = lambda: None
            self.assertIn("No TV is set up", tv.run({"do": "look"})["message"])
        finally:
            tv.config = real


class FakeTv:
    """tv_operator's device: scripted screens, recorded presses."""

    def __init__(self, shots, app="YouTube", awake=True):
        self.shots, self.app_name, self.is_awake = list(shots), app, awake
        self.done: "list[tuple]" = []

    def awake(self):
        return self.is_awake

    def wake(self):
        self.done.append(("wake",))
        self.is_awake = True
        return True, "Turned the TV on."

    def app(self):
        return self.app_name

    def capture(self):
        return self.shots.pop(0) if len(self.shots) > 1 else self.shots[0]

    def press(self, names):
        self.done.append(("press", tuple(names)))
        return True, "pressed " + ", ".join(names)

    def open_app(self, name):
        self.done.append(("open", name))
        self.app_name = name
        return True, f"Opened {name} on the TV."

    def type_text(self, text):
        self.done.append(("type", text))
        return True, f"Typed “{text}” on the TV."


def screen(n):
    return {"ok": True, "blocked": False, "partial": False, "image": f"img{n}",
            "sig": bytes([n * 40 % 256]) * (64 * 36)}


HIDDEN = {"ok": True, "blocked": True}


def operate(device, replies, **kw):
    import asyncio
    import tv_operator
    calls = []

    async def decide(prompt, image):
        calls.append((prompt, image))
        return replies.pop(0) if replies else '{"do":"fail","reason":"script ran out"}'

    async def nap(_s):
        return None

    res = asyncio.run(tv_operator.run_task("pick the second result", device=device,
                                           decide=decide, eyes=["gemini-x"], sleep=nap, **kw))
    return res, calls


class OperatorTest(unittest.TestCase):
    def test_presses_what_the_model_sees_then_finishes(self):
        lines = []

        async def on_step(line):
            lines.append(line)

        dev = FakeTv([screen(1), screen(2)])
        res, calls = operate(dev, ['{"do":"keys","keys":["down","down","ok"]}',
                                   '{"do":"done","summary":"Playing the second result."}'],
                             on_step=on_step)
        self.assertEqual((res["ok"], res["summary"]), (True, "Playing the second result."))
        self.assertEqual(dev.done, [("press", ("down", "down", "ok"))])
        self.assertEqual([img for _p, img in calls], ["img1", "img2"])
        self.assertIn("APP IN FRONT: YouTube", calls[0][0])
        self.assertEqual(lines, ["pressed down, down, ok"])

    def test_stops_when_no_model_can_see(self):
        res, _ = operate(FakeTv([screen(1)]), ['{"do":"blind"}'])
        self.assertFalse(res["ok"])
        self.assertIn("can't see the TV", res["summary"])

    def test_gives_up_when_presses_change_nothing(self):
        dev = FakeTv([screen(1)])                     # the same screen forever
        res, calls = operate(dev, ['{"do":"keys","keys":["right"]}'] * 5)
        self.assertFalse(res["ok"])
        self.assertIn("isn't responding", res["summary"])
        self.assertEqual(len(dev.done), 3)
        self.assertIn("changed NOTHING", calls[1][0])

    def test_a_hidden_screen_sends_no_image_and_says_why(self):
        dev = FakeTv([HIDDEN, screen(2)], app="Netflix")
        res, calls = operate(dev, ['{"do":"open_app","app":"YouTube"}',
                                   '{"do":"done","summary":"YouTube is open."}'])
        self.assertTrue(res["ok"])
        self.assertEqual(calls[0][1], "")
        self.assertIn("SCREEN HIDDEN: Netflix blocks screenshots", calls[0][0])
        self.assertEqual(calls[1][1], "img2")

    def test_gives_up_on_a_screen_it_can_never_see(self):
        res, _ = operate(FakeTv([HIDDEN], app="Netflix"), ['{"do":"keys","keys":["ok"]}'] * 5)
        self.assertFalse(res["ok"])
        self.assertIn("Netflix blocks screenshots", res["summary"])

    def test_stop_ends_it_before_the_next_press(self):
        dev = FakeTv([screen(1), screen(2), screen(3)])
        res, _ = operate(dev, ['{"do":"keys","keys":["down"]}'] * 3,
                         interrupted=lambda: len(dev.done) >= 1)
        self.assertTrue(res.get("stopped"))
        self.assertEqual(len(dev.done), 1)

    def test_wakes_a_sleeping_tv_first(self):
        dev = FakeTv([screen(1)], awake=False)
        res, _ = operate(dev, ['{"do":"done","summary":"Done."}'])
        self.assertTrue(res["ok"])
        self.assertEqual(dev.done, [("wake",)])

    def test_only_known_buttons_are_pressed(self):
        import tv_operator
        self.assertEqual(tv_operator._key_names("Down, down OK jump power"),
                         (["down", "down", "ok"], ["jump", "power"]))
        self.assertEqual(len(tv_operator._key_names(["down"] * 20)[0]), tv_operator.MAX_KEYS)
        dev = FakeTv([screen(1), screen(2)])
        res, calls = operate(dev, ['{"do":"keys","keys":["power"]}',
                                   '{"do":"done","summary":"ok"}'])
        self.assertEqual(dev.done, [])
        self.assertIn("No usable buttons", calls[1][0])

    def test_a_blank_screen_is_called_loading_not_missing(self):
        blank = {**screen(1), "blank": True}
        res, calls = operate(FakeTv([blank, screen(2)]),
                             ['{"do":"wait","seconds":2}', '{"do":"done","summary":"ok"}'])
        self.assertTrue(res["ok"])
        self.assertIn("blank (one flat colour)", calls[0][0])
        self.assertEqual(calls[0][1], "img1")       # the blank picture is still sent

    def test_types_into_the_focused_field(self):
        dev = FakeTv([screen(1), screen(2)])
        operate(dev, ['{"do":"type","text":"lofi hip hop"}', '{"do":"done","summary":"ok"}'])
        self.assertEqual(dev.done, [("type", "lofi hip hop")])


if __name__ == "__main__":
    unittest.main()
