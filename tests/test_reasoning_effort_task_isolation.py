"""Regression tests for #118: delegated tasks must not share the foreground effort state.

Hermes passes both ``session_id`` and ``task_id`` to ``llm_request`` middleware and to
``post_tool_call`` hooks. A delegated child or a background fork can share the parent's
session ID with a different task ID, and it inherits the parent's session context.
"""
from __future__ import annotations

import json
import unittest

from hermes_switchyard.reasoning_effort_adapter import (
    ReasoningEffortController,
    build_effort_record,
    last_receipt,
)

from tests.test_reasoning_effort_adapter import FakeClient
from tests.test_reasoning_effort_user_cap import OPUS, Env, opus_request, sent_effort

SESSION = "synthetic-session"
SESSION_KEY = "agent:main:synthetic:dm:1"
CHILD = "subagent-0-synthetic"


def codex_route(model: str) -> dict:
    return {"provider": "openai-codex", "model": model, "api_mode": "codex_responses"}


def codex(model: str, effort: str) -> dict:
    return {"model": model, "input": "synthetic", "reasoning": {"effort": effort, "summary": "auto"}}


class TaskIsolationTests(unittest.TestCase):
    def make(self, choice: str = "low", **kwargs):
        client = FakeClient(choice=choice)
        records: list[dict] = []
        env = kwargs.pop(
            "session_env", Env(HERMES_SESSION_ID=SESSION, HERMES_SESSION_KEY=SESSION_KEY)
        )
        controller = ReasoningEffortController(
            client_factory=lambda: client, record_decision=records.append, session_env=env, **kwargs,
        )
        return controller, client, records

    @staticmethod
    def request(controller, request, *, route, task, turn, session=SESSION):
        result = controller.on_llm_request(
            request, session_id=session, task_id=task, turn_id=turn, **route
        )
        return sent_effort(request, result)

    def parent(self, controller, request, route=OPUS, turn="t1"):
        return self.request(controller, request, route=route, task=SESSION, turn=turn)

    def child(self, controller, request, route=OPUS, turn="c1", session=SESSION):
        return self.request(controller, request, route=route, task=CHILD, turn=turn, session=session)

    # -- issue reproduction ----------------------------------------------------

    def test_child_model_and_cap_do_not_replace_foreground_status(self):
        controller, _, _ = self.make()
        self.parent(controller, codex("gpt-6-sol", "low"), route=codex_route("gpt-6-sol"))
        self.child(controller, codex("gpt-6-luna", "medium"), route=codex_route("gpt-6-luna"))
        status = controller.session_status()
        self.assertEqual(status["session_id"], SESSION)
        self.assertEqual(status["model"], "gpt-6-sol")
        self.assertEqual(status["user_level"], "low")
        self.assertEqual(status["last_sent"], "low")
        self.assertEqual(status["requests"], 1)
        text = controller.handle_command("effort status")
        self.assertIn("model: gpt-6-sol", text)
        self.assertIn("your level (cap): low", text)
        self.assertNotIn("gpt-6-luna", text)

    def test_a_b_a_with_different_models_keeps_the_foreground_choice(self):
        controller, client, _ = self.make(choice="low")
        self.assertEqual(self.parent(controller, opus_request("max")), "low")
        self.assertEqual(
            self.child(controller, codex("gpt-6-luna", "medium"), route=codex_route("gpt-6-luna")),
            "low",
        )
        calls = len(client.calls)
        self.assertEqual(self.parent(controller, opus_request("max")), "low")
        receipt = last_receipt()
        self.assertEqual(receipt["reason_code"], "cached")
        self.assertEqual(receipt["model"], OPUS["model"])
        self.assertEqual(len(client.calls), calls, "foreground re-baselined after a child request")
        status = controller.session_status(SESSION)
        self.assertEqual((status["model"], status["user_level"]), (OPUS["model"], "max"))

    def test_same_model_lower_child_level_does_not_pin_the_foreground(self):
        controller, _, _ = self.make(choice="low")
        self.assertEqual(self.parent(controller, opus_request("high")), "low")
        self.child(controller, opus_request("medium"))
        self.assertNotEqual(last_receipt()["reason_code"], "pinned_by_user_change")
        self.assertEqual(self.parent(controller, opus_request("high")), "low")
        status = controller.session_status(SESSION)
        self.assertEqual(status["mode"], "auto")
        self.assertEqual(status["user_level"], "high")

    def test_overlapping_child_turn_and_tools_keep_foreground_outcomes(self):
        controller, _, _ = self.make(choice="max", allow_raise=True)
        hook = controller.build_post_tool_call_hook()
        # "max" is outside the uncapped candidates, so the fake picks the lowest one.
        self.assertEqual(self.parent(controller, opus_request("high")), "low")
        hook(tool_name="shell", status="error", error_message="exit 1", session_id=SESSION, task_id=SESSION)
        self.child(controller, opus_request("medium"))
        hook(tool_name="shell", result='{"ok": true}', session_id=SESSION, task_id=CHILD)
        # The parent's latest tool failed, so allow_raise may offer one level above its cap.
        self.assertEqual(self.parent(controller, opus_request("high")), "max")
        receipt = last_receipt()
        self.assertIs(receipt["stuck_signal"], True)
        self.assertEqual(receipt["cap"], "max")
        self.assertEqual(receipt["turn_id"], "t1")

    # -- command binding -----------------------------------------------------

    def test_pin_and_auto_bind_to_the_foreground_task(self):
        controller, _, _ = self.make(choice="low")
        self.assertEqual(self.parent(controller, opus_request("high")), "low")
        self.assertEqual(self.child(controller, opus_request("high")), "low")
        self.assertIn("pinned", controller.handle_command("effort pin"))
        self.assertEqual(self.parent(controller, opus_request("high")), "high")
        self.assertEqual(self.child(controller, opus_request("high")), "low", "child followed the parent pin")
        self.assertEqual(controller.session_status()["mode"], "pinned")
        self.assertIn("auto", controller.handle_command("effort auto"))
        self.assertEqual(self.parent(controller, opus_request("high")), "low")
        status = controller.session_status()
        self.assertEqual((status["mode"], status["model"], status["user_level"]), ("auto", OPUS["model"], "high"))

    def test_pending_pin_is_not_consumed_by_a_child_that_runs_first(self):
        controller, client, _ = self.make(choice="low")
        self.assertTrue(controller.set_mode("pin")["pending"])
        # The child has its own session ID but inherits the parent's session context.
        self.assertEqual(self.child(controller, opus_request("high"), session="synthetic-child-session"), "low")
        self.assertEqual(self.parent(controller, opus_request("high")), "high")
        self.assertEqual(last_receipt()["reason_code"], "pinned")
        status = controller.session_status()
        self.assertEqual((status["session_id"], status["mode"]), (SESSION, "pinned"))
        self.assertEqual(len(client.calls), 1)

    def test_child_session_does_not_capture_the_command_session_key(self):
        env = Env(HERMES_SESSION_KEY=SESSION_KEY)
        controller, _, _ = self.make(session_env=env)
        self.parent(controller, opus_request("high"))
        self.child(controller, codex("gpt-6-luna", "medium"), route=codex_route("gpt-6-luna"),
                   session="synthetic-child-session")
        status = controller.session_status()
        self.assertEqual((status["session_id"], status["model"]), (SESSION, OPUS["model"]))

    # -- preserved contracts -------------------------------------------------

    def test_tui_foreground_task_keeps_tool_outcomes_without_session_context(self):
        env = Env(HERMES_SESSION_ID=SESSION, HERMES_SESSION_KEY=SESSION_KEY)
        controller, _, _ = self.make(choice="max", allow_raise=True, session_env=env)
        route = {"route": OPUS, "task": SESSION_KEY, "turn": "t1"}
        self.assertEqual(self.request(controller, opus_request("high"), **route), "low")
        env.values = {}  # A tool worker thread may not carry the session context.
        controller.build_post_tool_call_hook()(
            tool_name="shell", status="error", error_message="exit 1", session_id=SESSION, task_id=SESSION_KEY,
        )
        env.values = {"HERMES_SESSION_ID": SESSION, "HERMES_SESSION_KEY": SESSION_KEY}
        self.assertEqual(self.request(controller, opus_request("high"), **route), "max")
        self.assertIs(last_receipt()["stuck_signal"], True)
        self.assertEqual(controller.session_status()["requests"], 2)

    def test_foreground_task_stays_foreground_after_session_rotation(self):
        env = Env(HERMES_SESSION_ID=SESSION)
        controller, _, _ = self.make(choice="low", session_env=env)
        self.parent(controller, opus_request("high"))
        # Compression rotates the session ID mid-turn; the turn keeps its original task ID.
        env.values = {"HERMES_SESSION_ID": "synthetic-rotated-session"}
        self.request(controller, opus_request("high"), route=OPUS, task=SESSION, turn="t1",
                     session="synthetic-rotated-session")
        status = controller.session_status()
        self.assertEqual((status["session_id"], status["known"]), ("synthetic-rotated-session", True))
        self.assertIn("pinned", controller.handle_command("effort pin"))
        self.assertEqual(self.request(controller, opus_request("high"), route=OPUS, task=SESSION, turn="t1",
                                      session="synthetic-rotated-session"), "high")

    def test_requests_without_a_distinct_task_keep_session_state(self):
        controller, _, _ = self.make(choice="low", session_env=Env())
        for task in (None, SESSION):
            self.request(controller, opus_request("high"), route=OPUS, task=task, turn="t1")
        self.assertEqual(controller.session_status(SESSION)["requests"], 2)

    def test_child_receipts_and_records_carry_no_task_identity(self):
        controller, _, records = self.make(choice="low")
        self.child(controller, opus_request("high"))
        receipt = last_receipt()
        self.assertEqual(receipt["session_id"], SESSION)
        self.assertNotIn(CHILD, json.dumps(receipt))
        self.assertNotIn(CHILD, json.dumps([build_effort_record(record) for record in records]))

    def test_delegated_task_state_is_bounded(self):
        from hermes_switchyard import reasoning_effort_adapter as adapter

        controller, _, _ = self.make(choice="low")
        limit = adapter._TASK_STATE_LIMIT
        for index in range(limit + 20):
            self.request(controller, opus_request("high"), route=OPUS, task=f"subagent-{index}", turn="c")
        self.assertLessEqual(len(controller._task_states), limit)
        self.assertEqual(controller.session_status(SESSION)["known"], False)


if __name__ == "__main__":
    unittest.main()
