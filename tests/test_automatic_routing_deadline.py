"""Regression tests for automatic routing intervention deadlines (issue #26).

Uses delayed synthetic DecisionClient transports only. No live provider calls.
"""
from __future__ import annotations

import time
import unittest

from hermes_switchyard.automatic import (
    DEFAULT_AUTOMATIC_DEADLINE_SECONDS,
    AutomaticSkillRecommender,
    build_pre_llm_call_hook,
    redacted_routing_metadata,
)
from hermes_switchyard.client import (
    DEFAULT_AUTOMATIC_ROUTING_DEADLINE_SECONDS,
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    DecisionClient,
)
from hermes_switchyard.receipt_state import HOSTED_ERROR_CODES


def _allowed_policy(payload="SANITIZED_TASK_MARKER"):
    return {
        "version": 1,
        "decision": "allow",
        "data_class": "sanitized",
        "reason_code": "host_policy_allowed",
        "allowed_payload": payload,
    }


def _selection_response(choice="docker-management"):
    return {
        "model": "typesafe/jev-1.13",
        "answers": {
            "skill": {
                "choice": choice,
                "confidence": 0.99,
                "probabilities": {choice: 1.0},
            },
            "needs_skill": {"noul": 0.99},
        },
        "usage": {"cost": 0.01},
        "latency_ms": 5.0,
        "request_id": "req-late",
    }


class AutomaticRoutingDeadlineTests(unittest.TestCase):
    def test_automatic_deadline_is_separate_from_operation_deadline(self):
        self.assertEqual(DEFAULT_AUTOMATIC_DEADLINE_SECONDS, 20.0)
        self.assertEqual(DEFAULT_AUTOMATIC_ROUTING_DEADLINE_SECONDS, 20.0)
        self.assertEqual(DEFAULT_OPERATION_DEADLINE_SECONDS, 60.0)
        self.assertLess(
            DEFAULT_AUTOMATIC_ROUTING_DEADLINE_SECONDS,
            DEFAULT_OPERATION_DEADLINE_SECONDS,
        )
        # Comfortably below a typical Hermes plugin callback budget (~30s).
        self.assertLess(DEFAULT_AUTOMATIC_ROUTING_DEADLINE_SECONDS, 30.0)
        self.assertIn("deadline_exceeded", HOSTED_ERROR_CODES)
        self.assertIn("host_cancelled", HOSTED_ERROR_CODES)
        self.assertIn("late_result_discarded", HOSTED_ERROR_CODES)

    def test_delayed_provider_late_result_never_reaches_the_turn(self):
        calls = {"count": 0}

        def transport(_payload):
            calls["count"] += 1
            time.sleep(0.12)
            return _selection_response("docker-management")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[
                {"name": "docker-management", "description": "Docker"},
                {"name": "printer", "description": "Printers"},
            ],
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            # Force local abstention so a late hosted selection would be visible.
            local_threshold=2.0,
            deadline_seconds=0.05,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=transport
            ),
        )
        result = recommender.recommend(
            "public Docker maintenance",
            turn_egress_policy=_allowed_policy(),
        )
        self.assertEqual(calls["count"], 1)
        self.assertTrue(result["hosted_attempted"])
        self.assertEqual(result["hosted_error"], "late_result_discarded")
        self.assertEqual(result["hosted_error_code"], "late_result_discarded")
        self.assertEqual(result["routing_reason"], "late_result_discarded")
        self.assertNotEqual(result.get("source"), "jev")
        self.assertIsNone(result.get("selected"))
        self.assertEqual(result["intervention_deadline_seconds"], 0.05)
        receipt = recommender.last_receipt
        self.assertEqual(receipt["hosted_error"], "late_result_discarded")
        self.assertEqual(receipt["terminal_state"], "hosted_failure")
        self.assertFalse(receipt["hosted_succeeded"])
        metadata = redacted_routing_metadata(result)
        self.assertEqual(metadata["intervention_deadline_seconds"], 0.05)
        self.assertEqual(metadata["hosted_error_code"], "late_result_discarded")

    def test_deadline_exceeded_before_further_partition_requests(self):
        calls = {"count": 0}

        def transport(payload):
            calls["count"] += 1
            if calls["count"] == 1:
                time.sleep(0.08)
                answers = {}
                for name, question in payload["questions"].items():
                    if name == "needs_skill":
                        answers[name] = {"noul": 0.99}
                    else:
                        criteria = question["criteria"]
                        choice = next(iter(criteria))
                        answers[name] = {
                            "choice": choice,
                            "confidence": 0.99,
                            "probabilities": {
                                key: (1.0 if key == choice else 0.0) for key in criteria
                            },
                        }
                return {
                    "model": "typesafe/jev-1.13",
                    "answers": answers,
                    "usage": {"cost": 0.01},
                    "latency_ms": 5.0,
                    "request_id": "req-1",
                }
            raise AssertionError("further partition requests must not start after deadline")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[
                {"name": f"skill-{index}", "description": "public skill description"}
                for index in range(300)
            ],
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            local_threshold=2.0,
            deadline_seconds=0.05,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=transport
            ),
        )
        result = recommender.recommend(
            "find a public skill",
            turn_egress_policy=_allowed_policy("SANITIZED_LARGE_CATALOG_TASK"),
        )
        self.assertEqual(calls["count"], 1)
        self.assertIn(
            result["hosted_error"],
            {"deadline_exceeded", "late_result_discarded"},
        )
        self.assertNotEqual(result.get("source"), "jev")
        self.assertIsNone(result.get("selected"))

    def test_host_cancelled_is_recorded_distinctly(self):
        calls = {"count": 0}

        def transport(_payload):
            calls["count"] += 1
            raise AssertionError("cancelled host must not start provider work")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[
                {"name": "docker-management", "description": "Docker"},
            ],
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            local_threshold=2.0,
            deadline_seconds=5.0,
            cancel_check=lambda: True,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=transport
            ),
        )
        result = recommender.recommend(
            "public Docker maintenance",
            turn_egress_policy=_allowed_policy(),
        )
        self.assertEqual(calls["count"], 0)
        self.assertEqual(result["hosted_error"], "host_cancelled")
        self.assertEqual(result["routing_reason"], "host_cancelled")
        self.assertNotEqual(result.get("source"), "jev")
        self.assertEqual(recommender.last_receipt["hosted_error"], "host_cancelled")

    def test_hook_does_not_publish_late_recommendation_context(self):
        def transport(_payload):
            time.sleep(0.12)
            return _selection_response("docker-management")

        hook = build_pre_llm_call_hook(
            configured_candidates=[
                {"name": "docker-management", "description": "Docker"},
                {"name": "printer", "description": "Printers"},
            ],
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            local_threshold=2.0,
            deadline_seconds=0.05,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=transport
            ),
            consumer_mode="load",
            skill_loader=lambda name, task_id=None: name,
        )
        response = hook(
            user_message="public Docker maintenance",
            turn_egress_policy=_allowed_policy(),
        )
        self.assertIsNotNone(response)
        metadata = response["metadata"]
        recommendation = metadata["skill_recommendation"]
        self.assertEqual(recommendation["status"], "abstained")
        self.assertIsNone(recommendation["selected"])
        self.assertEqual(metadata["hosted_error_code"], "late_result_discarded")
        self.assertEqual(metadata["intervention_deadline_seconds"], 0.05)
        self.assertNotIn("context", response)


    def test_typed_deadline_exceeded_is_distinct_from_provider_timeout(self):
        from hermes_switchyard.client import DeadlineExceeded
        from hermes_switchyard.automatic import _hosted_error_code

        self.assertEqual(_hosted_error_code(DeadlineExceeded("budget")), "deadline_exceeded")
        self.assertEqual(
            _hosted_error_code(TimeoutError("provider socket timeout")),
            "transport_or_execution_failure",
        )


if __name__ == "__main__":
    unittest.main()
