"""Review-finding regressions: GUI finalization, partial accounting, receipts, wire ID.

Implements the test matrices in the PR18 revised review document, findings A-D.
All model and desktop interactions use synthetic transports and dispatchers;
no test reaches the network or a real desktop.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermes_switchyard
from hermes_switchyard import receipt_state
from hermes_switchyard.client import DecisionClient, PartialAccountingError
from hermes_switchyard.computer_use import run_computer_goal
from hermes_switchyard.routing import select_skill, select_skills


# ---------------------------------------------------------------- helpers

def _choice(criteria, selected=None, confidence=0.99):
    """A decisive bounded Choice over the exact offered criteria."""
    keys = list(criteria)
    selected = selected if selected in criteria else keys[0]
    if len(keys) == 1:
        probabilities = {keys[0]: 1.0}
    else:
        others = [key for key in keys if key != selected]
        probabilities = {key: 0.01 for key in others}
        probabilities[selected] = round(1.0 - 0.01 * len(others), 4)
    return {"choice": selected, "probabilities": probabilities, "confidence": confidence}


def _capture(*, label="Go", title="Docs", app="Chrome"):
    element = {"index": 1, "role": "Button", "label": label}
    return {"app": app, "window_title": title, "elements": [element]}


class ClickLoopClient:
    """Always chooses CLICK and the first offered target."""

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        answers = {}
        for name, question in questions.items():
            if name == "operation" or name.startswith("click_target"):
                selected = "CLICK" if name == "operation" else next(iter(question["criteria"]))
                answers[name] = _choice(question["criteria"], selected)
            elif name == "needs_skill":
                answers[name] = {"noul": 0.99}
            else:
                selected = next(iter(question["criteria"]))
                answers[name] = _choice(question["criteria"], selected)
        return {"model": "typesafe/jev-1.13", "o": _plan_marker(), "answers": answers,
                "usage": {"cost": 0.01}, "latency_ms": 2}


def _plan_marker():
    return "unused"


class CaptureSequenceDispatch:
    """Serves N successful capture calls, then raises on capture."""

    def __init__(self, good_captures, *, fail_message="capture service crashed"):
        self.remaining = good_captures
        self.fail_message = fail_message
        self.action_calls = []

    def __call__(self, tool, args):
        if args.get("action") == "capture":
            if self.remaining <= 0:
                raise RuntimeError(self.fail_message)
            self.remaining -= 1
            return _capture()
        self.action_calls.append(dict(args))
        return {"ok": True, "effect": {"confirmed": True, "status": "applied"},
                "verdict": "confirmed", "escalation": None}


# ------------------------------------------------------- A, B combined

class ComputerFinalizationTests(unittest.TestCase):
    """GUI ledger retention on expected mid-operation failures."""

    def _run(self, dispatch, *, client=None, min_actions=5, max_steps=5):
        return run_computer_goal(
            goal="click the Go button repeatedly", app="Chrome", max_steps=max_steps,
            dispatch=dispatch, client=client or ClickLoopClient(),
            public_or_sanitized_data_ack=True, min_actions_before_done=min_actions,
        )

    def test_pre_action_capture_failure_retains_prior_action(self):
        # Initial capture + fresh pre-action capture + post-action capture
        # succeed for action 1; the NEXT step's fresh pre-action capture fails.
        dispatch = CaptureSequenceDispatch(3)
        result = self._run(dispatch)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failure_phase"], "pre_action_capture")
        # The one executed, effect-confirmed action survives the boundary.
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(result["completed_action_count"], 1)
        # No second action was attempted.
        self.assertEqual(len(dispatch.action_calls), 1)
        self.assertTrue(result["reconcile_before_retry"])
        self.assertIn("not recoverable", result["evidence_note"])

    def test_deadline_failure_between_actions_retains_ledger(self):
        from hermes_switchyard.client import operation_deadline_scope

        # First two captures succeed, then the deadline expires.
        dispatch = CaptureSequenceDispatch(2)
        with operation_deadline_scope(0.05):
            result = self._run(dispatch)
        self.assertEqual(result["status"], "partial_failure")
        self.assertIn(result["failure_phase"], {
            "operation_deadline", "pre_action_capture", "post_action_capture",
        })
        self.assertGreaterEqual(len(result["actions"]), 1)
        self.assertEqual(result["completed_action_count"], len(result["actions"]))
        self.assertEqual(
            result["jev_request_count"],
            len(result["decisions"]),
        )

    def test_failed_post_action_capture_keeps_attempted_action(self):
        # Action executes, then the post-action observation fails.
        class PostCaptureDispatch(CaptureSequenceDispatch):
            def __call__(self, tool, args):
                if args.get("action") == "capture":
                    # Initial + fresh pre-action captures succeed.
                    if self.remaining <= 0:
                        raise RuntimeError(self.fail_message)
                    self.remaining -= 1
                    return _capture()
                self.action_calls.append(dict(args))
                # Simulate: effect reported, but next observation impossible.
                result = {"ok": True, "effect": {"confirmed": True, "status": "applied"},
                          "verdict": "confirmed", "escalation": None}
                self.observe_ok = False
                # Force the next capture to fail by exhausting the queue.
                self.remaining = 0
                return result

        dispatch = PostCaptureDispatch(2)
        result = self._run(dispatch)
        self.assertEqual(result["status"], "partial_failure")
        self.assertEqual(result["failure_phase"], "post_action_capture")
        self.assertEqual(len(result["actions"]), 1)
        self.assertEqual(len(dispatch.action_calls), 1)

    def test_no_actions_reraises_instead_of_empty_failure(self):
        class ImmediateFailDispatch(CaptureSequenceDispatch):
            def __call__(self, tool, args):
                if args.get("action") == "capture":
                    raise RuntimeError("capture service down")
                return super().__call__(tool, args)

        dispatch = ImmediateFailDispatch(0)
        with self.assertRaises(RuntimeError):
            self._run(dispatch)
        self.assertEqual(dispatch.action_calls, [])


# ------------------------------------------------------- B, B-part-2

class _PartialTransport:
    """First request succeeds with usage and id; second request fails."""

    def __init__(self, fail_message):
        self.calls = []
        self.fail_message = fail_message

    def __call__(self, payload):
        self.calls.append(payload)
        if len(self.calls) == 1:
            answers = {name: {"noul": 0.9} for name in payload["questions"]}
            return {"model": "typesafe/jev-1.13", "id": "wire-id-1",
                    "answers": answers, "usage": {"cost": 0.05, "total_tokens": 12},
                    "latency_ms": 7}
        raise RuntimeError(self.fail_message)


def _probe_questions(count=300):
    return {
        f"q{i}": {"type": "noul", "instructions": f"q{i}", "criteria": {"y": "y", "n": "n"}}
        for i in range(count)
    }


class PartialAccountingBoundaryTests(unittest.TestCase):
    """Wire partial accounting survives the whole chain to public output."""

    def test_public_json_preserves_partial_accounting(self):
        transport = _PartialTransport("connection reset")
        client = DecisionClient(api_key="test-key", transport=transport)
        with self.assertRaises(PartialAccountingError) as caught:
            client.decide({"x": 1}, _probe_questions(), public_or_sanitized_data_ack=True)
        public_json = hermes_switchyard._error(caught.exception)
        decoded = json.loads(public_json)
        self.assertEqual(decoded["status"], "error")
        partial = decoded["error"]["partial_accounting"]
        self.assertEqual(partial["request_count"], 1)
        self.assertEqual(partial["request_id"], "wire-id-1")
        self.assertEqual(partial["model"], "typesafe/jev-1.13")
        self.assertTrue(decoded["error"]["total_usage_incomplete"])
        self.assertEqual(partial["total_usage"]["cost"], 0.05)

    def test_accounting_not_doubled_in_single_batch_failure(self):
        transport = _PartialTransport("timeout")
        client = DecisionClient(api_key="test-key", transport=transport)
        with self.assertRaises(PartialAccountingError) as caught:
            client.decide({"x": 1}, _probe_questions(), public_or_sanitized_data_ack=True)
        decoded = json.loads(hermes_switchyard._error(caught.exception))
        self.assertEqual(decoded["error"]["partial_accounting"]["request_count"], 1)


# ------------------------------------------------------- C

class _MemoryFileHarness:
    def __init__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tmp.name, "receipt.json")

    def close(self):
        self._tmp.cleanup()


def _base_advisory():
    from hermes_switchyard.automatic import build_routing_receipt

    result = {
        "status": "abstained", "selected": None, "source": "none",
        "hosted_attempted": False, "candidate_count": 2, "cache_hit": False,
    }
    return build_routing_receipt(result)


class ReceiptContractTests(unittest.TestCase):
    """C1 unknown fields, C2 canonical persistence, C3 load evidence, cost=None."""

    def _store_read(self, receipt, harness):
        os.environ["HERMES_HOME"] = harness._tmp.name
        try:
            stored = receipt_state.store_latest_receipt(receipt)
            path = receipt_state._receipt_state_file()
            on_disk = json.loads(open(path, encoding="utf-8").read()) if stored else None
            readback = receipt_state.read_latest_receipt() if stored else None
            return stored, on_disk, readback
        finally:
            del os.environ["HERMES_HOME"]

    def test_unknown_field_rejected_even_in_advisory_record(self):
        harness = _MemoryFileHarness()
        try:
            receipt = _base_advisory()
            receipt["undeclared_extraneous"] = 1
            self.assertFalse(receipt_state.validate_receipt(receipt))
        finally:
            harness.close()

    def test_validate_does_not_mutate_and_rejects_contradictions(self):
        receipt = _base_advisory()
        receipt["verified"] = True
        self.assertFalse(receipt_state.validate_receipt(receipt))
        self.assertTrue(receipt["verified"])  # caller input not silently trusted

    def test_store_persists_canonical_record(self):
        harness = _MemoryFileHarness()
        try:
            receipt = _base_advisory()
            receipt["verified"] = True  # contradicts the contract
            stored, on_disk, readback = self._store_read(receipt, harness)
            self.assertTrue(stored)  # canonical form is persisted
            self.assertEqual(on_disk.get("verified"), False)
            self.assertEqual(readback["verified"], False)
            self.assertEqual(on_disk, readback)
        finally:
            harness.close()

    def test_loaded_receipt_requires_full_evidence_group(self):
        harness = _MemoryFileHarness()
        try:
            receipt = _base_advisory()
            receipt.update({
                "consumer_status": "loaded", "loaded_skill": None,
                "loaded_source": None, "skill_load_verified": None,
                "advisory_only": False,
            })
            self.assertFalse(receipt_state.validate_receipt(receipt))
            receipt.update({
                "loaded_skill": "docker-management", "loaded_source": "jev",
                "skill_load_verified": True, "advisory_only": False,
            })
            self.assertTrue(receipt_state.validate_receipt(receipt))
        finally:
            harness.close()

    def test_load_failed_receipt_cannot_claim_verified_load(self):
        receipt = _base_advisory()
        receipt.update({
            "consumer_status": "load_failed", "loaded_skill": "some-skill",
            "loaded_source": "jev", "skill_load_verified": True,
            "advisory_only": False,
        })
        self.assertFalse(receipt_state.validate_receipt(receipt))

    def test_unknown_cost_none_survives_round_trip(self):
        harness = _MemoryFileHarness()
        try:
            receipt = _base_advisory()
            receipt["total_usage"] = {"cost": None, "total_tokens": 5}
            stored, on_disk, readback = self._store_read(receipt, harness)
            self.assertTrue(stored)
            self.assertIsNone(on_disk["total_usage"]["cost"])
            self.assertIsNone(readback["total_usage"]["cost"])
        finally:
            harness.close()

    def test_receipt_uses_profile_data_and_migrates_legacy_once(self):
        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            legacy = Path(harness._tmp.name) / "plugins" / receipt_state.PLUGIN_NAME / "receipt.json"
            legacy.parent.mkdir(parents=True)
            receipt = _base_advisory()
            legacy.write_text(json.dumps(receipt), encoding="utf-8")

            migrated = receipt_state.read_latest_receipt()
            new_path = receipt_state._receipt_state_file()
            self.assertIsNotNone(new_path)
            assert new_path is not None
            self.assertEqual(
                new_path,
                Path(harness._tmp.name) / "plugin-data" / receipt_state.PLUGIN_NAME / "receipt.json",
            )
            self.assertEqual(migrated, receipt_state.canonicalize_receipt(receipt))
            self.assertTrue(new_path.is_file())
            self.assertTrue(legacy.is_file())
            self.assertNotEqual(new_path.parent, legacy.parent)
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()

    def test_receipt_does_not_overwrite_existing_new_state(self):
        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            new_path = receipt_state._receipt_state_file()
            self.assertIsNotNone(new_path)
            assert new_path is not None
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text('{"unrelated": true}\n', encoding="utf-8")
            legacy = Path(harness._tmp.name) / "plugins" / receipt_state.PLUGIN_NAME / "receipt.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps(_base_advisory()), encoding="utf-8")

            self.assertIsNone(receipt_state.read_latest_receipt())
            self.assertEqual(new_path.read_text(encoding="utf-8"), '{"unrelated": true}\n')
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()


# ------------------------------------------------------- D

class WireIdentifierTests(unittest.TestCase):
    """OpenRouter's documented `id` normalizes to the canonical surfaced field."""

    def _transport(self, response):
        calls = []
        def handler(payload):
            calls.append(payload)
            answers = {name: {"noul": 0.9} for name in payload["questions"]}
            return {**response, "answers": answers}
        return DecisionClient(api_key="test-key", transport=handler)

    def test_documented_id_is_preserved(self):
        client = self._transport({
            "model": "typesafe/jev-1.13", "id": "req-openrouter-1",
            "usage": {"cost": 0.01}, "latency_ms": 5,
        })
        result = client.decide(
            {"x": 1},
            {"one": {"type": "noul", "instructions": "x", "criteria": {"y": "y", "n": "n"}}},
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["request_id"], "req-openrouter-1")

    def test_canonical_request_id_field_still_works(self):
        client = self._transport({
            "model": "typesafe/jev-1.13", "request_id": "legacy-1",
            "usage": {}, "latency_ms": 5,
        })
        result = client.decide(
            {"x": 1},
            {"one": {"type": "noul", "instructions": "x", "criteria": {"y": "y", "n": "n"}}},
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(result["request_id"], "legacy-1")

    def test_missing_id_does_not_invent_one(self):
        client = self._transport({
            "model": "typesafe/jev-1.13", "usage": {}, "latency_ms": 5,
        })
        result = client.decide(
            {"x": 1},
            {"one": {"type": "noul", "instructions": "x", "criteria": {"y": "y", "n": "n"}}},
            public_or_sanitized_data_ack=True,
        )
        self.assertNotIn("request_id", result)

    def test_invalid_id_is_rejected(self):
        client = self._transport({"model": "m", "id": 400, "usage": {}, "latency_ms": 1})
        with self.assertRaises(ValueError):
            client.decide(
                {"x": 1},
                {"one": {"type": "noul", "instructions": "x", "criteria": {"y": "y", "n": "n"}}},
                public_or_sanitized_data_ack=True,
            )

    def test_conflicting_ids_are_rejected(self):
        client = self._transport({
            "model": "m", "id": "one", "request_id": "two",
            "usage": {}, "latency_ms": 1,
        })
        with self.assertRaises(ValueError):
            client.decide(
                {"x": 1},
                {"one": {"type": "noul", "instructions": "x", "criteria": {"y": "y", "n": "n"}}},
                public_or_sanitized_data_ack=True,
            )


if __name__ == "__main__":
    unittest.main()
