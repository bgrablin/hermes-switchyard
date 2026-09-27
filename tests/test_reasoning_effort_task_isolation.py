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

from tests.test_reasoning_effort_adapter import FakeClient, ensure_turn
from tests.test_reasoning_effort_user_cap import OPUS, Env, opus_request, sent_effort

SESSION = "synthetic-session"
SESSION_KEY = "agent:main:synthetic:dm:1"
CHILD = "subagent-0-synthetic"
ROTATED = "synthetic-session-rotated"


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
        # #121: Hermes fires pre_llm_call once per turn with the clean user message.
        ensure_turn(controller, session_id=session, task_id=task, turn_id=turn)
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

    # -- rotation before the first request (fresh controller) -----------------
    # Hermes binds the turn's task ID before turn-start compaction. Compression then moves the
    # agent and HERMES_SESSION_ID to a new session, so the first request this controller sees is
    # (session=ROTATED, task=SESSION). A mode the user set before that message is pending under
    # the IDs that the foreground command context carried.

    def rotate(self, env, **values):
        env.values = {"HERMES_SESSION_ID": ROTATED, **values}

    def test_pending_pin_applies_when_rotation_precedes_the_first_request(self):
        env = Env(HERMES_SESSION_ID=SESSION, HERMES_SESSION_KEY=SESSION_KEY)
        controller, client, _ = self.make(choice="low", session_env=env)
        self.assertTrue(controller.set_mode("pin")["pending"])
        self.rotate(env, HERMES_SESSION_KEY=SESSION_KEY)
        sent = self.request(controller, opus_request("high"), route=OPUS, task=SESSION, turn="t1", session=ROTATED)
        self.assertEqual(sent, "high", "foreground turn ignored the pending pin")
        self.assertEqual(last_receipt()["reason_code"], "pinned")
        self.assertEqual(len(client.calls), 0)
        status = controller.session_status()
        self.assertEqual((status["session_id"], status["known"], status["mode"]), (ROTATED, True, "pinned"))
        self.assertEqual((status["model"], status["user_level"]), (OPUS["model"], "high"))

    def test_pending_pin_without_session_key_applies_after_rotation(self):
        env = Env(HERMES_SESSION_ID=SESSION)  # CLI: no gateway session key
        controller, client, _ = self.make(choice="low", session_env=env)
        self.assertTrue(controller.set_mode("pin")["pending"])
        self.rotate(env)
        sent = self.request(controller, opus_request("high"), route=OPUS, task=SESSION, turn="t1", session=ROTATED)
        self.assertEqual(sent, "high")
        self.assertEqual(len(client.calls), 0)
        status = controller.session_status()
        self.assertEqual((status["known"], status["mode"]), (True, "pinned"))
        self.assertNotIn(SESSION, controller._pending_modes, "pending mode left behind")

    def test_rotation_with_pending_pin_keeps_children_and_forks_isolated(self):
        env = Env(HERMES_SESSION_ID=SESSION, HERMES_SESSION_KEY=SESSION_KEY)
        controller, _, _ = self.make(choice="low", session_env=env)
        controller.set_mode("pin")
        self.rotate(env, HERMES_SESSION_KEY=SESSION_KEY)
        # A background fork shares the rotated session ID; a delegated child has its own.
        fork = "synthetic-fork-uuid"
        self.assertEqual(self.request(controller, opus_request("high"), route=OPUS, task=fork, turn="f1",
                                      session=ROTATED), "low", "fork consumed the pending pin")
        self.assertEqual(self.child(controller, opus_request("high"), session="synthetic-child-session"), "low")
        self.assertEqual(self.request(controller, opus_request("high"), route=OPUS, task=SESSION, turn="t1",
                                      session=ROTATED), "high")
        # Same model, lower child level: the child must not change the foreground cap or mode.
        self.child(controller, opus_request("medium"), session=ROTATED, turn="c2")
        self.assertNotEqual(last_receipt()["reason_code"], "pinned")
        status = controller.session_status()
        self.assertEqual((status["session_id"], status["mode"], status["user_level"]), (ROTATED, "pinned", "high"))
        self.assertEqual(status["requests"], 1)

    def test_latest_pending_mode_wins_across_rotation_aliases(self):
        # The user sets one mode before a rotation and the opposite mode after it, before the
        # first request. Each command leaves a pending mode under a different alias.
        for session_key in ("", SESSION_KEY):
            for first, second, sent_level, final in (("pin", "auto", "low", "auto"), ("auto", "pin", "high", "pinned")):
                with self.subTest(session_key=bool(session_key), order=f"{first}->{second}"):
                    extra = {"HERMES_SESSION_KEY": session_key} if session_key else {}
                    env = Env(HERMES_SESSION_ID=SESSION, **extra)
                    controller, _, _ = self.make(choice="low", session_env=env)
                    self.assertTrue(controller.set_mode(first)["pending"])
                    self.rotate(env, **extra)
                    self.assertTrue(controller.set_mode(second)["pending"])
                    # A fork on the rotated session must not consume or apply either mode.
                    self.assertEqual(self.request(controller, opus_request("high"), route=OPUS,
                                                  task="synthetic-fork-uuid", turn="f1", session=ROTATED), "low")
                    sent = self.request(controller, opus_request("high"), route=OPUS, task=SESSION,
                                        turn="t1", session=ROTATED)
                    self.assertEqual(sent, sent_level, "an older pending mode overrode the latest command")
                    status = controller.session_status()
                    self.assertEqual((status["session_id"], status["mode"]), (ROTATED, final))
                    self.assertEqual(controller._pending_modes, {}, "pending mode left behind")

    def test_pending_auto_overrides_a_pinned_default(self):
        # (rotate, pin before the rotation): the last case leaves an older pin under another alias.
        for rotate, pin_first in ((False, False), (True, False), (True, True)):
            with self.subTest(rotate=rotate, pin_first=pin_first):
                env = Env(HERMES_SESSION_ID=SESSION)
                controller, client, _ = self.make(choice="low", session_env=env, mode="pinned")
                if pin_first:
                    self.assertTrue(controller.set_mode("pin")["pending"])
                else:
                    self.assertTrue(controller.set_mode("auto")["pending"])
                session = SESSION
                if rotate:
                    self.rotate(env)
                    session = ROTATED
                if pin_first:
                    self.assertTrue(controller.set_mode("auto")["pending"])
                sent = self.request(controller, opus_request("high"), route=OPUS, task=SESSION, turn="t1",
                                    session=session)
                self.assertEqual(sent, "low", "pinned default ignored the pending auto")
                self.assertEqual(len(client.calls), 1)
                status = controller.session_status()
                self.assertEqual((status["default_mode"], status["mode"]), ("pinned", "auto"))
                # A delegated child still starts from the configured default.
                self.assertEqual(self.child(controller, opus_request("high"), session=session), "high")

    def test_stale_pending_mode_does_not_override_a_newer_command(self):
        env = Env(HERMES_SESSION_ID=SESSION)  # CLI: no gateway session key
        controller, _, _ = self.make(choice="low", session_env=env)
        self.assertTrue(controller.set_mode("pin")["pending"])
        self.rotate(env)
        # The next turn is foreground under the rotated ID; the old pin stays under SESSION.
        self.request(controller, opus_request("high"), route=OPUS, task=ROTATED, turn="t2", session=ROTATED)
        self.assertFalse(controller.set_mode("auto")["pending"])
        # A late request from the earlier turn consumes the older pin but must not apply it.
        self.assertEqual(self.request(controller, opus_request("high"), route=OPUS, task=SESSION, turn="t1",
                                      session=ROTATED), "low")
        self.assertEqual(controller.session_status()["mode"], "auto")
        self.assertEqual(controller._pending_modes, {})

    def test_rotation_without_foreground_evidence_stays_capped_until_the_next_turn(self):
        # Documented limit: with no pending mode, no session key match, and no earlier request,
        # Hermes gives no positive signal that (ROTATED, SESSION) is the foreground turn. The
        # controller keeps that turn's state apart (at or below the request's level), and the
        # next turn, whose task ID is the rotated session ID, is foreground.
        env = Env(HERMES_SESSION_ID=SESSION)
        controller, _, _ = self.make(choice="max", session_env=env)
        self.rotate(env)
        sent = self.request(controller, opus_request("medium"), route=OPUS, task=SESSION, turn="t1", session=ROTATED)
        self.assertIn(sent, ("low", "medium"))
        self.assertEqual(controller.session_status()["known"], False)
        self.request(controller, opus_request("high"), route=OPUS, task=ROTATED, turn="t2", session=ROTATED)
        status = controller.session_status()
        self.assertEqual((status["session_id"], status["known"], status["model"]), (ROTATED, True, OPUS["model"]))

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
