"""Offline behavioral tests for the bounded Jev plugin.

All model and desktop interactions use synthetic transport/dispatch fixtures.
"""
from __future__ import annotations

import json
import math
import unittest
from unittest import mock

from jev_decision import client as client_module
from jev_decision.client import DecisionClient
from jev_decision.computer_use import StaleTargetError, run_computer_goal
from jev_decision.routing import route_model, select_skill


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
        return {"ok": True, "action": args.get("action")}

    @staticmethod
    def assert_tool(tool_name):
        if tool_name != "computer_use":
            raise AssertionError(f"unexpected tool {tool_name!r}")


def _capture(*, label="Go", title="Docs", app="Chrome", role="Button", bounds=None):
    element = {"index": 1, "role": role, "label": label}
    if bounds is not None:
        element["bounds"] = bounds
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
        operation = self.operations.pop(0)
        operation_criteria = questions["operation"]["criteria"]
        answers = {
            "operation": _choice(operation_criteria, operation),
            "click_target": _choice(questions["click_target"]["criteria"]),
            "text_target": _choice(questions["text_target"]["criteria"]),
            "value_target": _choice(questions["value_target"]["criteria"]),
            "hotkey": _choice(questions["hotkey"]["criteria"]),
        }
        return {
            "model": "typesafe/jev-1.13",
            "answers": answers,
            "usage": {},
        }


class RoutingTests(unittest.TestCase):
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
            )
        self.assertEqual(client.calls, [])


class ClientTests(unittest.TestCase):
    def test_ack_denial_prevents_transport(self):
        calls = []
        client = DecisionClient(api_key="test-key", transport=lambda payload: calls.append(payload))
        with self.assertRaises(PermissionError):
            client.decide("public", {"answer": {"type": "noul"}})
        self.assertEqual(calls, [])

    def test_concrete_version_suffix_is_allowed_but_substitution_is_not(self):
        question = {"answer": {"type": "noul", "criteria": {"fit": "fit"}}}
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
        question = {"answer": {"type": "choice", "criteria": {"a": "A", "b": "B"}}}
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
            failing.decide("public", {"answer": {"type": "noul"}}, public_or_sanitized_data_ack=True)
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
        client.decide("public", {"answer": {"type": "noul"}}, public_or_sanitized_data_ack=True)
        self.assertEqual(payloads[0]["model"], "typesafe/jev-1.13")
        self.assertEqual(payloads[0]["provider"], {"allow_fallbacks": False})

    def test_only_evidence_backed_model_aliases_are_accepted(self):
        with self.assertRaises(ValueError):
            DecisionClient(api_key="test-key", model="typesafe/jev-1.13-20260918")

        question = {"answer": {"type": "noul"}}
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
    def test_ack_and_empty_app_denial_happen_before_capture(self):
        dispatch = SyntheticDispatch([])
        client = ComputerClient(["BLOCKED"])
        with self.assertRaises(PermissionError):
            run_computer_goal(goal="x", app="Chrome", max_steps=1, dispatch=dispatch, client=client)
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

    def test_explicit_key_has_one_dispatch_without_foreground_retry(self):
        class FailingKeyDispatch(SyntheticDispatch):
            def __call__(self, tool_name, args):
                self.assert_tool(tool_name)
                if args.get("action") == "capture":
                    self.capture_calls += 1
                    return self.captures.pop(0)
                self.action_calls.append(dict(args))
                return {"ok": False, "error": "key delivery failed"}

        dispatch = FailingKeyDispatch([_capture(), _capture()])
        with self.assertRaises(RuntimeError):
            run_computer_goal(
                goal="save", app="Chrome", max_steps=1, dispatch=dispatch,
                client=ComputerClient(["HOTKEY"]), allowed_hotkeys=["SAVE"],
                public_or_sanitized_data_ack=True,
            )
        self.assertEqual(len(dispatch.action_calls), 1)
        self.assertEqual(dispatch.action_calls[0], {"action": "key", "keys": "ctrl+s"})

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

    def test_text_helper_rechecks_changed_target_before_dispatch(self):
        raw_before = "A" * 101 + "first"
        raw_after = "A" * 101 + "second"
        dispatch = SyntheticDispatch([
            _capture(label=raw_before, role="Edit"),
            _capture(label=raw_before, role="Edit"),
            _capture(label=raw_after, role="Edit"),
        ])
        client = ComputerClient(["TYPE_TEXT"])
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
    def test_registered_handlers_deny_missing_ack_before_client_network_or_capture(self):
        import jev_decision

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
        with mock.patch.object(jev_decision, "_secret", side_effect=AssertionError("client must not initialize")) as secret:
            with mock.patch.object(jev_decision, "DecisionClient", side_effect=AssertionError("network must not initialize")) as client:
                jev_decision.register(context)
                for name in ("jev_computer_use", "jev_skill_select", "jev_model_route"):
                    with self.subTest(name=name):
                        result = json.loads(context.tools[name]({}))
                        self.assertEqual(
                            result,
                            {
                                "status": "error",
                                "error": {
                                    "code": "ack_required",
                                    "reason": "public_or_sanitized_data_ack is required",
                                },
                            },
                        )
        self.assertEqual(secret.call_count, 0)
        self.assertEqual(client.call_count, 0)
        self.assertEqual(context.dispatch_calls, [])

    def test_windows_prompt_keeps_computer_use_pilot_optional(self):
        import jev_decision

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
        with mock.patch.object(jev_decision.sys, "platform", "win32"):
            jev_decision.register(context)
        prompt, options = context.prompts["jev-decision.windows-computer-use"]
        self.assertIn("configurable capability", prompt)
        self.assertIn("only when the caller explicitly approves the pilot", prompt)
        self.assertIn("public or sanitized", prompt)
        self.assertIn("not blanket egress authorization", prompt)
        self.assertIn("does not override mandatory skills", prompt)
        self.assertIn("user's native/computer-use preference", prompt)
        self.assertIn("Otherwise preserve the user's native/computer workflow", prompt)
        self.assertEqual(options["position"], "after_memory")


if __name__ == "__main__":
    unittest.main()
