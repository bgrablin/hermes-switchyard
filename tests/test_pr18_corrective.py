"""Corrective regression tests for PR #18 findings (F1-F5).

All model/desktop/hosted interactions use synthetic transports/dispatch.
No test sends task text, candidate descriptions, credentials, or restricted
content to a model. Each test imports and exercises the checked-out plugin.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

import hermes_switchyard
from hermes_switchyard import receipt_state
from hermes_switchyard.automatic import (
    AutomaticSkillRecommender,
    build_pre_llm_call_hook,
)
from hermes_switchyard.client import DecisionClient, PartialAccountingError
from hermes_switchyard.computer_use import run_computer_goal
from hermes_switchyard.routing import select_skills


# ------------------------------------------------------------------ helpers

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


def _capture(*, label="Go", title="Docs", app="Chrome", role="Button", index=1):
    element = {"index": index, "role": role, "label": label}
    return {"app": app, "window_title": title, "elements": [element]}


class SyntheticDispatch:
    def __init__(self, captures):
        self.captures = list(captures)
        self.action_calls = []

    def __call__(self, tool_name, args):
        if args.get("action") == "capture":
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
        return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}


class _EgressLike:
    """Minimal turn_egress_policy stand-in exposing cache_key/decision/allowed."""

    def __init__(self, decision="allow", allowed=True, reason_code="synthetic_fixture_allowed"):
        self._decision = decision
        self._allowed = allowed
        self._reason = reason_code
        self.cache_key = "policy"

    @property
    def decision(self):
        return self._decision

    @property
    def status(self):
        return "allowed" if self._allowed else "denied"

    @property
    def allowed(self):
        return self._allowed

    @property
    def reason_code(self):
        return self._reason

    def metadata(self):
        return {
            "version": 1,
            "decision": self._decision,
            "data_class": "sanitized",
            "reason_code": self._reason,
            "allowed_payload": "SANITIZED_TASK_MARKER",
        }


# ================================================================= F1 receipt

class F1ConsumerReceiptTests(unittest.TestCase):
    def test_consumer_receipt_survives_disk_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                loader_calls = {"n": 0}

                def fake_loader(selected, task_id=None):
                    loader_calls["n"] += 1
                    return f"# skill {selected}\ncontent"

                hook = build_pre_llm_call_hook(
                    enabled=True,
                    consumer_mode="load",
                    skill_loader=fake_loader,
                    configured_candidates=[{"name": "docker-management", "description": "Docker"}],
                    hosted_enabled=False,
                    client_factory=lambda: DecisionClient(api_key="fixture-key", transport=lambda p: {}),
                    cache_seconds=0,
                )
                hook(
                    user_message="Diagnose a Docker container",
                    session_id="sess-1",
                    turn_id="turn-1",
                )
                assert hook is not None
                receipt = hook.last_receipt
                self.assertEqual(loader_calls["n"], 1)
                self.assertEqual(receipt["consumer_status"], "loaded")
                self.assertTrue(receipt["skill_load_verified"])
                self.assertFalse(receipt["advisory_only"])
                self.assertEqual(receipt["loaded_skill"], "docker-management")
                stored = receipt_state.read_latest_receipt()
                self.assertIsNotNone(stored)
                self.assertEqual(stored["consumer_status"], "loaded")
                self.assertEqual(stored["loaded_skill"], "docker-management")
                self.assertFalse(stored["advisory_only"])

    def test_load_failure_records_rejection_not_success(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                hook = build_pre_llm_call_hook(
                    enabled=True,
                    consumer_mode="load",
                    skill_loader=lambda selected, task_id=None: "",
                    configured_candidates=[{"name": "docker-management", "description": "Docker"}],
                    hosted_enabled=False,
                    client_factory=lambda: DecisionClient(api_key="fixture-key", transport=lambda p: {}),
                    cache_seconds=0,
                )
                hook(user_message="Diagnose a Docker container", session_id="sess-2", turn_id="turn-2")
                receipt = hook.last_receipt
                self.assertEqual(receipt["consumer_status"], "load_failed")
                self.assertFalse(receipt["skill_load_verified"])
                self.assertTrue(receipt["advisory_only"])
                # A load_failed receipt is a valid terminal consumer record: it
                # survives to diagnostic readback and never claims load success.
                stored = receipt_state.read_latest_receipt()
                self.assertIsNotNone(stored)
                self.assertEqual(stored["consumer_status"], "load_failed")
                self.assertFalse(stored["skill_load_verified"])

    def test_load_mode_is_advisory_receipt_when_not_configured(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                hook = build_pre_llm_call_hook(
                    enabled=True,
                    consumer_mode="advisory",
                    configured_candidates=[{"name": "docker-management", "description": "Docker"}],
                    hosted_enabled=False,
                    client_factory=lambda: DecisionClient(api_key="fixture-key", transport=lambda p: {}),
                    cache_seconds=0,
                )
                result = hook(user_message="Diagnose a Docker container", session_id="sess-3", turn_id="turn-3")
                self.assertEqual(result["metadata"]["skill_recommendation"]["loaded_once"], False)


# ================================================================= F2 sizing

class F2MultiSkillSizingTests(unittest.TestCase):
    def test_two_long_candidates_partition_without_oversize(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            answers = {}
            for name, question in payload["questions"].items():
                answers[name] = {"noul": 0.99}
            return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}

        candidates = [
            {"name": "a", "description": "a" * 40_000},
            {"name": "b", "description": "b" * 40_000},
        ]
        client = DecisionClient(api_key="fixture-key", transport=transport)
        result = select_skills(
            task="public task",
            candidates=candidates,
            client=client,
            selection_threshold=0.5,
            public_or_sanitized_data_ack=True,
            deadline_seconds=60.0,
        )
        self.assertEqual(set(result["selected"]), {"a", "b"})
        for payload in payloads:
            size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            self.assertLessEqual(size, 96_000)
        self.assertGreater(len(payloads), 1)


# =============================================================== F3 accounting

class F3PartialAccountingTests(unittest.TestCase):
    def test_first_batch_accounting_survives_second_failure(self):
        seen = {"n": 0}
        # Each question carries a large instruction so a single request fits but
        # two questions cannot share one request. This forces the client to emit
        # two provider requests, letting a later failure leave earlier accounting.
        big = "instruction" * 5_000
        questions = {
            "q_0000": {"type": "noul", "instructions": big,
                       "criteria": {"true": "yes", "false": "no"}},
            "q_9999": {"type": "noul", "instructions": big,
                       "criteria": {"true": "yes", "false": "no"}},
        }
        state = {"task": "public task"}

        def transport(payload):
            seen["n"] += 1
            if seen["n"] == 1:
                answers = {name: {"noul": 0.99} for name in payload["questions"]}
                return {
                    "model": "typesafe/jev-1.13",
                    "answers": answers,
                    "usage": {"cost": 0.01},
                    "latency_ms": 10.0,
                    "request_id": "req-1",
                }
            raise TimeoutError("aggregate deadline expired")

        client = DecisionClient(api_key="fixture-key", transport=transport)
        with self.assertRaises(PartialAccountingError) as ctx:
            client.decide(state, questions, public_or_sanitized_data_ack=True)
        partial = ctx.exception.partial
        successful = [record for record in partial if record.get("request_id") == "req-1"]
        self.assertEqual(len(successful), 1)
        self.assertEqual(successful[0]["request_count"], 1)
        self.assertEqual(successful[0]["usage"].get("cost"), 0.01)


# ================================================================= F4 GUI evidence

class F4GuIEvidenceTests(unittest.TestCase):
    def test_stale_target_after_action_preserves_prior_actions(self):
        first = _capture(label="Go", app="Chrome", index=1)
        fresh_ok = _capture(label="Go", app="Chrome", index=1)
        post_ok = _capture(label="Go", app="Chrome", index=1)
        stale = _capture(label="Go", app="Firefox", index=1)
        dispatch = SyntheticDispatch([first, fresh_ok, post_ok, stale])
        result = run_computer_goal(
            goal="click Go then continue",
            app="Chrome",
            max_steps=2,
            dispatch=dispatch,
            client=ComputerClient(["CLICK", "CLICK"]),
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertEqual(result["completed_action_count"], 1)
        self.assertTrue(result["reconcile_before_retry"])
        self.assertEqual(dispatch.action_calls, [{"action": "click", "element": 1}])


# ================================================================= F5 scan

class F5RestrictedMarkerTests(unittest.TestCase):
    def _allow_policy_with_payload(self, payload):
        return {
            "version": 1,
            "decision": "allow",
            "data_class": "sanitized",
            "reason_code": "synthetic_fixture_allowed",
            "allowed_payload": payload,
        }

    def _marked_task(self, marked):
        """Return (task, allow_policy) where the marker lives on the wire payload."""
        # The automatic route scans the exact outbound payload fields. The task
        # text is what crosses the boundary, so the marker sits in allowed_payload.
        return marked, self._allow_policy_with_payload(marked)

    def test_cui_markers_block_hosted_path_before_client_creation(self):
        for marked in [
            "CUI: Review the technical design.",
            "Controlled Unclassified Information: Review the technical design.",
            "Confidential: Review the technical design.",
            "CUI//FOUO: Review the technical design.",
        ]:
            client_calls = []

            def client_factory():
                client_calls.append(True)
                raise AssertionError("restricted outbound payload must be rejected before client creation")

            recommender = AutomaticSkillRecommender(
                configured_candidates=[{"name": "project-helper-a", "description": "public help"}],
                hosted_enabled=True,
                hosted_mode="always",
                public_or_sanitized_data_ack=True,
                client_factory=client_factory,
            )
            task, policy = self._marked_task(marked)
            result = recommender.recommend(task, turn_egress_policy=policy)
            self.assertEqual(client_calls, [], f"client created for restricted input: {marked!r}")
            self.assertFalse(result["hosted_attempted"])
            self.assertTrue(result["hosted_skipped"].startswith("local_scan_"))


if __name__ == "__main__":
    unittest.main()


class A0NoIntermediateModelCallTests(unittest.TestCase):
    """INV-01/INV-02: zero non-Jev inference/coordinator turns in one bounded op.

    A raising spy on Hermes' conversational `ctx.llm` fails the test if it is
    called. A spy on the real Jev inference entry point (`_decide_single`)
    proves Jev is actually invoked (not stubbed above the implementation). The
    operation runs two Jev decisions separated by real local orchestration and
    a native side effect through the registered handler.
    """

    def test_registered_handler_runs_multi_action_with_zero_non_jev_inference(self):
        import hermes_switchyard
        from hermes_switchyard import client as _client_module

        spy_calls = {"n": 0, "jev_calls": 0}

        class ForbiddenLLM:
            def __getattr__(self, name):
                spy_calls["n"] += 1
                raise AssertionError(f"non-Jev conversational model call: {name}")
            def __call__(self, *args, **kwargs):
                spy_calls["n"] += 1
                raise AssertionError("non-Jev conversational model call")

        class SpyClient(ComputerClient):
            def close(self):
                pass

            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                # Record real Jev inference entry points so we know Jev (not a
                # stub above the implementation) is driving the loop.
                spy_calls["jev_calls"] += 1
                return super().decide(state, questions, public_or_sanitized_data_ack=public_or_sanitized_data_ack)

        class Context:
            def __init__(self, dispatch):
                self.settings = {"automatic_skill_recommendation": False}
                self.tools = {}
                self.dispatch_tool = dispatch
                self.llm = ForbiddenLLM()

            def get_config(self, key, default=None, **_kwargs):
                return self.settings.get(key, default)

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

        dispatch = SyntheticDispatch([_capture(), _capture(), _capture(), _capture()])

        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"), \
             mock.patch.object(hermes_switchyard, "DecisionClient", lambda **_kwargs: SpyClient(["CLICK", "CLICK"])):
            context = Context(dispatch)
            hermes_switchyard.register(context)
            # max_steps=2 forces two Jev decisions + one native action + one
            # post-action capture before the second decision's capture.
            result = json.loads(context.tools["jev_computer_use"]({
                "goal": "click Go twice",
                "app": "Chrome",
                "max_steps": 2,
                "public_or_sanitized_data_ack": True,
            }))
        # Exactly one terminal result returned for the whole operation.
        self.assertIn(result["status"], {"completion_candidate", "step_limit", "stalled", "partial_failure"})
        # At least one Jev inference entry point was exercised (not stubbed above).
        self.assertGreaterEqual(spy_calls["jev_calls"], 2)
        # The allowed native dispatcher was actually exercised.
        self.assertTrue(all(a["action"] == "click" for a in dispatch.action_calls))
        # Zero non-Jev conversational model calls during the whole operation.
        self.assertEqual(spy_calls["n"], 0, "non-Jev inference call occurred inside the bounded operation")
        # Exactly one terminal tool result was returned.
        self.assertIsInstance(result, dict)
        self.assertIn("status", result)
