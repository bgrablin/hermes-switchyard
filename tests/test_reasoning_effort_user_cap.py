"""Replay tests for #108: the user's /reasoning level is the cap for adaptive effort."""
from __future__ import annotations

import tempfile
import unittest
import io
import json
from contextlib import redirect_stdout
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from hermes_switchyard.reasoning_effort_adapter import (
    ReasoningEffortController,
    append_effort_record,
    build_effort_record,
    choose_reasoning_effort,
    effort_stats,
    last_receipt,
    model_is_excluded,
    normalize_mode,
    parse_bool_setting,
    parse_exclude_models,
    read_effort_history,
)

from tests.test_reasoning_effort_adapter import FakeClient

ASTRA = {"provider": "openai-codex", "model": "gpt-6-astra-900k", "api_mode": "codex_responses"}
OPUS = {"provider": "anthropic", "model": "claude-opus-5-5", "api_mode": "anthropic_messages"}
ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


def codex_request(effort: str, text: str = "synthetic") -> dict:
    return {"model": ASTRA["model"], "input": text, "reasoning": {"effort": effort, "summary": "auto"}}


def opus_request(effort: str) -> dict:
    return {
        "model": OPUS["model"],
        "messages": [{"role": "user", "content": "synthetic"}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": effort},
    }


def sent_effort(request: dict, result: dict | None) -> str | None:
    final = result["request"] if result else request
    if "reasoning" in final:
        return final["reasoning"]["effort"]
    if "output_config" in final:
        return final["output_config"]["effort"]
    return final.get("reasoning_effort")


class Env:
    def __init__(self, **values: str) -> None:
        self.values = dict(values)

    def __call__(self, name: str) -> str:
        return self.values.get(name, "")


class EffortReplayTests(unittest.TestCase):
    def make(self, choice: str = "max", **kwargs):
        client = FakeClient(choice=choice)
        records: list[dict] = []
        env = kwargs.pop("session_env", Env())
        controller = ReasoningEffortController(
            client_factory=lambda: client,
            record_decision=records.append,
            session_env=env,
            **kwargs,
        )
        return controller, client, records

    def call(self, controller, request, *, turn="t1", session="s1", route=ASTRA):
        result = controller.on_llm_request(request, session_id=session, turn_id=turn, **route)
        return sent_effort(request, result)

    def fail_tool(self, controller, session="s1"):
        controller.build_post_tool_call_hook()(tool_name="shell", status="error", error_message="exit 1", session_id=session)

    def ok_tool(self, controller, session="s1"):
        controller.build_post_tool_call_hook()(tool_name="shell", result='{"ok": true}', session_id=session)

    def test_astra_low_never_asks_jev_and_never_goes_up(self):
        controller, client, records = self.make(choice="max")
        for turn in ("t1", "t1", "t2"):
            self.assertEqual(self.call(controller, codex_request("low"), turn=turn), "low")
            self.fail_tool(controller)
        self.assertEqual(client.calls, [])
        self.assertTrue(all(record["reason_code"] == "no_room" for record in records))

    def test_opus_max_can_lower_and_never_exceeds_the_cap(self):
        controller, client, _ = self.make(choice="low")
        self.assertEqual(self.call(controller, opus_request("max"), route=OPUS), "low")
        self.fail_tool(controller)
        client.choice = "max"
        self.assertEqual(self.call(controller, opus_request("max"), route=OPUS), "max")
        offered = list(client.calls[-1][1]["reasoning_effort"]["criteria"])
        self.assertEqual(offered, ["low", "medium", "high", "max"])

    def test_the_old_ratchet_sequence_stays_at_or_below_the_user_level(self):
        # Replays the Astra session from #108: low, tool error, switch to medium, successes, new turns.
        controller, client, _ = self.make(choice="max")
        sent = [self.call(controller, codex_request("low"))]
        self.fail_tool(controller)
        sent.append(self.call(controller, codex_request("low")))
        sent.append(self.call(controller, codex_request("medium")))  # manual switch
        for _ in range(3):
            self.ok_tool(controller)
            sent.append(self.call(controller, codex_request("medium")))
        sent.append(self.call(controller, codex_request("medium"), turn="t2"))
        self.assertEqual(sent, ["low", "low", "medium", "medium", "medium", "medium", "medium"])
        self.assertTrue(all(ORDER.index(level) <= ORDER.index("medium") for level in sent))
        self.assertEqual(client.calls, [])  # low has no room; the switch pinned the session
        self.assertEqual(controller.session_status("s1")["mode"], "pinned")

    def test_manual_change_pins_and_auto_restores_with_new_cap(self):
        controller, client, _ = self.make(choice="low")
        self.assertEqual(self.call(controller, codex_request("high")), "low")
        self.assertEqual(self.call(controller, codex_request("xhigh")), "xhigh")
        self.assertEqual(last_receipt()["reason_code"], "pinned_by_user_change")
        calls = len(client.calls)
        self.assertEqual(self.call(controller, codex_request("xhigh"), turn="t2"), "xhigh")
        self.assertEqual(len(client.calls), calls)
        self.assertTrue(controller.set_mode("auto", session_id="s1")["ok"])
        self.assertEqual(self.call(controller, codex_request("xhigh"), turn="t2"), "low")
        offered = list(client.calls[-1][1]["reasoning_effort"]["criteria"])
        self.assertEqual(offered[-1], "xhigh")

    def test_model_switch_rebaselines_without_pinning(self):
        controller, client, _ = self.make(choice="low")
        self.call(controller, codex_request("high"))
        self.assertEqual(self.call(controller, opus_request("max"), route=OPUS), "low")
        self.assertEqual(controller.session_status("s1")["mode"], "auto")
        self.assertEqual(controller.session_status("s1")["user_level"], "max")

    def test_exclude_models_skips_jev_and_keeps_level(self):
        controller, client, records = self.make(choice="low", exclude_models=["*astra*"])
        self.assertEqual(self.call(controller, codex_request("max")), "max")
        self.assertEqual(client.calls, [])
        self.assertEqual(records[-1]["reason_code"], "excluded_model")
        self.assertEqual(self.call(controller, opus_request("max"), session="s2", route=OPUS), "low")

    def test_pinned_start_mode_never_calls_jev(self):
        controller, client, _ = self.make(choice="low", mode="pinned")
        self.assertEqual(self.call(controller, codex_request("high")), "high")
        self.assertEqual(client.calls, [])

    def test_allow_raise_adds_one_level_only_while_stuck(self):
        controller, client, _ = self.make(choice="medium", allow_raise=1)
        self.assertEqual(self.call(controller, codex_request("medium")), "medium")
        self.assertEqual(list(client.calls[-1][1]["reasoning_effort"]["criteria"])[-1], "medium")
        self.fail_tool(controller)
        client.choice = "high"
        self.assertEqual(self.call(controller, codex_request("medium")), "high")
        self.assertEqual(list(client.calls[-1][1]["reasoning_effort"]["criteria"])[-1], "high")
        self.assertFalse(client.calls[-1][0]["policy"]["ceiling_is_user_level"])
        self.assertIn("one wire level above", client.calls[-1][1]["reasoning_effort"]["instructions"])
        self.ok_tool(controller)
        client.choice = "medium"
        self.assertEqual(self.call(controller, codex_request("medium")), "medium")
        self.assertEqual(list(client.calls[-1][1]["reasoning_effort"]["criteria"])[-1], "medium")
        self.assertTrue(client.calls[-1][0]["policy"]["ceiling_is_user_level"])

    def test_allow_raise_off_by_default(self):
        controller, client, _ = self.make(choice="medium")
        self.call(controller, codex_request("medium"))
        self.fail_tool(controller)
        self.assertEqual(self.call(controller, codex_request("medium")), "medium")
        self.assertEqual(list(client.calls[-1][1]["reasoning_effort"]["criteria"])[-1], "medium")

    def test_new_turn_clears_the_stuck_flag(self):
        controller, client, _ = self.make(choice="medium")
        self.call(controller, codex_request("high"))
        self.fail_tool(controller)
        self.call(controller, codex_request("high"))
        self.assertTrue(client.calls[-1][0]["stuck_signal"])
        self.call(controller, codex_request("high"), turn="t2")
        self.assertFalse(client.calls[-1][0]["stuck_signal"])
        self.assertEqual(client.calls[-1][0]["recent_tool_outcomes"], [])

    def test_successful_tool_loop_reuses_one_choice_per_turn(self):
        controller, client, _ = self.make(choice="low")
        self.call(controller, codex_request("high"))
        for _ in range(10):
            self.ok_tool(controller)
            self.assertEqual(self.call(controller, codex_request("high")), "low")
        self.assertEqual(len(client.calls), 1)
        self.call(controller, codex_request("high"), turn="t2")
        self.assertEqual(len(client.calls), 2)

    def test_reasoning_none_is_a_baseline_and_manual_change_pins(self):
        controller, client, _ = self.make(choice="low")
        self.assertEqual(self.call(controller, codex_request("none")), "none")
        self.assertEqual(self.call(controller, codex_request("high")), "high")
        self.assertEqual(controller.session_status("s1")["mode"], "pinned")
        self.assertEqual(last_receipt()["reason_code"], "pinned_by_user_change")
        self.assertEqual(client.calls, [])

    def test_missing_client_and_missing_ack_are_not_jev_calls(self):
        for ack in (True, False):
            with self.subTest(ack=ack):
                controller = ReasoningEffortController(
                    client_factory=None if ack else lambda: FakeClient(),
                    public_or_sanitized_data_ack=ack,
                )
                controller.on_llm_request(codex_request("high"), session_id="s1", turn_id="t1", **ASTRA)
                self.assertFalse(last_receipt()["jev_called"])
                self.assertEqual(controller.session_status("s1")["jev_calls"], 0)

    def test_invalid_jev_choice_is_reported_separately(self):
        class InvalidClient:
            def decide(self, state, questions, **kwargs):
                return {"answers": {"reasoning_effort": {
                    "choice": "max", "confidence": 0.9,
                    "probabilities": {level: 1 / len(questions["reasoning_effort"]["criteria"])
                                      for level in questions["reasoning_effort"]["criteria"]},
                }}}

        controller = ReasoningEffortController(client_factory=InvalidClient)
        self.assertEqual(self.call(controller, codex_request("medium")), "medium")
        self.assertEqual(last_receipt()["reason_code"], "invalid_choice")

    def test_jev_failure_sends_the_user_level(self):
        controller, client, _ = self.make()
        client.error = TimeoutError("synthetic")
        self.assertEqual(self.call(controller, codex_request("high")), "high")
        self.assertEqual(last_receipt()["reason_code"], "kept_requested_on_jev_failure")

    def test_choose_offers_only_given_candidates(self):
        client = FakeClient(choice="max")
        result = choose_reasoning_effort(
            task="x", recent_tool_outcomes=[], requested_effort="medium",
            client=client, allowed_efforts=["low", "medium"],
        )
        self.assertEqual(list(client.calls[0][1]["reasoning_effort"]["criteria"]), ["low", "medium"])
        self.assertIn(result["effort"], {"low", "medium"})

    def test_command_auto_pin_status(self):
        env = Env(HERMES_SESSION_ID="s1")
        controller, client, _ = self.make(choice="low", session_env=env)
        self.assertIn("not made a model request", controller.handle_command("effort status"))
        self.assertIn("applies from your first message", controller.handle_command("effort pin"))
        self.assertEqual(self.call(controller, codex_request("high")), "high")
        self.assertIn("mode: pinned", controller.handle_command("effort status"))
        self.assertIn("auto", controller.handle_command("effort auto"))
        self.assertEqual(self.call(controller, codex_request("high")), "low")
        status = controller.handle_command("effort")
        self.assertIn("your level (cap): high", status)
        self.assertIn("last sent: low", status)
        self.assertIn("Usage", controller.handle_command("bogus"))

    def test_auto_command_discloses_raise_setting(self):
        env = Env(HERMES_SESSION_ID="s1")
        controller, _, _ = self.make(allow_raise=True, session_env=env)
        self.call(controller, codex_request("medium"))
        message = controller.handle_command("effort auto")
        self.assertIn("one level above", message)
        self.assertIn("deadline: 1.5", controller.handle_command("effort status"))

    def test_command_maps_gateway_session_key(self):
        env = Env(HERMES_SESSION_KEY="agent:main:discord:dm:1")
        controller, client, _ = self.make(choice="low", session_env=env)
        self.call(controller, codex_request("high"), session="real-session")
        env.values = {"HERMES_SESSION_KEY": "agent:main:discord:dm:1"}
        self.assertIn("pinned", controller.handle_command("effort pin"))
        self.assertEqual(controller.session_status("real-session")["mode"], "pinned")

    def test_pending_mode_applies_when_tool_hook_precedes_first_request(self):
        env = Env(HERMES_SESSION_KEY="synthetic-session-key")
        controller, client, _ = self.make(choice="low", session_env=env)
        self.assertTrue(controller.set_mode("pin")["pending"])
        controller.build_post_tool_call_hook()(session_id="real-session", result='{"ok": true}')
        self.assertEqual(self.call(controller, codex_request("high"), session="real-session"), "high")
        self.assertEqual(controller.session_status("real-session")["mode"], "pinned")
        self.assertEqual(client.calls, [])

    def test_command_without_session_context_uses_the_only_live_session(self):
        controller, client, _ = self.make(choice="low")
        self.call(controller, codex_request("high"), session="only")
        controller.handle_command("effort pin")
        self.assertEqual(controller.session_status("only")["mode"], "pinned")

    def test_settings_parsers(self):
        self.assertEqual(normalize_mode("PIN"), "pinned")
        self.assertEqual(normalize_mode("bogus"), "auto")
        for value in (True, 1, "1", "true", "YES", "on"):
            self.assertTrue(parse_bool_setting(value), value)
        for value in (False, 0, 2, "0", "off", None, "raise"):
            self.assertFalse(parse_bool_setting(value), value)
        self.assertEqual(parse_exclude_models("*Astra*, gpt-6-*"), ("*astra*", "gpt-6-*"))
        self.assertTrue(model_is_excluded("gpt-6-astra-900k", ("*astra*",)))
        self.assertFalse(model_is_excluded("", ("*",)))

    def test_decision_records_are_closed_set_and_feed_stats(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller, client, _ = self.make(choice="low")
            controller.record_decision = lambda receipt: append_effort_record(receipt, data_dir=tmp)
            self.call(controller, codex_request("high", text="private text marker"))
            self.call(controller, codex_request("xhigh"))
            raw = (Path(tmp) / "effort-history.jsonl").read_text()
            self.assertNotIn("private text marker", raw)
            records = read_effort_history(data_dir=tmp)
            self.assertEqual([r["sent"] for r in records], ["low", "xhigh"])
            self.assertEqual(records[0]["requested"], "high")
            stats = effort_stats(data_dir=tmp, since=timedelta(hours=1))
            self.assertEqual(stats["records"], 2)
            self.assertEqual(stats["lowered"], 1)
            self.assertEqual(stats["raised"], 0)
            self.assertEqual(stats["jev_calls"], 1)
            record = build_effort_record({"mode": "weird", "effort": "ultra-max", "reason_code": "x" * 500})
            self.assertIsNone(record["mode"])
            self.assertIsNone(record["sent"])

    def test_history_count_bound_with_varying_record_lengths(self):
        with tempfile.TemporaryDirectory() as tmp:
            for session in ("a", "b", "c", "a-longer-session-id"):
                self.assertTrue(append_effort_record({
                    "session_id": session, "mode": "auto", "requested_effort": "high",
                    "effort": "low", "reason_code": "jev_selected",
                }, data_dir=tmp, max_records=3))
            self.assertEqual(len(read_effort_history(data_dir=tmp)), 3)

    def test_stats_cli_exposes_reasoning_effort_object(self):
        from hermes_switchyard import _cli_handler

        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(_cli_handler(SimpleNamespace(
                switchyard_command="stats", since=None, json_output=True,
            )), 0)
        self.assertIn("reasoning_effort", json.loads(output.getvalue()))

    def test_history_since_and_writer_failure(self):
        from datetime import datetime, timezone

        with tempfile.TemporaryDirectory() as tmp:
            receipt = {"session_id": "s1", "mode": "auto", "requested_effort": "high",
                       "effort": "low", "reason_code": "jev_selected", "jev_called": True}
            self.assertTrue(append_effort_record(receipt, data_dir=tmp,
                                                 now=datetime(2020, 1, 1, tzinfo=timezone.utc)))
            self.assertEqual(effort_stats(data_dir=tmp, since=timedelta(days=1))["records"], 0)
            # An existing file cannot be the data directory; recording is best-effort.
            self.assertFalse(append_effort_record(
                receipt, data_dir=Path(tmp) / "effort-history.jsonl",
            ))


if __name__ == "__main__":
    unittest.main()
