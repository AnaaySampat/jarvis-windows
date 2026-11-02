import asyncio
import unittest
from unittest.mock import patch

from autonomy import companion_approvals as approvals
from autonomy.companion_screenshot import requested


class ApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.events = []

        async def emit(event, data):
            self.events.append((event, data))
        self.session = approvals.Session("task-1", "phone-1", emit, None)

    async def start(self, timeout=1):
        task = asyncio.create_task(self.session.request("screen", "capture the screen", timeout))
        await asyncio.sleep(0)
        return task, self.events[0][1]["payload"]

    async def test_only_the_requesting_phone_can_approve_the_exact_one_shot_action(self):
        task, challenge = await self.start()
        response = {**challenge, "approved": True}
        self.assertFalse(approvals.respond("other-phone", response))
        for field in ("task_id", "approval_id", "step_id", "action_digest", "expires_at", "approved"):
            self.assertFalse(approvals.respond("phone-1", {**response, field: "wrong"}))
        self.assertFalse(task.done())
        self.assertTrue(approvals.respond("phone-1", response))
        self.assertTrue(await task)
        self.assertFalse(approvals.respond("phone-1", response))
        self.assertEqual(self.events[-1][1]["kind"], "approval.resolved")
        self.assertTrue(self.events[-1][1]["payload"]["approved"])

    async def test_expiry_cancellation_and_timeout_fail_closed(self):
        task, challenge = await self.start()
        with patch.object(approvals.time, "time", return_value=challenge["expires_at"]):
            self.assertFalse(approvals.respond("phone-1", {**challenge, "approved": True}))
        approvals.cancel_task("task-1", "other-phone")
        self.assertFalse(task.done())
        approvals.cancel_task("task-1", "phone-1")
        self.assertFalse(await task)
        self.events.clear()
        task, _ = await self.start(timeout=0.01)
        self.assertFalse(await task)
        self.assertFalse(self.events[-1][1]["payload"]["approved"])


class ScreenshotRequestTests(unittest.TestCase):
    def test_direct_requests_capture_without_an_operator(self):
        for text in ("send me a screenshot of my laptop", "Take a screenshot",
                     "Could you send a screenshot of the PC to my phone?",
                     "capture the screen shot and send it to me"):
            with self.subTest(text=text):
                self.assertTrue(requested(text))
        for text in ("Open Notepad and take a screenshot", "What is a screenshot?",
                     "Take a screenshot then delete my files", "send me a screenshot of a website"):
            with self.subTest(text=text):
                self.assertFalse(requested(text))
