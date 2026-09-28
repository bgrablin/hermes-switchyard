"""v0.5.5 adaptive effort: first-rotation foreground, /reasoning as a new cap, step-level
adaptation, the per-turn receipt line, and the last-decisions status view.

All values are synthetic. The fake Jev decides only from the ``current_request`` text and the
closed-set step metadata that the controller sends; it never sees tool output.
"""
from __future__ import annotations

import json
import re
import unittest

from hermes_switchyard import egress_redaction, reasoning_effort_adapter as adapter
from hermes_switchyard.reasoning_effort_adapter import ReasoningEffortController, last_receipt

from tests.test_reasoning_effort_user_cap import OPUS, Env, opus_request, sent_effort

SESSION = "synthetic-session"

def setUpModule():
    # Text-path tests need a Hermes-like egress redactor; CI has Hermes, this box may not.
    egress_redaction._reset_for_tests(lambda text: text, loaded=True)

def tearDownModule():
    egress_redaction._reset_for_tests()

ROTATED = "synthetic-session-rotated"
CHILD = "subagent-0-synthetic"
# "status ping" is routine but not trivial: it reaches Jev, unlike the closed-list greetings.
ROUTINE = re.compile(r"^(hi|hello|thanks!?|thank you|ok|status ping)$", re.IGNORECASE)


class TextJev:
    """Fake Jev: greetings go to the lowest candidate; anything else keeps the highest.

    After a routine read streak on a read-only request ("review ..."), it also picks the lowest
    candidate.
    Consequential text (deletion, production) returns stakes 1.0.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def decide(self, state, questions, *, public_or_sanitized_data_ack=True):
        self.calls.append({"state": state, "questions": questions})
        levels = list(questions["reasoning_effort"]["criteria"])
        text = str(state.get("current_request") or "")
        stakes = 1.0 if re.search(r"\b(delete|prod|production|drop)\b", text, re.IGNORECASE) else 0.0
        routine = bool(ROUTINE.match(text.strip()))
        streak = int(state.get("routine_success_streak") or 0)
        pick = levels[0] if (routine or (streak >= 2 and stakes == 0.0 and "review" in text)) else levels[-1]
        return {"answers": {
            "reasoning_effort": {"choice": pick, "confidence": 0.9,
                                 "probabilities": {level: 1.0 if level == pick else 0.0 for level in levels}},
            "stakes": {"noul": stakes},
        }}


def make(**kwargs):
    jev = TextJev()
    env = kwargs.pop("session_env", Env(HERMES_SESSION_ID=SESSION))
    controller = ReasoningEffortController(client_factory=lambda: jev, session_env=env, **kwargs)
    return controller, jev, env


def begin(controller, text, *, session=SESSION, task=None, turn="t1", parent=""):
    controller.build_pre_llm_call_hook()(
        session_id=session, task_id=task or session, turn_id=turn, user_message=text,
        conversation_history=[], is_first_turn=False, model=OPUS["model"], platform="cli",
        parent_session_id=parent,
    )


def send(controller, effort, *, session=SESSION, task=None, turn="t1"):
    request = opus_request(effort)
    result = controller.on_llm_request(request, session_id=session, task_id=task or session, turn_id=turn, **OPUS)
    return sent_effort(request, result)


def tool(controller, name, *, ok=True, session=SESSION, task=None):
    hook = controller.build_post_tool_call_hook()
    if ok:
        hook(tool_name=name, result='{"ok": true}', session_id=session, task_id=task or session)
    else:
        hook(tool_name=name, status="error", error_message="exit 1", session_id=session, task_id=task or session)


class FirstRotationForegroundTests(unittest.TestCase):
    """#118 follow-up: a fresh controller whose first request arrives after a rotation."""

    def test_pre_llm_call_without_parent_marks_the_rotated_turn_foreground(self):
        controller, _, env = make()
        env.values = {"HERMES_SESSION_ID": ROTATED}
        # Hermes rotates before pre_llm_call: (session=ROTATED, task=SESSION, parent="").
        begin(controller, "hi", session=ROTATED, task=SESSION)
        self.assertEqual(send(controller, "high", session=ROTATED, task=SESSION), "low")
        status = controller.session_status()
        self.assertEqual((status["session_id"], status["known"]), (ROTATED, True))
        self.assertEqual((status["model"], status["user_level"]), (OPUS["model"], "high"))

    def test_delegated_child_pre_llm_call_stays_isolated(self):
        controller, _, _ = make()
        begin(controller, "hi")
        self.assertEqual(send(controller, "high"), "low")
        begin(controller, "hi", task=CHILD, turn="c1", parent=SESSION)
        self.assertEqual(send(controller, "medium", task=CHILD, turn="c1"), "medium")
        status = controller.session_status()
        self.assertEqual((status["user_level"], status["requests"]), ("high", 1))

    def test_host_without_parent_field_gives_no_foreground_evidence(self):
        controller, _, env = make()
        env.values = {"HERMES_SESSION_ID": ROTATED}
        controller.build_pre_llm_call_hook()(
            session_id=ROTATED, task_id=SESSION, turn_id="t1", user_message="hi",
        )
        # Without the field the rotated task stays isolated and capped (fail closed).
        self.assertEqual(send(controller, "high", session=ROTATED, task=SESSION), "low")
        self.assertFalse(controller.session_status()["known"])


class UserLevelChangeTests(unittest.TestCase):
    """Addendum item 2: /reasoning sets a new cap; it does not turn adaptation off."""

    def test_reasoning_max_mid_session_then_thanks_goes_below_max(self):
        controller, jev, _ = make()
        begin(controller, "refactor the parser module")
        self.assertEqual(send(controller, "high"), "high")
        # The user runs /reasoning max; Hermes sends max from the next request on.
        begin(controller, "status ping", turn="t2")
        sent = send(controller, "max", turn="t2")
        self.assertNotEqual(sent, "max")
        self.assertEqual(sent, "low")
        receipt = last_receipt()
        self.assertEqual((receipt["mode"], receipt["cap"]), ("auto", "max"))
        status = controller.session_status()
        self.assertEqual((status["mode"], status["user_level"]), ("auto", "max"))
        self.assertEqual(list(jev.calls[-1]["questions"]["reasoning_effort"]["criteria"])[-1], "max")

    def test_same_turn_level_change_rebaselines_and_asks_again(self):
        controller, jev, _ = make()
        begin(controller, "status ping")
        self.assertEqual(send(controller, "high"), "low")
        calls = len(jev.calls)
        self.assertEqual(send(controller, "xhigh"), "low")
        self.assertEqual(len(jev.calls), calls + 1, "a new cap must be a fresh decision")
        self.assertEqual(controller.session_status()["mode"], "auto")

    def test_explicit_pin_still_pins(self):
        controller, jev, _ = make()
        begin(controller, "hi")
        send(controller, "high")
        self.assertIn("pinned", controller.handle_command("effort pin"))
        self.assertEqual(send(controller, "max"), "max")
        self.assertEqual(last_receipt()["reason_code"], "pinned")


class StepAdaptationTests(unittest.TestCase):
    """Addendum item 3: bounded step-level re-asks from metadata only."""

    def run_rounds(self, controller, kinds, *, text="review the parser module", ok=None):
        begin(controller, text)
        sent = [send(controller, "high")]
        for index, name in enumerate(kinds):
            tool(controller, name, ok=True if ok is None else ok[index])
            sent.append(send(controller, "high"))
        return sent

    def test_routine_read_streak_asks_again_and_may_lower(self):
        controller, jev, _ = make()
        sent = self.run_rounds(controller, ["read_file", "search_files", "read_file", "read_file"])
        self.assertEqual(sent[0], "high")
        # A step choice is at most one level below the cap.
        self.assertIn("medium", sent[3:], "no step-level lowering after a routine read streak")
        self.assertNotIn("low", sent)
        self.assertEqual(list(jev.calls[-1]["questions"]["reasoning_effort"]["criteria"]), ["medium", "high"])
        self.assertEqual(len(jev.calls), 2)
        state = jev.calls[-1]["state"]
        self.assertEqual(state["turn_phase"], "after_tool")
        self.assertEqual(state["recent_tool_kinds"], ["read"])
        self.assertGreaterEqual(state["routine_success_streak"], 2)
        self.assertNotIn("read_file", json.dumps(jev.calls), "tool names reached Jev")
        self.assertNotIn('"ok": true', json.dumps(jev.calls), "tool output reached Jev")

    def test_write_round_restores_the_cap_without_a_call(self):
        controller, jev, _ = make()
        sent = self.run_rounds(controller, ["read_file", "read_file", "read_file", "write_file"])
        self.assertEqual(sent[3], "medium")
        self.assertEqual(sent[4], "high", "a lowered choice survived a write round")
        self.assertEqual(last_receipt()["reason_code"], "kept_requested_after_write")
        self.assertEqual(len(jev.calls), 2, "restoring the cap must not call Jev")
        # After a write in this turn, later read streaks do not ask again.
        for _ in range(4):
            tool(controller, "read_file")
            self.assertEqual(send(controller, "high"), "high")
        self.assertEqual(len(jev.calls), 2)

    def test_change_request_never_lowers_at_step_level(self):
        controller, jev, _ = make()
        sent = self.run_rounds(controller, ["read_file"] * 8, text="review and fix the parser module")
        self.assertEqual(set(sent), {"high"})
        self.assertEqual(len(jev.calls), 1, "a change request must not get step asks")

    def test_failure_never_lowers(self):
        controller, _, _ = make()
        sent = self.run_rounds(controller, ["read_file", "read_file", "read_file", "terminal"],
                               ok=[True, True, True, False])
        self.assertEqual(sent[-1], "high")

    def test_stakes_turn_never_lowers_at_step_level(self):
        controller, _, _ = make()
        sent = self.run_rounds(controller, ["read_file"] * 6, text="delete the prod database backups")
        self.assertEqual(set(sent), {"high"})

    def test_extra_calls_are_bounded(self):
        controller, jev, _ = make()
        self.run_rounds(controller, ["read_file"] * 30)
        # One new-turn call; step re-asks need 3 rounds and stop when two decisions agree.
        self.assertLessEqual(len(jev.calls), 3)

    def test_step_adaptation_can_be_turned_off(self):
        controller, jev, _ = make(step_adaptation=False)
        sent = self.run_rounds(controller, ["read_file"] * 6)
        self.assertEqual(set(sent), {"high"})
        self.assertEqual(len(jev.calls), 1)

    def test_tool_kinds_are_closed_set(self):
        classify_tool_kind = adapter.classify_tool_kind
        self.assertEqual(classify_tool_kind("read_file"), "read")
        self.assertEqual(classify_tool_kind("patch"), "write")
        self.assertEqual(classify_tool_kind("terminal"), "exec")
        self.assertEqual(classify_tool_kind("mcp_private_vendor_thing"), "other")
        self.assertEqual(classify_tool_kind(None), "other")


class ReceiptLineTests(unittest.TestCase):
    def finish(self, controller, text="Synthetic answer.", turn="t1", session=SESSION):
        hook = controller.build_transform_llm_output_hook()
        return hook(response_text=text, session_id=session, model=OPUS["model"], platform="cli", turn_id=turn)

    def test_receipt_line_is_off_when_configured_off(self):
        controller, _, _ = make(receipt_line=False)
        begin(controller, "hi")
        send(controller, "high")
        self.assertIsNone(self.finish(controller))

    def test_receipt_line_names_the_change_and_latency(self):
        controller, _, _ = make(receipt_line=True)
        begin(controller, "status ping")
        send(controller, "high")
        text = self.finish(controller)
        self.assertTrue(text.startswith("Synthetic answer."))
        line = text.splitlines()[-1]
        self.assertRegex(line, r"^Reasoning: high→low · \d+ ms$")

    def test_receipt_line_says_kept_when_jev_kept_the_level(self):
        controller, _, _ = make(receipt_line=True)
        begin(controller, "refactor the parser module")
        send(controller, "high")
        self.assertRegex(self.finish(controller).splitlines()[-1],
                         r"^Reasoning: kept at high — cloud decision · \d+ ms$")

    def test_child_turn_gets_no_receipt_line(self):
        controller, _, _ = make(receipt_line=True)
        begin(controller, "hi", task=CHILD, turn="c1", parent=SESSION)
        send(controller, "high", task=CHILD, turn="c1")
        self.assertIsNone(self.finish(controller, turn="c1"))

    def test_command_turns_the_receipt_line_on_and_off(self):
        controller, _, _ = make(receipt_line=False)
        self.assertIn("work", controller.handle_command("effort receipt on"))
        begin(controller, "hi")
        send(controller, "high")
        self.assertIsNotNone(self.finish(controller))
        self.assertIn("off", controller.handle_command("effort receipt off"))
        begin(controller, "hi", turn="t2")
        send(controller, "high", turn="t2")
        self.assertIsNone(self.finish(controller, turn="t2"))
        self.assertIn("always", controller.handle_command("effort receipt always"))
        self.assertTrue(controller.handle_command("effort receipt maybe").startswith("Usage:"))


class StatusHistoryTests(unittest.TestCase):
    def test_status_shows_the_last_five_decisions(self):
        controller, _, _ = make()
        for index in range(7):
            begin(controller, "hi" if index % 2 else "refactor the parser module", turn=f"t{index}")
            send(controller, "high", turn=f"t{index}")
        status = controller.session_status()
        self.assertEqual(len(status["recent"]), 5)
        self.assertEqual(set(status["recent"][0]), {"cap", "sent", "reason", "latency_ms"})
        text = controller.handle_command("effort status")
        self.assertIn("last 5 decisions (cap -> sent, why, latency):", text)
        self.assertIn("Cap high · last sent", text)
        self.assertNotIn("jev_selected", text)
        rows = [line for line in text.splitlines() if line.startswith("    ")]
        self.assertEqual(len(rows), 5)
        self.assertRegex(rows[-1], r"^    high -> (low|high), (cloud decision|local decision \(greeting\)), (\d+ ms|no call)$")


if __name__ == "__main__":
    unittest.main()
