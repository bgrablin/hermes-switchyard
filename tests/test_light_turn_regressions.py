"""Compound tasks must not trade capability for routing latency."""
from __future__ import annotations

from unittest import mock

from hermes_switchyard import automatic, egress_redaction
from hermes_switchyard.reasoning_effort_adapter import local_trivial_request
from hermes_switchyard.trivial_turn import hosted_skill_bypass_reason
from test_support import HermesHomeTestCase
from tests.test_reasoning_effort_v055 import begin, make, send


class LightTurnRegressionTests(HermesHomeTestCase):
    def test_compound_requests_keep_routing_and_effort(self):
        prompts = (
            "Say hello and then draft a cover letter for a data engineer job.",
            "Say hello in Spanish and give me a quick tax summary",
            "Say hello, and design a database schema for a payments ledger",
            "Reply with a short greeting and also translate this contract into French.",
            "Say hi to my customer and attach the invoice",
            "Say hello. Then draft a cover letter for a data engineer job.",
            "Read-only: list the current working directory and summarize what each file does.",
            "list the current working directory, read-only, and figure out why the build is slow",
            "List the current working directory, read-only. Reply with an investment strategy.",
            "Say hello; calculate my mortgage payment",
        )
        egress_redaction._reset_for_tests(lambda text: text, loaded=True)
        self.addCleanup(egress_redaction._reset_for_tests)
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertIsNone(hosted_skill_bypass_reason(prompt))
                self.assertFalse(local_trivial_request(prompt))
                controller, jev, _ = make()
                begin(controller, prompt)
                self.assertEqual(send(controller, "high"), "high")
                self.assertEqual(len(jev.calls), 1)

    def test_early_probe_preserves_single_word_skill_override(self):
        catalog = ({"name": "greeter", "description": "Compose greeting replies"},)
        for verb in ("Use", "Load"):
            for early in (False, True):
                with self.subTest(verb=verb, early=early):
                    hook = automatic.build_pre_llm_call_hook(
                        consumer_mode="advisory", routing_mode="hosted_sanitized",
                        early_light_bypass_before_discover=early,
                        client_factory=mock.Mock(side_effect=AssertionError("no hosted call")),
                    )
                    with mock.patch.object(automatic, "discover_available_skill_candidates", return_value=catalog) as discover:
                        result = hook(
                            user_message=f"{verb} greeter to reply with hello. Do not use tools.",
                            session_id="override", turn_id="t1", platform="cli",
                        )
                    self.assertGreater(discover.call_count, 0)
                    self.assertEqual(result["metadata"]["skill_recommendation"]["status"], "explicit_override")
