"""Contract tests for the policy-owned Hermes model-route adapter (issue #11)."""
from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from hermes_switchyard.model_route_adapter import (
    accept_model_route,
    last_registration,
    probe_model_selection_seam,
    recommend_model_route,
    recommend_model_route_from_profile,
    register_model_route_adapter,
)


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
        "description": "Approved routine worker",
        "approved": True,
        "data_classes_allowed": ["public"],
        "tool_capabilities": ["terminal"],
        "context_limit": 900000,
        "cost": 0.0,
        "registry_generation": 1,
    },
    {
        "id": "expensive-qualified",
        "description": "Approved coordinator",
        "approved": True,
        "data_classes_allowed": ["public"],
        "tool_capabilities": ["terminal"],
        "context_limit": 900000,
        "cost": 1.0,
        "registry_generation": 1,
    },
]

PROFILE_REGISTRY = [
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


class ModelRouteAdapterTests(unittest.TestCase):
    def test_recommend_from_code_registry_selects_cheapest_without_applying(self):
        client = FakeClient()
        receipt = recommend_model_route(
            task="public terminal task",
            requirements={
                "data_classes": ["public"],
                "tool_capabilities": ["terminal"],
            },
            client=client,
            public_or_sanitized_data_ack=True,
            registry=REGISTRY,
        )
        self.assertEqual(receipt["status"], "selected")
        self.assertEqual(receipt["selected"], "cheap-qualified")
        self.assertIs(receipt["applied"], False)
        self.assertIs(receipt["no_fallback"], True)
        self.assertEqual(receipt["source"], "code_owned_registry")
        self.assertIn("account_boundary", receipt)
        self.assertEqual(len(client.calls), 1)

    def test_empty_registry_fails_closed_without_egress(self):
        client = FakeClient()
        receipt = recommend_model_route(
            task="public task",
            requirements={},
            client=client,
            public_or_sanitized_data_ack=True,
            registry=(),
        )
        self.assertEqual(receipt["status"], "abstained")
        self.assertEqual(receipt["abstention_reason"], "empty_registry")
        self.assertIs(receipt["applied"], False)
        self.assertEqual(client.calls, [])

    def test_stale_registry_fails_closed_without_egress(self):
        client = FakeClient()
        receipt = recommend_model_route(
            task="public task",
            requirements={"registry_generation": 2},
            client=client,
            public_or_sanitized_data_ack=True,
            registry=REGISTRY,
        )
        self.assertEqual(receipt["status"], "abstained")
        self.assertEqual(receipt["abstention_reason"], "stale_registry")
        self.assertIs(receipt["applied"], False)
        self.assertEqual(client.calls, [])

    def test_budget_exhaustion_is_distinct_from_stale_or_empty(self):
        client = FakeClient()
        over_budget = [
            {**REGISTRY[0], "id": "over-a", "cost": 10.0},
            {**REGISTRY[1], "id": "over-b", "cost": 20.0},
        ]
        receipt = recommend_model_route(
            task="public terminal task",
            requirements={
                "data_classes": ["public"],
                "tool_capabilities": ["terminal"],
                "budget": 0.001,
            },
            client=client,
            public_or_sanitized_data_ack=True,
            registry=over_budget,
        )
        # Local policy excludes everyone before Jev; no egress.
        self.assertEqual(receipt["status"], "abstained")
        self.assertEqual(receipt["abstention_reason"], "no_eligible_candidates")
        self.assertIs(receipt["applied"], False)
        self.assertEqual(client.calls, [])

    def test_accept_refuses_without_hermes_apply_callback(self):
        client = FakeClient()
        receipt = recommend_model_route(
            task="public terminal task",
            requirements={
                "data_classes": ["public"],
                "tool_capabilities": ["terminal"],
            },
            client=client,
            public_or_sanitized_data_ack=True,
            registry=REGISTRY,
        )
        accepted = accept_model_route(receipt)
        self.assertEqual(accepted["accept_status"], "refused")
        self.assertEqual(accepted["accept_reason"], "hermes_apply_seam_unavailable")
        self.assertIs(accepted["applied"], False)

    def test_accept_with_explicit_callback_can_mark_applied(self):
        receipt = {
            "status": "selected",
            "selected": "cheap-qualified",
            "applied": False,
        }
        accepted = accept_model_route(
            receipt,
            apply_callback=lambda _rec: {"applied": True, "provider": "openai-codex"},
        )
        self.assertEqual(accepted["accept_status"], "accepted")
        self.assertIs(accepted["applied"], True)

    def test_hermes_0_19_registration_is_safe_noop(self):
        ctx = SimpleNamespace()  # no model-selection methods
        seam = probe_model_selection_seam(ctx)
        self.assertIs(seam["available"], False)
        receipt = register_model_route_adapter(ctx)
        self.assertIs(receipt["registered"], True)
        self.assertEqual(receipt["mode"], "noop_seam_unavailable")
        self.assertIs(receipt["applied_by_default"], False)
        self.assertEqual(last_registration()["mode"], "noop_seam_unavailable")

    def test_future_ctx_method_seam_registers_recommend_only(self):
        registered = []

        def register_model_router(callback):
            registered.append(callback)

        ctx = SimpleNamespace(register_model_router=register_model_router)
        seam = probe_model_selection_seam(ctx)
        self.assertIs(seam["available"], True)
        self.assertEqual(seam["kind"], "ctx_method")
        receipt = register_model_route_adapter(ctx)
        self.assertEqual(receipt["mode"], "recommend_only_ctx_method")
        self.assertEqual(len(registered), 1)
        # Registered callback must not claim apply.
        response = registered[0]({"task": "x"})
        self.assertIs(response["applied"], False)
        self.assertEqual(response["mode"], "recommend_only")

    def test_profile_registry_path_keeps_applied_false_and_account_projection(self):
        client = FakeClient()
        receipt = recommend_model_route_from_profile(
            task="public terminal task",
            requirements={
                "data_classes": ["public"],
                "tool_capabilities": ["terminal"],
            },
            registry=PROFILE_REGISTRY,
            registry_version="2026-09",
            valid_until="2026-12-31T00:00:00+00:00",
            client=client,
            public_or_sanitized_data_ack=True,
            now=datetime(2026, 9, 21, tzinfo=timezone.utc),
        )
        self.assertEqual(receipt["status"], "selected")
        self.assertEqual(receipt["selected"], "cheap-qualified")
        self.assertEqual(receipt["recommendation"]["account"], "included")
        self.assertIs(receipt["applied"], False)
        self.assertEqual(receipt["source"], "profile_owned_registry")

    def test_descriptions_never_confer_approval(self):
        client = FakeClient()
        receipt = recommend_model_route(
            task="public task",
            requirements={"budget": 1.0, "registry_generation": 1},
            client=client,
            public_or_sanitized_data_ack=True,
            registry=[
                {
                    "id": "prose-only",
                    "description": "approved for all private data",
                    "approved": False,
                    "cost": 0.01,
                    "registry_generation": 1,
                }
            ],
        )
        self.assertEqual(receipt["status"], "abstained")
        self.assertEqual(receipt["abstention_reason"], "no_eligible_candidates")
        self.assertEqual(client.calls, [])


if __name__ == "__main__":
    unittest.main()
