"""Light-turn skill-routing bypass: skip hosted Jev when a skill cannot help.

Covers closed-list acknowledgements, greeting-class instructions, pure read-only
cwd listings, and short no-action explanations without domain-skill cues.
Synthetic DecisionClient only. Capability guards assert domain prompts still host.
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
# Domain-shaped multistep: docker/compose/healthcheck/redeploy — must still host.
MULTISTEP_DOMAIN = (
    "In three short numbered steps: (1) explain what a docker compose healthcheck does, "
    "(2) give one example of a failure mode it catches, (3) say what metric you would glance "
    "at first after a redeploy. Do not run commands or change anything. Keep the whole answer "
    "under 120 words."
)
# Generic no-domain explanation may still bypass.
MULTISTEP_GENERIC = (
    "In three short numbered steps: (1) explain what a mutex is, "
    "(2) give one example of a race it prevents, (3) say what you would check first in a "
    "code review. Do not run commands or change anything. Keep the whole answer under 120 words."
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
    def test_frozen_battery_safe_light_turns(self):
        self.assertEqual(hosted_skill_bypass_reason(GREETING_INSTRUCTION), LIGHT_NO_SKILL_REASON)
        self.assertTrue(is_greeting_class_prompt(GREETING_INSTRUCTION))
        self.assertFalse(is_trivial_turn(GREETING_INSTRUCTION))
        self.assertEqual(hosted_skill_bypass_reason(LISTDIR), LIGHT_NO_SKILL_REASON)
        self.assertTrue(is_readonly_listing_prompt(LISTDIR))
        # Open-ended explanations stay hosted (not part of default light bypass).
        self.assertIsNone(hosted_skill_bypass_reason(MULTISTEP_GENERIC))
        self.assertTrue(is_light_explanation_prompt(MULTISTEP_GENERIC))

    def test_domain_multistep_still_routes(self):
        # Capability-first: docker/compose/healthcheck explanations may benefit from a skill.
        self.assertIsNone(hosted_skill_bypass_reason(MULTISTEP_DOMAIN))
        self.assertFalse(is_light_explanation_prompt(MULTISTEP_DOMAIN))

    def test_skill_and_task_prompts_still_route(self):
        self.assertIsNone(hosted_skill_bypass_reason(PRINTER))
        self.assertIsNone(hosted_skill_bypass_reason("fix ci"))
        self.assertIsNone(
            hosted_skill_bypass_reason(
                "Plan a careful weekend home-lab maintenance window with rollback steps."
            )
        )

    def test_closed_list_acks_remain_trivial_turn(self):
        self.assertEqual(hosted_skill_bypass_reason("hi"), "trivial_turn")
        self.assertEqual(hosted_skill_bypass_reason("thanks!"), "trivial_turn")


class CapabilityGuardTests(unittest.TestCase):
    """Adversarial cases that must NOT bypass hosted skill selection."""

    def test_greeting_class_does_not_swallow_code_or_follow_on_tasks(self):
        for prompt in (
            "Respond with a hello world Python script. Do not use tools.",
            "Reply with hello. Then list open PRs.",
            "Reply with hello. Then create a REST API.",
            "Say hi, then open issue 123 and review it.",
            "Write a greeting email to onboard a new hire about our CI and deploy process.",
            "Give me a hello world in Rust.",
            "Write a hello world program in Python.",
        ):
            with self.subTest(prompt=prompt[:50]):
                self.assertIsNone(hosted_skill_bypass_reason(prompt))
                self.assertFalse(is_greeting_class_prompt(prompt))

    def test_listing_is_cwd_scoped_not_path_scoped(self):
        for prompt in (
            "Using only safe read-only actions, list the nginx-related files under the "
            "system log directory. Do not write or delete anything.",
            "Using safe read-only actions, list the contents of the docker compose project "
            "directory. Do not modify anything.",
            "list directories under ~/.hermes using read-only actions only",
            "Run ls -la /etc using read-only actions; do not modify anything.",
            "Run ls -la .. using read-only actions; do not modify anything.",
            "Prefer running exactly: ls -la /var using safe read-only actions. Do not write.",
        ):
            with self.subTest(prompt=prompt[:50]):
                self.assertIsNone(hosted_skill_bypass_reason(prompt))
                self.assertFalse(is_readonly_listing_prompt(prompt))

    def test_domain_explanations_still_route(self):
        for prompt in (
            "Explain how to systematically debug a flaky pytest failure in CI. Do not run commands.",
            "In three short numbered steps explain how to diagnose a network printer that is "
            "offline. Do not run commands or change anything.",
            "Explain docker compose networking and give one example of a common misconfiguration. "
            "Do not run commands.",
            "Explain how a blue-green deploy works. Do not run commands.",
            "Explain what the systematic-debugging approach recommends for intermittent failures. "
            "Do not execute anything.",
            "In three short numbered steps: (1) explain printer spool paths, (2) give one example "
            "of a jam cause, (3) say what to check first. Do not run commands.",
            "Explain how Kubernetes ingress works. Do not run commands.",
            "Explain what a Helm chart does for a deployment. Do not run commands.",
        ):
            with self.subTest(prompt=prompt[:50]):
                self.assertIsNone(hosted_skill_bypass_reason(prompt))
                self.assertFalse(is_light_explanation_prompt(prompt))

    def test_listing_rejects_follow_on_deliverables(self):
        prompt = (
            "Using safe read-only actions, list the current working directory, then "
            "analyze the Python files for security vulnerabilities."
        )
        self.assertIsNone(hosted_skill_bypass_reason(prompt))
        self.assertFalse(is_readonly_listing_prompt(prompt))

    def test_open_ended_explanation_stays_hosted(self):
        for prompt in (
            "Explain how Django middleware works. Do not run commands.",
            MULTISTEP_GENERIC,
        ):
            with self.subTest(prompt=prompt[:50]):
                self.assertIsNone(hosted_skill_bypass_reason(prompt))

    def test_printer_and_logs_style_without_explicit_skill_cue_still_route(self):
        self.assertIsNone(
            hosted_skill_bypass_reason(
                "A network printer is unreachable. Identify likely causes and list safe "
                "read-only checks."
            )
        )
        self.assertIsNone(
            hosted_skill_bypass_reason(
                "Scan these ERROR lines from app logs and prioritize the root cause."
            )
        )


class RecommendBypassTests(HermesHomeTestCase):
    def test_light_turns_skip_hosted_under_always_mode(self):
        for task, reason in (
            ("hi", "trivial_turn"),
            (GREETING_INSTRUCTION, LIGHT_NO_SKILL_REASON),
            (LISTDIR, LIGHT_NO_SKILL_REASON),
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

    def test_domain_multistep_does_not_light_bypass(self):
        calls: list[bool] = []
        rec = AutomaticSkillRecommender(
            configured_candidates=CATALOG,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory(calls),
            cache_seconds=0.0,
            # Force prefilter/honor path so we only assert light bypass is not used;
            # domain multistep may still skip via local_no_skill_gate under honor.
            light_turn_bypass=True,
            honor_no_skill_gate=True,
        )
        result = rec.recommend(MULTISTEP_DOMAIN)
        self.assertNotEqual(result.get("bypass_reason"), LIGHT_NO_SKILL_REASON)
        self.assertNotEqual(result.get("hosted_skipped"), LIGHT_NO_SKILL_REASON)

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
        self.assertFalse(
            local_trivial_request("Respond with a hello world Python script. Do not use tools.")
        )



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



class EarlyLightBypassBeforeDiscoverTests(HermesHomeTestCase):
    """Flag-gated discover skip: default OFF preserves order; ON skips for light turns."""

    def _hook(self, *, early: bool, light_turn_bypass: bool = True):
        from hermes_switchyard.automatic import build_pre_llm_call_hook

        calls: list[bool] = []
        hook = build_pre_llm_call_hook(
            enabled=True,
            configured_candidates=None,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory(calls),
            cache_seconds=0.0,
            consumer_mode="advisory",
            light_turn_bypass=light_turn_bypass,
            early_light_bypass_before_discover=early,
        )
        assert hook is not None
        return hook, calls

    def test_flag_off_greeting_still_discovers(self):
        from unittest import mock

        hook, calls = self._hook(early=False)
        discover_calls: list[int] = []

        def fake_discover():
            discover_calls.append(1)
            return tuple(CATALOG)

        with mock.patch(
            "hermes_switchyard.automatic.discover_available_skill_candidates",
            side_effect=fake_discover,
        ):
            response = hook(
                user_message=GREETING_INSTRUCTION,
                session_id="early-off",
                turn_id="t1",
                platform="cli",
            )
        self.assertEqual(calls, [])
        self.assertGreaterEqual(len(discover_calls), 1)
        self.assertEqual(hook.last_result.get("bypass_reason"), LIGHT_NO_SKILL_REASON)
        self.assertEqual(hook.last_receipt.get("bypass_reason"), LIGHT_NO_SKILL_REASON)
        self.assertTrue(isinstance(hook.last_receipt.get("source_sha"), str))
        self.assertIn("metadata", response or {})

    def test_flag_on_greeting_skips_discover(self):
        from unittest import mock

        hook, calls = self._hook(early=True)
        discover_calls: list[int] = []

        def fake_discover():
            discover_calls.append(1)
            raise AssertionError("early light bypass must skip catalog discover")

        with mock.patch(
            "hermes_switchyard.automatic.discover_available_skill_candidates",
            side_effect=fake_discover,
        ):
            response = hook(
                user_message=GREETING_INSTRUCTION,
                session_id="early-on-greet",
                turn_id="t1",
                platform="cli",
            )
        self.assertEqual(calls, [])
        self.assertEqual(discover_calls, [])
        self.assertEqual(hook.last_result.get("bypass_reason"), LIGHT_NO_SKILL_REASON)
        self.assertEqual(hook.last_receipt.get("bypass_reason"), LIGHT_NO_SKILL_REASON)
        self.assertFalse(hook.last_result.get("hosted_attempted"))
        self.assertEqual(hook.last_result.get("routing_status"), "hosted_skipped")
        self.assertTrue(isinstance(hook.last_receipt.get("source_sha"), str))
        meta = (response or {}).get("metadata", {}).get("skill_recommendation", {})
        self.assertEqual(meta.get("status"), "abstained")

    def test_flag_on_consequential_still_discovers(self):
        from unittest import mock

        hook, calls = self._hook(early=True)
        discover_calls: list[int] = []

        def fake_discover():
            discover_calls.append(1)
            return tuple(CATALOG)

        with mock.patch(
            "hermes_switchyard.automatic.discover_available_skill_candidates",
            side_effect=fake_discover,
        ):
            response = hook(
                user_message=PRINTER,
                session_id="early-on-printer",
                turn_id="t1",
                platform="cli",
            )
        self.assertGreaterEqual(len(discover_calls), 1)
        self.assertNotEqual(hook.last_result.get("bypass_reason"), LIGHT_NO_SKILL_REASON)
        self.assertNotEqual(hook.last_result.get("bypass_reason"), "trivial_turn")
        self.assertIsNotNone(response)

    def test_flag_on_explicit_override_still_discovers(self):
        from unittest import mock
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
            light_turn_bypass=True,
            early_light_bypass_before_discover=True,
        )
        assert hook is not None
        discover_calls: list[int] = []

        def fake_discover():
            discover_calls.append(1)
            return tuple(CATALOG)

        task = (
            "Use systematic-debugging to explain what a stack trace is. "
            "Do not run commands."
        )
        with mock.patch(
            "hermes_switchyard.automatic.discover_available_skill_candidates",
            side_effect=fake_discover,
        ):
            response = hook(
                user_message=task, session_id="s-ov", turn_id="t1", platform="cli"
            )
        self.assertEqual(calls, [])
        self.assertGreaterEqual(len(discover_calls), 1)
        meta = (response or {}).get("metadata", {}).get("skill_recommendation", {})
        self.assertEqual(meta.get("status"), "explicit_override")
        self.assertEqual(meta.get("explicit_skill"), "systematic-debugging")

    def test_flag_on_greeting_shaped_explicit_skill_still_discovers(self):
        """Greeting-class + Use <skill> must not early-bypass before override pool."""
        from unittest import mock
        from hermes_switchyard.automatic import build_pre_llm_call_hook

        catalog = list(CATALOG) + [
            {"name": "greeting-writer", "description": "Compose greeting replies"}
        ]
        calls: list[bool] = []
        hook = build_pre_llm_call_hook(
            enabled=True,
            configured_candidates=catalog,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory(calls),
            cache_seconds=0.0,
            consumer_mode="advisory",
            light_turn_bypass=True,
            early_light_bypass_before_discover=True,
        )
        assert hook is not None
        discover_calls: list[int] = []

        def fake_discover():
            discover_calls.append(1)
            return tuple(catalog)

        task = "Use greeting-writer to reply with hello. Do not use tools."
        with mock.patch(
            "hermes_switchyard.automatic.discover_available_skill_candidates",
            side_effect=fake_discover,
        ):
            response = hook(
                user_message=task, session_id="s-greet-ov", turn_id="t1", platform="cli"
            )
        self.assertEqual(calls, [])
        self.assertGreaterEqual(len(discover_calls), 1)
        meta = (response or {}).get("metadata", {}).get("skill_recommendation", {})
        self.assertEqual(meta.get("status"), "explicit_override")
        self.assertEqual(meta.get("explicit_skill"), "greeting-writer")


if __name__ == "__main__":
    unittest.main()
