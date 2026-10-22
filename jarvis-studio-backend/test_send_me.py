"""send_to_me (actions/send_me.py): only ever the user's own WhatsApp chat or mailbox,
with a fake channel and a fake SMTP server."""

import asyncio
import os
import tempfile
import threading
import unittest
from unittest import mock

from actions import send_me

SELF = "own-chat"


class FakeChannel:
    def __init__(self, linked=True):
        self.linked, self.sent = linked, []

    def own_chat(self):
        return SELF if self.linked else None

    async def send(self, chat, text):
        self.sent.append(("text", chat, text))
        return True

    async def send_image(self, chat, data, caption=""):
        self.sent.append(("image", chat, caption))
        return True

    async def send_file(self, chat, data, name, mime, caption=""):
        self.sent.append(("file", chat, name))
        return True


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout=30):
        self.host = host

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def login(self, user, password):
        self.user = user

    def send_message(self, msg):
        FakeSMTP.sent.append((self.host, msg))


class SendToMeTests(unittest.TestCase):
    def setUp(self):
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()
        self.addCleanup(self.loop.call_soon_threadsafe, self.loop.stop)
        self.ch = FakeChannel()
        send_me.attach_main(self.loop, lambda: self.ch)
        self.dir = tempfile.mkdtemp()
        self.pdf = os.path.join(self.dir, "summary.pdf")
        with open(self.pdf, "wb") as fh:
            fh.write(b"%PDF-1.4 hello")
        p = mock.patch("actions.fs_access.is_allowed", lambda path: path.startswith(self.dir))
        p.start()
        self.addCleanup(p.stop)
        p = mock.patch("actions.email_box.configured", lambda: False)
        p.start()
        self.addCleanup(p.stop)

    def test_text_goes_to_the_owners_own_chat_whatever_recipient_is_named(self):
        res = send_me.run({"type": "send_to_me", "text": "hello", "to": "someone@else.com"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.ch.sent, [("text", SELF, "hello")])

    def test_a_fresh_screenshot_goes_as_a_picture(self):
        shot = os.path.join(self.dir, "shot.png")
        with open(shot, "wb") as fh:
            fh.write(b"\x89PNG")
        with mock.patch("actions.skills.screenshot", lambda: (True, f"Screenshot saved to {shot}.")):
            res = send_me.run({"attach": "screenshot", "text": "your screen"})
        self.assertTrue(res["ok"], res)
        self.assertEqual(self.ch.sent, [("image", SELF, "your screen")])

    def test_a_document_goes_as_a_file_and_a_long_text_follows_it(self):
        res = send_me.run({"attach": self.pdf, "text": "x" * 1500})
        self.assertTrue(res["ok"], res)
        self.assertEqual([s[0] for s in self.ch.sent], ["file", "text"])

    def test_files_outside_the_allowed_folders_are_refused(self):
        res = send_me.run({"attach": r"C:\Windows\System32\config\SAM"})
        self.assertFalse(res["ok"])
        self.assertIn("isn't in a folder I'm allowed to read", res["message"])
        self.assertEqual(self.ch.sent, [])

    def test_nothing_linked_says_where_to_link(self):
        self.ch.linked = False
        res = send_me.run({"text": "hi"})
        self.assertFalse(res["ok"])
        self.assertIn("Settings ▸ Connectors", res["message"])

    def test_email_goes_from_and_to_the_connected_address_only(self):
        self.ch.linked = False
        FakeSMTP.sent = []
        with mock.patch("actions.email_box.configured", lambda: True), \
             mock.patch("actions.email_box.settings", lambda: {
                 "address": "me@gmail.com", "provider": "gmail", "host": "imap.gmail.com",
                 "port": 993}), \
             mock.patch("app_secrets.get", lambda k: "abcd efgh"), \
             mock.patch.object(send_me, "SMTP_SSL", FakeSMTP):
            res = send_me.run({"text": "Downloads summary\n3 files", "attach": self.pdf,
                               "to": "stranger@x.com"})
        self.assertTrue(res["ok"], res)
        self.assertIn("me@gmail.com", res["message"])
        host, msg = FakeSMTP.sent[0]
        self.assertEqual((host, msg["From"], msg["To"]), ("smtp.gmail.com", "me@gmail.com",
                                                          "me@gmail.com"))
        self.assertEqual(msg["Subject"], "Downloads summary")
        self.assertEqual([a.get_filename() for a in msg.iter_attachments()], ["summary.pdf"])

    def test_smtp_server_follows_the_imap_one(self):
        self.assertEqual(send_me.smtp_server("icloud", ""), ("smtp.mail.me.com", 587))
        self.assertEqual(send_me.smtp_server("other", "imap.example.com"), ("smtp.example.com", 465))
        self.assertIsNone(send_me.smtp_server("other", "mail.example.com"))


if __name__ == "__main__":
    unittest.main()
