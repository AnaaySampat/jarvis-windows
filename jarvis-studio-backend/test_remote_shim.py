"""Regression tests for the phone-task shim: an authenticated phone task runs
WITHOUT a PC prompt (approval lives on the phone as a fingerprint before send),
and a second concurrent remote task is rejected (single-flight — autopilot drives
one shared desktop)."""

import asyncio
import unittest
import tempfile
import threading
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import AsyncMock, patch

import main
import workflow_store


class RemoteShimTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # These existing task/approval tests start past passkey verification;
        # the real verifier and missing/wrong-key boundaries are tested below.
        authorization = patch.object(main, "_task_passkey", SimpleNamespace(
            verify=lambda *_: main.Verification(True, revision="test-authorized"),
            is_current=lambda _: True,
        ))
        authorization.start()
        self.addCleanup(authorization.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        journal_patch = patch.object(main, "_workflow_journal", workflow_store.Journal(Path(self.temp.name) / "workflows.sqlite3"))
        journal_patch.start()
        self.addCleanup(journal_patch.stop)
        chrome_patch = patch.object(main, "_users_chrome", AsyncMock(return_value=False))
        chrome_patch.start()
        self.addCleanup(chrome_patch.stop)
        main._remote_task_busy = False
        main._remote_task_id = ""
        main._remote_task_owner = ""
        main._interrupt = None
        main._remote_pending_question = None
        main._remote_finished.clear()

    async def _submit(self, *, busy=False, submit=None, result=None, calls=None, subscribe=None,
                      run_task=None, answer_approval=None, approval_callback=None):
        ran = {"v": False}
        events = []  # (task_id, event, data) captured from emit_task_event
        calls = calls if calls is not None else []  # consent/run call order

        async def fake_run_task(kind, goal, **kw):
            ran["v"] = True
            calls.append("run")
            outcome = dict(result or {"ok": True, "summary": "done", "findings": []})
            if outcome.get("ok"):
                outcome.setdefault("evidence", [{"id": "observed-window", "kind": "observation", "detail": "Window is present"}])
            return outcome

        async def fake_emit(task_id, event, data, **kw):
            events.append((task_id, event, data))
            if data.get("kind") == "approval.challenge" and approval_callback is not None:
                await approval_callback(data["payload"])
            if data.get("kind") == "approval.challenge" and answer_approval is not None:
                await main.handle_approval_response({**data["payload"], "approved": answer_approval})
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
             patch.object(main.autopilot, "run_task", run_task or fake_run_task):
            await main.handle_task_submit(submit or {"goal": "open notepad", "kind": "computer"})
        return ran["v"], events

    async def test_the_hud_draws_a_phone_task_like_its_own(self):
        # Before, the HUD got only task.* events, which it never drew: a phone task ran
        # with nothing on the PC's screen.
        hud = []

        async def fake_hud_emit(event, data):
            hud.append((event, data))

        async def stepping_run_task(kind, goal, **kw):
            await kw["on_step"]("Opened Notepad")
            return {"ok": True, "summary": "Notepad is open.", "findings": [],
                    "evidence": [{"id": "observed-window", "kind": "observation", "detail": "Notepad visible"}]}

        with patch.object(main, "emit", fake_hud_emit):
            await self._submit(submit={"goal": "open notepad", "kind": "computer",
                                       "task_id": "task-hud1"},
                               run_task=stepping_run_task)
        events = [e for e, _ in hud]
        self.assertEqual(events[:1], ["agent_task"])
        self.assertEqual(hud[0][1], {"id": "task-hud1", "kind": "computer",
                                     "goal": "open notepad", "source": "phone"})
        self.assertIn(("agent_step", {"id": "task-hud1", "line": "Opened Notepad", "ok": True}), hud)
        end = next(d for e, d in hud if e == "agent_task_end")
        self.assertEqual((end["id"], end["ok"], end["summary"]), ("task-hud1", True, "Notepad is open."))

    async def test_authenticated_task_runs_without_a_pc_prompt(self):
        # Approval is on the phone; the desktop trusts an authenticated task.
        ran, _ = await self._submit()
        self.assertTrue(ran, "task did not run")

    async def test_each_new_phone_task_requires_the_laptop_passkey(self):
        with tempfile.TemporaryDirectory() as directory:
            store = main.TaskPasskeyStore(directory)
            responses = []
            async def send(event, data):
                responses.append((event, data))
            with patch.object(main, "_task_passkey", store), patch.object(main, "send_to_current", send):
                ran, _ = await self._submit()
                self.assertFalse(ran)
                self.assertEqual(responses[-1][1]["error_code"], "passkey_not_configured")
                store.set("test-only-task-passkey")
                for supplied in (None, "incorrect-test-key", "test-only-task-passkey", None):
                    ran, _ = await self._submit(submit={
                        "goal": "open notepad", "kind": "computer", "passkey": supplied,
                    })
                    self.assertEqual(ran, supplied == "test-only-task-passkey")
                self.assertNotIn("test-only-task-passkey", str(responses))

    async def test_only_the_laptop_can_set_or_view_passkey_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            store = main.TaskPasskeyStore(directory)
            responses = []
            async def send(event, data):
                responses.append((event, data))
            with patch.object(main, "_task_passkey", store), patch.object(main, "send_to_current", send):
                with patch.object(main, "is_current_sender_remote", return_value=True):
                    await main.handle_task_passkey_set({"passkey": "test-only-task-passkey"})
                    await main.handle_task_passkey_state({})
                self.assertFalse(store.state()["configured"])
                self.assertEqual(responses, [])
                with patch.object(main, "is_current_sender_remote", return_value=False):
                    await main.handle_task_passkey_set({"passkey": "test-only-task-passkey"})
                    await main.handle_task_passkey_state({})
                self.assertTrue(store.state()["configured"])
                self.assertNotIn("test-only-task-passkey", str(responses))
                self.assertNotIn("digest", str(responses))

    async def test_local_task_route_is_exempt_from_remote_passkey_verification(self):
        with patch.object(main, "is_current_sender_remote", return_value=False), \
             patch.object(main._task_passkey, "verify") as verify:
            await main.handle_task_submit({"goal": "open notepad"})
        verify.assert_not_called()

    async def test_stop_during_passkey_verification_prevents_later_execution(self):
        started, release = threading.Event(), threading.Event()
        def verify(*_args):
            started.set()
            release.wait(2)
            return main.Verification(True, revision="test")
        responses = []
        async def send(event, data):
            responses.append(data)
        with patch.object(main, "_task_passkey", SimpleNamespace(verify=verify, is_current=lambda _: True)), \
             patch.object(main, "send_to_current", send):
            task = asyncio.create_task(self._submit(submit={
                "task_id": "checking-passkey", "goal": "open notepad", "kind": "computer",
            }))
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                with patch.object(main, "is_current_sender_remote", return_value=True), \
                     patch.object(main, "current_authenticated_device", return_value="dev1"):
                    await main.handle_task_cancel({"task_id": "checking-passkey"})
            finally:
                release.set()
            ran, _ = await task
        self.assertFalse(ran)
        self.assertEqual(responses[-1]["state"], "cancelled")

    async def test_changing_the_passkey_before_admission_invalidates_an_older_check(self):
        with tempfile.TemporaryDirectory() as directory:
            store = main.TaskPasskeyStore(directory)
            store.set("old-test-task-passkey")
            responses = []
            async def classify(_goal):
                await asyncio.to_thread(store.set, "new-test-task-passkey")
                return "computer"
            async def send(event, data):
                responses.append(data)
            with patch.object(main, "_task_passkey", store), patch.object(main, "_classify_task_kind", classify), \
                 patch.object(main, "send_to_current", send):
                ran, _ = await self._submit(submit={
                    "goal": "open notepad", "kind": "auto", "passkey": "old-test-task-passkey",
                })
            self.assertFalse(ran)
            self.assertEqual(responses[-1]["error_code"], "passkey_changed")

    async def test_screenshot_asks_on_phone_before_capturing_and_returns_the_actual_image(self):
        calls = []
        hud = []
        image = "data:image/png;base64,c3ludGhldGlj"
        async def emit(event, data):
            hud.append(event)
        with patch.object(main.skills, "capture_screen_b64", return_value=image) as capture, \
             patch.object(main, "emit", emit):
            _, events = await self._submit(
                submit={"goal": "send me a screenshot of my laptop", "kind": "browser", "task_id": "shot-1"},
                calls=calls, answer_approval=True,
            )
        capture.assert_called_once()
        self.assertEqual(calls, [], "capturing a screenshot must not arm mouse/browser control")
        self.assertNotIn("permission_request", hud)
        kinds = [data.get("kind") for _, _, data in events]
        self.assertIn("approval.challenge", kinds)
        self.assertIn("approval.resolved", kinds)
        terminal = next(data for _, event, data in events if event == "task.status")
        self.assertEqual(terminal["state"], "succeeded")
        self.assertEqual(terminal["artifacts"][0]["data_url"], image)
        self.assertIn(terminal["artifacts"][0]["id"], terminal["proof"]["evidence_ids"])

    async def test_denied_phone_approval_never_captures_the_screen(self):
        with patch.object(main.skills, "capture_screen_b64") as capture:
            _, events = await self._submit(
                submit={"goal": "take a screenshot", "kind": "computer"}, answer_approval=False,
            )
        capture.assert_not_called()
        terminal = next(data for _, event, data in events if event == "task.status")
        self.assertEqual(terminal["state"], "failed")
        self.assertNotIn("artifacts", terminal)

    async def test_the_laptop_and_another_phone_cannot_approve_the_request(self):
        async def attempt(challenge):
            response = {**challenge, "approved": True}
            with patch.object(main, "is_current_sender_remote", return_value=False):
                await main.handle_approval_response(response)
                await main.handle_permission_response({"id": challenge["step_id"], "approved": True})
            with patch.object(main, "current_authenticated_device", return_value="other-phone"):
                await main.handle_approval_response(response)
            self.assertEqual(len(main.companion_approvals.pending_for_task(challenge["task_id"], "dev1")), 1)
            capture.assert_not_called()
        with patch.object(main.skills, "capture_screen_b64", return_value="data:image/png;base64,c2FtcGxl") as capture:
            await self._submit(submit={"goal": "take a screenshot", "kind": "computer"},
                               approval_callback=attempt, answer_approval=True)
        capture.assert_called_once()

    async def test_stop_while_waiting_on_phone_never_captures_or_delivers_an_image(self):
        main._interrupt = asyncio.Event()
        async def stop(challenge):
            await main.handle_task_cancel({"task_id": challenge["task_id"]})
        with patch.object(main.skills, "capture_screen_b64") as capture:
            _, events = await self._submit(submit={"goal": "take a screenshot", "kind": "computer"},
                                          approval_callback=stop)
        capture.assert_not_called()
        terminal = next(data for _, event, data in events if event == "task.status")
        self.assertEqual(terminal["state"], "cancelled")
        self.assertNotIn("artifacts", terminal)

    async def test_phone_approval_timeout_never_captures_the_screen(self):
        request = main.companion_approvals.Session.request
        async def short_request(session, kind, description, timeout=120):
            return await request(session, kind, description, timeout=0.001)
        with patch.object(main.companion_approvals.Session, "request", short_request), \
             patch.object(main.skills, "capture_screen_b64") as capture:
            _, events = await self._submit(submit={"goal": "take a screenshot", "kind": "computer"})
        capture.assert_not_called()
        self.assertEqual(next(data for _, event, data in events if event == "task.status")["state"], "failed")

    async def test_nested_permission_requests_are_routed_to_the_originating_phone(self):
        hud = []
        async def emit(event, data):
            hud.append(event)
        async def run(kind, goal, **kwargs):
            approved = await main.request_permission("read", "read a requested file")
            return {"ok": approved, "summary": "Read" if approved else "Denied"}
        with patch.object(main, "emit", emit):
            _, events = await self._submit(run_task=run, answer_approval=True)
        self.assertNotIn("permission_request", hud)
        self.assertTrue(any(data.get("kind") == "approval.challenge" for _, _, data in events))

    async def test_remote_permission_without_a_task_context_cannot_prompt_the_desktop(self):
        with patch.object(main, "is_current_sender_remote", return_value=True), \
             patch.object(main, "emit") as emit:
            self.assertFalse(await main.request_permission("screen", "capture the screen"))
        emit.assert_not_called()

    async def test_task_status_and_recovery_index_are_owner_scoped(self):
        main._remote_finished.update({
            "mine": ("dev1", [(1, "task.accepted", {"task_id": "mine", "goal": "my task"}),
                              (2, "task.status", {"task_id": "mine", "state": "succeeded", "artifacts": ["large image"]})]),
            "theirs": ("dev2", [(1, "task.status", {"task_id": "theirs", "summary": "private"})]),
        })
        sent = []
        async def send(event, data):
            sent.append((event, data))
        with patch.object(main, "is_current_sender_remote", return_value=True), \
             patch.object(main, "current_authenticated_device", return_value="dev1"), \
             patch.object(main, "send_to_current", send), \
             patch.object(main, "subscribe_task", return_value=True):
            await main.handle_task_list({"source_device": "dev2"})
            await main.handle_task_status({"task_id": "theirs"})
            self.assertEqual(len(sent), 1)
            rows = sent[0][1]["tasks"]
            self.assertEqual([row["task_id"] for row in rows], ["mine"])
            self.assertNotIn("artifacts", rows[0])
            await main.handle_task_status({"task_id": "mine"})
        self.assertEqual(sent[-1][1]["artifacts"], ["large image"])

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
        self.assertEqual(terminal["proof"]["evidence_ids"], ["observed-window"])

    async def test_boolean_success_without_evidence_is_unverified(self):
        _, events = await self._submit(result={"ok": True, "summary": "done", "evidence": []})
        terminal = next(d for _, event, d in events if event == "task.status")
        self.assertEqual(terminal["status"], "unverified")
        self.assertNotIn("proof", terminal)

    async def test_restart_resubmission_uses_journal_instead_of_repeating_action(self):
        await self._submit(submit={"goal": "open notepad", "kind": "computer", "task_id": "task-restart"})
        main._remote_finished.clear()
        main._remote_task_id = ""
        events = await self._replay({"goal": "open notepad", "kind": "computer", "task_id": "task-restart"})
        self.assertEqual(events[-1][2]["state"], "succeeded")

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
