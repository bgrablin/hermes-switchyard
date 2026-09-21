from __future__ import annotations

import unittest
from datetime import datetime, timezone

from hermes_switchyard.model_policy import recommend_approved_model


class FakeClient:
    def __init__(self, *, score=0.95, error=None):
        self.score = score
        self.error = error
        self.calls = []

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        self.calls.append((state, questions, public_or_sanitized_data_ack))
        if self.error is not None:
            raise self.error
        return {
            "model": "typesafe/jev-1.13",
            "answers": {name: {"noul": self.score} for name in questions},
            "usage": {"cost": 0.001},
            "latency_ms": 1,
        }


REGISTRY = [
    {
        "id": "cheap-qualified",
        "provider": "openai-codex",
        "model": "gpt-5.6-luna-900k",
        "account": "included",
        "approved": True,
        "data_classes_allowed": ["public"],
        "tool_capabilities": ["terminal"],
        "context_limit": 900000,
        "cost": 0.0,
        "description": "Approved routine worker",
    },
    {
        "id": "expensive-qualified",
        "provider": "openai-codex",
        "model": "gpt-5.6-sol-900k",
        "account": "included",
        "approved": True,
        "data_classes_allowed": ["public"],
        "tool_capabilities": ["terminal"],
        "context_limit": 900000,
        "cost": 1.0,
        "description": "Approved coordinator",
    },
]


class ApprovedModelPolicyTests(unittest.TestCase):
    def test_selects_cheapest_qualified_registry_entry_without_switching(self):
        client = FakeClient()
        result = recommend_approved_model(
            task="public terminal task",
            requirements={"data_classes": ["public"], "tool_capabilities": ["terminal"]},
            registry=REGISTRY,
            registry_version="2026-09",
            valid_until="2026-12-31T00:00:00+00:00",
            client=client,
            public_or_sanitized_data_ack=True,
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["selected"], "cheap-qualified")
        self.assertEqual(result["recommendation"]["provider"], "openai-codex")
        self.assertEqual(result["recommendation"]["model"], "gpt-5.6-luna-900k")
        self.assertEqual(result["recommendation"]["account"], "included")
        self.assertFalse(result["applied"])
        self.assertEqual(result["registry_version"], "2026-09")
        self.assertEqual(len(client.calls), 1)

    def test_stale_registry_abstains_without_provider_call(self):
        client = FakeClient()
        result = recommend_approved_model(
            task="public task",
            requirements={},
            registry=REGISTRY,
            registry_version="2026-09",
            valid_until="2026-09-01T00:00:00+00:00",
            client=client,
            public_or_sanitized_data_ack=True,
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "stale_registry")
        self.assertEqual(result["abstention_reason"], "registry_expired")
        self.assertEqual(client.calls, [])

    def test_invalid_or_unauthorized_registry_fails_before_provider(self):
        for registry in (
            [{**REGISTRY[0], "approved": False}],
            [{**REGISTRY[0], "provider": ""}],
            [{**REGISTRY[0], "account": ""}],
            [{**REGISTRY[0], "model": ""}],
        ):
            client = FakeClient()
            result = recommend_approved_model(
                task="public task",
                requirements={},
                registry=registry,
                registry_version="2026-09",
                valid_until="2026-12-31T00:00:00+00:00",
                client=client,
                public_or_sanitized_data_ack=True,
                now=datetime(2026, 9, 19, tzinfo=timezone.utc),
            )
            self.assertEqual(result["status"], "invalid_registry")
            self.assertEqual(client.calls, [])

    def test_provider_failure_is_distinct_and_never_falls_back(self):
        client = FakeClient(error=RuntimeError("synthetic provider failure"))
        result = recommend_approved_model(
            task="public task",
            requirements={},
            registry=REGISTRY,
            registry_version="2026-09",
            valid_until="2026-12-31T00:00:00+00:00",
            client=client,
            public_or_sanitized_data_ack=True,
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "provider_unavailable")
        self.assertIsNone(result["selected"])
        self.assertNotIn("synthetic provider failure", str(result))
        self.assertEqual(len(client.calls), 1)


    def test_bad_registry_field_types_fail_closed_as_invalid_registry(self):
        for overrides in (
            {"tool_capabilities": "terminal"},
            {"context_limit": 0},
            {"context_limit": "900k"},
            {"cost": float("nan")},
            {"cost": float("inf")},
            {"data_classes_allowed": ["public", "public"]},
        ):
            registry = [{**REGISTRY[0], **overrides}]
            client = FakeClient()
            result = recommend_approved_model(
                task="public task",
                requirements={},
                registry=registry,
                registry_version="2026-09",
                valid_until="2026-12-31T00:00:00+00:00",
                client=client,
                public_or_sanitized_data_ack=True,
                now=datetime(2026, 9, 19, tzinfo=timezone.utc),
            )
            self.assertEqual(result["status"], "invalid_registry", overrides)
            self.assertEqual(result["abstention_reason"], "registry_validation_failed")
            self.assertEqual(client.calls, [])

    def test_malformed_jev_response_is_provider_unavailable_not_generic_error(self):
        class MalformedClient:
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                return {"unexpected": True}

        result = recommend_approved_model(
            task="public task",
            requirements={},
            registry=REGISTRY,
            registry_version="2026-09",
            valid_until="2026-12-31T00:00:00+00:00",
            client=MalformedClient(),
            public_or_sanitized_data_ack=True,
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(result["status"], "provider_unavailable")
        self.assertEqual(result["abstention_reason"], "invalid_response")

    def test_fit_miss_keeps_abstained_even_when_others_were_over_budget(self):
        over_budget = [
            {**REGISTRY[0], "id": "over-budget", "cost": 10.0},
            {**REGISTRY[1], "id": "over-budget-2", "cost": 20.0},
        ]

        class FailingFitClient(FakeClient):
            def __init__(self):
                super().__init__(score=0.05)

        # Over-budget-only registry: budget_exhausted is correct.
        client_a = FailingFitClient()
        result_a = recommend_approved_model(
            task="public terminal task",
            requirements={"data_classes": ["public"], "tool_capabilities": ["terminal"], "budget": 0.001},
            registry=over_budget,
            registry_version="2026-09",
            valid_until="2026-12-31T00:00:00+00:00",
            client=client_a,
            public_or_sanitized_data_ack=True,
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(result_a["status"], "budget_exhausted")

        # Mixed registry: an eligible candidate was evaluated and missed fit; abstained, not budget_exhausted.
        client_b = FailingFitClient()
        result_b = recommend_approved_model(
            task="public terminal task",
            requirements={"data_classes": ["public"], "tool_capabilities": ["terminal"], "budget": 0.001},
            registry=REGISTRY + over_budget,
            registry_version="2026-09",
            valid_until="2026-12-31T00:00:00+00:00",
            client=client_b,
            public_or_sanitized_data_ack=True,
            now=datetime(2026, 9, 19, tzinfo=timezone.utc),
        )
        self.assertEqual(result_b["status"], "abstained")


if __name__ == "__main__":
    unittest.main()
