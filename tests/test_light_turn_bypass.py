"""Light-turn skill-routing bypass: skip hosted Jev when a skill cannot help.

Covers closed-list acknowledgements, greeting-class instructions, pure read-only
listings, and short no-action explanations. Synthetic DecisionClient only.
"""
from __future__ import annotations

import unittest

from hermes_switchyard.automatic import AutomaticSkillRecommender
from hermes_switchyard.trivial_turn import (
    LIGHT_NO_SKILL_REASON,
    hosted_skill_bypass_reason,
    is_greeting_class_prompt,
    is_light_explanation_prompt,
    is_readonly_listing_prompt,
    is_trivial_turn,
)
from test_support import HermesHomeTestCase

CATALOG = [
    {"name": "docker-management", "description": "Manage Docker containers and Compose"},
    {"name": "network-printer-operations", "description": "Diagnose network printers"},
    {"name": "systematic-debugging", "description": "Debug application errors and logs"},
]

GREETING_INSTRUCTION = (
    "Reply with exactly one short greeting sentence. Do not use tools. Do not ask questions."
)
LISTDIR = (
    "Using only safe read-only actions, list the names of entries in the current working "
    "directory. Prefer running exactly: ls -la . Do not write, patch, delete, install, or "
    "change any system state. After listing, reply with a short bullet summary of what you saw."
)
MULTISTEP = (
    "In three short numbered steps: (1) explain what a docker compose healthcheck does, "
    "(2) give one example of a failure mode it catches, (3) say what metric you would glance "
    "at first after a redeploy. Do not run commands or change anything. Keep the whole answer "
    "under 120 words."
)
PRINTER = (
    "This is a public synthetic evaluation task. A network printer is unreachable. "
    "Identify likely causes and list safe read-only checks. Use a relevant available skill "
    "if one is appropriate, then finish with EVAL_SKILL=<identifier>."
)


def forbidden_factory(calls):
    def factory():
        calls.append(True)
        raise AssertionError("hosted client must not be constructed")

    return factory


class DetectorTests(unittest.TestCase):
    def test_frozen_battery_light_turns(self):
        self.assertEqual(hosted_skill_bypass_reason(GREETING_INSTRUCTION), LIGHT_NO_SKILL_REASON)
        self.assertTrue(is_greeting_class_prompt(GREETING_INSTRUCTION))
        self.assertFalse(is_trivial_turn(GREETING_INSTRUCTION))
        self.assertEqual(hosted_skill_bypass_reason(LISTDIR), LIGHT_NO_SKILL_REASON)
        self.assertTrue(is_readonly_listing_prompt(LISTDIR))
        self.assertEqual(hosted_skill_bypass_reason(MULTISTEP), LIGHT_NO_SKILL_REASON)
        self.assertTrue(is_light_explanation_prompt(MULTISTEP))

    def test_skill_and_task_prompts_still_route(self):
        self.assertIsNone(hosted_skill_bypass_reason(PRINTER))
        self.assertIsNone(hosted_skill_bypass_reason("fix ci"))
        self.assertIsNone(
            hosted_skill_bypass_reason(
                "Plan a careful weekend home-lab maintenance window with rollback steps."
            )
        )
        # Greeting-only grammar: coding tasks that merely contain "hello" must route.
        self.assertFalse(is_greeting_class_prompt("Write a hello world program in Python."))
        self.assertIsNone(hosted_skill_bypass_reason("Write a hello world program in Python."))

    def test_closed_list_acks_remain_trivial_turn(self):
        self.assertEqual(hosted_skill_bypass_reason("hi"), "trivial_turn")
        self.assertEqual(hosted_skill_bypass_reason("thanks!"), "trivial_turn")


class RecommendBypassTests(HermesHomeTestCase):
    def test_light_turns_skip_hosted_under_always_mode(self):
        for task, reason in (
            ("hi", "trivial_turn"),
            (GREETING_INSTRUCTION, LIGHT_NO_SKILL_REASON),
            (LISTDIR, LIGHT_NO_SKILL_REASON),
            (MULTISTEP, LIGHT_NO_SKILL_REASON),
        ):
            with self.subTest(task=task[:40]):
                calls: list[bool] = []
                rec = AutomaticSkillRecommender(
                    configured_candidates=CATALOG,
                    routing_mode="hosted_sanitized",
                    hosted_mode="always",
                    public_or_sanitized_data_ack=True,
                    client_factory=forbidden_factory(calls),
                    cache_seconds=0.0,
                )
                result = rec.recommend(task)
                self.assertEqual(calls, [])
                self.assertFalse(result["hosted_attempted"])
                self.assertEqual(result["bypass_reason"], reason)
                self.assertEqual(result["hosted_skipped"], reason)
                self.assertEqual(rec.last_receipt["bypass_reason"], reason)

    def test_light_turn_bypass_flag_can_disable(self):
        # With bypass off, a greeting instruction still constructs a client path;
        # use uncertain_only + honor so we only assert the flag is consulted.
        calls: list[bool] = []
        rec = AutomaticSkillRecommender(
            configured_candidates=CATALOG,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory(calls),
            cache_seconds=0.0,
            light_turn_bypass=False,
            honor_no_skill_gate=True,
        )
        # Near-zero overlap greeting instruction may still hit no_skill_gate.
        result = rec.recommend(GREETING_INSTRUCTION)
        # Must not use light_no_skill when the flag is off.
        self.assertNotEqual(result.get("bypass_reason"), LIGHT_NO_SKILL_REASON)

    def test_honor_no_skill_gate_under_always(self):
        calls: list[bool] = []
        rec = AutomaticSkillRecommender(
            configured_candidates=CATALOG,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory(calls),
            cache_seconds=0.0,
            light_turn_bypass=False,  # force the prefilter path
            honor_no_skill_gate=True,
        )
        result = rec.recommend("hello how are you today")
        self.assertEqual(calls, [])
        self.assertFalse(result["hosted_attempted"])
        self.assertEqual(result["bypass_reason"], "local_no_skill_gate")

    def test_honor_no_skill_gate_false_restores_always_fan_out(self):
        from hermes_switchyard import egress_redaction
        from hermes_switchyard.client import DecisionClient, EXPECTED_MODEL
        import threading

        egress_redaction._reset_for_tests(lambda text: text, loaded=True)
        self.addCleanup(egress_redaction._reset_for_tests)

        class Transport:
            def __init__(self):
                self.payloads = []
                self.lock = threading.Lock()

            def __call__(self, payload):
                with self.lock:
                    self.payloads.append(payload)
                answers = {}
                for name, question in payload["questions"].items():
                    if question["type"] == "noul":
                        answers[name] = {"noul": 0.05}
                        continue
                    keys = list(question["criteria"])
                    answers[name] = {
                        "choice": keys[0],
                        "probabilities": {k: 1.0 / len(keys) for k in keys},
                        "confidence": 0.1,
                    }
                return {"model": EXPECTED_MODEL, "answers": answers, "usage": {}, "latency_ms": 1.0}

        transport = Transport()
        rec = AutomaticSkillRecommender(
            configured_candidates=CATALOG,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=transport),
            cache_seconds=0.0,
            light_turn_bypass=False,
            honor_no_skill_gate=False,
            two_stage=None,
        )
        result = rec.recommend("hello how are you today")
        self.assertTrue(result["hosted_attempted"], result.get("hosted_skipped"))
        self.assertGreaterEqual(len(transport.payloads), 1)


class AdaptiveGreetingClassTests(unittest.TestCase):
    def test_greeting_instruction_is_local_trivial(self):
        from hermes_switchyard.reasoning_effort_adapter import local_trivial_request

        self.assertTrue(local_trivial_request(GREETING_INSTRUCTION))
        self.assertTrue(local_trivial_request("hi"))
        self.assertFalse(local_trivial_request(PRINTER))
        self.assertFalse(local_trivial_request(LISTDIR))
        self.assertFalse(local_trivial_request("Write a hello world program in Python."))



class PrecedenceTests(HermesHomeTestCase):
    """routing_mode=off and explicit override must beat light-turn bypass."""

    def test_off_mode_greeting_reports_routing_mode_off(self):
        calls: list[bool] = []
        rec = AutomaticSkillRecommender(
            configured_candidates=CATALOG,
            routing_mode="off",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory(calls),
            cache_seconds=0.0,
        )
        result = rec.recommend("hi")
        self.assertEqual(calls, [])
        self.assertEqual(result["routing_reason"], "routing_mode_off")
        self.assertEqual(result["routing_status"], "disabled")
        self.assertNotEqual(result.get("bypass_reason"), LIGHT_NO_SKILL_REASON)

    def test_hook_explicit_override_beats_light_explanation(self):
        from hermes_switchyard.automatic import build_pre_llm_call_hook

        calls: list[bool] = []
        hook = build_pre_llm_call_hook(
            enabled=True,
            configured_candidates=CATALOG,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory(calls),
            cache_seconds=0.0,
            consumer_mode="advisory",
        )
        assert hook is not None
        task = (
            "Use systematic-debugging to explain what a stack trace is. "
            "Do not run commands."
        )
        response = hook(user_message=task, session_id="s1", turn_id="t1", platform="cli")
        self.assertEqual(calls, [])
        meta = (response or {}).get("metadata", {}).get("skill_recommendation", {})
        self.assertEqual(meta.get("status"), "explicit_override")
        self.assertEqual(meta.get("explicit_skill"), "systematic-debugging")
        self.assertEqual(hook.last_result.get("hosted_skipped"), "explicit_override")


if __name__ == "__main__":
    unittest.main()
