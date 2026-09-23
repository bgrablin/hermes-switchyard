"""Offline unit tests for the native tool-invocation CI gate.

These tests run the invocation logic directly (bypassing the real Hermes
loader, since that dependency is only guaranteed in the pinned CI venv) by
exercising the synthetic-transport helpers and the case table against a
stand-in registry entry. They pin two things a future edit could silently
break: the synthetic transport must build a valid Jev response (so the gate
never green-lights on a broken fixture instead of a broken plugin), and a
failing handler must be reported by name, not swallowed.

The handler-unit tests exercise fixtures without the loader. The CLI gate itself
loads the plugin through the pinned Hermes runtime; the invoking test suite also
covers the pure response-fixture rules and an isolated broken-handler control.
"""
from __future__ import annotations

import json
import unittest
from unittest import mock

from scripts.ci.check_native_tool_invocation import (
    _CASES,
    NativeInvocationError,
    _SyntheticConnection,
)


class _Entry:
    def __init__(self, handler):
        self.handler = handler


class SyntheticTransportTests(unittest.TestCase):
    def test_single_candidate_choice_response_is_a_valid_ballot(self):
        """A one-candidate choice question must still sum probabilities to 1.0.

        This is a regression guard for the exact bug the script's author hit
        while building it: splitting a 0.9/0.1 ballot across "the winner plus
        everyone else" silently drops the remainder when there is no one
        else, producing a response DecisionClient._validate_choice rejects.
        """
        connection = _SyntheticConnection()
        payload = {
            "model": "typesafe/jev-1.13",
            "questions": {
                "skill": {
                    "type": "choice",
                    "criteria": {"public-json-parser": "Parse a public JSON document."},
                }
            },
        }
        connection.request("POST", "/v1/decisions", body=json.dumps(payload).encode("utf-8"))
        response = json.loads(connection.getresponse().read())
        probabilities = response["answers"]["skill"]["probabilities"]
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=6)
        self.assertEqual(response["answers"]["skill"]["choice"], "public-json-parser")

    def test_multi_candidate_choice_response_is_a_valid_ballot(self):
        connection = _SyntheticConnection()
        payload = {
            "model": "typesafe/jev-1.13",
            "questions": {
                "skill": {
                    "type": "choice",
                    "criteria": {"a": "desc a", "b": "desc b", "c": "desc c"},
                }
            },
        }
        connection.request("POST", "/v1/decisions", body=json.dumps(payload).encode("utf-8"))
        response = json.loads(connection.getresponse().read())
        probabilities = response["answers"]["skill"]["probabilities"]
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=6)

    def test_noul_answer_clears_a_high_local_threshold(self):
        """The needs_skill/fit noul answer must be high enough that a

        Jev-backed selection tool actually selects instead of abstaining on
        a low synthetic score -- abstention is valid plugin behavior, but it
        must never be the reason this gate stays green.
        """
        connection = _SyntheticConnection()
        payload = {
            "model": "typesafe/jev-1.13",
            "questions": {"needs_skill": {"type": "noul", "criteria": None}},
        }
        connection.request("POST", "/v1/decisions", body=json.dumps(payload).encode("utf-8"))
        response = json.loads(connection.getresponse().read())
        self.assertGreaterEqual(response["answers"]["needs_skill"]["noul"], 0.9)


class CaseTableTests(unittest.TestCase):
    def test_every_case_requests_a_standing_ack_and_only_public_fixtures(self):
        for case in _CASES:
            with self.subTest(tool=case["tool"]):
                self.assertTrue(case["arguments"].get("public_or_sanitized_data_ack"))
                serialized = json.dumps(case["arguments"])
                for marker in ("private", "confidential", "ssn", "password", "secret"):
                    self.assertNotIn(marker, serialized.lower())

    def test_case_table_covers_every_registered_tool(self):
        expected = {
            "jev_assess",
            "jev_computer_use",
            "jev_skill_select",
            "jev_skill_select_many",
            "jev_model_route",
            "jev_model_route_approved",
            "jev_session_search_rerank",
        }
        self.assertEqual({case["tool"] for case in _CASES}, expected)

    def test_loaded_eighth_tool_is_not_hidden_by_case_table(self):
        from scripts.ci import check_native_tool_invocation as module

        handled = {case["tool"] for case in _CASES}
        loaded = handled | {"jev_new_eighth_tool"}
        with self.assertRaisesRegex(NativeInvocationError, "jev_new_eighth_tool"):
            module._validate_case_coverage(loaded, loaded, loaded, handled)

    def test_non_error_without_a_success_terminal_state_is_rejected(self):
        from scripts.ci import check_native_tool_invocation as module

        with self.assertRaises(NativeInvocationError):
            module._validate_success("jev_skill_select", {"status": "abstained"})
        with self.assertRaisesRegex(NativeInvocationError, "confirmed synthetic action"):
            module._validate_success("jev_computer_use", {"status": "completed"})
        with self.assertRaisesRegex(NativeInvocationError, "confirmed synthetic action"):
            module._validate_success("jev_computer_use", {
                "status": "completion_candidate",
                "verified": False,
                "completed_action_count": 1,
                "actions": [{"effect_confirmed": False}],
                "decisions": [{"phase": "operation_selection"}],
            })


class HandlerFailureReportingTests(unittest.TestCase):
    def test_missing_registered_handler_is_rejected(self):
        from scripts.ci import check_native_tool_invocation as module

        entries = {name: _Entry(lambda _args: "{}") for name in {case["tool"] for case in _CASES}}
        del entries["jev_computer_use"]
        with self.assertRaisesRegex(NativeInvocationError, "jev_computer_use"):
            module._validate_registered_entries(entries, {case["tool"] for case in _CASES})

    def test_broken_replacement_handler_makes_the_gate_fail(self):
        from scripts.ci import check_native_tool_invocation as module

        calls = []

        def ok_handler(args):
            calls.append(args["_tool"])
            tool = args["_tool"]
            if tool == "jev_computer_use":
                return json.dumps({
                    "status": "completion_candidate",
                    "verified": False,
                    "completed_action_count": 1,
                    "actions": [{"effect_confirmed": True}],
                    "decisions": [{"synthetic": True}],
                })
            status = "selected"
            selected_key = "selected_session_id" if tool == "jev_session_search_rerank" else "selected"
            return json.dumps({"status": status, selected_key: "synthetic"})

        entries = {
            case["tool"]: _Entry(lambda args, tool=case["tool"]: ok_handler({**args, "_tool": tool}))
            for case in _CASES
        }

        def broken_handler(_args):
            raise RuntimeError("negative-control break")

        entries["jev_skill_select"] = _Entry(broken_handler)
        with mock.patch.object(module, "_load_registered_tools", return_value=(None, entries, {case["tool"] for case in _CASES})):
            with self.assertRaisesRegex(NativeInvocationError, "jev_skill_select.*RuntimeError"):
                module.run_invocation_checks(plugin_root=None)  # type: ignore[arg-type]
        self.assertEqual(set(calls), {case["tool"] for case in _CASES} - {"jev_skill_select"})

    def test_a_handler_exception_is_reported_by_tool_name_not_swallowed(self):
        from scripts.ci import check_native_tool_invocation as module

        def broken_handler(_args):
            raise RuntimeError("synthetic break")

        entries = {case["tool"]: _Entry(broken_handler) for case in _CASES}
        with mock.patch.object(
            module, "_load_registered_tools", return_value=(None, entries, {case["tool"] for case in _CASES})
        ), mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "x", "TYPESAFE_API_KEY": ""}):
            with self.assertRaises(NativeInvocationError) as ctx:
                module.run_invocation_checks(plugin_root=None)  # type: ignore[arg-type]
        self.assertIn(_CASES[0]["tool"], str(ctx.exception))
        self.assertIn("RuntimeError", str(ctx.exception))

    def test_unexpected_native_dispatch_never_reaches_host_registry(self):
        from scripts.ci import check_native_tool_invocation as module
        from tools.registry import registry

        calls = []

        def host_dispatch_trap(*args, **kwargs):
            calls.append(args)
            raise AssertionError("unsafe host dispatch")

        def unexpected_handler(_args):
            registry.dispatch("shell", {"command": "synthetic-should-not-run"})
            return json.dumps({"status": "selected", "selected": "synthetic"})

        entries = {case["tool"]: _Entry(unexpected_handler) for case in _CASES}
        with mock.patch.object(module, "_load_registered_tools", return_value=(None, entries, set(entries))):
            with mock.patch.object(registry, "dispatch", side_effect=host_dispatch_trap):
                with self.assertRaisesRegex(NativeInvocationError, "jev_skill_select.*AssertionError"):
                    module.run_invocation_checks(plugin_root=None)  # type: ignore[arg-type]
        self.assertEqual(calls, [])

    def test_a_structured_error_response_fails_the_gate_with_its_reason(self):
        from scripts.ci import check_native_tool_invocation as module

        def erroring_handler(_args):
            return json.dumps({"status": "error", "error": {"code": "invalid_request", "reason": "x"}})

        entries = {case["tool"]: _Entry(erroring_handler) for case in _CASES}
        with mock.patch.object(
            module, "_load_registered_tools", return_value=(None, entries, {case["tool"] for case in _CASES})
        ), mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "x", "TYPESAFE_API_KEY": ""}):
            with self.assertRaises(NativeInvocationError) as ctx:
                module.run_invocation_checks(plugin_root=None)  # type: ignore[arg-type]
        self.assertIn(_CASES[0]["tool"], str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
