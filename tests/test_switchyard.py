"""Offline behavioral tests for the bounded Jev plugin.

All model and desktop interactions use synthetic transport/dispatch fixtures.
"""
from __future__ import annotations

import json
import math
import io
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace
from unittest import mock

from hermes_switchyard import client as client_module
from hermes_switchyard.client import DecisionClient
from hermes_switchyard.computer_use import StaleTargetError, _hotkeys_for_platform, run_computer_goal
from hermes_switchyard.routing import route_model, select_skill


class FakeDecisionClient:
    def __init__(self, response_factory):
        self.response_factory = response_factory
        self.calls = []

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        if public_or_sanitized_data_ack is not True:
            raise AssertionError("test client requires the acknowledgement")
        self.calls.append((state, questions))
        return self.response_factory(state, questions)


def _choice(criteria, selected=None, confidence=0.95, probabilities=None):
    keys = list(criteria)
    selected = selected if selected in criteria else keys[0]
    if probabilities is None:
        if len(keys) == 1:
            probabilities = {keys[0]: 1.0}
        else:
            remainder = 1.0 - 0.9
            probabilities = {key: remainder / (len(keys) - 1) for key in keys}
            probabilities[selected] = 0.9
    return {"choice": selected, "confidence": confidence, "probabilities": probabilities}


def _skill_response(*, confidence=0.95, needs=0.95, winning=0.95):
    def response(_state, questions):
        criteria = questions["skill"]["criteria"]
        selected = next(iter(criteria))
        if len(criteria) > 1:
            other = next(key for key in criteria if key != selected)
            probabilities = {selected: winning, other: 1.0 - winning}
        else:
            probabilities = {selected: 1.0}
        return {
            "model": "typesafe/jev-1.13",
            "answers": {
                "skill": _choice(criteria, selected, confidence, probabilities),
                "needs_skill": {"noul": needs},
            },
            "usage": {},
        }

    return response


def _route_response(scores):
    def response(_state, questions):
        return {
            "model": "typesafe/jev-1.13",
            "answers": {name: {"noul": scores.get(name, 0.0)} for name in questions},
            "usage": {},
        }

    return response


class SyntheticDispatch:
    def __init__(self, captures):
        self.captures = list(captures)
        self.action_calls = []
        self.capture_calls = 0

    def __call__(self, tool_name, args):
        self.assert_tool(tool_name)
        if args.get("action") == "capture":
            self.capture_calls += 1
            if not self.captures:
                raise AssertionError("fixture ran out of captures")
            return self.captures.pop(0)
        self.action_calls.append(dict(args))
        return {
            "ok": True,
            "action": args.get("action"),
            "effect": {"confirmed": True, "status": "applied"},
            "verdict": "confirmed",
            "escalation": None,
        }

    @staticmethod
    def assert_tool(tool_name):
        if tool_name != "computer_use":
            raise AssertionError(f"unexpected tool {tool_name!r}")


def _capture(*, label="Go", title="Docs", app="Chrome", role="Button", bounds=None, focused=None):
    element = {"index": 1, "role": role, "label": label}
    if bounds is not None:
        element["bounds"] = bounds
    if focused is not None:
        element["focused"] = focused
    return {
        "app": app,
        "window_title": title,
        "elements": [element],
    }


class ComputerClient:
    def __init__(self, operations):
        self.operations = list(operations)
        self.calls = []

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        if public_or_sanitized_data_ack is not True:
            raise AssertionError("test client requires the acknowledgement")
        self.calls.append((state, questions))
        answers = {}
        if "operation" in questions:
            operation = self.operations.pop(0)
            answers["operation"] = _choice(questions["operation"]["criteria"], operation)
            answers["hotkey"] = _choice(questions["hotkey"]["criteria"])
        else:
            for name, question in questions.items():
                answers[name] = _choice(question["criteria"])
        return {
            "model": "typesafe/jev-1.13",
            "answers": answers,
            "usage": {},
        }


class RoutingTests(unittest.TestCase):
    def test_skill_selection_searches_catalog_larger_than_one_choice(self):
        candidates = [
            {"name": f"skill-{index}", "description": f"public skill {index} " + ("x" * 1_000)}
            for index in range(300)
        ]

        class LargeCatalogClient:
            def __init__(self):
                self.calls = []

            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                self.assert_ack(public_or_sanitized_data_ack)
                self.calls.append((state, questions))
                answers = {}
                if "needs_skill" in questions:
                    answers["needs_skill"] = {"noul": 0.99}
                if "skill" in questions:
                    criteria = questions["skill"]["criteria"]
                    answers["skill"] = _choice(criteria, "skill-299", confidence=0.99)
                for name, question in questions.items():
                    if not name.startswith("skill_chunk_"):
                        continue
                    criteria = question["criteria"]
                    selected = "skill-299" if "skill-299" in criteria else next(iter(criteria))
                    answers[name] = _choice(criteria, selected, confidence=0.99)
                return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}

            @staticmethod
            def assert_ack(value):
                if value is not True:
                    raise AssertionError("test client requires the acknowledgement")

        client = LargeCatalogClient()
        result = select_skill(
            task="find the last public skill",
            candidates=candidates,
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["selected"], "skill-299")
        self.assertGreater(len(client.calls), 1)
        self.assertTrue(all(len(json.dumps({"state": state, "questions": questions})) < 96_000 for state, questions in client.calls))
        self.assertEqual(result["request_count"], len(client.calls))
        offered = {
            name
            for _state, questions in client.calls
            for question_name, question in questions.items()
            if question_name.startswith("skill_chunk_")
            for name in question["criteria"]
            if name != "__jev_none_of_these__"
        }
        self.assertEqual(offered, {candidate["name"] for candidate in candidates})
        self.assertEqual(result["offered_count"], 300)
        self.assertEqual(result["excluded_count"], 0)
        self.assertEqual(result["shortlist_policy"], "full_partition_fan_out")

    def test_skill_fan_out_has_one_aggregate_request_budget(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            answers = {}
            for name, question in payload["questions"].items():
                if name == "needs_skill":
                    answers[name] = {"noul": 0.99}
                else:
                    answers[name] = _choice(question["criteria"])
            return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}

        client = DecisionClient(api_key="test-key", transport=transport)
        candidates = [
            {"name": f"skill-{index}", "description": "public description"}
            for index in range(300)
        ]
        with mock.patch("hermes_switchyard.routing._PARTITION_CRITERIA_BYTES", 100):
            with self.assertRaises(ValueError):
                select_skill(
                    task="public task", candidates=candidates, client=client,
                    public_or_sanitized_data_ack=True,
                )
        self.assertEqual(len(payloads), 64)

    def test_exact_identifiers_reject_whitespace_without_egress(self):
        client = FakeDecisionClient(_skill_response())
        with self.assertRaises(ValueError):
            select_skill(
                task="x",
                candidates=[{"name": " skill", "description": "x"}],
                client=client,
                public_or_sanitized_data_ack=True,
            )
        self.assertEqual(client.calls, [])

    def test_skill_abstains_on_each_conservative_gate(self):
        for response in (
            _skill_response(confidence=0.70),
            _skill_response(needs=0.70),
            _skill_response(winning=0.70),
        ):
            client = FakeDecisionClient(response)
            result = select_skill(
                task="x",
                candidates=[{"name": "skill-a", "description": "special"}, {"name": "skill-b", "description": "other"}],
                client=client,
                public_or_sanitized_data_ack=True,
            )
            self.assertIsNone(result["selected"])
            self.assertEqual(result["status"], "abstained")
            self.assertTrue(result["abstention_reason"])

    def test_skill_ack_denial_happens_before_client(self):
        client = FakeDecisionClient(_skill_response())
        with self.assertRaises(PermissionError):
            select_skill(
                task="x",
                candidates=[{"name": "skill-a", "description": "special"}],
                client=client,
                public_or_sanitized_data_ack=False,
            )
        self.assertEqual(client.calls, [])

    def test_model_filters_policy_metadata_and_selects_cheapest_qualified(self):
        candidates = [
            {
                "id": "cheap",
                "description": "cheap eligible route",
                "approved": True,
                "data_classes_allowed": ["public"],
                "tool_capabilities": ["browser"],
                "context_limit": 8192,
                "cost": 0.10,
            },
            {
                "id": "expensive",
                "description": "expensive eligible route",
                "approved": True,
                "data_classes_allowed": ["public"],
                "tool_capabilities": ["browser"],
                "context_limit": 8192,
                "cost": 0.50,
            },
            {
                "id": "unapproved",
                "description": "description must not override policy",
                "approved": False,
                "data_classes_allowed": ["public"],
                "tool_capabilities": ["browser"],
                "context_limit": 8192,
                "cost": 0.01,
            },
            {
                "id": "wrong-class",
                "description": "wrong data class",
                "approved": True,
                "data_classes_allowed": ["private"],
                "tool_capabilities": ["browser"],
                "context_limit": 8192,
                "cost": 0.01,
            },
            {
                "id": "wrong-tool",
                "description": "missing tool",
                "approved": True,
                "data_classes_allowed": ["public"],
                "tool_capabilities": ["terminal"],
                "context_limit": 8192,
                "cost": 0.01,
            },
            {
                "id": "short-context",
                "description": "too little context",
                "approved": True,
                "data_classes_allowed": ["public"],
                "tool_capabilities": ["browser"],
                "context_limit": 100,
                "cost": 0.01,
            },
            {
                "id": "over-budget",
                "description": "too expensive",
                "approved": True,
                "data_classes_allowed": ["public"],
                "tool_capabilities": ["browser"],
                "context_limit": 8192,
                "cost": 2.00,
            },
        ]
        client = FakeDecisionClient(_route_response({"fit_0": 0.90, "fit_1": 0.90}))
        result = route_model(
            task="browse public data",
            candidates=candidates,
            requirements={
                "data_classes": ["public"],
                "tool_capabilities": ["browser"],
                "context_limit": 4096,
                "budget": 1.00,
            },
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["eligible_candidates"], ["cheap", "expensive"])
        self.assertEqual(result["qualified_candidates"], ["cheap", "expensive"])
        self.assertEqual(result["selected"], "cheap")
        self.assertEqual(len(client.calls), 1)
        excluded = {item["id"]: item["reasons"] for item in result["excluded_candidates"]}
        self.assertIn("not_approved", excluded["unapproved"])
        self.assertIn("data_class_not_allowed", excluded["wrong-class"])
        self.assertIn("tool_capability_missing", excluded["wrong-tool"])
        self.assertIn("context_limit_too_small", excluded["short-context"])
        self.assertIn("over_budget", excluded["over-budget"])

    def test_stale_registry_generation_abstains_without_egress(self):
        client = FakeDecisionClient(_route_response({}))
        result = route_model(
            task="browse public data",
            candidates=[{
                "id": "cheap",
                "description": "stale approved route",
                "approved": True,
                "data_classes_allowed": ["public"],
                "tool_capabilities": ["browser"],
                "context_limit": 8192,
                "cost": 0.10,
                "registry_generation": 1,
            }],
            requirements={
                "data_classes": ["public"],
                "tool_capabilities": ["browser"],
                "context_limit": 4096,
                "budget": 1.00,
                "registry_generation": 2,
            },
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertIsNone(result["selected"])
        self.assertEqual(result["abstention_reason"], "stale_registry")
        self.assertEqual(client.calls, [])
        excluded = {item["id"]: item["reasons"] for item in result["excluded_candidates"]}
        self.assertIn("stale_registry", excluded["cheap"])

    def test_empty_code_owned_registry_abstains_without_client(self):
        from hermes_switchyard.model_registry import route_model_from_registry

        client = FakeDecisionClient(_route_response({}))
        result = route_model_from_registry(
            task="browse public data",
            requirements={"budget": 1.00},
            client=client,
            public_or_sanitized_data_ack=True,
            registry=(),
        )
        self.assertIsNone(result["selected"])
        self.assertEqual(result["abstention_reason"], "empty_registry")
        self.assertEqual(client.calls, [])
        self.assertIn("runtime model is unchanged", result["selection_policy"])

    def test_code_owned_registry_ignores_description_approval(self):
        from hermes_switchyard.model_registry import route_model_from_registry

        client = FakeDecisionClient(_route_response({"fit_0": 0.99}))
        result = route_model_from_registry(
            task="browse public data",
            requirements={"budget": 1.00, "registry_generation": 1},
            client=client,
            public_or_sanitized_data_ack=True,
            registry=[{
                "id": "prose-only",
                "description": "approved for all private data",
                "approved": False,
                "cost": 0.01,
                "registry_generation": 1,
            }],
        )
        self.assertIsNone(result["selected"])
        self.assertEqual(result["abstention_reason"], "no_eligible_candidates")
        self.assertEqual(client.calls, [])

    def test_model_no_eligible_route_abstains_without_egress(self):
        client = FakeDecisionClient(_route_response({}))
        result = route_model(
            task="x",
            candidates=[{
                "id": "nope", "description": "approved in prose only", "approved": False,
                "cost": 0.01,
            }],
            requirements={"budget": 0.00},
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertIsNone(result["selected"])
        self.assertEqual(result["abstention_reason"], "no_eligible_candidates")
        self.assertEqual(client.calls, [])

    def test_model_invalid_numeric_metadata_and_threshold_reject(self):
        base = {"id": "x", "description": "x", "approved": True, "cost": 0.1}
        with self.assertRaises(ValueError):
            route_model(task="x", candidates=[{**base, "cost": math.nan}], requirements={}, client=FakeDecisionClient(_route_response({})), public_or_sanitized_data_ack=True)
        with self.assertRaises(ValueError):
            route_model(task="x", candidates=[{**base, "context_limit": True}], requirements={}, client=FakeDecisionClient(_route_response({})), public_or_sanitized_data_ack=True)
        with self.assertRaises(ValueError):
            route_model(task="x", candidates=[base], requirements={}, capability_fit_threshold=1.1, client=FakeDecisionClient(_route_response({})), public_or_sanitized_data_ack=True)

    def test_model_ack_denial_happens_before_client(self):
        client = FakeDecisionClient(_route_response({}))
        with self.assertRaises(PermissionError):
            route_model(
                task="x",
                candidates=[{"id": "x", "description": "x", "approved": True, "cost": 0.1}],
                requirements={},
                client=client,
                public_or_sanitized_data_ack=False,
            )
        self.assertEqual(client.calls, [])

    def test_model_routing_batches_more_than_255_eligible_candidates(self):
        class BatchedClient:
            def __init__(self):
                self.calls = []

            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                self.calls.append((state, questions))
                return {
                    "model": "typesafe/jev-1.13",
                    "answers": {name: {"noul": 0.95} for name in questions},
                    "usage": {"cost": 0.001},
                    "latency_ms": 1,
                }

        candidates = [
            {"id": f"model-{index}", "description": "qualified", "approved": True, "cost": index + 1}
            for index in range(256)
        ]
        client = BatchedClient()
        result = route_model(
            task="public routing task", candidates=candidates, requirements={}, client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["selected"], "model-0")
        self.assertEqual(len(client.calls), 2)
        self.assertTrue(all(len(questions) <= 200 for _state, questions in client.calls))
        self.assertEqual(len(result["capability_fit_scores"]), 256)
        self.assertEqual(result["request_count"], 2)
        self.assertAlmostEqual(result["total_usage"]["cost"], 0.002)

    def test_model_routing_persists_only_allowlisted_usage_keys(self):
        class SecretUsageClient:
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                return {
                    "model": "typesafe/jev-1.13",
                    "answers": {name: {"noul": 0.95} for name in questions},
                    "usage": {"cost": 0.001, "provider_controlled_secret_name": 7},
                    "latency_ms": 1,
                }

        result = route_model(
            task="public routing task",
            candidates=[{"id": "x", "description": "x", "approved": True, "cost": 0.1}],
            requirements={},
            client=SecretUsageClient(),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["total_usage"], {"cost": 0.001})
        self.assertNotIn("provider_controlled_secret_name", json.dumps(result))

    def test_aggregate_metadata_unknown_cost_is_not_zero(self):
        from hermes_switchyard import routing

        for first, second in ((None, 0.001), (0.001, None)):
            with self.subTest(first=first, second=second):
                result = routing._aggregate_metadata(
                    [
                        {"usage": {"cost": first}, "latency_ms": 1, "request_count": 1},
                        {"usage": {"cost": second}, "latency_ms": 1, "request_count": 1},
                    ]
                )
                self.assertIsNone(result["total_usage"].get("cost"))


class ClientTests(unittest.TestCase):
    def test_score_answers_are_supported_and_validated(self):
        question = {
            "severity": {
                "type": "score",
                "instructions": "Rate severity from cosmetic to blocking.",
                "criteria": ["cosmetic", "blocking"],
            }
        }
        client = DecisionClient(
            api_key="test-key",
            transport=lambda _payload: {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "severity": {
                        "score": 0.8,
                        "legend": {"0": "cosmetic", "1": "blocking"},
                        "probabilities": {"0": 0.2, "1": 0.8},
                        "confidence": 0.8,
                    }
                },
            },
        )
        result = client.decide("public", question, public_or_sanitized_data_ack=True)
        self.assertEqual(result["answers"]["severity"]["score"], 0.8)

    def test_score_rejects_relabeling_and_inconsistent_weighted_value(self):
        question = {"severity": {
            "type": "score", "instructions": "Rate severity.",
            "criteria": ["cosmetic", "blocking"],
        }}
        for legend, score in (({"0": "minor", "1": "blocking"}, 0.8), ({"0": "cosmetic", "1": "blocking"}, 0.2)):
            client = DecisionClient(api_key="test-key", transport=lambda _payload, legend=legend, score=score: {
                "model": "typesafe/jev-1.13",
                "answers": {"severity": {
                    "score": score, "legend": legend,
                    "probabilities": {"0": 0.2, "1": 0.8}, "confidence": 0.8,
                }},
            })
            with self.assertRaises(ValueError):
                client.decide("public", question, public_or_sanitized_data_ack=True)

    def test_question_validation_precedes_transport_and_large_sets_are_batched(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            return {
                "model": "typesafe/jev-1.13",
                "answers": {name: {"noul": 0.9} for name in payload["questions"]},
                "usage": {"cost": 0.001},
                "latency_ms": 1,
            }

        client = DecisionClient(api_key="test-key", transport=transport)
        with self.assertRaises(ValueError):
            client.decide("public", {"bad": {"type": "noul", "instructions": 7}}, public_or_sanitized_data_ack=True)
        self.assertEqual(calls, [])
        questions = {
            f"q{index}": {"type": "noul", "instructions": "Is this true?"}
            for index in range(256)
        }
        result = client.decide("public", questions, public_or_sanitized_data_ack=True)
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(len(call["questions"]) <= 255 for call in calls))
        self.assertEqual(len(result["answers"]), 256)
        self.assertEqual(result["request_count"], 2)
        self.assertAlmostEqual(result["total_usage"]["cost"], 0.002)
        self.assertNotIn("provider_controlled_secret_name", result["total_usage"])

    def test_client_usage_keeps_only_allowlisted_keys(self):
        client = DecisionClient(
            api_key="test-key",
            transport=lambda _payload: {
                "model": "typesafe/jev-1.13",
                "answers": {"q": {"noul": 0.9}},
                "usage": {
                    "prompt_tokens": 4,
                    "provider_controlled_secret_name": 7,
                    "note": "provider text",
                },
            },
        )
        result = client.decide(
            "public",
            {"q": {"type": "noul", "instructions": "Is this true?"}},
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["usage"], {"prompt_tokens": 4.0})
        self.assertNotIn("provider_controlled_secret_name", json.dumps(result))

    def test_merge_usage_unknown_cost_is_not_zero(self):
        total: dict = {}
        DecisionClient._merge_usage(total, {"cost": None})
        DecisionClient._merge_usage(total, {"cost": 0.002})
        self.assertIsNone(total.get("cost"))
        omitted = {}
        DecisionClient._merge_usage(omitted, {"prompt_tokens": 1})
        DecisionClient._merge_usage(omitted, {"cost": 0.002, "prompt_tokens": 1})
        self.assertIsNone(omitted.get("cost"))
        self.assertEqual(omitted.get("prompt_tokens"), 2.0)

    def test_total_question_budget_rejects_before_transport(self):
        from hermes_switchyard import schemas
        calls = []
        client = DecisionClient(api_key="test-key", transport=lambda payload: (calls.append(payload) or {}))
        maximum = schemas.ASSESS["parameters"]["properties"]["questions"]["maxProperties"]
        questions = {
            f"q{index}": {"type": "noul", "instructions": "Is this true?"}
            for index in range(maximum + 1)
        }
        with self.assertRaises(ValueError):
            client.decide("public", questions, public_or_sanitized_data_ack=True)
        self.assertEqual(calls, [])

    def test_serialized_splitting_cannot_exceed_request_budget(self):
        calls = []
        client = DecisionClient(api_key="test-key", transport=lambda payload: (calls.append(payload) or {}))
        questions = {
            f"q{index}": {"type": "noul", "instructions": "x" * 50_000}
            for index in range(65)
        }
        with self.assertRaises(ValueError):
            client.decide("public", questions, public_or_sanitized_data_ack=True)
        self.assertEqual(calls, [])

    def test_direct_typesafe_endpoint_is_supported_without_openrouter_provider_hint(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            return {
                "model": "jev-latest",
                "answers": {"answer": {"noul": 0.9}},
            }

        client = DecisionClient(
            api_key="test-key",
            endpoint="https://api.typesafe.ai/v1/systemone",
            model="jev-latest",
            transport=transport,
        )
        client.decide("public", {"answer": {"type": "noul", "instructions": "Is the statement true?"}}, public_or_sanitized_data_ack=True)
        self.assertNotIn("provider", payloads[0])

    def test_ack_denial_prevents_transport(self):
        calls = []
        client = DecisionClient(api_key="test-key", transport=lambda payload: calls.append(payload))
        with self.assertRaises(PermissionError):
            client.decide("public", {"answer": {"type": "noul", "instructions": "Is the statement true?"}}, public_or_sanitized_data_ack=False)
        self.assertEqual(calls, [])

    def test_concrete_version_suffix_is_allowed_but_substitution_is_not(self):
        question = {"answer": {"type": "noul", "instructions": "Does it fit?", "criteria": {"fit": "fit"}}}
        concrete = DecisionClient(
            api_key="test-key",
            transport=lambda _payload: {
                "model": "typesafe/jev-1.13-20260917",
                "answers": {"answer": {"noul": 0.9}},
            },
        )
        result = concrete.decide("public", question, public_or_sanitized_data_ack=True)
        self.assertEqual(result["model"], "typesafe/jev-1.13-20260917")
        substituted = DecisionClient(
            api_key="test-key",
            transport=lambda _payload: {
                "model": "other/model",
                "answers": {"answer": {"noul": 0.9}},
            },
        )
        with self.assertRaises(ValueError):
            substituted.decide("public", question, public_or_sanitized_data_ack=True)

    def test_invalid_typed_response_and_usage_fail_closed(self):
        question = {"answer": {"type": "choice", "instructions": "Choose one.", "criteria": {"a": "A", "b": "B"}}}
        invalid = DecisionClient(
            api_key="fixture-key-value",
            transport=lambda _payload: {
                "model": "typesafe/jev-1.13",
                "answers": {"answer": {"choice": "a", "confidence": 0.9, "probabilities": {"a": 2.0, "b": -1.0}}},
                "usage": {"cost": -1},
            },
        )
        with self.assertRaises(ValueError):
            invalid.decide("public", question, public_or_sanitized_data_ack=True)
        failing = DecisionClient(
            api_key="fixture-key-value",
            transport=lambda _payload: (_ for _ in ()).throw(RuntimeError("fixture-key-value leaked")),
        )
        with self.assertRaisesRegex(RuntimeError, "Jev transport failed") as caught:
            failing.decide("public", {"answer": {"type": "noul", "instructions": "Is the statement true?"}}, public_or_sanitized_data_ack=True)
        self.assertNotIn("fixture-key-value", str(caught.exception))

    def test_arbitrary_endpoint_is_rejected(self):
        with self.assertRaises(ValueError):
            DecisionClient(api_key="test-key", endpoint="https://evil.example/decisions")

    def test_transport_payload_pins_provider_fallback_and_model(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            return {
                "model": "typesafe/jev-1.13",
                "answers": {"answer": {"noul": 0.9}},
            }

        client = DecisionClient(api_key="test-key", transport=transport)
        client.decide("public", {"answer": {"type": "noul", "instructions": "Is the statement true?"}}, public_or_sanitized_data_ack=True)
        self.assertEqual(payloads[0]["model"], "typesafe/jev-1.13")
        self.assertEqual(payloads[0]["provider"], {"allow_fallbacks": False})

    def test_only_evidence_backed_model_aliases_are_accepted(self):
        with self.assertRaises(ValueError):
            DecisionClient(api_key="test-key", model="typesafe/jev-1.13-20260918")

        question = {"answer": {"type": "noul", "instructions": "Is the statement true?"}}
        mismatched = DecisionClient(
            api_key="test-key",
            model="typesafe/jev-1.13-20260917",
            transport=lambda _payload: {
                "model": "typesafe/jev-1.13",
                "answers": {"answer": {"noul": 0.9}},
            },
        )
        with self.assertRaises(ValueError):
            mismatched.decide("public", question, public_or_sanitized_data_ack=True)

    def test_redirect_handler_rejects_redirects(self):
        request = client_module.urllib.request.Request(client_module.DEFAULT_ENDPOINT)
        with self.assertRaises(client_module.urllib.error.HTTPError) as caught:
            client_module._NoRedirectHandler().http_error_302(request, None, 302, "redirect", {})
        caught.exception.close()


class ComputerUseTests(unittest.TestCase):
    def test_platform_hotkeys_use_command_on_macos(self):
        mac = _hotkeys_for_platform("darwin")
        linux = _hotkeys_for_platform("linux")
        self.assertEqual(mac["SAVE"], "cmd+s")
        self.assertEqual(mac["COPY"], "cmd+c")
        self.assertEqual(mac["REDO"], "cmd+shift+z")
        self.assertEqual(linux["SAVE"], "ctrl+s")

    def test_operation_and_target_selection_use_separate_bounded_requests(self):
        dispatch = SyntheticDispatch([_capture(), _capture(), _capture()])
        client = ComputerClient(["CLICK"])
        result = run_computer_goal(
            goal="click Go", app="Chrome", max_steps=1, dispatch=dispatch, client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["actions"][0]["element"], 1)
        self.assertEqual(set(client.calls[0][1]), {"operation", "hotkey"})
        self.assertEqual(set(client.calls[1][1]), {"click_target"})

    def test_dense_accessibility_tree_is_partitioned_without_dropping_late_controls(self):
        elements = [
            {"index": index, "role": "Button", "label": f"Action {index}"}
            for index in range(1, 301)
        ]
        captures = [{"app": "Chrome", "window_title": "Dense", "elements": elements} for _ in range(3)]
        dispatch = SyntheticDispatch(captures)

        class DenseClient:
            def __init__(self):
                self.calls = []
                self.states = []

            def decide(self, _state, questions, *, public_or_sanitized_data_ack=False):
                if public_or_sanitized_data_ack is not True:
                    raise AssertionError("test client requires the acknowledgement")
                self.states.append(_state)
                self.calls.append(questions)
                answers = {}
                for name, question in questions.items():
                    criteria = question["criteria"]
                    if name == "operation":
                        answers[name] = _choice(criteria, "CLICK")
                    elif name.startswith("click_target"):
                        if "_final_" in name:
                            selected = "299"
                        elif "299" in criteria:
                            selected = "299"
                        else:
                            selected = next(key for key in criteria if key != "__jev_no_target__")
                        answers[name] = _choice(criteria, selected)
                    else:
                        answers[name] = _choice(criteria)
                return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}

        client = DenseClient()
        result = run_computer_goal(
            goal="click Action 299",
            app="Chrome",
            max_steps=1,
            dispatch=dispatch,
            client=client,
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(dispatch.action_calls, [{"action": "click", "element": 299}])
        self.assertEqual(result["actions"][0]["element"], 299)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(len(client.calls[1]), 2)
        self.assertEqual(result["jev_request_count"], 3)
        self.assertEqual(result["native_action_count"], 1)
        operation_state = client.states[0]
        summaries = operation_state["control_partition_summaries"]
        self.assertEqual(sum(item["count"] for item in summaries), 300)
        self.assertTrue(any(item["start_position"] <= 150 <= item["end_position"] for item in summaries))

    def test_checkbox_control_is_offered_to_jev(self):
        dispatch = SyntheticDispatch([_capture(label="Agree", role="CheckBox"), _capture(label="Agree", role="CheckBox"), _capture(label="Agree", role="CheckBox")])
        result = run_computer_goal(
            goal="click Agree",
            app="Chrome",
            max_steps=1,
            dispatch=dispatch,
            client=ComputerClient(["CLICK"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["actions"][0]["element"], 1)

    def test_ack_and_empty_app_denial_happen_before_capture(self):
        dispatch = SyntheticDispatch([])
        client = ComputerClient(["BLOCKED"])
        with self.assertRaises(PermissionError):
            run_computer_goal(goal="x", app="Chrome", max_steps=1, dispatch=dispatch, client=client, public_or_sanitized_data_ack=False)
        self.assertEqual(dispatch.capture_calls, 0)
        with self.assertRaises(ValueError):
            run_computer_goal(
                goal="x", app=" ", max_steps=1, dispatch=dispatch, client=client,
                public_or_sanitized_data_ack=True,
            )
        self.assertEqual(dispatch.capture_calls, 0)

    def test_done_is_completion_candidate_and_unverified(self):
        dispatch = SyntheticDispatch([_capture()])
        result = run_computer_goal(
            goal="inspect", app="Chrome", max_steps=1, dispatch=dispatch,
            client=ComputerClient(["DONE"]), public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "completion_candidate")
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification_owner"], "coordinator")
        self.assertEqual(dispatch.action_calls, [])

    def test_default_hotkeys_are_empty(self):
        dispatch = SyntheticDispatch([_capture()])
        result = run_computer_goal(
            goal="inspect", app="Chrome", max_steps=1, dispatch=dispatch,
            client=ComputerClient(["BLOCKED"]), public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(dispatch.action_calls, [])

    def test_timeout_after_side_effect_preserves_partial_progress_receipt(self):
        class TimeoutAfterClickClient(ComputerClient):
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                if len(self.calls) >= 2:
                    raise TimeoutError("aggregate deadline expired")
                return super().decide(
                    state,
                    questions,
                    public_or_sanitized_data_ack=public_or_sanitized_data_ack,
                )

        dispatch = SyntheticDispatch([_capture(), _capture(), _capture()])
        result = run_computer_goal(
            goal="click then continue", app="Chrome", max_steps=2,
            dispatch=dispatch, client=TimeoutAfterClickClient(["CLICK"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["completed_action_count"], 1)
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertEqual(result["failure_phase"], "operation_decision")
        self.assertTrue(result["reconcile_before_retry"])

    def test_action_dispatch_timeout_preserves_attempted_action_receipt(self):
        class TimeoutDispatch(SyntheticDispatch):
            def __call__(self, tool_name, args):
                if args.get("action") != "capture":
                    self.action_calls.append(dict(args))
                    raise TimeoutError("native dispatch deadline")
                return super().__call__(tool_name, args)

        dispatch = TimeoutDispatch([_capture(), _capture()])
        result = run_computer_goal(
            goal="click Go", app="Chrome", max_steps=1,
            dispatch=dispatch, client=ComputerClient(["CLICK"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["completed_action_count"], 0)
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertEqual(result["failure_phase"], "action_dispatch")
        self.assertTrue(result["reconcile_before_retry"])

    def test_target_selection_timeout_after_side_effect_preserves_partial_progress(self):
        class TimeoutOnSecondTargetClient(ComputerClient):
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                if any(name.startswith("click_target") for name in questions):
                    target_call_count = sum(
                        any(name.startswith("click_target") for name in prior_questions)
                        for _prior_state, prior_questions in self.calls
                    )
                    if target_call_count >= 1:
                        raise TimeoutError("target selection deadline")
                return super().decide(
                    state,
                    questions,
                    public_or_sanitized_data_ack=public_or_sanitized_data_ack,
                )

        dispatch = SyntheticDispatch([_capture(), _capture(), _capture()])
        result = run_computer_goal(
            goal="click twice", app="Chrome", max_steps=2,
            dispatch=dispatch, client=TimeoutOnSecondTargetClient(["CLICK", "CLICK"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["completed_action_count"], 1)
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertEqual(result["failure_phase"], "target_selection")
        self.assertTrue(result["reconcile_before_retry"])

    def test_drag_target_selection_timeout_after_side_effect_preserves_partial_progress(self):
        class TimeoutOnDragSourceClient(ComputerClient):
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                if any(name.startswith("drag_source") for name in questions):
                    raise TimeoutError("drag source deadline")
                return super().decide(
                    state,
                    questions,
                    public_or_sanitized_data_ack=public_or_sanitized_data_ack,
                )

        dispatch = SyntheticDispatch([_capture(), _capture(), _capture()])
        result = run_computer_goal(
            goal="click then drag", app="Chrome", max_steps=2,
            dispatch=dispatch, client=TimeoutOnDragSourceClient(["CLICK", "DRAG"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["completed_action_count"], 1)
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertEqual(result["failure_phase"], "target_selection")
        self.assertTrue(result["reconcile_before_retry"])

    def test_explicit_key_returns_partial_receipt_without_foreground_retry(self):
        class FailingKeyDispatch(SyntheticDispatch):
            def __call__(self, tool_name, args):
                self.assert_tool(tool_name)
                if args.get("action") == "capture":
                    self.capture_calls += 1
                    return self.captures.pop(0)
                self.action_calls.append(dict(args))
                return {"ok": False, "error": "key delivery failed"}

        dispatch = FailingKeyDispatch([_capture(), _capture()])
        result = run_computer_goal(
            goal="save", app="Chrome", max_steps=1, dispatch=dispatch,
            client=ComputerClient(["HOTKEY"]), allowed_hotkeys=["SAVE"],
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(len(dispatch.action_calls), 1)
        self.assertEqual(dispatch.action_calls[0], {"action": "key", "keys": "ctrl+s"})
        self.assertEqual(result["status"], "partial_failure")
        self.assertTrue(result["reconcile_before_retry"])
        self.assertEqual(result["attempted_action_count"], 1)

    def test_uncertain_operation_abstains_before_target_or_action(self):
        class UncertainClient(ComputerClient):
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                self.calls.append((state, questions))
                return {
                    "model": "typesafe/jev-1.13",
                    "answers": {
                        "operation": _choice(
                            questions["operation"]["criteria"],
                            "CLICK",
                            confidence=0.0,
                            probabilities={key: 1 / len(questions["operation"]["criteria"]) for key in questions["operation"]["criteria"]},
                        ),
                        "hotkey": _choice(questions["hotkey"]["criteria"]),
                    },
                    "usage": {},
                }

        dispatch = SyntheticDispatch([_capture()])
        result = run_computer_goal(
            goal="click Go", app="Chrome", max_steps=1, dispatch=dispatch,
            client=UncertainClient([]), public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(dispatch.action_calls, [])

    def test_no_target_choice_abstains_without_action(self):
        class NoTargetClient(ComputerClient):
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                self.calls.append((state, questions))
                answers = {}
                for name, question in questions.items():
                    if name == "operation":
                        answers[name] = _choice(question["criteria"], "CLICK")
                    elif name.startswith("click_target"):
                        answers[name] = _choice(question["criteria"], "__jev_no_target__")
                    else:
                        answers[name] = _choice(question["criteria"])
                return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}

        dispatch = SyntheticDispatch([_capture(), _capture()])
        result = run_computer_goal(
            goal="click Go", app="Chrome", max_steps=1, dispatch=dispatch,
            client=NoTargetClient([]), public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["jev_request_count"], 2)
        self.assertEqual(len(result["decisions"]), 2)
        self.assertEqual(dispatch.action_calls, [])

    def test_unconfirmed_effect_stops_and_preserves_action_receipt(self):
        class NoopDispatch(SyntheticDispatch):
            def __call__(self, tool_name, args):
                result = super().__call__(tool_name, args)
                if args.get("action") != "capture":
                    result["effect"] = {"confirmed": False, "status": "suspected_noop"}
                    result["verdict"] = "suspected_noop"
                return result

        dispatch = NoopDispatch([_capture(), _capture()])
        result = run_computer_goal(
            goal="click Go", app="Chrome", max_steps=2, dispatch=dispatch,
            client=ComputerClient(["CLICK", "DONE"]), public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "unconfirmed_effect")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(len(dispatch.action_calls), 1)

    def test_escalation_stops_after_side_effect_without_continuing(self):
        class EscalatingDispatch(SyntheticDispatch):
            def __call__(self, tool_name, args):
                result = super().__call__(tool_name, args)
                if args.get("action") != "capture":
                    result["escalation"] = {"required": True}
                    result["verdict"] = "escalate"
                return result

        dispatch = EscalatingDispatch([_capture(), _capture()])
        result = run_computer_goal(
            goal="click Go", app="Chrome", max_steps=2, dispatch=dispatch,
            client=ComputerClient(["CLICK", "DONE"]), public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "escalated")
        self.assertEqual(len(result["actions"]), 1)
        self.assertTrue(result["reconcile_before_retry"])

    def test_drag_finalists_carry_source_context_and_exclude_source_destination(self):
        captures = [
            {
                "app": "Chrome",
                "window_title": "Drag",
                "elements": [
                    {"index": 1, "role": "Button", "label": "Same"},
                    {"index": 2, "role": "Button", "label": "Same"},
                ],
            }
            for _ in range(4)
        ]
        dispatch = SyntheticDispatch(captures)

        class DragClient:
            def __init__(self):
                self.states = []

            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                self.states.append((state, questions))
                answers = {}
                for name, question in questions.items():
                    criteria = question["criteria"]
                    if name == "operation":
                        answers[name] = _choice(criteria, "DRAG")
                    elif name.startswith("drag_source"):
                        answers[name] = _choice(criteria, "1")
                    elif name.startswith("drag_target"):
                        if "1" in criteria:
                            raise AssertionError("source was offered as destination")
                        answers[name] = _choice(criteria, "2")
                    else:
                        answers[name] = _choice(criteria)
                return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}

        client = DragClient()
        result = run_computer_goal(
            goal="drag the first button to the second", app="Chrome", max_steps=1,
            dispatch=dispatch, client=client, public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["actions"][0]["operation"], "DRAG")
        source_state = next(state for state, questions in client.states if "drag_source" in questions)
        destination_state = next(state for state, questions in client.states if "drag_target" in questions)
        self.assertEqual(source_state["selected_operation"], "DRAG")
        self.assertEqual(source_state["target_role"], "source")
        self.assertEqual(destination_state["target_role"], "destination")
        self.assertEqual(destination_state["selected_source_id"], "1")

    def test_changed_target_is_refused_before_action_dispatch(self):
        dispatch = SyntheticDispatch([_capture(label="Go"), _capture(label="Other")])
        with self.assertRaises(StaleTargetError):
            run_computer_goal(
                goal="click Go", app="Chrome", max_steps=1, dispatch=dispatch,
                client=ComputerClient(["CLICK"]), public_or_sanitized_data_ack=True,
            )
        self.assertEqual(dispatch.action_calls, [])
        self.assertEqual(dispatch.capture_calls, 2)

    def test_matching_target_is_revalidated_before_click(self):
        dispatch = SyntheticDispatch([_capture(), _capture(), _capture()])
        result = run_computer_goal(
            goal="click Go", app="Chrome", max_steps=1, dispatch=dispatch,
            client=ComputerClient(["CLICK"]), public_or_sanitized_data_ack=True,
        )
        self.assertEqual(len(dispatch.action_calls), 1)
        self.assertEqual(dispatch.action_calls[0], {"action": "click", "element": 1})
        self.assertEqual(result["actions"][0]["element"], 1)

    def test_matching_target_accepts_tuple_list_bounds_equivalence(self):
        dispatch = SyntheticDispatch([
            _capture(bounds=[0, 0, 10, 10]),
            _capture(bounds=(0, 0, 10, 10)),
            _capture(bounds=[0, 0, 10, 10]),
        ])
        run_computer_goal(
            goal="click Go", app="Chrome", max_steps=1, dispatch=dispatch,
            client=ComputerClient(["CLICK"]), public_or_sanitized_data_ack=True,
        )
        self.assertEqual(dispatch.action_calls, [{"action": "click", "element": 1}])

    def test_registered_cua_loop_dispatches_native_without_conversational_llm_per_action(self):
        import hermes_switchyard

        class ForbiddenLLM:
            def __getattr__(self, name):
                raise AssertionError(f"conversational ctx.llm call: {name}")

        class Context:
            def __init__(self, dispatch):
                self.settings = {"automatic_skill_recommendation": False}
                self.tools = {}
                self.dispatch_tool = dispatch
                self.llm = ForbiddenLLM()

            def get_config(self, key, default=None):
                return self.settings.get(key, default)

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

        dispatch = SyntheticDispatch([_capture(), _capture(), _capture()])
        class NativeClient(ComputerClient):
            def close(self):
                pass

        with mock.patch.object(hermes_switchyard, "DecisionClient", lambda **_kwargs: NativeClient(["CLICK"])), \
             mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"):
            context = Context(dispatch)
            hermes_switchyard.register(context)
            result = json.loads(context.tools["jev_computer_use"]({
                "goal": "click Go",
                "app": "Chrome",
                "max_steps": 1,
                "public_or_sanitized_data_ack": True,
            }))
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(dispatch.action_calls, [{"action": "click", "element": 1}])

    def test_caller_text_input_dispatches_exact_value_without_llm_or_provider_value(self):
        dispatch = SyntheticDispatch([
            _capture(label="Search field", role="Edit"),
            _capture(label="Search field", role="Edit"),
            _capture(label="Search field", role="Edit"),
        ])
        client = ComputerClient(["SET_VALUE"])
        result = run_computer_goal(
            goal="enter public text", app="Chrome", max_steps=1,
            dispatch=dispatch, client=client,
            text_inputs=[{"field_label": " search  field ", "value": "bounded caller text"}],
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(dispatch.action_calls, [{"action": "set_value", "element": 1, "value": "bounded caller text"}])
        self.assertEqual(dispatch.capture_calls, 3)
        wire = json.dumps(client.calls, sort_keys=True)
        receipt = json.dumps(result, sort_keys=True)
        self.assertNotIn("bounded caller text", wire)
        self.assertNotIn("bounded caller text", receipt)

    def test_type_text_uses_native_type_only_for_fresh_focused_control(self):
        dispatch = SyntheticDispatch([
            _capture(label="Search field", role="Edit", focused=True),
            _capture(label="Search field", role="Edit", focused=True),
            _capture(label="Search field", role="Edit", focused=True),
        ])
        client = ComputerClient(["TYPE_TEXT"])
        result = run_computer_goal(
            goal="type public text into the focused field", app="Chrome", max_steps=1,
            dispatch=dispatch, client=client,
            text_inputs=[{"field_label": "Search field", "value": "bounded caller text"}],
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(dispatch.action_calls, [{"action": "type", "text": "bounded caller text"}])
        self.assertEqual(dispatch.capture_calls, 3)

    def test_platform_role_spelling_is_normalized_before_target_selection(self):
        dispatch = SyntheticDispatch([
            _capture(label="Continue", role="button"),
            _capture(label="Continue", role="button"),
            _capture(label="Continue", role="button"),
        ])
        result = run_computer_goal(
            goal="click Continue", app="Chrome", max_steps=1,
            dispatch=dispatch, client=ComputerClient(["CLICK"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(dispatch.action_calls, [{"action": "click", "element": 1}])

    def test_repetition_detection_uses_fresh_post_action_state_identity(self):
        def state_capture(status):
            return {
                "app": "Chrome",
                "window_title": "Stable",
                "elements": [
                    {"index": 1, "role": "Button", "label": "Continue"},
                    {"index": 2, "role": "Text", "label": f"Progress {status}"},
                ],
            }

        dispatch = SyntheticDispatch([
            state_capture(0), state_capture(0), state_capture(1),
            state_capture(1), state_capture(2),
            state_capture(2), state_capture(3),
        ])
        result = run_computer_goal(
            goal="continue until complete", app="Chrome", max_steps=3,
            dispatch=dispatch, client=ComputerClient(["CLICK", "CLICK", "CLICK"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(result["completed_action_count"], 3)

    def test_repetition_detection_stalls_on_identical_fresh_post_action_state(self):
        stable = _capture(label="Continue", title="Stable")
        dispatch = SyntheticDispatch([stable.copy() for _ in range(7)])
        result = run_computer_goal(
            goal="continue until complete", app="Chrome", max_steps=5,
            dispatch=dispatch, client=ComputerClient(["CLICK", "CLICK", "CLICK", "CLICK", "CLICK"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["status"], "stalled")
        self.assertEqual(result["completed_action_count"], 3)

    def test_caller_text_input_absent_or_ambiguous_abstains_before_side_effect(self):
        for inputs in (
            [{"field_label": "Other field", "value": "bounded caller text"}],
            [
                {"field_label": "Search field", "value": "first"},
                {"field_label": " search  field ", "value": "second"},
            ],
        ):
            dispatch = SyntheticDispatch([_capture(label="Search field", role="Edit")])

            class NoTextClient(ComputerClient):
                def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                    if "TYPE_TEXT" in questions["operation"]["criteria"]:
                        raise AssertionError("TYPE_TEXT must not be offered without one unique caller value")
                    self.operations[:] = ["BLOCKED"]
                    return super().decide(state, questions, public_or_sanitized_data_ack=public_or_sanitized_data_ack)

            result = run_computer_goal(
                goal="enter public text", app="Chrome", max_steps=1,
                dispatch=dispatch, client=NoTextClient(["BLOCKED"]), text_inputs=inputs,
                public_or_sanitized_data_ack=True,
            )
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(dispatch.action_calls, [])

    def test_text_helper_rechecks_changed_target_before_dispatch(self):
        raw_before = "A" * 101 + "first"
        raw_after = "A" * 101 + "second"
        dispatch = SyntheticDispatch([
            _capture(label=raw_before, role="Edit"),
            _capture(label=raw_before, role="Edit"),
            _capture(label=raw_after, role="Edit"),
        ])
        client = ComputerClient(["SET_VALUE"])
        helper_calls = []

        def text_helper(goal, field, context, history):
            helper_calls.append((goal, field, context, history))
            return "public text"

        with self.assertRaises(StaleTargetError):
            run_computer_goal(
                goal="enter public text", app="Chrome", max_steps=1,
                dispatch=dispatch, client=client, text_helper=text_helper,
                public_or_sanitized_data_ack=True,
            )
        self.assertEqual(dispatch.action_calls, [])
        self.assertEqual(dispatch.capture_calls, 3)
        self.assertEqual(len(helper_calls), 1)
        self.assertNotIn(raw_before, json.dumps(client.calls[0][0], sort_keys=True))
        self.assertNotIn(raw_before, json.dumps(helper_calls[0], sort_keys=True))

    def test_raw_identity_rejects_change_after_sanitization_and_truncation(self):
        raw_before = "B" * 100 + "first@example.com"
        raw_after = "B" * 100 + "second@example.com"
        dispatch = SyntheticDispatch([
            _capture(label=raw_before),
            _capture(label=raw_after, bounds=(1, 2, 3, 4)),
        ])
        client = ComputerClient(["CLICK"])
        with self.assertRaises(StaleTargetError):
            run_computer_goal(
                goal="click the button", app="Chrome", max_steps=1,
                dispatch=dispatch, client=client,
                public_or_sanitized_data_ack=True,
            )
        self.assertEqual(dispatch.action_calls, [])
        self.assertEqual(dispatch.capture_calls, 2)
        self.assertNotIn(raw_before, json.dumps(client.calls[0][0], sort_keys=True))
        self.assertNotIn(raw_after, json.dumps(client.calls[0][0], sort_keys=True))


class PluginEntryPointTests(unittest.TestCase):
    def test_cli_status_is_redacted_and_reports_fail_closed_default_readiness(self):
        import hermes_switchyard
        hermes_switchyard.reset_runtime_status()
        output = io.StringIO()
        with mock.patch.object(hermes_switchyard, "_secret", return_value="synthetic-secret"), \
             redirect_stdout(output):
            code = hermes_switchyard._cli_handler(
                SimpleNamespace(switchyard_command="status", json_output=True, toolsets=None)
            )
        result = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertNotIn("synthetic-secret", output.getvalue())
        # Unregistered: no premature hosted-construction claim; ack is unknown
        # until register() publishes install defaults.
        self.assertIsNotNone(result.get("status"))
        self.assertIsNone(result.get("public_or_sanitized_data_ack"))
        self.assertIs(result.get("hosted_construction_allowed"), False)
        self.assertIn(result["status"], {
            "ready", "credential_required", "exposure_unverified",
            "tools_not_registered", "tools_not_callable",
        })

    def test_cli_setup_uses_masked_prompt_and_profile_secret_writer(self):
        import hermes_switchyard
        try:
            import hermes_cli.config  # noqa: F401
            import hermes_cli.secret_prompt  # noqa: F401
        except ImportError as exc:
            self.skipTest(f"Hermes CLI secret prompt unavailable: {exc}")
        ensure_result = {
            "ok": True,
            "reason": "unchanged",
            "added": [],
            "already_present": ["cli:computer_use", "cli:hermes_switchyard"],
            "focus_override": {"active": False},
            "cleared_suppressions": [],
            "toolsets": ["computer_use", "hermes_switchyard"],
            "platforms": ["cli"],
        }
        with mock.patch("hermes_cli.secret_prompt.masked_secret_prompt", return_value="synthetic-key"), \
             mock.patch("hermes_cli.config.save_env_value") as save, \
             mock.patch.object(hermes_switchyard, "ensure_platform_toolsets", return_value=ensure_result) as ensure:
            code = hermes_switchyard._cli_handler(SimpleNamespace(jev_command="setup", provider="typesafe"))
        self.assertEqual(code, 0)
        save.assert_called_once_with("TYPESAFE_API_KEY", "synthetic-key")
        ensure.assert_called_once_with()

    def test_tool_availability_uses_the_configured_provider_secret(self):
        import hermes_switchyard

        class Context:
            def __init__(self, settings=None):
                self.checks = {}
                self.settings = dict(settings or {"jev_provider": "openrouter"})
            def get_config(self, key, default=None):
                return self.settings.get(key, default)
            def register_auxiliary_task(self, *_args, **_kwargs): pass
            def register_tool(self, *, name, check_fn, **_kwargs): self.checks[name] = check_fn
            def register_skill(self, *_args, **_kwargs): pass
            def register_hook(self, *_args, **_kwargs): pass

        context = Context()
        with mock.patch.object(hermes_switchyard, "_secret", side_effect=lambda provider="auto": "typesafe" if provider == "typesafe" else ""):
            hermes_switchyard.register(context)
            self.assertTrue(context.checks["jev_assess"]())
        with mock.patch.object(hermes_switchyard, "_secret", side_effect=lambda provider="auto": "openrouter" if provider == "openrouter" else ""):
            self.assertTrue(context.checks["jev_assess"]())
            incompatible = Context({"jev_provider": "not-a-provider"})
            hermes_switchyard.register(incompatible)
            self.assertFalse(incompatible.checks["jev_assess"]())

    def test_registered_handlers_deny_false_ack_before_client_network_or_capture(self):
        import hermes_switchyard

        class Context:
            def __init__(self):
                self.tools = {}
                self.dispatch_calls = []

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_skill(self, *_args, **_kwargs):
                pass

            def dispatch_tool(self, *args, **kwargs):
                self.dispatch_calls.append((args, kwargs))
                raise AssertionError("desktop capture must not run")

        context = Context()
        with mock.patch.object(hermes_switchyard, "_secret", side_effect=AssertionError("client must not initialize")) as secret:
            with mock.patch.object(hermes_switchyard, "DecisionClient", side_effect=AssertionError("network must not initialize")) as client:
                hermes_switchyard.register(context)
                for name in (
                    "jev_assess",
                    "jev_computer_use",
                    "jev_skill_select",
                    "jev_skill_select_many",
                    "jev_model_route",
                    "jev_model_route_approved",
                    "jev_session_search_rerank",
                ):
                    with self.subTest(name=name):
                        result = json.loads(context.tools[name]({"public_or_sanitized_data_ack": False}))
                        self.assertEqual(
                            result,
                            {
                                "status": "error",
                                "error": {
                                    "code": "ack_required",
                                    "reason": "public_or_sanitized_data_ack is false",
                                },
                            },
                        )
        self.assertEqual(secret.call_count, 0)
        self.assertEqual(client.call_count, 0)
        self.assertEqual(context.dispatch_calls, [])

    def test_jev_computer_use_registers_in_computer_use_toolset(self):
        import hermes_switchyard

        class Context:
            def __init__(self):
                self.toolsets = {}

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, toolset, **_kwargs):
                self.toolsets[name] = toolset

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

        context = Context()
        hermes_switchyard.register(context)
        self.assertEqual(context.toolsets["jev_computer_use"], "computer_use")
        for name in ("jev_assess", "jev_skill_select", "jev_skill_select_many", "jev_model_route", "jev_session_search_rerank"):
            self.assertEqual(context.toolsets[name], "hermes_switchyard")

    def test_jev_computer_use_is_visible_without_credentials(self):
        import hermes_switchyard

        class Context:
            def __init__(self):
                self.checks = {}

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, check_fn, **_kwargs):
                self.checks[name] = check_fn

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

            def register_system_prompt_section(self, *_args, **_kwargs):
                pass

        context = Context()
        with mock.patch.object(hermes_switchyard, "_secret", return_value=""):
            with mock.patch.object(hermes_switchyard.sys, "platform", "win32"):
                hermes_switchyard.register(context)
                self.assertTrue(context.checks["jev_computer_use"]())
                self.assertTrue(context.checks["jev_assess"]())
            with mock.patch.object(hermes_switchyard.sys, "platform", "plan9"):
                hermes_switchyard.register(context)
                self.assertFalse(context.checks["jev_computer_use"]())

    def test_session_search_rerank_unavailable_client_still_validates_request(self):
        import hermes_switchyard

        class Context:
            def __init__(self):
                self.tools = {}

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

            def register_system_prompt_section(self, *_args, **_kwargs):
                pass

        context = Context()
        with mock.patch.object(hermes_switchyard, "_secret", side_effect=RuntimeError("missing key")):
            hermes_switchyard.register(context)
            result = json.loads(context.tools["jev_session_search_rerank"]({"candidates": [{"session_id": "sess-a"}]}))
        self.assertEqual(
            result,
            {
                "status": "error",
                "error": {
                    "code": "invalid_request",
                    "reason": "request validation failed",
                },
            },
        )

    def test_windows_prompt_keeps_computer_use_pilot_configurable(self):
        import hermes_switchyard

        class Context:
            def __init__(self):
                self.tools = {}
                self.prompts = {}

            def get_config(self, _key, default=None):
                return default

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_system_prompt_section(self, name, text, **kwargs):
                self.prompts[name] = (text, kwargs)

        context = Context()
        with mock.patch.object(hermes_switchyard.sys, "platform", "win32"):
            hermes_switchyard.register(context)
        prompt, options = context.prompts["hermes-switchyard.computer-use"]
        self.assertIn("registered by default", prompt)
        self.assertIn("computer_use toolset is enabled", prompt)
        self.assertIn("Windows, macOS, and Linux", prompt)
        self.assertIn("public_or_sanitized_data_ack", prompt)
        self.assertIn("not blanket egress authorization", prompt)
        self.assertIn("does not override mandatory skills", prompt)
        self.assertIn("user's native/computer-use preference", prompt)
        self.assertIn("Use low-level computer_use", prompt)
        self.assertEqual(options["position"], "after_memory")


if __name__ == "__main__":
    unittest.main()
