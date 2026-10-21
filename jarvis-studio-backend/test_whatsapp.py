"""The WhatsApp channel (whatsapp_channel.py + main._run_whatsapp_turn): who may command
JARVIS, how a turn's reply comes back, approvals asked in the chat. neonize itself isn't
needed: messages are plain objects shaped like its protos."""

import asyncio
import base64
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest import mock

import whatsapp_channel as wa

ME, LID, FRIEND = "919800000001", "123456789012345", "919800000002"


def jid(user, server="s.whatsapp.net"):
    return NS(User=user, Server=server)


def src(chat, sender, from_me=False, group=False, sender_alt=None):
    return NS(Chat=chat, Sender=sender, IsFromMe=from_me, IsGroup=group,
              SenderAlt=sender_alt or jid("", ""))


class AdmitTests(unittest.TestCase):
    now = 1_000_000.0

    def admit(self, s, text="hi", allow=(), sent=None):
        return wa.admit(s, text, {ME, LID}, list(allow), sent or self.now, self.now)

    def test_the_owners_own_chat_is_admitted_and_its_replies_are_not(self):
        self.assertEqual(self.admit(src(jid(ME), jid(ME), from_me=True)), "")
        self.assertEqual(self.admit(src(jid(LID, "lid"), jid(LID, "lid"), from_me=True)), "")
        self.assertEqual(self.admit(src(jid(ME), jid(ME), from_me=True), "🤖 Done."), "own reply")

    def test_self_chat_is_known_even_before_the_account_is(self):
        s = src(jid(LID, "lid"), jid(LID, "lid"), from_me=True, sender_alt=jid(ME))
        self.assertEqual(wa.admit(s, "x", set(), [], self.now, self.now), "")

    def test_the_owner_writing_to_a_friend_is_never_a_command(self):
        self.assertEqual(self.admit(src(jid(FRIEND), jid(ME), from_me=True)), "outgoing")

    def test_others_need_the_list_and_groups_never_count(self):
        self.assertEqual(self.admit(src(jid(FRIEND), jid(FRIEND))), "not allowed")
        self.assertEqual(self.admit(src(jid(FRIEND), jid(FRIEND)), allow=[FRIEND]), "")
        # A sender known by its LID, with the phone number as the alternate id.
        s = src(jid("777", "lid"), jid("777", "lid"), sender_alt=jid(FRIEND))
        self.assertEqual(self.admit(s, allow=[FRIEND]), "")
        g = src(jid("1203630@", "g.us"), jid(FRIEND), group=True)
        self.assertEqual(self.admit(g, allow=[FRIEND]), "group or broadcast")

    def test_a_message_from_while_jarvis_was_off_is_stale(self):
        old = self.now - wa.STALE_S - 1
        self.assertEqual(self.admit(src(jid(ME), jid(ME), from_me=True), sent=old), "stale")
        # …but a stranger's old message is still just not allowed.
        self.assertEqual(self.admit(src(jid(FRIEND), jid(FRIEND)), sent=old), "not allowed")


class TextTests(unittest.TestCase):
    def test_a_self_chat_message_must_start_with_the_name(self):
        self.assertEqual(wa.addressed("Jarvis, what's my CPU at?"), "what's my CPU at?")
        self.assertEqual(wa.addressed("hey JARVIS open notepad"), "open notepad")
        self.assertEqual(wa.addressed("Jarvis"), "")
        self.assertIsNone(wa.addressed("buy milk"))
        self.assertIsNone(wa.addressed("jarvisification is a word"))

    def test_only_a_plain_yes_approves(self):
        for a in ("yes", "Yes!", "ok", "go ahead", "👍"):
            self.assertTrue(wa.is_yes(a), a)
        for a in ("no", "not now", "yes but don't", "what is it?", ""):
            self.assertFalse(wa.is_yes(a), a)

    def test_numbers_and_markdown(self):
        self.assertEqual(wa.norm_number("+91 98000-00002"), FRIEND)
        self.assertEqual(wa.norm_number("12"), "")
        self.assertEqual(wa.for_whatsapp("**Two** meetings:\n| When | What |\n|---|---|\n| 9 | Standup |"),
                         "*Two* meetings:\nWhen · What\n9 · Standup")
        parts = wa.chunks("word " * 2000, 500)
        self.assertTrue(all(len(p) <= 500 for p in parts))
        self.assertEqual(" ".join(parts).split(), ["word"] * 2000)

    def test_status_is_the_whole_message_only(self):
        for text in ("status", "Status?", "what are you doing?", "what're you up to",
                     "how's it going", "progress", "are you busy?"):
            self.assertTrue(wa.is_status_request(text), text)
        for text in ("status of my amazon order", "what are you doing tonight at 8",
                     "update windows", ""):
            self.assertFalse(wa.is_status_request(text), text)

    def test_progress_text_shows_the_last_steps_and_the_end(self):
        lines = [f"step {i}" for i in range(8)]
        text = wa.progress_text("book a table", lines)
        self.assertTrue(text.startswith("⏳ Working on it: book a table"))
        self.assertIn("… 3 earlier steps", text)
        self.assertIn("→ step 7", text)
        self.assertNotIn("step 2", text)
        self.assertIn("Jarvis, status", text)
        done = wa.progress_text("book a table", lines, "failed", "The site was down.")
        self.assertTrue(done.startswith("⚠️ Didn't finish"))
        self.assertTrue(done.endswith("The site was down."))

    def test_rate_limit(self):
        r = wa.RateLimit(2, 60)
        self.assertEqual([r.ok(0), r.ok(1), r.ok(2), r.ok(61.5)], [True, True, False, True])


class ComposeTests(unittest.TestCase):
    def test_what_was_said_wins_and_actions_fill_in_for_a_silent_turn(self):
        png = b"\x89PNG fake"
        url = "data:image/png;base64," + base64.b64encode(png).decode()
        with tempfile.TemporaryDirectory() as d:
            shot = Path(d) / "s.png"
            shot.write_bytes(b"shot")
            text, images = wa.compose([
                ("response_end", {"text": "", "actions": [
                    {"type": "load_tools", "ok": True, "message": "Loaded", "internal": True},
                    {"type": "screenshot", "ok": True, "message": "Screenshot saved.",
                     "file": str(shot)}]}),
                ("action", [{"type": "generate_image", "ok": True, "message": "A cat",
                             "image": url}]),
            ])
        self.assertEqual(text, "✓ Screenshot saved.\n\n✓ A cat")
        self.assertEqual([b for b, _ in images], [b"shot", png])
        text, _ = wa.compose([("response_end", {"text": "Opened it.", "actions": [
            {"type": "open_app", "ok": True, "message": "Opening Notepad"}]}),
            ("response", "Opened it.")])
        self.assertEqual(text, "Opened it.")


class FakeClient:
    def __init__(self):
        self.sent, self.downloads, self.edits = [], 0, []
        self.edit_ok = True

    async def send_message(self, chat, text):
        self.sent.append(text)
        return NS(ID=f"id{len(self.sent)}")

    async def edit_message(self, chat, message_id, new_message):
        if not self.edit_ok:
            raise RuntimeError("too old to edit")
        self.edits.append((message_id, new_message.conversation))

    async def download_any(self, msg):
        self.downloads += 1
        return b"OggS"


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


async def settle():
    """Let the progress flusher run (it sleeps for real only when throttled)."""
    for _ in range(5):
        await asyncio.sleep(0)


def message(chat, sender, text="", from_me=False, voice=False, sent=None, mid="m1",
            quoted=None, photo=None):
    """A MessageEv: `quoted` is the text of the message it replies to, `photo` the
    bytes of an attached picture (its caption is `text`)."""
    has = {"audioMessage": voice, "extendedTextMessage": quoted is not None,
           "imageMessage": photo is not None}
    reply = NS(HasField=lambda f: quoted is not None and f == "quotedMessage",
               quotedMessage=NS(text=quoted))
    media = NS(contextInfo=reply, fileLength=len(photo or b""), mimetype="image/jpeg")
    msg = NS(text=text, HasField=lambda f: has.get(f, False), audioMessage=media,
             extendedTextMessage=NS(contextInfo=reply), imageMessage=media)
    info = NS(MessageSource=src(chat, sender, from_me), ID=mid,
              Timestamp=int(sent or time.time()))
    return NS(Info=info, Message=msg)


def fake_neonize() -> dict:
    """The bits of neonize the channel imports, as plain modules."""
    fake = types.ModuleType("neonize.utils.message")
    fake.extract_text = lambda msg: msg.text
    proto = types.ModuleType("neonize.proto.waE2E.WAWebProtobufsE2E_pb2")
    proto.Message = lambda conversation="": NS(conversation=conversation)
    return {"neonize": types.ModuleType("neonize"),
            "neonize.utils": types.ModuleType("neonize.utils"),
            "neonize.utils.message": fake,
            "neonize.proto": types.ModuleType("neonize.proto"),
            "neonize.proto.waE2E": types.ModuleType("neonize.proto.waE2E"),
            "neonize.proto.waE2E.WAWebProtobufsE2E_pb2": proto}


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        for name, mod in fake_neonize().items():
            p = mock.patch.dict(sys.modules, {name: mod})
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(wa, "settings", lambda: {"enabled": True, "allow": [FRIEND]})
        p.start()
        self.addCleanup(p.stop)
        self.commands = []

        async def on_command(text, chat, heard, attachment=None):
            self.commands.append((text, heard) + ((attachment,) if attachment else ()))

        async def transcribe(audio):
            return "Jarvis, what's the weather?"

        async def on_state(_s):
            pass

        self.statuses = []

        async def on_status(chat):
            self.statuses.append(chat.User)

        self.ch = wa.Channel(on_command, on_state, transcribe, on_status=on_status)
        self.ch._me = {ME}
        self.client = self.ch.client = FakeClient()

    async def test_status_is_answered_without_running_a_turn(self):
        me = jid(ME)
        await self.ch._on_message(self.client, message(me, me, "Jarvis, status?", True))
        await self.ch._on_message(self.client, message(me, me, "Jarvis, status of order 12",
                                                       True, mid="m2"))
        self.assertEqual(self.statuses, [ME])
        self.assertEqual(self.commands, [("status of order 12", False)])

    async def test_status_while_a_question_is_open_is_not_its_answer(self):
        me = jid(ME)
        asking = asyncio.create_task(self.ch.ask(me, "Approve? Reply yes or no.", 5))
        await asyncio.sleep(0)
        await self.ch._on_message(self.client, message(me, me, "Jarvis, status?", True))
        self.assertEqual(self.statuses, [ME])
        self.assertFalse(asking.done())
        await self.ch._on_message(self.client, message(me, me, "yes", True, mid="m2"))
        self.assertEqual(await asking, "yes")

    async def test_a_task_gets_one_message_edited_as_it_goes(self):
        clock = Clock()
        me = jid(ME)
        p = wa.Progress(self.ch, me, "find a flight to Goa", "t1", clock=clock)
        p.start()
        await settle()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn("find a flight to Goa", self.client.sent[0])
        p.step("Opened Google Flights")
        p.step("Typed Goa")                      # inside EDIT_EVERY_S: one edit for both
        clock.t += wa.EDIT_EVERY_S
        await settle()
        self.assertEqual(len(self.client.edits), 1)
        mid, text = self.client.edits[0]
        self.assertEqual(mid, "id1")
        self.assertIn("→ Opened Google Flights\n→ Typed Goa", text)
        p.finish(True)
        await p.close()
        self.assertTrue(self.client.edits[-1][1].startswith(f"{wa.MARK} ✅ Finished"))
        self.assertEqual(len(self.client.sent), 1)          # never a message per step

    async def test_when_edits_fail_progress_comes_as_a_new_message_now_and_then(self):
        clock = Clock()
        self.client.edit_ok = False
        p = wa.Progress(self.ch, jid(ME), "tidy my desktop", clock=clock)
        p.start()
        await settle()
        p.step("Listed the Desktop")
        clock.t += wa.EDIT_EVERY_S
        await settle()
        self.assertEqual(len(self.client.sent), 1)          # not yet: FALLBACK_EVERY_S
        p.step("Made a folder")
        clock.t += wa.FALLBACK_EVERY_S
        await settle()
        self.assertEqual(len(self.client.sent), 2)
        self.assertIn("→ Made a folder", self.client.sent[1])

    async def test_a_task_that_never_reports_its_end_is_not_called_done(self):
        p = wa.Progress(self.ch, jid(ME), "open notepad", clock=Clock())
        p.start()
        await settle()
        await p.close()
        self.assertIn("⏹ Ended", self.client.sent[-1] if not self.client.edits
                      else self.client.edits[-1][1])

    async def test_self_chat_commands_need_the_name_and_notes_are_left_alone(self):
        me = jid(ME)
        await self.ch._on_message(self.client, message(me, me, "Jarvis, open notepad", True))
        await self.ch._on_message(self.client, message(me, me, "buy milk", True, mid="m2"))
        await self.ch._on_message(self.client, message(me, me, "Jarvis, open notepad", True))
        self.assertEqual(self.commands, [("open notepad", False)])     # m1 not run twice
        self.assertEqual(self.client.sent, [])

    async def test_a_listed_number_needs_the_name_too_and_strangers_get_nothing(self):
        f = jid(FRIEND)
        await self.ch._on_message(self.client, message(f, f, "dinner at 8?"))
        await self.ch._on_message(self.client, message(f, f, "Jarvis, lights?", mid="m2"))
        await self.ch._on_message(self.client, message(jid("9111"), jid("9111"),
                                                       "Jarvis, hi", mid="m3"))
        self.assertEqual(self.commands, [("lights?", False)])
        self.assertEqual(self.client.sent, [])

    async def test_a_reply_to_jarvis_needs_no_name(self):
        me = jid(ME)
        await self.ch._on_message(self.client, message(
            me, me, "and tomorrow?", True, quoted=f"{wa.MARK} Sunny, 31°."))
        await self.ch._on_message(self.client, message(
            me, me, "and tomorrow?", True, quoted="my own note", mid="m2"))
        self.assertEqual(self.commands, [("and tomorrow?", False)])

    async def test_a_photo_comes_with_its_command(self):
        me = jid(ME)
        await self.ch._on_message(self.client, message(
            me, me, "Jarvis, what plant is this?", True, photo=b"jpg"))
        await self.ch._on_message(self.client, message(me, me, "", True, photo=b"jpg", mid="m2"))
        self.assertEqual(self.commands, [("what plant is this?", False,
                                          ("photo.jpg", "image/jpeg", b"OggS"))])
        big = message(me, me, "Jarvis, read this", True, photo=b"x" * (wa.MAX_ATTACH + 1),
                      mid="m3")
        await self.ch._on_message(self.client, big)
        self.assertEqual(len(self.commands), 1)
        self.assertIn("too large", self.client.sent[-1])

    async def test_voice_notes_are_transcribed_and_old_commands_explained(self):
        me = jid(ME)
        await self.ch._on_message(self.client, message(me, me, from_me=True, voice=True))
        self.assertEqual(self.commands, [("what's the weather?", True)])
        await self.ch._on_message(self.client, message(
            me, me, "Jarvis, lock the PC", True, sent=time.time() - 3600, mid="m2"))
        self.assertEqual(len(self.commands), 1)
        self.assertIn("while I was offline", self.client.sent[-1])

    async def test_an_old_command_delivered_twice_is_answered_once(self):
        me = jid(ME)
        old = message(me, me, "Jarvis, lock the PC", True, sent=time.time() - 3600)
        await self.ch._on_message(self.client, old)
        await self.ch._on_message(self.client, old)
        self.assertEqual(len(self.client.sent), 1)

    async def test_an_answer_under_the_accounts_other_id_still_counts(self):
        self.ch._me = {ME, LID}
        asking = asyncio.create_task(self.ch.ask(jid(ME), "Approve?", 5))
        await asyncio.sleep(0)
        lid = jid(LID, "lid")
        await self.ch._on_message(self.client, message(lid, lid, "no", True))
        self.assertEqual(await asking, "no")
        self.assertEqual(self.commands, [])

    async def test_an_answer_to_a_question_is_not_a_command(self):
        me = jid(ME)
        asking = asyncio.create_task(self.ch.ask(me, "Approve? Reply yes or no.", 5))
        await asyncio.sleep(0)
        await self.ch._on_message(self.client, message(me, me, "yes", True))
        self.assertEqual(await asking, "yes")
        self.assertEqual(self.commands, [])
        self.assertTrue(self.client.sent[0].startswith(wa.MARK))


class RemoteGateTests(unittest.TestCase):
    """During a WhatsApp command everything but a lookup asks, consent windows or not."""

    def setUp(self):
        import actions
        self.actions = actions
        self.addCleanup(actions.remote_turn, False)
        for name, val in (("is_approved", True), ("approve", None)):
            p = mock.patch.object(actions.browser, name, lambda *a, v=val: v)
            p.start()
            self.addCleanup(p.stop)

    def test_lookups_run_and_everything_else_asks(self):
        a = self.actions
        browse = {"type": "browser", "do": "click", "target": "Buy"}
        self.assertFalse(a.needs_permission(browse)[0])          # at the PC: approved
        a.remote_turn(True)
        self.assertFalse(a.needs_permission({"type": "weather"})[0])
        gated, kind, desc = a.needs_permission({"type": "set_volume", "level": 80})
        self.assertEqual((gated, kind), (True, "remote"))
        self.assertIn("level 80", desc)
        self.assertEqual(a.needs_permission(browse)[:2], (True, "browser"))
        self.assertFalse(a.consented("browser"))
        a.grant("browser")                                       # yes in the chat
        self.assertTrue(a.consented("browser"))
        self.assertFalse(a.needs_permission(browse)[0])
        self.assertTrue(a.needs_permission({"type": "set_volume", "level": 80})[0])
        a.remote_turn(False)
        self.assertFalse(a.needs_permission({"type": "set_volume", "level": 80})[0])


class TurnTests(unittest.IsolatedAsyncioTestCase):
    """main._run_whatsapp_turn: quiet on the PC, reply and approvals in the chat."""

    async def test_the_reply_goes_back_and_approval_is_asked_in_the_chat(self):
        import main
        sent, asked, quiet, shown = [], [], [], []
        chat = jid(ME)

        class FakeWa:
            async def typing(self, c, on):
                pass

            async def send(self, c, text):
                sent.append(text)

            async def send_image(self, c, data, caption=""):
                sent.append(("image", data))

            async def ask(self, c, question, timeout, on_timeout=""):
                asked.append(question)
                return "yes"

        async def fake_turn(text, emit_transcription, task_tier, shown_text=""):
            shown.append(shown_text)
            quiet.append(main.skills.is_muted())
            asked_first = main.needs_permission({"type": "open_app", "target": "notepad"})[0]
            quiet.append(asked_first)
            approved = await main.request_permission("computer", "shut the PC down")
            await main.emit("response_end", {"text": f"Approved: {approved}", "actions": []})

        main._pipeline_lock = asyncio.Lock()
        with mock.patch.object(main, "_wa", FakeWa()), \
             mock.patch.object(main, "_pipeline_turn", fake_turn):
            await main._run_whatsapp_turn("shut down the PC", chat)
        self.assertEqual(shown, ["📱 shut down the PC"])
        self.assertEqual(quiet, [True, True])
        self.assertFalse(main.needs_permission({"type": "open_app", "target": "notepad"})[0])
        self.assertFalse(main.skills.is_muted())          # quiet ends with the turn
        self.assertIn("shut the PC down", asked[0])
        self.assertEqual(sent, ["Approved: True"])
        self.assertIsNone(main._wa_chat)

    async def test_a_file_is_read_like_a_pc_upload(self):
        import main
        got, sent = [], []

        class FakeWa:
            async def typing(self, c, on):
                pass

            async def send(self, c, text):
                sent.append(text)

        async def fake_upload(name, mime, blob, prompt, task_tier=""):
            got.append((name, mime, blob, prompt))
            await main.emit("response", "A fern.")

        main._pipeline_lock = asyncio.Lock()
        with mock.patch.object(main, "_wa", FakeWa()),              mock.patch.object(main, "_upload_turn", fake_upload):
            await main._run_whatsapp_turn("what plant is this?", jid(ME),
                                          attachment=("photo.jpg", "image/jpeg", b"jpg"))
        self.assertEqual(got, [("photo.jpg", "image/jpeg", b"jpg", "what plant is this?")])
        self.assertEqual(sent, ["A fern."])

    async def test_a_task_shows_its_progress_before_the_reply(self):
        import main
        client = FakeClient()
        ch = wa.Channel(None, None, None)
        ch.client = client
        typing = []
        ch.typing = lambda c, on: typing.append(on) or asyncio.sleep(0)

        async def fake_turn(text, emit_transcription, task_tier, shown_text=""):
            await main.emit("agent_task", {"id": "t9", "kind": "browser", "goal": "play lofi",
                                           "source": "whatsapp"})
            await settle()
            await main.emit("agent_step", {"id": "t9", "line": "Opened YouTube"})
            await main.emit("agent_task_end", {"id": "t9", "ok": True, "summary": "Playing."})
            await main.emit("response_end", {"text": "Lofi is playing.", "actions": []})

        main._pipeline_lock = asyncio.Lock()
        with mock.patch.dict(sys.modules, fake_neonize()), \
             mock.patch.object(main, "_wa", ch), \
             mock.patch.object(main, "_pipeline_turn", fake_turn):
            await main._run_whatsapp_turn("play lofi on youtube", jid(ME))
        self.assertIn("⏳ Working on it: play lofi", client.sent[0])
        self.assertTrue(client.edits[-1][1].startswith(f"{wa.MARK} ✅ Finished: play lofi"))
        self.assertIn("→ Opened YouTube", client.edits[-1][1])
        self.assertEqual(client.sent[-1], f"{wa.MARK} Lofi is playing.")   # the reply last
        self.assertEqual((typing[0], typing[-1]), (True, False))

    async def test_a_command_that_has_to_wait_is_told_what_for(self):
        import main
        sent, ran = [], []

        class FakeWa:
            async def send(self, c, text):
                sent.append(text)

        async def fake_turn(text, chat, heard=False, attachment=None):
            ran.append(text)

        main._pipeline_lock = asyncio.Lock()
        await main._pipeline_lock.acquire()
        tracker = main.activity.Activity()
        tracker.on_event("agent_task", {"id": "x", "kind": "computer", "goal": "tidy my desktop"})
        try:
            with mock.patch.object(main, "_wa", FakeWa()), \
                 mock.patch.object(main.activity, "tracker", tracker), \
                 mock.patch.object(main, "_run_whatsapp_turn", fake_turn):
                await main._on_whatsapp_command("what's the time", jid(ME), False)
                await settle()
                self.assertIn("I'm busy with a desktop task: “tidy my desktop”", sent[0])
                self.assertIn("Jarvis, status", sent[0])
                await main._whatsapp_status(jid(ME))
        finally:
            main._pipeline_lock.release()
        self.assertEqual(ran, ["what's the time"])
        self.assertIn("Working on a desktop task", sent[1])


if __name__ == "__main__":
    unittest.main()
