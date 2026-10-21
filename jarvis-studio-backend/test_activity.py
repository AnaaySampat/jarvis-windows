"""activity.py: what "Jarvis, status" says, built from the same events the HUD draws."""

import unittest

import activity


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class ActivityTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.a = activity.Activity(self.clock)

    def feed(self, *events):
        for event, data in events:
            self.a.on_event(event, data)

    def test_a_running_task_says_where_it_came_from_and_its_last_steps(self):
        self.feed(("transcription", "📱 find a flight to Goa"),
                  ("status", "working"),
                  ("agent_task", {"id": "t1", "kind": "browser", "goal": "find a flight to Goa",
                                  "source": "whatsapp"}))
        for i in range(6):
            self.feed(("agent_step", {"id": "t1", "line": f"step {i}"}))
        self.feed(("agent_step", {"id": "other", "line": "not this task"}))
        self.feed(("agent_shot", {"id": "t1", "image": "data:image/jpeg;base64,AAAA"}))
        self.clock.t += 83
        text = self.a.describe()
        self.assertIn("a browser task from WhatsApp: “find a flight to Goa”", text)
        self.assertIn("1 min 23 s so far", text)
        self.assertNotIn("step 1", text)                      # only the last few
        self.assertIn("→ step 5", text)
        self.assertNotIn("not this task", text)
        self.assertEqual(self.a.screenshot(), "data:image/jpeg;base64,AAAA")
        self.assertTrue(self.a.busy())

    def test_a_phone_task_and_a_plain_question(self):
        self.feed(("agent_task", {"id": "p", "kind": "computer", "goal": "open notepad",
                                  "source": "phone"}))
        self.assertIn("a desktop task from your phone", self.a.headline())
        self.feed(("agent_task_end", {"id": "p", "ok": True, "summary": "Notepad is open."}))
        self.feed(("transcription", "what's the weather?"), ("status", "thinking"))
        self.assertEqual(self.a.headline(), "answering “what's the weather?”")
        self.assertEqual(self.a.screenshot(), "")

    def test_idle_tells_the_last_task_honestly(self):
        self.feed(("agent_task", {"id": "t", "kind": "browser", "goal": "book a table"}),
                  ("agent_task_end", {"id": "t", "ok": False, "summary": "The site was down."}),
                  ("status", "idle"))
        self.clock.t += 120
        text = self.a.describe()
        self.assertIn("Nothing running right now.", text)
        self.assertIn("“book a table” ✗ The site was down.", text)
        self.assertFalse(self.a.busy())
        self.clock.t += activity._RECENT_S
        self.assertNotIn("book a table", self.a.describe())

    def test_idle_with_no_task_recalls_the_last_answer(self):
        self.feed(("transcription", "what time is it?"), ("status", "thinking"),
                  ("response_end", {"sid": "1", "text": "It's 9:40.", "actions": []}),
                  ("status", "idle"))
        self.assertIn("“what time is it?” → It's 9:40.", self.a.describe())

    def test_commands_waiting_their_turn_are_counted(self):
        self.a.queued("set an alarm for 7")
        self.a.queued("what's on my agenda")
        self.assertIn("Waiting after it: 2 commands (next: “set an alarm for 7”).",
                      self.a.describe())
        self.a.dequeued("set an alarm for 7")
        self.a.dequeued("set an alarm for 7")              # twice is harmless
        self.assertIn("1 command (next: “what's on my agenda”)", self.a.describe())


if __name__ == "__main__":
    unittest.main()
