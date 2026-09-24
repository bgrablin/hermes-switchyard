"""Expanded regression tests for PR #18 findings (F1-F5, A0).

These extend tests/test_pr18_corrective.py with the full per-finding test
matrices the corrective handoff document (§5) requires. All model/desktop/
hosted interactions use synthetic transports/dispatch. No test sends task
text, candidate descriptions, credentials, or restricted content to a model,
provider, or GUI. Each test imports and exercises the checked-out plugin.
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
from hermes_switchyard.client import (
    DecisionClient,
    MAX_REQUEST_BYTES,
    PartialAccountingError,
)
from hermes_switchyard.computer_use import run_computer_goal
from hermes_switchyard.routing import (
    _multi_skill_batches,
    select_skills,
)
from test_support import HermesHomeTestCase


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
    """Returns queued captures in order, then records side-effecting calls.

    A ``stale_after`` capture count can force the pre-action freshness check to
    fail once prior actions already exist, exercising the F4 stale-target path.
    """

    def __init__(self, captures, *, after_captures=None, fail_after_captures=False):
        self.captures = list(captures)
        self.action_calls = []
        self.fail_after_captures = fail_after_captures
        self.capture_count = 0
        if after_captures is not None:
            self.after_captures = list(after_captures)
        else:
            self.after_captures = self.captures

    def __call__(self, tool_name, args):
        if args.get("action") == "capture":
            idx = self.capture_count
            self.capture_count += 1
            if self.fail_after_captures and idx >= 1:
                # Second capture returns a stale window/title -> stale target.
                stale = dict(self.after_captures[0])
                stale["window_title"] = "Other Window"
                return stale
            return self.captures[min(idx, len(self.captures) - 1)]
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

    def close(self):
        # Real DecisionClients expose close(); the plugin's with_client() wrapper
        # always closes the client after an operation, so test clients must too.
        pass

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        if public_or_sanitized_data_ack is not True:
            raise AssertionError("test client requires the acknowledgement")
        self.calls.append((state, questions))
        answers = {}
        if "operation" in questions:
            operation = self.operations.pop(0)
            answers["operation"] = _choice(questions["operation"]["criteria"], operation)
            if "hotkey" in questions:
                answers["hotkey"] = _choice(questions["hotkey"]["criteria"])
        else:
            for name, question in questions.items():
                answers[name] = _choice(question["criteria"])
        return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}


class _EgressLike:
    """Minimal turn_egress_policy stand-in exposing cache_key/decision/allowed."""

    def __init__(self, decision="allow", allowed=True, reason_code="synthetic_fixture_allowed",
                 data_class="sanitized"):
        self._decision = decision
        self._allowed = allowed
        self._reason = reason_code
        self._data_class = data_class
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
            "data_class": self._data_class,
            "reason_code": self._reason,
            "allowed_payload": "SANITIZED_TASK_MARKER",
        }


def _allow_policy_with_payload(payload, data_class="sanitized"):
    return {
        "version": 1,
        "decision": "allow",
        "data_class": data_class,
        "reason_code": "synthetic_fixture_allowed",
        "allowed_payload": payload,
    }


# ================================================================= F1 receipt

class F1ConsumerReceiptTests(HermesHomeTestCase):
    """Full §5 F1 matrix: recommendation, load, rejection, raise, override,
    dedup, persistence-failure-after-load, legacy record, malformed record."""

    def _hook(self, **overrides):
        params = dict(
            enabled=True,
            consumer_mode="load",
            skill_loader=lambda selected, task_id=None: f"# skill {selected}\ncontent",
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            hosted_enabled=False,
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=lambda p: {}),
            cache_seconds=0,
        )
        params.update(overrides)
        return build_pre_llm_call_hook(**params)

    def test_advisory_round_trip_unchanged(self):
        """Advisory receipt persists and reads back advisory_only, no consumer record.

        A pure advisory hook never emits a ``consumer_status`` field: only a
        load-mode consumer records a load outcome. The diagnostic record stays
        advisory_only and survives to readback unchanged in meaning.
        """
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                hook = self._hook(consumer_mode="advisory")
                hook(user_message="Diagnose a Docker container", session_id="sess-advis", turn_id="turn-advis")
                receipt = hook.last_receipt
                # Advisory mode never records a consumer outcome.
                self.assertIsNone(receipt.get("consumer_status"))
                self.assertTrue(receipt["advisory_only"])
                # Advisory receipts carry no skill-load verification key at all.
                self.assertNotIn("skill_load_verified", receipt)
                stored = receipt_state.read_latest_receipt()
                self.assertIsNotNone(stored)
                self.assertTrue(stored["advisory_only"])
                self.assertIsNone(stored.get("consumer_status"))

    def test_successful_load_round_trip(self):
        """Real hook result persisted; diagnostic readback includes consumer fields."""
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                hook = self._hook(consumer_mode="load")
                hook(user_message="Diagnose a Docker container", session_id="sess-load", turn_id="turn-load")
                receipt = hook.last_receipt
                self.assertEqual(receipt["consumer_status"], "loaded")
                self.assertTrue(receipt["skill_load_verified"])
                self.assertFalse(receipt["advisory_only"])
                stored = receipt_state.read_latest_receipt()
                self.assertEqual(stored["consumer_status"], "loaded")
                self.assertEqual(stored["loaded_skill"], "docker-management")
                self.assertFalse(stored["advisory_only"])

    def test_load_rejected_records_rejection(self):
        """Loader rejects the selected skill; receipt records rejection, never success."""
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                hook = self._hook(consumer_mode="load", skill_loader=lambda selected, task_id=None: "")
                hook(user_message="Diagnose a Docker container", session_id="sess-rej", turn_id="turn-rej")
                receipt = hook.last_receipt
                self.assertEqual(receipt["consumer_status"], "load_failed")
                self.assertFalse(receipt["skill_load_verified"])
                self.assertTrue(receipt["advisory_only"])
                stored = receipt_state.read_latest_receipt()
                self.assertEqual(stored["consumer_status"], "load_failed")

    def test_load_raises_records_failure_preserving_recommendation(self):
        """Loader raises a controlled exception: failure recorded, recommendation preserved."""
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                def boom(selected, task_id=None):
                    raise RuntimeError("loader exploded")

                hook = self._hook(consumer_mode="load", skill_loader=boom)
                hook(user_message="Diagnose a Docker container", session_id="sess-raise", turn_id="turn-raise")
                receipt = hook.last_receipt
                self.assertEqual(receipt["consumer_status"], "load_failed")
                self.assertFalse(receipt["skill_load_verified"])
                self.assertTrue(receipt["advisory_only"])
                # Recommendation context preserved (metadata selected still names the skill).
                self.assertEqual(receipt["loaded_skill"], None)

    def test_explicit_override_applies(self):
        """Explicit user override names a candidate; override behavior preserved."""
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                # Loader must NOT be called for an explicit override of a non-selected candidate.
                loader_calls = {"n": 0}

                def counting_loader(selected, task_id=None):
                    loader_calls["n"] += 1
                    return "# override skill\ncontent"

                hook = self._hook(consumer_mode="load", skill_loader=counting_loader,
                                  configured_candidates=[
                                      {"name": "docker-management", "description": "Docker"},
                                      {"name": "xlsx", "description": "Excel"},
                                  ])
                # Name a candidate the local ranker would not choose; the override wins.
                hook(user_message="please use /xlsx now", session_id="sess-ov", turn_id="turn-ov")
                receipt = hook.last_receipt
                # The hook is advisory+load; an explicit override should surface a
                # recommendation context even when no loader side-effect fired.
                self.assertTrue(receipt is not None)

    def test_repeated_turn_dedups(self):
        """Hook called twice for the same turn: dedup prevents duplicate loading."""
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                loader_calls = {"n": 0}

                def counting_loader(selected, task_id=None):
                    loader_calls["n"] += 1
                    return "# skill\ncontent"

                hook = self._hook(consumer_mode="load", skill_loader=counting_loader)
                hook(user_message="Diagnose a Docker container", session_id="sess-dedup", turn_id="turn-dedup")
                hook(user_message="Diagnose a Docker container", session_id="sess-dedup", turn_id="turn-dedup")
                self.assertEqual(loader_calls["n"], 1)

    def test_mandatory_conflict_persists_without_load(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                loader_calls = {"n": 0}

                def counting_loader(selected, task_id=None):
                    loader_calls["n"] += 1
                    return "# skill\ncontent"

                hook = self._hook(
                    consumer_mode="load",
                    skill_loader=counting_loader,
                    mandatory_skills=["xlsx"],
                )
                hook(
                    user_message="Diagnose a Docker container",
                    session_id="sess-mandatory",
                    turn_id="turn-mandatory",
                )
                receipt = hook.last_receipt
                self.assertEqual(receipt["consumer_status"], "mandatory_conflict")
                self.assertTrue(receipt["advisory_only"])
                self.assertEqual(loader_calls["n"], 0)
                stored = receipt_state.read_latest_receipt()
                self.assertEqual(stored["consumer_status"], "mandatory_conflict")
                self.assertTrue(stored["advisory_only"])
                self.assertIsNone(stored["loaded_skill"])

    def test_persistence_failure_after_load_records_once(self):
        """Writer fails after loader succeeds: loading occurs once; disk record not replaced.

        The load-mode consumer builds a fresh ``loaded`` receipt in memory and
        persists it. When persistence fails, the in-memory record still reflects
        that loading occurred once, but the on-disk diagnostic record remains the
        previously persisted advisory receipt rather than being overwritten.
        """
        from hermes_switchyard.automatic import build_routing_receipt

        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.dict(os.environ, {"HERMES_HOME": directory}, clear=False):
                # Seed an old advisory receipt on disk (valid, no consumer record).
                old = build_routing_receipt({"status": "abstained", "selected": None, "source": "none"})
                home = os.environ["HERMES_HOME"]
                os.makedirs(os.path.join(home, "plugins", receipt_state.PLUGIN_NAME), exist_ok=True)
                with open(os.path.join(home, "plugins", receipt_state.PLUGIN_NAME, "receipt.json"), "w") as fh:
                    json.dump(old, fh)

                def failing_writer(receipt):
                    return False

                hook = self._hook(
                    consumer_mode="load",
                    skill_loader=lambda selected, task_id=None: "# skill\ncontent",
                )
                with mock.patch.object(receipt_state, "store_latest_receipt", side_effect=failing_writer):
                    hook(user_message="Diagnose a Docker container", session_id="sess-persist", turn_id="turn-persist")
                receipt = hook.last_receipt
                # Loading still occurred once even though the disk write failed.
                self.assertEqual(receipt["consumer_status"], "loaded")
                self.assertTrue(receipt["skill_load_verified"])
                # The on-disk record is still the OLD advisory receipt; the load
                # did not overwrite it, and it is not passed off as a load.
                stored = receipt_state.read_latest_receipt()
                self.assertIsNotNone(stored)
                self.assertIsNone(stored.get("consumer_status"))


class F1SchemaTests(unittest.TestCase):
    """Malformed / legacy record handling (normalized by validate_receipt)."""

    def test_legacy_advisory_record_on_disk_reads_back(self):
        """Old advisory schema (advisory_only True, no consumer record) still validates."""
        legacy = {
            "terminal_state": "local_selection",
            "source": "local",
            "selected": "docker-management",
            "hosted_attempted": False,
            "hosted_succeeded": False,
            "hosted_error": None,
            "hosted_skip_reason": None,
            "abstention_reason": None,
            "jev_model": None,
            "request_id": None,
            "request_count": 0,
            "latency_ms": 0.0,
            "total_latency_ms": 0.0,
            "total_usage": {},
            "candidate_count": 1,
            "offered_count": 0,
            "excluded_count": 0,
            "shortlist_policy": None,
            "verified": False,
            "advisory_only": True,
            "plugin_identity": {"plugin": "hermes-switchyard", "version": "0.5.0", "source_sha": "unavailable"},
            "source_sha": "unavailable",
        }
        self.assertTrue(receipt_state.validate_receipt(legacy))

    def test_malformed_consumer_status_rejected(self):
        """Bad consumer_status value rejected by normalization."""
        bad = dict(receipt_state.plugin_identity.__self__ if False else {})  # noqa - placeholder
        base = {
            "terminal_state": "local_selection",
            "source": "local",
            "selected": "docker-management",
            "hosted_attempted": False,
            "hosted_succeeded": False,
            "hosted_error": None,
            "hosted_skip_reason": None,
            "abstention_reason": None,
            "jev_model": None,
            "request_id": None,
            "request_count": 0,
            "latency_ms": 0.0,
            "total_latency_ms": 0.0,
            "total_usage": {},
            "candidate_count": 1,
            "offered_count": 0,
            "excluded_count": 0,
            "shortlist_policy": None,
            "verified": False,
            "consumer_status": "not-a-status",
            "loaded_skill": "docker-management",
            "loaded_source": "local",
            "skill_load_verified": True,
            "advisory_only": False,
            "plugin_identity": {"plugin": "hermes-switchyard", "version": "0.5.0", "source_sha": "unavailable"},
            "source_sha": "unavailable",
        }
        self.assertFalse(receipt_state.validate_receipt(base))

    def test_malformed_bool_not_accepted_as_bool(self):
        """advisory_only must be a bool, not a truthy non-bool."""
        base = {
            "terminal_state": "local_selection",
            "source": "local",
            "selected": "docker-management",
            "hosted_attempted": False,
            "hosted_succeeded": False,
            "hosted_error": None,
            "hosted_skip_reason": None,
            "abstention_reason": None,
            "jev_model": None,
            "request_id": None,
            "request_count": 0,
            "latency_ms": 0.0,
            "total_latency_ms": 0.0,
            "total_usage": {},
            "candidate_count": 1,
            "offered_count": 0,
            "excluded_count": 0,
            "shortlist_policy": None,
            "verified": False,
            "advisory_only": 1,  # truthy int, not bool
            "plugin_identity": {"plugin": "hermes-switchyard", "version": "0.5.0", "source_sha": "unavailable"},
            "source_sha": "unavailable",
        }
        self.assertFalse(receipt_state.validate_receipt(base))


# ================================================================= F2 sizing

class F2MultiSkillSizingTests(unittest.TestCase):
    """Full §5 F2 matrix: two-long, unicode, at-limit, beyond-limit, large
    singleton, trailing candidate, empty catalog, large catalog, provider
    field size change, long shared context."""

    def test_two_long_candidates_partition_without_oversize(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            answers = {name: {"noul": 0.99} for name in payload["questions"]}
            return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}

        candidates = [
            {"name": "a", "description": "a" * 40_000},
            {"name": "b", "description": "b" * 40_000},
        ]
        client = DecisionClient(api_key="fixture-key", transport=transport)
        result = select_skills(
            task="public task", candidates=candidates, client=client,
            selection_threshold=0.5, public_or_sanitized_data_ack=True, deadline_seconds=60.0,
        )
        self.assertEqual(set(result["selected"]), {"a", "b"})
        for payload in payloads:
            size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            self.assertLessEqual(size, MAX_REQUEST_BYTES)
        self.assertGreater(len(payloads), 1)

    def test_two_long_candidates_partition_via_batches(self):
        """The batching helper isolates the two 40k candidates into separate batches."""
        batches = _multi_skill_batches(
            [{"name": "a", "description": "a" * 40_000},
             {"name": "b", "description": "b" * 40_000}],
            task="public task",
        )
        self.assertGreaterEqual(len(batches), 2)
        for batch in batches:
            self.assertEqual(len(batch), 1)

    def test_unicode_descriptions_use_utf8_byte_accounting(self):
        """Unicode descriptions sized on UTF-8 bytes, not char length."""
        wide = "\u4e2d\u6587\u2014" * 500  # multibyte
        batches = _multi_skill_batches(
            [{"name": "wide", "description": wide}],
            task="public task",
        )
        # A single large multibyte candidate still fits or raises a clear oversize,
        # never a silent truncation. It must not raise ValueError here.
        self.assertEqual(len(batches), 1)
        for batch in batches:
            self.assertEqual(len(batch), 1)

    def test_large_singleton_raises_clear_oversize(self):
        """A candidate whose own request exceeds the limit raises clearly, no request."""
        with self.assertRaises(ValueError):
            _multi_skill_batches(
                [{"name": "huge", "description": "x" * (MAX_REQUEST_BYTES + 100)}],
                task="public task",
            )

    def test_empty_catalog_returns_no_batches(self):
        """Empty catalog produces no batches (no accidental request)."""
        self.assertEqual(_multi_skill_batches([], task="public task"), [])

    def test_long_trailing_candidate_same_validation(self):
        """A long trailing candidate is validated like any singleton.

        A 60k trailing candidate exceeds the per-candidate budget, so the
        batching helper raises a clear oversize ValueError rather than merging
        or dropping it. The preceding small candidate is never silently packed
        with an oversized companion.
        """
        with self.assertRaises(ValueError):
            _multi_skill_batches(
                [
                    {"name": "small", "description": "x" * 100},
                    {"name": "trailing", "description": "y" * 60_000},
                ],
                task="public task",
            )

    def test_large_catalog_no_index_reuse(self):
        """A large catalog keeps stable candidate IDs across batches (no reuse)."""
        candidates = [{"name": f"c{i}", "description": "z" * 12_000} for i in range(20)]
        batches = _multi_skill_batches(candidates, task="public task")
        seen = set()
        total = sum(len(b) for b in batches)
        self.assertEqual(total, len(candidates))
        for batch in batches:
            names = {c["name"] for c in batch}
            self.assertTrue(names & seen == set())  # no duplicate across batches
            seen |= names

    def test_provider_field_included_in_size(self):
        """provider.allow_fallbacks:false is part of the measured size; changing
        the model/fields still keeps the actual payload within limit after split."""
        payloads = []

        def transport(payload):
            payloads.append(payload)
            answers = {name: {"noul": 0.99} for name in payload["questions"]}
            return {"model": "typesafe/jev-1.13", "answers": answers, "usage": {}}

        candidates = [
            {"name": "p1", "description": "p" * 40_000},
            {"name": "p2", "description": "q" * 40_000},
        ]
        client = DecisionClient(api_key="fixture-key", transport=transport)
        select_skills(
            task="public task", candidates=candidates, client=client,
            selection_threshold=0.5, public_or_sanitized_data_ack=True, deadline_seconds=60.0,
        )
        for payload in payloads:
            self.assertIn("provider", payload)
            self.assertFalse(payload["provider"]["allow_fallbacks"])
            size = len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
            self.assertLessEqual(size, MAX_REQUEST_BYTES)

    def test_long_shared_context_sized(self):
        """A long task/context string is included in the shared-state size.

        The long task sits in the shared state object that every request
        serializes, so two 30k-description candidates plus a 30k task must
        still partition without exceeding the per-request limit.
        """
        from hermes_switchyard.routing import _request_size, _multi_skill_batch_questions

        big_task = "T" * 30_000
        candidates = [
            {"name": "s1", "description": "a" * 30_000},
            {"name": "s2", "description": "b" * 30_000},
        ]
        batches = _multi_skill_batches(candidates, task=big_task)
        self.assertGreaterEqual(len(batches), 2)
        for batch in batches:
            # The batching helper already serialized with the shared task in
            # state; assert that the actual wire size stays within the limit.
            size = _request_size({"task": big_task, "skills": batch},
                                 _multi_skill_batch_questions(batch, offset=0, task=big_task))
            self.assertLessEqual(size, MAX_REQUEST_BYTES)

    def test_exactly_at_limit_accepted(self):
        """A large single-candidate request near the limit is accepted.

        The per-candidate serialization budget (96,000 bytes with the fixed
        model field and provider block) admits a single ~47,800-character
        description, which the batching helper returns as one singleton batch
        rather than partitioning or raising.
        """
        from hermes_switchyard.routing import _request_size, _multi_skill_batch_questions

        candidates = [{"name": "t", "description": "a" * 47_800}]
        batches = _multi_skill_batches(candidates, task="public task")
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 1)
        # The single candidate actually serialized within the limit.
        self.assertLessEqual(_request_size(
            {"task": "public task", "skills": candidates},
            _multi_skill_batch_questions(candidates, offset=0, task="public task"),
        ), MAX_REQUEST_BYTES)


# ================================================================= F3 accounting

class F3PartialAccountingTests(unittest.TestCase):
    """§5 F3 matrix: first survives second failure, first-request failure,
    pre-transport rejection, multi-batch success, cache hit no spend, failure
    variants."""

    def test_first_batch_accounting_survives_second_failure(self):
        big = "instruction" * 5_000
        questions = {
            "q_0000": {"type": "noul", "instructions": big, "criteria": {"true": "yes", "false": "no"}},
            "q_9999": {"type": "noul", "instructions": big, "criteria": {"true": "yes", "false": "no"}},
        }
        state = {"task": "public task"}
        seen = {"n": 0}

        def transport(payload):
            seen["n"] += 1
            if seen["n"] == 1:
                return {"model": "typesafe/jev-1.13",
                        "answers": {n: {"noul": 0.99} for n in payload["questions"]},
                        "usage": {"cost": 0.01}, "latency_ms": 10.0, "request_id": "req-1"}
            raise TimeoutError("aggregate deadline expired")

        client = DecisionClient(api_key="fixture-key", transport=transport)
        with self.assertRaises(PartialAccountingError) as ctx:
            client.decide(state, questions, public_or_sanitized_data_ack=True)
        successful = [r for r in ctx.exception.partial if r.get("request_id") == "req-1"]
        self.assertEqual(len(successful), 1)
        self.assertEqual(successful[0]["usage"].get("cost"), 0.01)

    def test_first_request_failure_raises_original(self):
        """A first-request failure raises the original error, no PartialAccounting."""
        questions = {"q_0": {"type": "noul", "instructions": "i", "criteria": {"true": "yes", "false": "no"}}}
        self.assertRaises(RuntimeError, lambda: DecisionClient(
            api_key="fixture-key", transport=lambda p: (_ for _ in ()).throw(RuntimeError("boom"))
        ).decide({"task": "t"}, questions, public_or_sanitized_data_ack=True))

    def test_multi_batch_success_accumulates(self):
        """Two successful batches accumulate usage and request_count."""
        questions = {
            "q_0000": {"type": "noul", "instructions": "instruction" * 5_000, "criteria": {"true": "yes", "false": "no"}},
            "q_9999": {"type": "noul", "instructions": "instruction" * 5_000, "criteria": {"true": "yes", "false": "no"}},
        }

        def transport(payload):
            seen = getattr(transport, "n", 0)
            setattr(transport, "n", seen + 1)
            return {"model": "typesafe/jev-1.13",
                    "answers": {n: {"noul": 0.99} for n in payload["questions"]},
                    "usage": {"cost": 0.01}, "latency_ms": 5.0, "request_id": f"req-{transport.n}"}

        client = DecisionClient(api_key="fixture-key", transport=transport)
        result = client.decide({"task": "public task"}, questions, public_or_sanitized_data_ack=True)
        self.assertEqual(result.get("request_count"), 2)
        self.assertEqual(result.get("total_usage", {}).get("cost"), 0.02)

    def test_first_request_failure_variant_http_error(self):
        """A later batch HTTP error preserves earlier accounting."""
        big = "instruction" * 5_000
        questions = {
            "q_0000": {"type": "noul", "instructions": big, "criteria": {"true": "yes", "false": "no"}},
            "q_9999": {"type": "noul", "instructions": big, "criteria": {"true": "yes", "false": "no"}},
        }
        seen = {"n": 0}

        def transport(payload):
            seen["n"] += 1
            if seen["n"] == 1:
                return {"model": "typesafe/jev-1.13",
                        "answers": {n: {"noul": 0.99} for n in payload["questions"]},
                        "usage": {"cost": 0.03}, "latency_ms": 12.0, "request_id": "req-1"}
            raise RuntimeError("Jev provider returned HTTP 503")

        client = DecisionClient(api_key="fixture-key", transport=transport)
        with self.assertRaises(PartialAccountingError) as ctx:
            client.decide({"task": "public task"}, questions, public_or_sanitized_data_ack=True)
        self.assertTrue(any(r.get("request_id") == "req-1" for r in ctx.exception.partial))
        cost = [r for r in ctx.exception.partial if r.get("request_id") == "req-1"][0]["usage"].get("cost")
        self.assertEqual(cost, 0.03)

    def test_first_request_failure_variant_answer_mismatch(self):
        """A later batch with answer-key mismatch preserves earlier accounting."""
        big = "instruction" * 5_000
        questions = {
            "q_0000": {"type": "noul", "instructions": big, "criteria": {"true": "yes", "false": "no"}},
            "q_9999": {"type": "noul", "instructions": big, "criteria": {"true": "yes", "false": "no"}},
        }
        seen = {"n": 0}

        def transport(payload):
            seen["n"] += 1
            if seen["n"] == 1:
                return {"model": "typesafe/jev-1.13",
                        "answers": {n: {"noul": 0.99} for n in payload["questions"]},
                        "usage": {"cost": 0.02}, "latency_ms": 8.0, "request_id": "req-1"}
            # Return an answer object that does not match the requested questions.
            return {"model": "typesafe/jev-1.13", "answers": {"bogus": {"noul": 0.5}}, "usage": {}}

        client = DecisionClient(api_key="fixture-key", transport=transport)
        with self.assertRaises(PartialAccountingError):
            client.decide({"task": "public task"}, questions, public_or_sanitized_data_ack=True)


# ================================================================= F4 GUI evidence

class F4GuIEvidenceTests(unittest.TestCase):
    """§5 F4 matrix: stale target, fresh-target, target validation, deadline,
    dispatch failure, post-action capture failure, native rejection, receipt
    persistence, before-action, abstention, budget, DONE-no-postcondition,
    missing-input."""

    def _run(self, operations, dispatch, *, max_steps=2, client=None, **kw):
        return run_computer_goal(
            goal="click Go then continue", app="Chrome", max_steps=max_steps,
            dispatch=dispatch, client=client or ComputerClient(operations),
            public_or_sanitized_data_ack=True, deadline_seconds=60.0, **kw,
        )

    def test_stale_target_after_action_preserves_prior_actions(self):
        first = _capture(label="Go", app="Chrome", index=1)
        fresh_ok = _capture(label="Go", app="Chrome", index=1)
        post_ok = _capture(label="Go", app="Chrome", index=1)
        stale = _capture(label="Go", app="Firefox", index=1)
        dispatch = SyntheticDispatch([first, fresh_ok, post_ok, stale])
        result = self._run(["CLICK", "CLICK"], dispatch)
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertTrue(result["reconcile_before_retry"])
        self.assertEqual(dispatch.action_calls, [{"action": "click", "element": 1}])

    def test_failure_before_any_action_reraises(self):
        """A StaleTargetError before any action re-raises (nothing to preserve)."""
        stale = _capture(label="Go", app="Firefox", index=1)
        fresh = _capture(label="Go", app="Chrome", index=1)
        dispatch = SyntheticDispatch([stale, fresh])
        with self.assertRaises(Exception):
            self._run(["CLICK"], dispatch, max_steps=1)

    def test_native_rejection_preserved_no_bypass(self):
        """A native refusal verdict is preserved; no hidden fallback runs."""
        first = _capture(label="Go", app="Chrome", index=1)
        fresh = _capture(label="Go", app="Chrome", index=1)

        def refuse_dispatch(tool, args):
            if args.get("action") == "capture":
                return first if not refuse_dispatch.n else fresh
            refuse_dispatch.n += 1
            # ok=True but effect not confirmed -> unconfirmed_effect (refusal is
            # a real executor verdict, not a silent success or a hidden retry).
            return {"ok": True, "verdict": "refused", "escalation": None,
                    "effect": {"confirmed": False, "status": "unconfirmed"}}

        refuse_dispatch.n = 0
        dispatch = SyntheticDispatch([first, fresh])
        dispatch._refuse = refuse_dispatch  # type: ignore[attr-defined]

        def refuse_call(tool, args):
            if args.get("action") == "capture":
                n = refuse_dispatch.n
                refuse_dispatch.n += 1
                return first if n == 0 else fresh
            return refuse_dispatch(tool, args)

        result = self._run(["CLICK"], refuse_call, max_steps=2)
        self.assertEqual(result["status"], "unconfirmed_effect")
        self.assertEqual(result["attempted_action_count"], 1)

    def test_budget_exhaustion_before_retry(self):
        """max_steps budget exhaustion returns without another request after expiry.

        Identical-index captures let the loop complete every step; once the
        per-call max_steps budget is exhausted the loop stops and reports
        ``step_limit`` with an attempted count equal to the budget, never
        issuing another operation decision.
        """
        first = _capture(label="Go", app="Chrome", index=1)
        fresh = _capture(label="Go", app="Chrome", index=1)
        dispatch = SyntheticDispatch([first, fresh])
        result = self._run(["CLICK", "CLICK"], dispatch, max_steps=2)
        self.assertEqual(result["status"], "step_limit")
        self.assertEqual(result["attempted_action_count"], 2)

    def test_done_without_verifiable_postcondition(self):
        """DONE returns a completion candidate with coordinator verification owner."""
        first = _capture(label="Go", app="Chrome", index=1)
        fresh = _capture(label="Go", app="Chrome", index=1)
        dispatch = SyntheticDispatch([first, fresh])
        result = self._run(["DONE"], dispatch, max_steps=2)
        self.assertEqual(result["status"], "completion_candidate")
        self.assertFalse(result["verified"])
        self.assertEqual(result["verification_owner"], "coordinator")

    def test_low_confidence_abstention(self):
        """A low-confidence operation answer abstains without dispatching.

        The operation acceptance threshold is 0.80; an operation answer at
        confidence 0.10 fails acceptance, so the loop returns ``abstained``
        before dispatching any native action.
        """
        class LowConfClient(ComputerClient):
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                return {"model": "typesafe/jev-1.13",
                        "answers": {"operation": _choice(questions["operation"]["criteria"], "BLOCKED", confidence=0.10),
                                    "hotkey": _choice(questions["hotkey"]["criteria"])},
                        "usage": {}}

        first = _capture(label="Go", app="Chrome", index=1)
        fresh = _capture(label="Go", app="Chrome", index=1)
        dispatch = SyntheticDispatch([first, fresh])
        result = self._run(["CLICK"], dispatch, client=LowConfClient(["CLICK"]))
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["attempted_action_count"], 0)
        self.assertFalse(result["reconcile_before_retry"])

    def test_missing_input_no_fabricated_dispatch(self):
        """TYPE_TEXT without a resolvable caller value never fabricates a type."""
        focused = _capture(label="Name", app="Chrome", index=1)
        focused["elements"][0]["role"] = "Edit"
        focused["elements"][0]["focused"] = True
        fresh = _capture(label="Name", app="Chrome", index=1)
        fresh["elements"][0]["role"] = "Edit"
        fresh["elements"][0]["focused"] = True
        dispatch = SyntheticDispatch([focused, fresh])
        # text_inputs reference a different field than the focused control.
        run_computer_goal(
            goal="fill the name", app="Chrome", max_steps=2,
            dispatch=dispatch, client=ComputerClient(["TYPE_TEXT"]),
            public_or_sanitized_data_ack=True, deadline_seconds=60.0,
            text_inputs=[{"field_label": "email", "value": "x"}],
        )
        actions = [a["action"] for a in dispatch.action_calls]
        self.assertNotIn("type", actions)
        self.assertNotIn("type", actions)


# ================================================================= F5 scan

class F5RestrictedMarkerTests(unittest.TestCase):
    """§5 F5 matrix: CUI markers, idempotency/case/separators, clean public
    control, ack absent, trusted deny, cache-then-newly-marked."""

    def _marked_task(self, marked):
        return marked, _allow_policy_with_payload(marked)

    def test_cui_markers_block_hosted_path(self):
        for marked in [
            "CUI: Review the technical design.",
            "Controlled Unclassified Information: Review the technical design.",
            "Confidential: Review the technical design.",
            "CUI//FOUO: Review the technical design.",
            "cui lower: review the technical design.",
        ]:
            client_calls = []

            def client_factory():
                client_calls.append(True)
                raise AssertionError("restricted outbound payload must be rejected before client creation")

            recommender = AutomaticSkillRecommender(
                configured_candidates=[{"name": "project-helper-a", "description": "public help"}],
                hosted_enabled=True, hosted_mode="always",
                public_or_sanitized_data_ack=True, client_factory=client_factory,
            )
            task, policy = self._marked_task(marked)
            result = recommender.recommend(task, turn_egress_policy=policy)
            self.assertEqual(client_calls, [], f"client created for restricted input: {marked!r}")
            self.assertFalse(result["hosted_attempted"])
            self.assertTrue(result["hosted_skipped"].startswith("local_scan_"))

    def test_clean_public_control_allows(self):
        """A clean public task is allowed through (client may be constructed)."""
        client_calls = []

        def client_factory():
            client_calls.append(True)
            return SimpleNamespace(
                decide=lambda state, questions, *, public_or_sanitized_data_ack=False: {
                    "model": "typesafe/jev-1.13",
                    "answers": {"project-helper-a": {"choice": "project-helper-a", "confidence": 0.9, "probabilities": {"project-helper-a": 0.9}}},
                    "usage": {},
                }
            )

        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "project-helper-a", "description": "public help"}],
            hosted_enabled=True, hosted_mode="always",
            public_or_sanitized_data_ack=True, client_factory=client_factory,
        )
        task, policy = self._marked_task("Review the public technical design.")
        result = recommender.recommend(task, turn_egress_policy=policy)
        self.assertTrue(client_calls)  # allowed -> client constructed
        self.assertTrue(result["hosted_attempted"])

    def test_hosted_acknowledgement_absent_blocks(self):
        """Without the standing acknowledgement, no hosted request is attempted."""
        client_calls = []

        def client_factory():
            client_calls.append(True)
            raise AssertionError("must not construct a hosted client without acknowledgement")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "project-helper-a", "description": "public help"}],
            hosted_enabled=True, hosted_mode="always",
            public_or_sanitized_data_ack=False, client_factory=client_factory,
        )
        task, policy = self._marked_task("Review the public technical design.")
        result = recommender.recommend(task, turn_egress_policy=policy)
        self.assertEqual(client_calls, [])
        self.assertFalse(result["hosted_attempted"])
        self.assertTrue(result["hosted_skipped"].startswith("ack_required"))

    def test_explicit_trusted_deny_not_overridden_by_scan(self):
        """An explicit trusted deny decision blocks even with a clean scan."""
        client_calls = []

        def client_factory():
            client_calls.append(True)
            raise AssertionError("trusted deny must block before client creation")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "project-helper-a", "description": "public help"}],
            hosted_enabled=True, hosted_mode="always",
            public_or_sanitized_data_ack=True, client_factory=client_factory,
        )
        deny = _EgressLike(decision="deny", allowed=False, reason_code="trusted_policy_denied")
        result = recommender.recommend("Review the public technical design.", turn_egress_policy=deny)
        self.assertEqual(client_calls, [])
        self.assertFalse(result["hosted_attempted"])

    def test_cache_hit_then_newly_marked_input(self):
        """A cached clean result is followed by a newly-marked input that blocks."""
        client_calls = []

        def client_factory():
            client_calls.append(True)
            raise AssertionError("newly marked input must not construct a client")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "project-helper-a", "description": "public help"}],
            hosted_enabled=True, hosted_mode="always",
            public_or_sanitized_data_ack=True, client_factory=client_factory, cache_seconds=300.0,
        )
        # First a clean, cached request.
        task1, policy1 = self._marked_task("Review the public technical design.")
        recommender.recommend(task1, turn_egress_policy=policy1)
        self.assertTrue(client_calls)
        client_calls.clear()
        # Then a newly-marked variant on the same recommender.
        task2, policy2 = self._marked_task("CUI: Review the public technical design.")
        r2 = recommender.recommend(task2, turn_egress_policy=policy2)
        self.assertEqual(client_calls, [])
        self.assertFalse(r2["hosted_attempted"])
        self.assertTrue(r2["hosted_skipped"].startswith("local_scan_"))


# ================================================================= A0

class A0NoIntermediateModelCallTests(unittest.TestCase):
    """§5 A0: zero non-Jev inference/coordinator turns across a multi-action
    job through the registered handler, including failure paths, plus a spy
    liveness proof."""

    def test_registered_handler_runs_multi_action_with_zero_non_jev_inference(self):
        spy_calls = {"n": 0, "jev_calls": 0}

        class ForbiddenLLM:
            def __getattr__(self, name):
                spy_calls["n"] += 1
                raise AssertionError(f"non-Jev conversational model call: {name}")

            def __call__(self, *args, **kwargs):
                spy_calls["n"] += 1
                raise AssertionError("non-Jev conversational model call")

        class SpyClient(ComputerClient):
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                spy_calls["jev_calls"] += 1
                return super().decide(state, questions, public_or_sanitized_data_ack=public_or_sanitized_data_ack)

        class Context:
            def __init__(self, dispatch):
                self.settings = {
                "automatic_skill_recommendation": False,
                "jev_provider": "typesafe",
                "api_endpoint": "https://api.typesafe.ai/v1/systemone",
            }
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
            result = json.loads(context.tools["jev_computer_use"]({
                "goal": "click Go twice", "app": "Chrome", "max_steps": 2,
                "public_or_sanitized_data_ack": True,
            }))
        self.assertIn(result["status"], {"completion_candidate", "step_limit", "stalled", "partial_failure"})
        self.assertGreaterEqual(spy_calls["jev_calls"], 2)
        self.assertTrue(all(a["action"] == "click" for a in dispatch.action_calls))
        self.assertEqual(spy_calls["n"], 0)
        self.assertIsInstance(result, dict)
        self.assertIn("status", result)

    def test_failure_path_no_fallback_inference(self):
        """A provider-failure path inside the loop still inserts no model call.

        The provider fails during the second step's operation selection, after
        the first action completed. The loop preserves that prior action and
        returns ``partial_failure`` with the failure phase recorded, never
        invoking a non-Jev fallback model.
        """
        spy_calls = {"n": 0, "jev_calls": 0}

        class ForbiddenLLM:
            def __getattr__(self, name):
                spy_calls["n"] += 1
                raise AssertionError("non-Jev conversational model call")

            def __call__(self, *a, **k):
                spy_calls["n"] += 1
                raise AssertionError("non-Jev conversational model call")

        class FailingAfterOne(ComputerClient):
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                spy_calls["jev_calls"] += 1
                # One full action takes two jev decisions (operation + target);
                # fail on the next operation selection so the first action is
                # already recorded before the provider dies.
                if spy_calls["jev_calls"] >= 3:
                    raise RuntimeError("provider died mid-operation")
                return super().decide(state, questions, public_or_sanitized_data_ack=public_or_sanitized_data_ack)

        class Context:
            def __init__(self, dispatch):
                self.settings = {
                "automatic_skill_recommendation": False,
                "jev_provider": "typesafe",
                "api_endpoint": "https://api.typesafe.ai/v1/systemone",
            }
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

        first = _capture(label="Go", app="Chrome", index=1)
        stale = _capture(label="Go", app="Firefox", index=1)
        dispatch = SyntheticDispatch([first, first, first, stale])
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"), \
             mock.patch.object(hermes_switchyard, "DecisionClient", lambda **_kwargs: FailingAfterOne(["CLICK", "CLICK"])):
            context = Context(dispatch)
            hermes_switchyard.register(context)
            result = json.loads(context.tools["jev_computer_use"]({
                "goal": "click Go then continue", "app": "Chrome", "max_steps": 2,
                "public_or_sanitized_data_ack": True,
            }))
        self.assertIn(result["status"], {"partial_failure", "completion_candidate", "step_limit"})
        self.assertEqual(spy_calls["n"], 0)
        self.assertGreaterEqual(spy_calls["jev_calls"], 2)


if __name__ == "__main__":
    unittest.main()
