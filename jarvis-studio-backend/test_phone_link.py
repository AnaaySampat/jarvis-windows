"""phone_link.py: "set an alarm on my phone" from the PC or WhatsApp goes to the paired
phone over its live link, and only that phone's own answer counts."""

import asyncio
import unittest
from unittest import mock

import actions
import phone_link
from llm import live_tools as lt


class LinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sent = []
        self.live = {"phone-1"}

        async def send(device, event, data):
            self.sent.append((device, event, data))
            return 1 if device in self.live else 0
        phone_link.configure(asyncio.get_running_loop(), send, lambda: set(self.live),
                             lambda: True)
        self.addCleanup(phone_link.configure, None, None, None, None)

    async def answer(self, device, **data):
        await asyncio.sleep(0)
        cid = self.sent[-1][2]["id"]
        return phone_link.on_result(device, {"id": cid, **data})

    async def test_the_phones_own_answer_comes_back(self):
        task = asyncio.create_task(phone_link.command("set an alarm for 7am"))
        await asyncio.sleep(0)
        device, event, data = self.sent[0]
        self.assertEqual((device, event, data["text"]), ("phone-1", "phone.command",
                                                         "set an alarm for 7am"))
        self.assertFalse(await self.answer("someone-else", ok=True, reply="Done"))
        self.assertTrue(await self.answer("phone-1", ok=True, reply="Alarm set for 07:00."))
        res = await task
        self.assertTrue(res["ok"])
        self.assertEqual(res["message"], "Phone: Alarm set for 07:00.")
        self.assertIn("The phone did it", res["feed_to_model"]["content"])
        self.assertEqual(phone_link._pending, {})

    async def test_only_a_literal_true_is_success(self):
        task = asyncio.create_task(phone_link.command("turn on the flashlight"))
        await asyncio.sleep(0)
        await self.answer("phone-1", ok="yes", reply="Flashlight on, I think.")
        res = await task
        self.assertFalse(res["ok"])
        self.assertIn("did NOT do it", res["feed_to_model"]["content"])

    async def test_no_phone_online_says_so_and_sends_nothing(self):
        self.live.clear()
        res = await phone_link.command("set a timer for 5 minutes")
        self.assertFalse(res["ok"])
        self.assertEqual(res["message"], phone_link.NOT_CONNECTED)
        self.assertEqual(self.sent, [])

    async def test_a_phone_that_never_answers_is_not_called_done(self):
        res = await phone_link.command("set an alarm", timeout=0.05)
        self.assertFalse(res["ok"])
        self.assertIn("didn't answer", res["message"])
        self.assertEqual(phone_link._pending, {})

    async def test_an_action_on_an_executor_thread_waits_for_the_answer(self):
        loop = asyncio.get_running_loop()
        job = loop.run_in_executor(None, actions.run_action,
                                   {"type": "phone_command", "request": "silence my phone"})
        for _ in range(100):
            if self.sent:
                break
            await asyncio.sleep(0.01)
        await self.answer("phone-1", ok=True, reply="Ringer is on silent.")
        res = await job
        self.assertEqual((res["type"], res["ok"]), ("phone_command", True))


class ToolTests(unittest.TestCase):
    def setUp(self):
        lt._recent.clear()
        lt._used.clear()

    def test_the_tool_exists_only_with_a_paired_phone(self):
        with mock.patch.object(phone_link, "paired", lambda: False):
            names = {d["name"] for d in lt.function_declarations(places_ok=True, web_ok=True)}
            self.assertNotIn("phone_command", names)
        with mock.patch.object(phone_link, "paired", lambda: True):
            names = {d["name"] for d in lt.function_declarations(places_ok=True, web_ok=True)}
            self.assertIn("phone_command", names)
            lt.begin_turn("set an alarm on my phone for 6:30")
            self.assertIn("phone_command", {d["name"] for d in lt.turn_declarations()})
            lt.begin_turn("what's the weather")
            self.assertNotIn("phone_command", {d["name"] for d in lt.turn_declarations()})
        self.assertEqual(lt.to_spec("phone_command", {"request": "wake me at 7"}),
                         {"type": "phone_command", "request": "wake me at 7"})

    def test_from_whatsapp_it_asks_first_and_at_the_pc_it_doesnt(self):
        spec = {"type": "phone_command", "request": "set an alarm for 7am"}
        self.assertFalse(actions.needs_permission(spec)[0])
        actions.remote_turn(True)
        try:
            gated, _kind, desc = actions.needs_permission(spec)
        finally:
            actions.remote_turn(False)
        self.assertTrue(gated)
        self.assertEqual(desc, "ask your phone to: set an alarm for 7am")


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_a_paired_phone_can_answer(self):
        import main
        seen = []
        with mock.patch.object(main.phone_link, "on_result", lambda d, data: seen.append(d)), \
             mock.patch.object(main, "is_current_sender_remote", return_value=False):
            await main.handle_phone_result({"id": "x", "ok": True})
        self.assertEqual(seen, [])
        with mock.patch.object(main.phone_link, "on_result", lambda d, data: seen.append(d)), \
             mock.patch.object(main, "is_current_sender_remote", return_value=True), \
             mock.patch.object(main, "current_authenticated_device", return_value="phone-1"):
            await main.handle_phone_result({"id": "x", "ok": True})
        self.assertEqual(seen, ["phone-1"])


if __name__ == "__main__":
    unittest.main()
