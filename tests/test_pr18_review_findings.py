"""Review-finding regressions: GUI finalization, partial accounting, receipts, wire ID.

Implements the test matrices in the PR18 revised review document, findings A-D.
All model and desktop interactions use synthetic transports and dispatchers;
no test reaches the network or a real desktop.
"""
from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermes_switchyard
from hermes_switchyard import receipt_state
from hermes_switchyard.client import DecisionClient, PartialAccountingError
from hermes_switchyard.computer_use import run_computer_goal
from test_support import HermesHomeTestCase


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


class ReceiptContractTests(HermesHomeTestCase):
    """C1 unknown fields, C2 canonical persistence, C3 load evidence, cost=None."""

    def _store_read(self, receipt, harness):
        os.environ["HERMES_HOME"] = harness._tmp.name
        try:
            stored = receipt_state.store_latest_receipt(receipt)
            path = receipt_state._receipt_state_file()
            if stored:
                assert path is not None
                on_disk = json.loads(path.read_text(encoding="utf-8"))
            else:
                on_disk = None
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


    def test_consumption_contract_rejects_contradictory_delivery_adoption_pairs(self):
        receipt = _base_advisory()
        receipt.update({
            "delivery_status": "skipped",
            "adoption_status": "not_adopted",
            "outcome_status": "unverified",
        })
        self.assertFalse(receipt_state.validate_receipt(receipt))
        self.assertIsNone(receipt_state.canonicalize_receipt(receipt))

        receipt = _base_advisory()
        receipt.update({
            "delivery_status": "delivered",
            "adoption_status": "suppressed",
            "outcome_status": "unverified",
        })
        self.assertFalse(receipt_state.validate_receipt(receipt))

        receipt = _base_advisory()
        receipt.update({
            "delivery_status": "not_delivered",
            "adoption_status": "adopted",
            "outcome_status": "unverified",
        })
        self.assertFalse(receipt_state.validate_receipt(receipt))

    def test_consumption_contract_accepts_exact_valid_pairs(self):
        for delivery, adoption in receipt_state.VALID_DELIVERY_ADOPTION_PAIRS:
            receipt = _base_advisory()
            receipt.update({
                "delivery_status": delivery,
                "adoption_status": adoption,
                "outcome_status": "unverified",
            })
            self.assertTrue(
                receipt_state.validate_receipt(receipt),
                msg=f"expected valid pair {(delivery, adoption)}",
            )

    def test_loaded_consumer_cannot_claim_not_adopted(self):
        receipt = _base_advisory()
        receipt.update({
            "consumer_status": "loaded",
            "loaded_skill": "docker-management",
            "loaded_source": "local",
            "skill_load_verified": True,
            "advisory_only": False,
            "delivery_status": "delivered",
            "adoption_status": "not_adopted",
            "outcome_status": "unverified",
        })
        self.assertFalse(receipt_state.validate_receipt(receipt))
        self.assertIsNone(receipt_state.canonicalize_receipt(receipt))

        receipt["adoption_status"] = "adopted"
        self.assertTrue(receipt_state.validate_receipt(receipt))

    def test_mandatory_conflict_consumer_requires_skipped_suppressed(self):
        receipt = _base_advisory()
        receipt.update({
            "consumer_status": "mandatory_conflict",
            "loaded_skill": None,
            "loaded_source": None,
            "skill_load_verified": False,
            "advisory_only": True,
            "delivery_status": "delivered",
            "adoption_status": "not_adopted",
            "outcome_status": "unverified",
        })
        self.assertFalse(receipt_state.validate_receipt(receipt))
        receipt.update({
            "delivery_status": "skipped",
            "adoption_status": "suppressed",
        })
        self.assertTrue(receipt_state.validate_receipt(receipt))

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
            self.assertFalse(legacy.exists(), "verified migration must retire the legacy artifact")
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

    def test_valid_distinct_legacy_receipt_is_not_retired(self):
        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            current = _base_advisory()
            current["candidate_count"] = 3
            self.assertIsNotNone(receipt_state.canonicalize_receipt(current))
            new_path = receipt_state._receipt_state_file()
            assert new_path is not None
            new_path.parent.mkdir(parents=True, exist_ok=True)
            new_path.write_text(json.dumps(current), encoding="utf-8")
            legacy = Path(harness._tmp.name) / "plugins" / receipt_state.PLUGIN_NAME / "receipt.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps(_base_advisory()), encoding="utf-8")

            self.assertEqual(receipt_state.read_latest_receipt(), receipt_state.canonicalize_receipt(current))
            self.assertTrue(legacy.is_file(), "an unrelated legacy receipt was not migrated")
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()

    def test_symlinked_new_receipt_cannot_retire_legacy(self):
        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            receipt = _base_advisory()
            outside = Path(harness._tmp.name) / "other-receipt.json"
            outside.write_text(json.dumps(receipt), encoding="utf-8")
            new_path = receipt_state._receipt_state_file()
            assert new_path is not None
            new_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                new_path.symlink_to(outside)
            except OSError:
                self.skipTest("symlinks are unavailable on this host")
            legacy = Path(harness._tmp.name) / "plugins" / receipt_state.PLUGIN_NAME / "receipt.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps(receipt), encoding="utf-8")

            receipt_state.read_latest_receipt()
            self.assertTrue(legacy.is_file())
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()

    def test_migration_publication_is_atomically_non_clobbering_under_race(self):
        import threading

        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            legacy = Path(harness._tmp.name) / "plugins" / receipt_state.PLUGIN_NAME / "receipt.json"
            legacy.parent.mkdir(parents=True)
            receipt = _base_advisory()
            legacy.write_text(json.dumps(receipt), encoding="utf-8")
            barrier = threading.Barrier(2)
            results = []
            lock = threading.Lock()

            def migrate():
                barrier.wait()
                value = receipt_state.read_latest_receipt()
                with lock:
                    results.append(value)

            threads = [threading.Thread(target=migrate) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            new_path = receipt_state._receipt_state_file()
            self.assertIsNotNone(new_path)
            assert new_path is not None
            canonical = receipt_state.canonicalize_receipt(receipt)
            self.assertTrue(new_path.is_file())
            self.assertEqual(json.loads(new_path.read_text(encoding="utf-8")), canonical)
            self.assertEqual(results, [canonical, canonical])
            self.assertEqual(list(new_path.parent.glob(".receipt-*.tmp")), [])
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()

    @unittest.skipUnless(os.name == "posix", "POSIX mode bits")
    def test_stored_receipt_has_private_permissions(self):
        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            self.assertTrue(receipt_state.store_latest_receipt(_base_advisory()))
            path = receipt_state._receipt_state_file()
            self.assertIsNotNone(path)
            assert path is not None
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()

    def test_receipt_store_fails_closed_when_permissions_cannot_be_enforced(self):
        # An unprotected receipt must never be published: if the kernel
        # refuses the permission restriction (chmod on POSIX or the DACL
        # on Windows), the store fails and leaves nothing behind.
        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            with mock.patch.object(
                receipt_state, "_apply_private_permissions", side_effect=OSError("denied")
            ):
                self.assertFalse(receipt_state.store_latest_receipt(_base_advisory()))
            path = receipt_state._receipt_state_file()
            self.assertIsNotNone(path)
            assert path is not None
            self.assertFalse(path.exists())
            self.assertEqual(list(path.parent.glob(".receipt-*.tmp")), [])
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()

    @unittest.skipUnless(os.name == "nt", "Windows security descriptor")
    def test_stored_receipt_carries_a_protected_private_dacl(self):
        # Windows has no POSIX mode bits. Windows privacy is the security
        # descriptor, and Hermes's own Windows permission contract grants
        # the current user full control while removing other grants. The
        # receipt must carry that contract explicitly (a protected DACL),
        # not depend on location inheritance; Hermes homes are
        # operator-configurable and the hosted matrix even uses a runner
        # temp directory.
        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            self.assertTrue(receipt_state.store_latest_receipt(_base_advisory()))
            path = receipt_state._receipt_state_file()
            self.assertIsNotNone(path)
            assert path is not None
            self._assert_private_windows_dacl(path)
            # Replacement keeps working and stays protected.
            self.assertTrue(receipt_state.store_latest_receipt(_base_advisory()))
            self._assert_private_windows_dacl(path)
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()

    @unittest.skipUnless(os.name == "nt", "Windows security descriptor")
    def test_migrated_receipt_carries_a_protected_private_dacl(self):
        # The one-way legacy migration must publish the same protected
        # file as the normal store path.
        harness = _MemoryFileHarness()
        try:
            os.environ["HERMES_HOME"] = harness._tmp.name
            legacy = Path(harness._tmp.name) / "plugins" / "hermes-switchyard" / "receipt.json"
            legacy.parent.mkdir(parents=True)
            legacy.write_text(json.dumps(_base_advisory()), encoding="utf-8")
            self.assertIsNotNone(receipt_state.read_latest_receipt())
            path = receipt_state._receipt_state_file()
            self.assertIsNotNone(path)
            assert path is not None
            self._assert_private_windows_dacl(path)
        finally:
            os.environ.pop("HERMES_HOME", None)
            harness.close()

    def _assert_private_windows_dacl(self, path):
        from hermes_switchyard import _win_acl

        owner = _win_acl.current_user_sid()
        shape = _win_acl.read_dacl(path)
        self.assertEqual(shape["owner"], owner)
        self.assertTrue(shape["dacl_present"])
        self.assertFalse(shape["dacl_defaulted"], "DACL must be explicitly applied, not inherited")
        granted = {sid for sid, _mask, ace_type in shape["aces"] if ace_type == 0}
        self.assertLessEqual(granted, {owner, "S-1-5-18", "S-1-5-32-544"})
        self.assertTrue({"S-1-5-18", "S-1-5-32-544"}.issubset(granted), granted)
        self.assertFalse(granted & {"S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-32-546"})
        for principal in ("S-1-1-0", "S-1-5-11", "S-1-5-32-545", "S-1-5-32-546"):
            self.assertEqual(_win_acl.effective_rights(path, principal), 0, principal)
        attributes = path.stat().st_file_attributes
        self.assertFalse(attributes & getattr(stat, "FILE_ATTRIBUTE_READONLY", 0x1))


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
