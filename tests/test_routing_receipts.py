"""Privacy-safe typed routing-receipt tests for issue #9.

Hosted calls in this file use a synthetic DecisionClient transport. No test
sends task text, candidate descriptions, conversation history, or credentials to
a model, and the receipt surface never carries provider exception text.
"""
from __future__ import annotations

import json
import unittest

from jev_decision.automatic import (
    AutomaticSkillRecommender,
    build_routing_receipt,
)
from jev_decision.client import DecisionClient


# The stable terminal-state enum the receipt surface exposes.
_TERMINAL = {
    "local_selection",
    "hosted_selection",
    "hosted_abstention",
    "hosted_failure_local_fallback",
    "hosted_skipped",
    "cache_hit",
}

# Marker strings that must never appear anywhere in a receipt, even as JSON.
_FORBIDDEN_MARKERS = [
    "SYNTHETIC_TASK_MARKER",
    "SYNTHETIC_CANDIDATE_DESCRIPTION_MARKER",
    "PRIVATE_HISTORY_MARKER",
    "SYNTHETIC_CREDENTIAL_MARKER",
    "fixture-key",
    "Jev transport failed",
]


def _skipped_result():
    return {
        "status": "abstained",
        "selected": None,
        "source": "none",
        "abstention_reason": None,
        "hosted_skipped": "public_or_sanitized_data_ack_required",
        "hosted_attempted": False,
        "cache_hit": False,
        "local_score": 0.0,
    }


class ReceiptSchemaTests(unittest.TestCase):
    def test_receipt_has_stable_typed_fields_and_advisory_semantics(self):
        receipt = build_routing_receipt(_skipped_result())
        self.assertIn(receipt["terminal_state"], _TERMINAL)
        self.assertEqual(receipt["terminal_state"], "hosted_skipped")
        self.assertFalse(receipt["verified"])
        self.assertTrue(receipt["advisory_only"])
        self.assertIsInstance(receipt["hosted_attempted"], bool)
        self.assertIsInstance(receipt["hosted_succeeded"], bool)
        self.assertIn(receipt["source"], {"local", "jev", "none"})
        self.assertEqual(
            receipt["hosted_skip_reason"],
            "public_or_sanitized_data_ack_required",
        )
        self.assertIsNone(receipt["hosted_error"])
        self.assertEqual(receipt["candidate_count"], 0)
        self.assertEqual(receipt["request_count"], 0)
        self.assertEqual(receipt["total_latency_ms"], 0.0)
        self.assertEqual(receipt["total_usage"], {})
        self.assertIsInstance(receipt["plugin_identity"], dict)

    def test_hosted_selection_receipt_carries_aggregate_fields(self):
        result = {
            "status": "selected",
            "selected": "docker-management",
            "source": "jev",
            "abstention_reason": None,
            "hosted_attempted": True,
            "hosted_error": None,
            "cache_hit": False,
            "candidate_count": 1,
            "offered_count": 1,
            "excluded_count": 0,
            "shortlist_policy": "complete_candidate_set",
            "jev_model": "typesafe/jev-1.13",
            "jev_latency_ms": 120.0,
            "jev_usage": {},
            "jev_total_latency_ms": 120.0,
            "jev_total_usage": {"cost": 0.01},
            "jev_request_count": 1,
        }
        receipt = build_routing_receipt(result)
        self.assertEqual(receipt["terminal_state"], "hosted_selection")
        self.assertTrue(receipt["hosted_attempted"])
        self.assertTrue(receipt["hosted_succeeded"])
        self.assertIsNone(receipt["hosted_error"])
        self.assertEqual(receipt["jev_model"], "typesafe/jev-1.13")
        self.assertEqual(receipt["request_count"], 1)
        self.assertEqual(receipt["latency_ms"], 120.0)
        self.assertEqual(receipt["total_latency_ms"], 120.0)
        self.assertEqual(receipt["total_usage"], {"cost": 0.01})

    def test_valid_hosted_abstention_receipt_is_succeeded_not_error(self):
        result = {
            "status": "abstained",
            "selected": None,
            "source": "none",
            "abstention_reason": "needs_skill_below_threshold",
            "hosted_attempted": True,
            "hosted_error": None,
            "cache_hit": False,
            "candidate_count": 2,
        }
        receipt = build_routing_receipt(result)
        self.assertEqual(receipt["terminal_state"], "hosted_abstention")
        self.assertTrue(receipt["hosted_attempted"])
        self.assertTrue(receipt["hosted_succeeded"])
        self.assertIsNone(receipt["hosted_error"])
        self.assertTrue(receipt["abstention_reason"])

    def test_hosted_failure_local_fallback_receipt_has_stable_error_code(self):
        result = {
            "status": "selected",
            "selected": "docker-management",
            "source": "local",
            "abstention_reason": None,
            "hosted_attempted": True,
            "hosted_error": "transport_or_execution_failure",
            "cache_hit": False,
            "candidate_count": 1,
        }
        receipt = build_routing_receipt(result)
        self.assertEqual(receipt["terminal_state"], "hosted_failure_local_fallback")
        self.assertTrue(receipt["hosted_attempted"])
        self.assertFalse(receipt["hosted_succeeded"])
        self.assertEqual(receipt["hosted_error"], "transport_or_execution_failure")


class ReceiptPrivacyTests(unittest.TestCase):
    def test_receipt_never_carries_forbidden_markers(self):
        # A real recommend() result never carries these, but the receipt builder
        # must strip task text, candidate identifiers, history, and provider
        # exception text even if they are present on the result.
        result = {
            "status": "selected",
            "selected": "docker-management",
            "source": "jev",
            "abstention_reason": None,
            "hosted_attempted": True,
            "hosted_error": None,
            "cache_hit": False,
            "task": "SYNTHETIC_TASK_MARKER private Docker maintenance",
            "candidates_considered": ["docker-management"],
            "jev_usage": {},
            "jev_total_usage": {"cost": 0.01},
            "candidate_count": 3,
        }
        receipt = build_routing_receipt(result)
        blob = json.dumps(receipt, sort_keys=True)
        for marker in _FORBIDDEN_MARKERS:
            self.assertNotIn(marker, blob, f"receipt leaked forbidden marker {marker!r}")


class ReceiptEndToEndTests(unittest.TestCase):
    def _small_transport(self):
        def transport(_payload):
            return {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "skill": {
                        "choice": "docker-management",
                        "confidence": 0.95,
                        "probabilities": {"docker-management": 1.0},
                    },
                    "needs_skill": {"noul": 0.95},
                },
                "usage": {},
                "latency_ms": 40.0,
            }

        return transport

    def _large_transport(self):
        def transport(payload):
            answers = {}
            for name, question in payload["questions"].items():
                if name == "needs_skill":
                    answers[name] = {"noul": 0.99}
                else:
                    criteria = question["criteria"]
                    selected = "skill-299" if "skill-299" in criteria else next(iter(criteria))
                    answers[name] = {
                        "choice": selected,
                        "confidence": 0.99,
                        "probabilities": {k: (1.0 if k == selected else 0.0) for k in criteria},
                    }
            return {
                "model": "typesafe/jev-1.13",
                "answers": answers,
                "usage": {"cost": 0.001},
                "latency_ms": 1.0,
            }

        return transport

    def test_exactly_one_terminal_receipt_per_attempt(self):
        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            hosted_enabled=True,
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=self._small_transport()
            ),
        )
        for _ in range(3):
            recommender.recommend("Diagnose a Docker container")
            self.assertIsNotNone(recommender.last_receipt)
            self.assertIn(recommender.last_receipt["terminal_state"], _TERMINAL)
            # Exactly one receipt is retained per attempt; it never grows into
            # a list of multiple terminal receipts.
            self.assertNotIsInstance(recommender.last_receipt["terminal_state"], list)

    def test_receipt_is_advisory_and_never_loads_skill(self):
        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            hosted_enabled=True,
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=self._small_transport()
            ),
        )
        recommender.recommend("Diagnose a Docker container")
        receipt = recommender.last_receipt
        self.assertFalse(receipt["verified"])
        self.assertTrue(receipt["advisory_only"])
        self.assertEqual(receipt["terminal_state"], "hosted_selection")

    def test_hosted_transport_failure_falls_back_to_local(self):
        def transport(_payload):
            raise RuntimeError("Jev connection failed")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            hosted_enabled=True,
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=transport
            ),
        )
        result = recommender.recommend("Diagnose a Docker container")
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["source"], "local")
        receipt = recommender.last_receipt
        self.assertEqual(receipt["terminal_state"], "hosted_failure_local_fallback")
        self.assertTrue(receipt["hosted_attempted"])
        self.assertFalse(receipt["hosted_succeeded"])
        self.assertEqual(receipt["hosted_error"], "transport_or_execution_failure")
        self.assertFalse(receipt["verified"])

    def test_large_catalog_receipt_reports_aggregate_request_usage_latency(self):
        recommender = AutomaticSkillRecommender(
            hosted_enabled=True,
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=self._large_transport()
            ),
        )
        candidates = [
            {"name": f"skill-{index}", "description": "public skill description"}
            for index in range(300)
        ]
        recommender.recommend("find the last public skill", candidates=candidates)
        receipt = recommender.last_receipt
        self.assertEqual(receipt["candidate_count"], 300)
        self.assertGreater(receipt["request_count"], 1)
        self.assertGreater(receipt["total_latency_ms"], 0.0)
        self.assertGreater(receipt["total_usage"].get("cost", 0.0), 0.0)
        self.assertEqual(receipt["terminal_state"], "hosted_selection")


if __name__ == "__main__":
    unittest.main()
