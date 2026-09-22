"""Offline unit tests for the native tool-invocation CI gate.

These tests run the invocation logic directly (bypassing the real Hermes
loader, since that dependency is only guaranteed in the pinned CI venv) by
exercising the synthetic-transport helpers and the case table against a
stand-in registry entry. They pin two things a future edit could silently
break: the synthetic transport must build a valid Jev response (so the gate
never green-lights on a broken fixture instead of a broken plugin), and a
failing handler must be reported by name, not swallowed.

The end-to-end path (real Hermes loader + real registry + real handlers) is
proven interactively against a pinned Hermes checkout as part of review, the
same way ``check_native_hermes.py`` itself is -- both scripts import
Hermes-only modules that are not installed in this repository's own test
environment.
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

    def test_case_table_covers_every_jev_backed_tool_exercised_by_the_live_contract(self):
        # These are exactly the tools live_jev_contract.py exercises with a
        # real paid call; this offline gate should not silently narrow to
        # fewer tools than the live contract already covers for free.
        covered = {case["tool"] for case in _CASES}
        self.assertTrue({"jev_skill_select", "jev_model_route"}.issubset(covered))


class HandlerFailureReportingTests(unittest.TestCase):
    def test_a_handler_exception_is_reported_by_tool_name_not_swallowed(self):
        from scripts.ci import check_native_tool_invocation as module

        def broken_handler(_args):
            raise RuntimeError("synthetic break")

        entries = {case["tool"]: _Entry(broken_handler) for case in _CASES}
        with mock.patch.object(
            module, "_load_registered_tools", return_value=(None, entries)
        ), mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "x", "TYPESAFE_API_KEY": ""}):
            with self.assertRaises(NativeInvocationError) as ctx:
                module.run_invocation_checks(plugin_root=None)  # type: ignore[arg-type]
        self.assertIn(_CASES[0]["tool"], str(ctx.exception))
        self.assertIn("RuntimeError", str(ctx.exception))

    def test_a_structured_error_response_fails_the_gate_with_its_reason(self):
        from scripts.ci import check_native_tool_invocation as module

        def erroring_handler(_args):
            return json.dumps({"status": "error", "error": {"code": "invalid_request", "reason": "x"}})

        entries = {case["tool"]: _Entry(erroring_handler) for case in _CASES}
        with mock.patch.object(
            module, "_load_registered_tools", return_value=(None, entries)
        ), mock.patch.dict("os.environ", {"OPENROUTER_API_KEY": "x", "TYPESAFE_API_KEY": ""}):
            with self.assertRaises(NativeInvocationError) as ctx:
                module.run_invocation_checks(plugin_root=None)  # type: ignore[arg-type]
        self.assertIn(_CASES[0]["tool"], str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
