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


if __name__ == "__main__":
    unittest.main()
