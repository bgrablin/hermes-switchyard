"""Privacy-safe typed routing-receipt tests.

Hosted calls in this file use a synthetic DecisionClient transport. No test
sends task text, candidate descriptions, conversation history, or credentials to
a model, and the receipt surface never carries provider exception text.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hermes_switchyard
from hermes_switchyard import receipt_state
from hermes_switchyard.automatic import (
    AutomaticSkillRecommender,
    build_routing_receipt,
)
from hermes_switchyard.client import DecisionClient


# The stable terminal-state enum the receipt surface exposes.
_TERMINAL = {
    "local_selection",
    "hosted_selection",
    "hosted_abstention",
    "hosted_failure",
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


def _allowed_policy(payload="SANITIZED_TASK_MARKER"):
    return {
        "version": 1,
        "decision": "allow",
        "data_class": "sanitized",
        "reason_code": "synthetic_fixture_allowed",
        "allowed_payload": payload,
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
        self.assertEqual(receipt["source_sha"], "unavailable")
        self.assertEqual(receipt["plugin_identity"]["source_sha"], "unavailable")
        self.assertTrue(receipt_state.validate_receipt(receipt))

    def test_local_selection_is_distinct_from_hosted_skip(self):
        receipt = build_routing_receipt(
            {
                "selected": "docker-management",
                "source": "local",
                "hosted_attempted": False,
                "hosted_skipped": "disabled",
                "cache_hit": False,
                "candidate_count": 1,
            }
        )
        self.assertEqual(receipt["terminal_state"], "local_selection")
        self.assertEqual(receipt["source"], "local")
        self.assertEqual(receipt["hosted_skip_reason"], "disabled")
        self.assertTrue(receipt_state.validate_receipt(receipt))

    def test_source_sha_comes_only_from_a_validated_release_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "plugin.yaml").write_text(
                "name: hermes-switchyard\nversion: 0.4.1\n", encoding="utf-8"
            )
            manifest = {
                "files": [{"path": "plugin.yaml", "sha256": "0" * 64, "size": 1}],
                "format": 1,
                "manifest_version": 1,
                "plugin": "hermes-switchyard",
                "source_sha": "a" * 40,
                "version": "0.4.1",
            }
            (root / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(receipt_state.resolve_source_sha(root), "a" * 40)
            manifest["source_sha"] = "A" * 40
            (root / "SOURCE-MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(receipt_state.resolve_source_sha(root), "unavailable")

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
        self.assertTrue(receipt_state.validate_receipt(receipt))

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

    def test_hosted_failure_without_local_winner_is_distinct_terminal_state(self):
        result = {
            "status": "abstained",
            "selected": None,
            "source": "none",
            "abstention_reason": None,
            "hosted_attempted": True,
            "hosted_error": "transport_or_execution_failure",
            "cache_hit": False,
            "candidate_count": 1,
        }
        receipt = build_routing_receipt(result)
        self.assertEqual(receipt["terminal_state"], "hosted_failure")
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
                "usage": {"cost": 0.01},
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
        for attempt in range(3):
            recommender.recommend(
                "Diagnose a Docker container",
                turn_egress_policy=_allowed_policy(),
            )
            self.assertIsNotNone(recommender.last_receipt)
            self.assertIn(recommender.last_receipt["terminal_state"], _TERMINAL)
            # Exactly one receipt is retained per attempt; it never grows into
            # a list of multiple terminal receipts.
            self.assertNotIsInstance(recommender.last_receipt["terminal_state"], list)
            if attempt == 0:
                self.assertEqual(recommender.last_receipt["terminal_state"], "hosted_selection")
            else:
                self.assertEqual(recommender.last_receipt["terminal_state"], "cache_hit")
                self.assertFalse(recommender.last_receipt["hosted_attempted"])
                self.assertEqual(recommender.last_receipt["request_count"], 0)
                self.assertEqual(recommender.last_receipt["total_usage"], {})
                self.assertEqual(recommender.last_receipt["total_latency_ms"], 0.0)

    def test_receipt_is_advisory_and_never_loads_skill(self):
        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            hosted_enabled=True,
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(
                api_key="fixture-key", transport=self._small_transport()
            ),
        )
        recommender.recommend(
            "Diagnose a Docker container",
            turn_egress_policy=_allowed_policy(),
        )
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
        result = recommender.recommend(
            "Diagnose a Docker container",
            turn_egress_policy=_allowed_policy(),
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["source"], "local")
        receipt = recommender.last_receipt
        self.assertEqual(receipt["terminal_state"], "hosted_failure_local_fallback")
        self.assertTrue(receipt["hosted_attempted"])
        self.assertFalse(receipt["hosted_succeeded"])
        self.assertEqual(receipt["hosted_error"], "transport_or_execution_failure")
        self.assertFalse(receipt["verified"])

    def test_empty_and_missing_catalog_attempts_replace_previous_receipt(self):
        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            hosted_enabled=False,
        )
        recommender.recommend("Diagnose a Docker container")
        self.assertEqual(recommender.last_receipt["terminal_state"], "local_selection")
        recommender.recommend("   ")
        self.assertEqual(recommender.last_receipt["terminal_state"], "hosted_skipped")
        self.assertEqual(recommender.last_receipt["hosted_skip_reason"], "empty_task")
        self.assertTrue(receipt_state.validate_receipt(recommender.last_receipt))

    def test_supported_receipt_command_reads_plugin_owned_state(self):
        receipt = build_routing_receipt(_skipped_result())
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                self.assertTrue(receipt_state.store_latest_receipt(receipt))
                with mock.patch("builtins.print") as printer:
                    code = hermes_switchyard._cli_handler(
                        SimpleNamespace(jev_command="receipt", json_output=True)
                    )
                self.assertEqual(code, 0)
                printed = json.loads(printer.call_args.args[0])
                self.assertEqual(printed, receipt)

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
        recommender.recommend(
            "find the last public skill",
            candidates=candidates,
            turn_egress_policy=_allowed_policy("SANITIZED_LARGE_CATALOG_TASK"),
        )
        receipt = recommender.last_receipt
        self.assertEqual(receipt["candidate_count"], 300)
        self.assertGreater(receipt["request_count"], 1)
        self.assertGreater(receipt["total_latency_ms"], 0.0)
        self.assertGreater(receipt["total_usage"].get("cost", 0.0), 0.0)
        self.assertEqual(receipt["terminal_state"], "hosted_selection")

    def test_exact_outbound_candidate_identifiers_are_scanned_before_client_creation(self):
        client_calls = []

        def client_factory():
            client_calls.append(True)
            raise AssertionError("restricted outbound payload must be rejected before client creation")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[
                {"name": "password-recovery", "description": "PRIVATE_DESCRIPTION_MARKER"},
            ],
            hosted_enabled=True,
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=client_factory,
        )
        result = recommender.recommend(
            "public task",
            turn_egress_policy=_allowed_policy("SANITIZED_TASK_MARKER"),
        )
        self.assertEqual(client_calls, [])
        self.assertFalse(result["hosted_attempted"])
        self.assertEqual(result["hosted_skipped"], "local_scan_restricted_data")
        self.assertNotIn("PRIVATE_DESCRIPTION_MARKER", json.dumps(result, sort_keys=True))

    def test_exact_hosted_wire_contains_only_accepted_task_and_candidate_identifiers(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            key = next(iter(payload["questions"]))
            criteria = payload["questions"][key]["criteria"]
            selected = next(iter(criteria))
            return {
                "model": "typesafe/jev-1.13",
                "answers": {
                    key: {
                        "choice": selected,
                        "confidence": 1.0,
                        "probabilities": {name: (1.0 if name == selected else 0.0) for name in criteria},
                    }
                },
                "usage": {},
            }

        recommender = AutomaticSkillRecommender(
            configured_candidates=[
                {"name": "public-skill", "description": "PRIVATE_DESCRIPTION_MARKER"},
            ],
            hosted_enabled=True,
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=transport),
        )
        recommender.recommend(
            "ORIGINAL_TASK_MARKER",
            turn_egress_policy=_allowed_policy("SANITIZED_TASK_MARKER"),
        )
        wire = json.dumps(payloads, sort_keys=True)
        self.assertIn("SANITIZED_TASK_MARKER", wire)
        self.assertIn("public-skill", wire)
        self.assertNotIn("ORIGINAL_TASK_MARKER", wire)
        self.assertNotIn("PRIVATE_DESCRIPTION_MARKER", wire)
        self.assertNotIn("fixture-key", wire)


if __name__ == "__main__":
    unittest.main()
