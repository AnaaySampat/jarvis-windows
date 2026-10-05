"""Regression tests for the phone-task shim: an authenticated phone task runs
WITHOUT a PC prompt (approval lives on the phone as a fingerprint before send),
and a second concurrent remote task is rejected (single-flight — autopilot drives
one shared desktop)."""

import asyncio
import unittest
from unittest.mock import patch

import main


class RemoteShimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        main._remote_task_busy = False
        main._remote_task_id = ""
        main._remote_task_owner = ""
        main._interrupt = None
        main._remote_pending_question = None
        main._remote_finished.clear()

    async def _submit(self, *, busy=False, submit=None, result=None, calls=None, subscribe=None):
        ran = {"v": False}
        events = []  # (task_id, event, data) captured from emit_task_event
        calls = calls if calls is not None else []  # consent/run call order

        async def fake_run_task(kind, goal, **kw):
            ran["v"] = True
            calls.append("run")
            return result or {"ok": True, "summary": "done", "findings": []}

        async def fake_emit(task_id, event, data, **kw):
            events.append((task_id, event, data))
            return kw["seq"] if "seq" in kw else len(events)

        class FakeReg:
            directory = "."
            def list_devices(self): return [{"device_id": "dev1", "name": "Test phone"}]
            def record_event(self, entry): pass

        main._remote_task_busy = busy
        with patch.object(main, "is_current_sender_remote", return_value=True), \
             patch.object(main, "current_authenticated_device", return_value="dev1"), \
             patch.object(main, "_device_registry", FakeReg()), \
             patch.object(main, "emit_task_event", fake_emit), \
             patch.object(main, "subscribe_task", subscribe or (lambda *a, **k: True)), \
             patch.object(main, "subscribe_local_clients", lambda *a, **k: 0), \
             patch.object(main.browser, "is_approved", lambda: False), \
             patch.object(main.browser, "approve", lambda m=None: calls.append("approve")), \
             patch.object(main.browser, "revoke", lambda: calls.append("revoke")), \
             patch.object(main.computer, "is_armed", lambda: False), \
             patch.object(main.computer, "arm", lambda m=None: (calls.append("arm"), (True, "armed"))[1]), \
             patch.object(main.computer, "disarm", lambda: (calls.append("disarm"), (True, ""))[1]), \
             patch.object(main.autopilot, "run_task", fake_run_task):
            await main.handle_task_submit(submit or {"goal": "open notepad", "kind": "computer"})
        return ran["v"], events

    async def test_authenticated_task_runs_without_a_pc_prompt(self):
        # Approval is on the phone; the desktop trusts an authenticated task.
        ran, _ = await self._submit()
        self.assertTrue(ran, "task did not run")

    async def test_single_flight_rejects_a_concurrent_task(self):
        ran, _ = await self._submit(busy=True)
        self.assertFalse(ran, "a second concurrent task was not rejected")

    async def test_honours_phone_task_id_and_emits_durable_vocabulary(self):
        # The phone correlates events by the id it submitted, so the host must run
        # under that exact id and emit task.accepted + a terminal task.status whose
        # proof passes only when the task really succeeded.
        _, events = await self._submit(
            submit={"goal": "open notepad", "kind": "computer", "task_id": "task-abc123"},
            result={"ok": True, "summary": "done", "findings": ["saw the window"]},
        )
        self.assertTrue(events and all(e[0] == "task-abc123" for e in events),
                        "host did not run under the phone-supplied task_id")
        kinds = [e[1] for e in events]
        self.assertIn("task.accepted", kinds)
        terminal = next(d for tid, ev, d in events if ev == "task.status")
        self.assertEqual(terminal["state"], "succeeded")
        self.assertTrue(terminal["proof"]["passed"])
        self.assertEqual(terminal["proof"]["evidence_ids"], ["saw the window"])

    async def test_remote_task_arms_consent_and_restores_it(self):
        # autopilot.run_task re-checks the PC-side consent window every step, so
        # an unarmed remote task dies at step 0 ("control expired mid-task").
        # The shim must arm before running and restore the prior (unconsented)
        # posture after — for BOTH kinds.
        calls = []
        await self._submit(submit={"goal": "open notepad", "kind": "computer"}, calls=calls)
        self.assertEqual(calls, ["arm", "run", "disarm"])
        calls = []
        await self._submit(submit={"goal": "open youtube", "kind": "browser"}, calls=calls)
        self.assertEqual(calls, ["approve", "run", "revoke"])

    async def test_remote_cancel_stops_only_the_owners_task(self):
        # A phone task.cancel trips the interrupt the autopilot loop polls, so the
        # running task breaks — but ONLY when it comes from the device that submitted
        # it. A different (or spoofed) device cannot stop someone else's task.
        main._interrupt = asyncio.Event()
        main._remote_task_id = "task-xyz"
        main._remote_task_owner = "dev1"
        with patch.object(main, "is_current_sender_remote", return_value=True), \
             patch.object(main, "current_authenticated_device", return_value="other"):
            await main.handle_task_cancel({"task_id": "task-xyz"})
        self.assertFalse(main._interrupt.is_set(), "a non-owner cancelled the task")
        with patch.object(main, "is_current_sender_remote", return_value=True), \
             patch.object(main, "current_authenticated_device", return_value="dev1"):
            await main.handle_task_cancel({"task_id": "task-xyz", "reason": "user_cancelled"})
        self.assertTrue(main._interrupt.is_set(), "owner cancel did not trip the interrupt")

    async def test_auto_kind_is_classified_on_the_desktop(self):
        # A kind-less "auto" task (live-view command mode) is routed by the desktop
        # classifier, not silently forced to browser.
        seen = {}

        async def fake_run_task(kind, goal, **kw):
            seen["kind"] = kind
            return {"ok": True, "summary": "done", "findings": []}

        async def fake_emit(task_id, event, data, **kw):
            return 1

        async def fake_classify(goal):
            return "computer"

        class FakeReg:
            def list_devices(self): return [{"device_id": "dev1", "name": "P"}]
            def record_event(self, entry): pass

        main._remote_task_busy = False
        with patch.object(main, "is_current_sender_remote", return_value=True), \
             patch.object(main, "current_authenticated_device", return_value="dev1"), \
             patch.object(main, "_device_registry", FakeReg()), \
             patch.object(main, "emit_task_event", fake_emit), \
             patch.object(main, "subscribe_task", lambda *a, **k: True), \
             patch.object(main, "subscribe_local_clients", lambda *a, **k: 0), \
             patch.object(main, "_classify_task_kind", fake_classify), \
             patch.object(main.browser, "is_approved", lambda: False), \
             patch.object(main.browser, "approve", lambda m=None: None), \
             patch.object(main.browser, "revoke", lambda: None), \
             patch.object(main.computer, "is_armed", lambda: False), \
             patch.object(main.computer, "arm", lambda m=None: (True, "armed")), \
             patch.object(main.computer, "disarm", lambda: (True, "")), \
             patch.object(main.autopilot, "run_task", fake_run_task):
            await main.handle_task_submit({"goal": "open notepad", "kind": "auto"})
        self.assertEqual(seen.get("kind"), "computer",
                         "an 'auto' task must be classified, not forced to browser")

    async def test_clarify_answer_resolves_only_for_owner_and_prompt(self):
        main._remote_task_id = "task-xyz"
        main._remote_task_owner = "dev1"
        fut = asyncio.get_running_loop().create_future()
        main._remote_pending_question = ("q-abc", fut)
        with patch.object(main, "is_current_sender_remote", return_value=True), \
             patch.object(main, "current_authenticated_device", return_value="dev1"):
            await main.handle_task_answer(
                {"task_id": "task-xyz", "prompt_id": "q-abc", "answer": "yes do it"})
        self.assertTrue(fut.done() and fut.result() == "yes do it")

        # A foreign device may not answer another phone's question.
        fut2 = asyncio.get_running_loop().create_future()
        main._remote_pending_question = ("q-def", fut2)
        with patch.object(main, "is_current_sender_remote", return_value=True), \
             patch.object(main, "current_authenticated_device", return_value="intruder"):
            await main.handle_task_answer(
                {"task_id": "task-xyz", "prompt_id": "q-def", "answer": "x"})
        self.assertFalse(fut2.done(), "a foreign device must not resolve the clarify")

    async def test_failed_task_has_no_passing_proof(self):
        _, events = await self._submit(
            result={"ok": False, "summary": "could not", "findings": []},
        )
        terminal = next(d for tid, ev, d in events if ev == "task.status")
        self.assertEqual(terminal["state"], "failed")
        self.assertNotIn("proof", terminal)

    async def test_busy_rejection_reaches_the_phone_as_terminal_failure(self):
        # The submitter isn't subscribed yet and the phone has no "rejected" state:
        # it must get a subscribed, terminal "failed" with a reason, not silence.
        subscribed = []
        ran, events = await self._submit(
            busy=True, submit={"goal": "x", "kind": "browser", "task_id": "task-busy"},
            subscribe=lambda tid, *a: subscribed.append(tid) or True)
        self.assertFalse(ran)
        self.assertEqual(subscribed, ["task-busy"])
        (tid, ev, data), = events
        self.assertEqual((tid, ev, data["state"]), ("task-busy", "task.status", "failed"))
        self.assertIn("busy", data["summary"])

    async def _replay(self, data, device="dev1"):
        events = []

        async def fake_emit(task_id, event, payload, **kw):
            events.append((task_id, event, payload, kw.get("seq")))
            return kw.get("seq", 0)

        main._remote_emit_lock = asyncio.Lock()
        with patch.object(main, "is_current_sender_remote", return_value=True),              patch.object(main, "current_authenticated_device", return_value=device),              patch.object(main, "emit_task_event", fake_emit),              patch.object(main, "subscribe_task", lambda *a, **k: True),              patch.object(main.autopilot, "run_task", side_effect=AssertionError("re-ran")):
            await main.handle_task_submit(data) if "goal" in data else                 await main.handle_task_subscribe(data)
        return events

    async def test_finished_task_replays_its_result_to_a_reconnecting_owner(self):
        # Phone socket died mid-task and came back after it finished: the terminal
        # status must replay (with original seqs) instead of a false stall timeout.
        await self._submit(submit={"goal": "open notepad", "kind": "computer",
                                   "task_id": "task-done"})
        events = await self._replay({"task_id": "task-done", "resume_after_seq": 1})
        self.assertTrue(events and events[-1][1] == "task.status")
        self.assertEqual(events[-1][2]["state"], "succeeded")
        self.assertTrue(all(seq > 1 for *_x, seq in events), "replayed seen rows")
        self.assertEqual(await self._replay({"task_id": "task-done"}, device="intruder"), [],
                         "a foreign device must not see another phone's result")

    async def test_resubmitting_a_finished_task_replays_instead_of_rerunning(self):
        await self._submit(submit={"goal": "open notepad", "kind": "computer",
                                   "task_id": "task-again"})
        events = await self._replay({"goal": "open notepad", "kind": "computer",
                                     "task_id": "task-again"})
        self.assertEqual([e[1] for e in events][0], "task.accepted")
        self.assertEqual(events[-1][2]["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
