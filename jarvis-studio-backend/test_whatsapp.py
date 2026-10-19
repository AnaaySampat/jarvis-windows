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
        self.sent, self.downloads = [], 0

    async def send_message(self, chat, text):
        self.sent.append(text)

    async def download_any(self, msg):
        self.downloads += 1
        return b"OggS"


def message(chat, sender, text="", from_me=False, voice=False, sent=None, mid="m1"):
    msg = NS(text=text, HasField=lambda f: voice and f == "audioMessage")
    info = NS(MessageSource=src(chat, sender, from_me), ID=mid,
              Timestamp=int(sent or time.time()))
    return NS(Info=info, Message=msg)


class ChannelTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fake = types.ModuleType("neonize.utils.message")
        fake.extract_text = lambda msg: msg.text
        for name, mod in (("neonize", types.ModuleType("neonize")),
                          ("neonize.utils", types.ModuleType("neonize.utils")),
                          ("neonize.utils.message", fake)):
            p = mock.patch.dict(sys.modules, {name: mod})
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(wa, "settings", lambda: {"enabled": True, "allow": [FRIEND]})
        p.start()
        self.addCleanup(p.stop)
        self.commands = []

        async def on_command(text, chat, heard):
            self.commands.append((text, heard))

        async def transcribe(audio):
            return "Jarvis, what's the weather?"

        async def on_state(_s):
            pass

        self.ch = wa.Channel(on_command, on_state, transcribe)
        self.ch._me = {ME}
        self.client = self.ch.client = FakeClient()

    async def test_self_chat_commands_need_the_name_and_notes_are_left_alone(self):
        me = jid(ME)
        await self.ch._on_message(self.client, message(me, me, "Jarvis, open notepad", True))
        await self.ch._on_message(self.client, message(me, me, "buy milk", True, mid="m2"))
        await self.ch._on_message(self.client, message(me, me, "Jarvis, open notepad", True))
        self.assertEqual(self.commands, [("open notepad", False)])     # m1 not run twice
        self.assertEqual(self.client.sent, [])

    async def test_a_listed_number_needs_no_name_and_strangers_get_nothing(self):
        await self.ch._on_message(self.client, message(jid(FRIEND), jid(FRIEND), "lights?"))
        await self.ch._on_message(self.client, message(jid("9111"), jid("9111"), "hi", mid="m2"))
        self.assertEqual(self.commands, [("lights?", False)])
        self.assertEqual(self.client.sent, [])

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
            approved = await main.request_permission("computer", "shut the PC down")
            await main.emit("response_end", {"text": f"Approved: {approved}", "actions": []})

        main._pipeline_lock = asyncio.Lock()
        with mock.patch.object(main, "_wa", FakeWa()), \
             mock.patch.object(main, "_pipeline_turn", fake_turn):
            await main._run_whatsapp_turn("shut down the PC", chat)
        self.assertEqual(shown, ["📱 shut down the PC"])
        self.assertEqual(quiet, [True])
        self.assertFalse(main.skills.is_muted())          # quiet ends with the turn
        self.assertIn("shut the PC down", asked[0])
        self.assertEqual(sent, ["Approved: True"])
        self.assertIsNone(main._wa_chat)


if __name__ == "__main__":
    unittest.main()
