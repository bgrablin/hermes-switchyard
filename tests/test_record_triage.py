"""Offline tests for the bounded jev_assess record-triage workflow.

Every provider interaction uses a synthetic transport behind the real
DecisionClient, so request shaping, response validation, accounting, and the
no-fallback payload are exercised for real. Records are synthetic.
"""
from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import hermes_switchyard
from hermes_switchyard import record_triage
from hermes_switchyard.client import DecisionClient
from hermes_switchyard.record_triage import run_record_triage, verify_artifact

MODEL = "typesafe/jev-1.13"
SEVERITIES = ["cosmetic", "minor", "major", "critical"]
SECRET_MARKER = "SYNTHETIC_PROVIDER_SECRET_MARKER"


def _record(rid, *, title=None, body=None, component="parser", **extra):
    record = {
        "id": rid,
        "title": title if title is not None else f"Title for {rid}",
        "body": body if body is not None else f"Steps to reproduce {rid}: run the tool twice.",
        "data_class": "synthetic",
    }
    if component is not None:
        record["component"] = component
    record.update(extra)
    return record


def _choice_answer(criteria, chosen, probability):
    others = [key for key in criteria if key != chosen]
    probabilities = {chosen: probability}
    for key in others:
        probabilities[key] = (1.0 - probability) / len(others)
    return {"choice": chosen, "probabilities": probabilities, "confidence": probability}


def _score_answer(level, probability):
    winner = SEVERITIES.index(level)
    others = [index for index in range(len(SEVERITIES)) if index != winner]
    probabilities = {str(winner): probability}
    for index in others:
        probabilities[str(index)] = (1.0 - probability) / len(others)
    score = sum(index * probabilities[str(index)] for index in range(len(SEVERITIES)))
    return {
        "score": score,
        "legend": {str(i): name for i, name in enumerate(SEVERITIES)},
        "probabilities": probabilities,
        "confidence": probability,
    }


class Script:
    """Per-record scripted answers; unknown records get a confident 'qualified'."""

    def __init__(self, **by_id):
        self.by_id = by_id
        self.payloads = []
        self.cost = None
        self.fail_calls = {}
        self.sleep = {}

    def transport(self, payload):
        call = len(self.payloads)
        self.payloads.append(copy.deepcopy(payload))
        if call in self.sleep:
            time.sleep(self.sleep[call])
        if call in self.fail_calls:
            raise self.fail_calls[call]
        answers = {}
        for name, question in payload["questions"].items():
            kind, rid = name.split("__", 1)
            spec = self.by_id.get(rid, {})
            if kind == "disposition":
                chosen, probability = spec.get("disposition", ("qualified", 0.95))
                answers[name] = _choice_answer(question["criteria"], chosen, probability)
            else:
                level, probability = spec.get("severity", ("major", 0.9))
                answers[name] = _score_answer(level, probability)
        usage = {"prompt_tokens": 10, "completion_tokens": 2}
        cost = self.cost[call] if isinstance(self.cost, list) else self.cost
        if cost is not None:
            usage["cost"] = cost
        return {"model": MODEL, "answers": answers, "usage": usage, "request_id": f"req-{call}"}

    def client(self):
        return DecisionClient(api_key="test-key", transport=self.transport)

    def sent_ids(self):
        ids = []
        for payload in self.payloads:
            ids.extend(item["id"] for item in payload["state"]["records"])
        return ids


class TriageCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = Path(self._tmp.name) / "triage"

    def run_triage(self, records, script, **kwargs):
        return run_record_triage(records, client=script.client(), out_dir=self.out, **kwargs)

    def manifest(self):
        return json.loads((self.out / "manifest.json").read_text(encoding="utf-8"))

    def by_id(self, result):
        return {item["id"]: item for item in result["records"]}


class SuccessTests(TriageCase):
    def test_batch_is_assessed_acted_on_and_independently_verified(self):
        records = [
            _record("rec-001", component="parser"),
            _record("rec-002", component="cli"),
            _record("rec-003"),
            _record("rec-004"),
        ]
        script = Script(**{
            "rec-001": {"disposition": ("qualified", 0.95), "severity": ("critical", 0.9)},
            "rec-002": {"disposition": ("qualified", 0.95), "severity": ("minor", 0.9)},
            "rec-003": {"disposition": ("needs_info", 0.92)},
            "rec-004": {"disposition": ("out_of_scope", 0.91)},
        })
        script.cost = 0.002
        result = self.run_triage(records, script)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["accounting"]["requests_completed"], 1)
        self.assertEqual(len(script.payloads), 1)
        by_id = self.by_id(result)
        first = by_id["rec-001"]
        self.assertEqual(first["attempt"], {"batch": 0, "requested": True, "completed": True})
        self.assertEqual(first["decision"]["status"], "accepted")
        self.assertEqual(first["decision"]["source"], "jev")
        self.assertEqual(first["decision"]["disposition"], "qualified")
        self.assertEqual(first["consumer"]["status"], "acted")
        self.assertEqual(first["consumer"]["action"], "queue_qualified")
        self.assertEqual(by_id["rec-003"]["consumer"]["action"], "request_info")
        self.assertEqual(by_id["rec-004"]["consumer"]["action"], "close_out_of_scope")

        manifest = self.manifest()
        self.assertEqual(manifest["queues"]["parser"], ["rec-001"])
        self.assertEqual(manifest["queues"]["cli"], ["rec-002"])
        action = json.loads((self.out / "actions" / "rec-001.json").read_text(encoding="utf-8"))
        self.assertEqual(action["queue"], "parser")
        self.assertEqual(action["priority"], "p0")

        report = verify_artifact(self.out, records)
        self.assertEqual(report["errors"], [])
        self.assertTrue(report["verified"])

    def test_stages_are_separate_and_run_never_self_certifies(self):
        script = Script()
        result = self.run_triage([_record("rec-001")], script)
        item = result["records"][0]
        self.assertEqual(set(item), {"id", "attempt", "decision", "consumer"})
        self.assertIs(result["verified"], False)
        self.assertEqual(result["verification"], "not_run")

    def test_payload_keeps_no_fallback_and_sends_only_allowed_record_fields(self):
        script = Script()
        result = self.run_triage([_record("rec-001")], script)
        payload = script.payloads[0]
        self.assertEqual(payload["provider"], {"allow_fallbacks": False})
        self.assertEqual(set(payload["state"]["records"][0]), {"id", "title", "body", "component"})
        self.assertEqual(result["status"], "complete")

    def test_severity_abstention_keeps_record_qualified_but_unrated(self):
        script = Script(**{"rec-001": {"severity": ("major", 0.4)}})
        result = self.run_triage([_record("rec-001")], script)
        item = result["records"][0]
        self.assertEqual(item["consumer"]["action"], "queue_qualified")
        action = json.loads((self.out / "actions" / "rec-001.json").read_text(encoding="utf-8"))
        self.assertEqual(action["priority"], "unrated")
        self.assertTrue(verify_artifact(self.out, [_record("rec-001")])["verified"])


class AbstentionTests(TriageCase):
    def test_low_confidence_disposition_abstains_and_takes_no_action(self):
        records = [_record("rec-001"), _record("rec-002")]
        script = Script(**{"rec-001": {"disposition": ("qualified", 0.55)}})
        result = self.run_triage(records, script)
        held = self.by_id(result)["rec-001"]
        self.assertEqual(held["decision"]["status"], "abstained")
        self.assertEqual(held["decision"]["reason"], "below_threshold")
        self.assertEqual(held["consumer"]["status"], "skipped")
        self.assertFalse((self.out / "actions" / "rec-001.json").exists())
        self.assertTrue((self.out / "actions" / "rec-002.json").exists())
        self.assertNotIn("rec-001", sum(self.manifest()["queues"].values(), []))
        self.assertTrue(verify_artifact(self.out, records)["verified"])

    def test_high_confidence_but_low_winning_probability_also_abstains(self):
        def transport(payload):
            answers = {}
            for name, question in payload["questions"].items():
                if name.startswith("disposition__"):
                    answer = _choice_answer(question["criteria"], "qualified", 0.5)
                    answer["confidence"] = 0.99
                    answers[name] = answer
                else:
                    answers[name] = _score_answer("major", 0.9)
            return {"model": MODEL, "answers": answers, "usage": {}}

        client = DecisionClient(api_key="test-key", transport=transport)
        result = run_record_triage([_record("rec-001")], client=client, out_dir=self.out)
        self.assertEqual(result["records"][0]["decision"]["status"], "abstained")


class MalformedResponseTests(TriageCase):
    def test_choice_outside_offered_alternatives_fails_the_batch_closed(self):
        def transport(payload):
            answers = {}
            for name, question in payload["questions"].items():
                if name.startswith("disposition__"):
                    answers[name] = {
                        "choice": "escalate_to_ceo",
                        "probabilities": {"escalate_to_ceo": 1.0},
                        "confidence": 1.0,
                    }
                else:
                    answers[name] = _score_answer("major", 0.9)
            return {"model": MODEL, "answers": answers, "usage": {"cost": 0.01}}

        client = DecisionClient(api_key="test-key", transport=transport)
        result = run_record_triage([_record("rec-001")], client=client, out_dir=self.out)
        item = result["records"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(item["attempt"]["completed"], False)
        self.assertEqual(item["decision"]["status"], "unassessed")
        self.assertEqual(item["decision"]["reason"], "invalid_response")
        self.assertEqual(item["consumer"]["status"], "skipped")
        self.assertEqual(list((self.out / "actions").iterdir()), [])
        self.assertTrue(result["accounting"]["usage_incomplete"])

    def test_answer_that_bypasses_client_validation_is_rejected_per_record(self):
        class LaxClient:
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                answers = {}
                for name, question in questions.items():
                    if name == "disposition__rec-002":
                        answers[name] = {"choice": "qualified"}  # no probabilities
                    elif name.startswith("disposition__"):
                        answers[name] = _choice_answer(question["criteria"], "qualified", 0.95)
                    else:
                        answers[name] = _score_answer("major", 0.9)
                return {"model": MODEL, "answers": answers, "usage": {}, "request_count": 1}

        records = [_record("rec-001"), _record("rec-002")]
        result = run_record_triage(records, client=LaxClient(), out_dir=self.out)
        by_id = self.by_id(result)
        self.assertEqual(by_id["rec-001"]["consumer"]["status"], "acted")
        self.assertEqual(by_id["rec-002"]["decision"]["status"], "unassessed")
        self.assertEqual(by_id["rec-002"]["decision"]["reason"], "malformed_answer")
        self.assertEqual(by_id["rec-002"]["consumer"]["status"], "skipped")
        self.assertEqual(result["status"], "partial")

    def test_non_object_response_is_unassessed_not_guessed(self):
        class BrokenClient:
            def decide(self, *_args, **_kwargs):
                return ["not", "an", "object"]

        result = run_record_triage([_record("rec-001")], client=BrokenClient(), out_dir=self.out)
        self.assertEqual(result["records"][0]["decision"]["status"], "unassessed")
        self.assertEqual(result["status"], "failed")


class ParserBoundaryTests(TriageCase):
    def test_parse_answers_rejects_non_normalized_disposition_and_severity_distributions(self):
        answers = {
            "disposition__rec-001": _choice_answer(record_triage.DISPOSITIONS, "qualified", 0.95),
            "severity__rec-001": _score_answer("major", 0.9),
        }
        for question_name in ("disposition__rec-001", "severity__rec-001"):
            with self.subTest(question_name=question_name):
                candidate = copy.deepcopy(answers)
                probabilities = candidate[question_name]["probabilities"]
                candidate[question_name]["probabilities"] = {
                    key: 1.0 for key in probabilities
                }
                with self.assertRaises(ValueError):
                    record_triage._parse_answers("rec-001", candidate, 0.80, 0.80)

    def test_parse_answers_rejects_distributions_outside_client_tolerance(self):
        answers = {
            "disposition__rec-001": _choice_answer(record_triage.DISPOSITIONS, "qualified", 0.95),
            "severity__rec-001": _score_answer("major", 0.9),
        }
        for question_name in ("disposition__rec-001", "severity__rec-001"):
            with self.subTest(question_name=question_name):
                candidate = copy.deepcopy(answers)
                probabilities = candidate[question_name]["probabilities"]
                keys = list(probabilities)
                candidate[question_name]["probabilities"] = {
                    key: (0.90 if key == keys[0] else 0.10) for key in keys
                }
                with self.assertRaises(ValueError):
                    record_triage._parse_answers("rec-001", candidate, 0.80, 0.80)

    def test_parse_answers_accepts_distributions_within_client_tolerance(self):
        answers = {
            "disposition__rec-001": _choice_answer(record_triage.DISPOSITIONS, "qualified", 0.95),
            "severity__rec-001": _score_answer("major", 0.9),
        }
        answers["disposition__rec-001"]["probabilities"] = {
            "qualified": 0.818,
            "needs_info": 0.096,
            "out_of_scope": 0.096,
        }
        answers["severity__rec-001"]["probabilities"] = {
            "0": 0.030333333333333334,
            "1": 0.030333333333333334,
            "2": 0.919,
            "3": 0.030333333333333334,
        }

        decision = record_triage._parse_answers("rec-001", answers, 0.80, 0.80)

        self.assertEqual(decision["status"], "accepted")
        self.assertEqual(decision["winning_probability"], 0.818)
        self.assertEqual(decision["severity_probability"], 0.919)


class DeadlineTests(TriageCase):
    def test_aggregate_deadline_fails_overrunning_batch_and_stops_later_ones(self):
        records = [_record(f"rec-00{i}") for i in range(1, 7)]
        script = Script()
        script.sleep[0] = 0.06  # finishes inside the 0.09s aggregate deadline
        script.sleep[1] = 0.06  # overruns it
        result = self.run_triage(records, script, records_per_batch=2, deadline_seconds=0.09)
        by_id = self.by_id(result)
        self.assertEqual(len(script.payloads), 2)  # third batch is never sent
        self.assertEqual(result["status"], "partial")
        self.assertEqual(by_id["rec-001"]["consumer"]["status"], "acted")
        self.assertEqual(by_id["rec-003"]["attempt"], {
            "batch": 1, "requested": True, "completed": False, "reason": "deadline_exceeded",
        })
        self.assertEqual(by_id["rec-005"]["attempt"], {
            "batch": 2, "requested": False, "completed": False, "reason": "deadline_exceeded",
        })
        for rid in ("rec-003", "rec-005"):
            self.assertEqual(by_id[rid]["decision"]["status"], "unassessed")
            self.assertEqual(by_id[rid]["decision"]["reason"], "deadline_exceeded")
            self.assertEqual(by_id[rid]["consumer"]["status"], "skipped")
        self.assertEqual(result["accounting"]["batches_failed"], 1)
        self.assertEqual(result["accounting"]["batches_not_attempted"], 1)
        self.assertTrue(verify_artifact(self.out, records)["verified"])

    def test_invalid_deadline_is_rejected_before_any_request(self):
        script = Script()
        with self.assertRaises(ValueError):
            self.run_triage([_record("rec-001")], script, deadline_seconds=0)
        self.assertEqual(script.payloads, [])


class PartialBatchFailureTests(TriageCase):
    def test_failed_batch_is_unassessed_and_others_proceed_without_retry(self):
        records = [_record(f"rec-00{i}") for i in range(1, 7)]
        script = Script()
        script.cost = 0.003
        script.fail_calls[1] = RuntimeError(SECRET_MARKER)
        result = self.run_triage(records, script, records_per_batch=2)

        self.assertEqual(len(script.payloads), 3)  # one attempt per batch, no retry
        self.assertEqual(result["status"], "partial")
        by_id = self.by_id(result)
        for rid in ("rec-001", "rec-002", "rec-005", "rec-006"):
            self.assertEqual(by_id[rid]["consumer"]["status"], "acted", rid)
        for rid in ("rec-003", "rec-004"):
            self.assertEqual(by_id[rid]["attempt"]["requested"], True)
            self.assertEqual(by_id[rid]["attempt"]["completed"], False)
            self.assertEqual(by_id[rid]["decision"]["status"], "unassessed")
            self.assertEqual(by_id[rid]["decision"]["reason"], "provider_failed")
            self.assertEqual(by_id[rid]["consumer"]["status"], "skipped")
            self.assertFalse((self.out / "actions" / f"{rid}.json").exists())
        accounting = result["accounting"]
        self.assertEqual(accounting["batches_failed"], 1)
        self.assertEqual(accounting["batches_completed"], 2)
        self.assertTrue(accounting["usage_incomplete"])
        self.assertFalse(accounting["cost_known"])
        self.assertIsNone(accounting["total_cost"])
        self.assertNotIn(SECRET_MARKER, json.dumps(result))
        self.assertNotIn(SECRET_MARKER, (self.out / "manifest.json").read_text(encoding="utf-8"))
        self.assertTrue(verify_artifact(self.out, records)["verified"])

    def test_failed_batch_records_never_receive_a_local_guess(self):
        records = [_record("rec-001")]
        script = Script()
        script.fail_calls[0] = RuntimeError("boom")
        result = self.run_triage(records, script)
        self.assertEqual(result["records"][0]["decision"]["source"], None)
        self.assertEqual(result["records"][0]["decision"]["status"], "unassessed")


class DeterministicBypassTests(TriageCase):
    def test_local_rules_decide_records_without_sending_them_to_jev(self):
        records = [
            _record("rec-001", body="   "),
            _record("rec-002", title="Same Title", body="same   body text"),
            _record("rec-003", title="same title", body="Same body TEXT"),
            _record("rec-004"),
        ]
        script = Script()
        result = self.run_triage(records, script)
        by_id = self.by_id(result)

        self.assertEqual(script.sent_ids(), ["rec-002", "rec-004"])
        for rid in ("rec-001", "rec-003"):
            self.assertEqual(by_id[rid]["attempt"], {"batch": None, "requested": False, "completed": False})
            self.assertEqual(by_id[rid]["decision"]["source"], "local_rule")
        self.assertEqual(by_id["rec-001"]["decision"]["rule"], "empty_body")
        self.assertEqual(by_id["rec-001"]["consumer"]["action"], "request_info")
        self.assertEqual(by_id["rec-003"]["decision"]["rule"], "exact_duplicate_of:rec-002")
        self.assertEqual(by_id["rec-003"]["consumer"]["action"], "close_out_of_scope")
        self.assertNotIn("rec-001", json.dumps(script.payloads))
        self.assertNotIn("rec-003", json.dumps(script.payloads))
        self.assertTrue(verify_artifact(self.out, records)["verified"])

    def test_fully_local_batch_makes_no_provider_request_and_reports_zero_cost(self):
        records = [_record("rec-001", body=""), _record("rec-002", body="\n")]
        script = Script()
        result = self.run_triage(records, script)
        self.assertEqual(script.payloads, [])
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["accounting"]["requests_completed"], 0)
        self.assertEqual(result["accounting"]["batches_attempted"], 0)
        self.assertTrue(result["accounting"]["cost_known"])
        self.assertEqual(result["accounting"]["total_cost"], 0.0)
        self.assertTrue(verify_artifact(self.out, records)["verified"])


class ConsumerRejectionTests(TriageCase):
    def test_accepted_qualified_decision_is_rejected_without_routable_component(self):
        records = [_record("rec-001", component="not-a-component"), _record("rec-002", component=None)]
        script = Script()
        result = self.run_triage(records, script)
        for item in result["records"]:
            self.assertEqual(item["decision"]["status"], "accepted")  # decision stands...
            self.assertEqual(item["consumer"]["status"], "rejected")  # ...consumer refuses
            self.assertEqual(item["consumer"]["reason"], "component_not_routable")
        self.assertEqual(list((self.out / "actions").iterdir()), [])
        self.assertEqual(self.manifest()["queues"], {})
        self.assertTrue(verify_artifact(self.out, records)["verified"])


class VerifierTests(TriageCase):
    def setUp(self):
        super().setUp()
        self.records = [_record("rec-001"), _record("rec-002", body="")]
        self.script = Script()
        self.run_triage(self.records, self.script)
        self.assertTrue(verify_artifact(self.out, self.records)["verified"])

    def test_tampered_action_file_is_detected(self):
        path = self.out / "actions" / "rec-001.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["queue"] = "cli"
        path.write_text(json.dumps(payload), encoding="utf-8")
        report = verify_artifact(self.out, self.records)
        self.assertFalse(report["verified"])
        self.assertIn("action_file_hash_mismatch:rec-001", report["errors"])

    def test_forged_manifest_action_that_policy_would_not_allow_is_detected(self):
        manifest = self.manifest()
        entry = manifest["records"][1]  # locally decided empty_body -> request_info
        entry["consumer"]["action"] = "close_out_of_scope"
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = verify_artifact(self.out, self.records)
        self.assertFalse(report["verified"])
        self.assertIn("action_disposition_mismatch:rec-002", report["errors"])

    def test_forged_local_rule_claim_is_detected(self):
        manifest = self.manifest()
        entry = manifest["records"][0]  # a Jev-decided record claiming a local rule
        entry["decision"]["source"] = "local_rule"
        entry["decision"]["rule"] = "empty_body"
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = verify_artifact(self.out, self.records)
        self.assertFalse(report["verified"])
        self.assertIn("local_rule_not_rederivable:rec-001", report["errors"])

    def test_stray_action_file_and_changed_input_are_detected(self):
        (self.out / "actions" / "rec-999.json").write_text("{}", encoding="utf-8")
        self.assertIn("unexpected_action_file:rec-999.json", verify_artifact(self.out, self.records)["errors"])
        (self.out / "actions" / "rec-999.json").unlink()
        changed = copy.deepcopy(self.records)
        changed[0]["title"] = "edited after the run"
        self.assertIn("input_digest_mismatch", verify_artifact(self.out, changed)["errors"])

    def test_missing_manifest_is_not_verified(self):
        (self.out / "manifest.json").unlink()
        report = verify_artifact(self.out, self.records)
        self.assertFalse(report["verified"])
        self.assertIn("manifest_unreadable", report["errors"])

    def test_recorded_evidence_below_threshold_is_detected(self):
        manifest = self.manifest()
        manifest["records"][0]["decision"]["confidence"] = 0.10
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = verify_artifact(self.out, self.records)
        self.assertIn("accepted_below_threshold:rec-001", report["errors"])


class CopilotReviewRegressionTests(TriageCase):
    def test_verifier_rejects_abstention_without_completed_jev_evidence(self):
        records = [_record("rec-001")]
        script = Script()
        script.fail_calls[0] = RuntimeError("provider failure")
        self.run_triage(records, script)
        manifest = self.manifest()
        entry = manifest["records"][0]
        entry["decision"]["status"] = "abstained"
        manifest["unprocessed"] = {}
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        report = verify_artifact(self.out, records)

        self.assertFalse(report["verified"])
        self.assertIn("abstention_evidence_invalid:rec-001", report["errors"])

    def test_verifier_exact_compares_needs_info_and_out_of_scope_payloads(self):
        records = [_record("rec-001"), _record("rec-002")]
        script = Script(**{
            "rec-001": {"disposition": ("needs_info", 0.95)},
            "rec-002": {"disposition": ("out_of_scope", 0.95)},
        })
        self.run_triage(records, script)
        original_manifest = (self.out / "manifest.json").read_bytes()
        original_files = {
            record_id: (self.out / "actions" / f"{record_id}.json").read_bytes()
            for record_id in ("rec-001", "rec-002")
        }
        for record_id, field, value in (
            ("rec-001", "message", "forged request-info message"),
            ("rec-002", "reason", "forged out-of-scope reason"),
        ):
            with self.subTest(record_id=record_id):
                path = self.out / "actions" / f"{record_id}.json"
                payload = json.loads(original_files[record_id])
                payload[field] = value
                data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
                path.write_bytes(data)
                manifest = json.loads(original_manifest)
                entry = next(item for item in manifest["records"] if item["id"] == record_id)
                entry["consumer"]["sha256"] = hashlib.sha256(data).hexdigest()
                (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                try:
                    report = verify_artifact(self.out, records)
                    self.assertFalse(report["verified"])
                    self.assertIn(f"action_payload_mismatch:{record_id}", report["errors"])
                finally:
                    (self.out / "manifest.json").write_bytes(original_manifest)
                    for restored_id, content in original_files.items():
                        (self.out / "actions" / f"{restored_id}.json").write_bytes(content)

    def test_verifier_rejects_non_object_manifest_entries_without_raising(self):
        records = [_record("rec-001"), _record("rec-002")]
        self.run_triage(records, Script())
        manifest = self.manifest()
        manifest["records"].append(5)
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        try:
            report = verify_artifact(self.out, records)
        except Exception as exc:  # noqa: BLE001 -- the contract is fail-closed, not a raised TypeError
            self.fail(f"verify_artifact raised {type(exc).__name__}")
        self.assertFalse(report["verified"])
        self.assertTrue(report["errors"])

    def test_verifier_rejects_forged_jev_duplicate_rule_on_out_of_scope(self):
        records = [_record("rec-001"), _record("rec-002")]
        script = Script(**{"rec-001": {"disposition": ("out_of_scope", 0.95)}})
        self.run_triage(records, script)
        manifest = self.manifest()
        entry = next(item for item in manifest["records"] if item["id"] == "rec-001")
        entry["decision"]["rule"] = "exact_duplicate_of:rec-002"
        path = self.out / "actions" / "rec-001.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["reason"] = "exact_duplicate_of:rec-002"
        data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
        path.write_bytes(data)
        entry["consumer"]["sha256"] = hashlib.sha256(data).hexdigest()
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

        report = verify_artifact(self.out, records)

        self.assertFalse(report["verified"])
        self.assertTrue(
            "jev_rule_not_allowed:rec-001" in report["errors"]
            or "action_payload_mismatch:rec-001" in report["errors"]
        )


class InputValidationTests(TriageCase):
    def assert_rejected_before_provider(self, records, exception=ValueError, **kwargs):
        script = Script()
        with self.assertRaises(exception):
            self.run_triage(records, script, **kwargs)
        self.assertEqual(script.payloads, [])
        self.assertFalse(self.out.exists())

    def test_unsafe_or_duplicate_identifiers_are_rejected(self):
        self.assert_rejected_before_provider([_record("../escape")])
        self.assert_rejected_before_provider([_record("UPPER")])
        self.assert_rejected_before_provider([_record("rec-001"), _record("rec-001")])

    def test_records_must_declare_public_or_synthetic_data_class(self):
        record = _record("rec-001")
        del record["data_class"]
        self.assert_rejected_before_provider([record])
        self.assert_rejected_before_provider([_record("rec-001", data_class="private")])
        self.assert_rejected_before_provider([_record("rec-001", data_class=["public"])])

    def test_unknown_fields_oversize_and_count_are_rejected(self):
        self.assert_rejected_before_provider([_record("rec-001", secret_field="x")])
        self.assert_rejected_before_provider([_record("rec-001", body="x" * (record_triage.MAX_BODY_CHARS + 1))])
        self.assert_rejected_before_provider([_record(f"rec-{i:03d}") for i in range(record_triage.MAX_RECORDS + 1)])
        self.assert_rejected_before_provider([])

    def test_ack_refusal_prevents_provider_request(self):
        self.assert_rejected_before_provider(
            [_record("rec-001")], exception=PermissionError, public_or_sanitized_data_ack=False
        )

    def test_existing_non_empty_output_directory_is_refused(self):
        self.out.mkdir(parents=True)
        (self.out / "keep.txt").write_text("keep", encoding="utf-8")
        script = Script()
        with self.assertRaises(FileExistsError):
            self.run_triage([_record("rec-001")], script)
        self.assertEqual(script.payloads, [])
        self.assertEqual((self.out / "keep.txt").read_text(encoding="utf-8"), "keep")


class AccountingTests(TriageCase):
    def test_costs_sum_only_when_every_completed_batch_reports_one(self):
        records = [_record(f"rec-00{i}") for i in range(1, 5)]
        script = Script()
        script.cost = [0.01, 0.02]
        result = self.run_triage(records, script, records_per_batch=2)
        accounting = result["accounting"]
        self.assertTrue(accounting["cost_known"])
        self.assertAlmostEqual(accounting["total_cost"], 0.03)
        self.assertEqual(accounting["requests_completed"], 2)
        self.assertEqual(accounting["request_ids"], ["req-0", "req-1"])

    def test_missing_cost_is_unknown_never_zero(self):
        script = Script()
        script.cost = [0.01, None]
        result = self.run_triage([_record(f"rec-00{i}") for i in range(1, 5)], script, records_per_batch=2)
        accounting = result["accounting"]
        self.assertFalse(accounting["cost_known"])
        self.assertIsNone(accounting["total_cost"])
        self.assertEqual(accounting["known_cost_subtotal"], 0.01)

    def test_provider_controlled_usage_keys_are_not_persisted(self):
        script = Script()

        def transport(payload):
            result = script.transport(payload)
            result["usage"]["provider_controlled_secret_name"] = 7
            return result

        client = DecisionClient(api_key="test-key", transport=transport)
        result = run_record_triage([_record("rec-001")], client=client, out_dir=self.out)

        self.assertEqual(
            result["accounting"]["total_usage"],
            {"prompt_tokens": 10.0, "completion_tokens": 2.0},
        )
        manifest = self.manifest()
        self.assertEqual(manifest["accounting"]["total_usage"], result["accounting"]["total_usage"])
        self.assertNotIn("provider_controlled_secret_name", json.dumps(manifest))

    def test_workflow_request_budget_is_enforced_locally(self):
        records = [_record(f"rec-{i:03d}") for i in range(1, 6)]
        script = Script()
        with mock.patch.object(record_triage, "MAX_WORKFLOW_REQUESTS", 2):
            result = self.run_triage(records, script, records_per_batch=1)
        self.assertEqual(len(script.payloads), 2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(self.by_id(result)["rec-003"]["attempt"]["reason"], "request_budget_exhausted")


class PartialRetentionTests(TriageCase):
    """Completed assessments survive any later failure; unprocessed records are explicit evidence."""

    def test_any_single_failed_batch_leaves_every_other_batch_acted(self):
        records = [_record(f"rec-00{i}") for i in range(1, 7)]
        for failing in (0, 1, 2):  # first, middle, last
            with self.subTest(failing=failing):
                out = Path(self._tmp.name) / f"case-{failing}"
                script = Script()
                script.fail_calls[failing] = RuntimeError(SECRET_MARKER)
                result = run_record_triage(records, client=script.client(), out_dir=out, records_per_batch=2)
                self.assertEqual(len(script.payloads), 3)
                acted = {item["id"] for item in result["records"] if item["consumer"]["status"] == "acted"}
                lost = {r["id"] for r in script.payloads[failing]["state"]["records"]}
                self.assertEqual(acted, {r["id"] for r in records} - lost)
                self.assertEqual(result["status"], "partial")

    def test_consecutive_failed_batches_do_not_stop_a_later_success(self):
        records = [_record(f"rec-00{i}") for i in range(1, 7)]
        script = Script()
        script.fail_calls[0] = RuntimeError("a")
        script.fail_calls[1] = TypeError("b")  # the client normalises every transport error
        result = self.run_triage(records, script, records_per_batch=2)
        by_id = self.by_id(result)
        self.assertEqual(len(script.payloads), 3)
        self.assertEqual(by_id["rec-005"]["consumer"]["status"], "acted")
        self.assertEqual(by_id["rec-001"]["decision"]["reason"], "provider_failed")
        self.assertEqual(by_id["rec-003"]["decision"]["reason"], "provider_failed")
        self.assertEqual(result["accounting"]["batches_failed"], 2)

    def test_oversize_records_are_split_by_the_workflow_not_dropped_unattempted(self):
        # Control characters serialise to six bytes, so eight maximal bodies cannot share one request.
        records = [_record(f"rec-00{i}", body="\x00" * record_triage.MAX_BODY_CHARS) for i in range(1, 9)]
        script = Script()
        result = self.run_triage(records, script)
        self.assertGreater(len(script.payloads), 1)
        for payload in script.payloads:
            self.assertLessEqual(len(json.dumps(payload, ensure_ascii=False).encode("utf-8")), 96_000)
        self.assertEqual(sorted(script.sent_ids()), sorted(r["id"] for r in records))
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["accounting"]["requests_completed"], len(script.payloads))
        self.assertTrue(verify_artifact(self.out, records)["verified"])

    def test_failure_among_split_batches_keeps_the_completed_ones(self):
        records = [_record(f"rec-00{i}", body="\x00" * record_triage.MAX_BODY_CHARS) for i in range(1, 9)]
        for failing in (0, 1):
            with self.subTest(failing=failing):
                out = Path(self._tmp.name) / f"split-{failing}"
                script = Script()
                script.fail_calls[failing] = RuntimeError(SECRET_MARKER)
                result = run_record_triage(records, client=script.client(), out_dir=out)
                self.assertGreaterEqual(len(script.payloads), 2)
                lost = {r["id"] for r in script.payloads[failing]["state"]["records"]}
                self.assertTrue(lost)
                for item in result["records"]:
                    expected = "skipped" if item["id"] in lost else "acted"
                    self.assertEqual(item["consumer"]["status"], expected, item["id"])
                self.assertEqual(result["status"], "partial")
                self.assertTrue(verify_artifact(out, records)["verified"])

    def test_unexpected_exception_while_reading_an_answer_is_contained(self):
        class Hostile(dict):
            def get(self, *_args, **_kwargs):
                raise RuntimeError(SECRET_MARKER)

        class Client:
            calls = 0

            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                Client.calls += 1
                if Client.calls == 1:
                    return {"model": MODEL, "answers": Hostile(), "usage": {}}
                return Script().transport({"questions": questions, "state": state})

        records = [_record(f"rec-00{i}") for i in range(1, 5)]
        result = run_record_triage(records, client=Client(), out_dir=self.out, records_per_batch=2)
        by_id = self.by_id(result)
        self.assertEqual(by_id["rec-001"]["decision"]["reason"], "malformed_answer")
        self.assertEqual(by_id["rec-003"]["consumer"]["status"], "acted")
        self.assertNotIn(SECRET_MARKER, json.dumps(result))
        self.assertTrue(verify_artifact(self.out, records)["verified"])

    def test_consumer_error_for_one_record_does_not_lose_the_others(self):
        records = [_record("rec-001"), _record("rec-002"), _record("rec-003")]
        original = record_triage._consume

        def flaky(record, decision):
            if record["id"] == "rec-002":
                raise RuntimeError(SECRET_MARKER)
            return original(record, decision)

        with mock.patch.object(record_triage, "_consume", flaky):
            result = self.run_triage(records, Script())
        by_id = self.by_id(result)
        self.assertEqual(by_id["rec-002"]["decision"]["status"], "accepted")
        self.assertEqual(by_id["rec-002"]["consumer"]["status"], "failed")
        self.assertEqual(by_id["rec-002"]["consumer"]["reason"], "consumer_error")
        self.assertEqual(by_id["rec-001"]["consumer"]["status"], "acted")
        self.assertEqual(by_id["rec-003"]["consumer"]["status"], "acted")
        self.assertNotIn(SECRET_MARKER, json.dumps(result))
        self.assertTrue(verify_artifact(self.out, records)["verified"])

    def test_unprocessed_records_are_explicit_in_the_manifest_with_attempt_evidence(self):
        records = [_record(f"rec-00{i}") for i in range(1, 7)]
        script = Script()
        script.fail_calls[1] = RuntimeError(SECRET_MARKER)
        script.sleep[0] = 0.0
        self.run_triage(records, script, records_per_batch=2)
        manifest = self.manifest()
        self.assertEqual(manifest["unprocessed"], {"rec-003": "provider_failed", "rec-004": "provider_failed"})
        entry = {item["id"]: item for item in manifest["records"]}["rec-003"]
        self.assertEqual(entry["attempt"], {
            "batch": 1, "requested": True, "completed": False, "reason": "provider_failed",
        })
        self.assertEqual(manifest["accounting"]["records_unprocessed"], 2)
        self.assertNotIn(SECRET_MARKER, json.dumps(manifest))

    def test_verifier_rejects_a_hidden_or_invented_unprocessed_record(self):
        records = [_record("rec-001"), _record("rec-002")]
        script = Script()
        script.fail_calls[0] = RuntimeError("x")
        self.run_triage(records, script, records_per_batch=1)
        self.assertTrue(verify_artifact(self.out, records)["verified"])
        manifest = self.manifest()
        del manifest["unprocessed"]["rec-001"]
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("unprocessed_mismatch", verify_artifact(self.out, records)["errors"])

    def test_verifier_rejects_unknown_unassessed_reason_and_wrong_decision_source(self):
        records = [_record("rec-001"), _record("rec-002")]
        script = Script()
        script.fail_calls[0] = RuntimeError("x")
        self.run_triage(records, script, records_per_batch=1)
        manifest = self.manifest()
        manifest["records"][0]["decision"]["reason"] = "vibes"
        manifest["unprocessed"]["rec-001"] = "vibes"
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("unassessed_reason_invalid:rec-001", verify_artifact(self.out, records)["errors"])

    def test_verifier_rejects_attempt_evidence_that_contradicts_the_decision(self):
        records = [_record("rec-001"), _record("rec-002", body=""), _record("rec-003")]
        script = Script()
        script.fail_calls[0] = RuntimeError("x")
        self.run_triage(records, script, records_per_batch=1)
        self.assertTrue(verify_artifact(self.out, records)["verified"])
        for index, forged, code in (
            (1, {"batch": 0, "requested": True, "completed": True}, "attempt_mismatch:rec-002"),  # local rule "sent"
            (2, {"batch": 1, "requested": True, "completed": False, "reason": "provider_failed"}, "attempt_mismatch:rec-003"),
            (0, {"batch": 0, "requested": True, "completed": False, "reason": "deadline_exceeded"}, "attempt_mismatch:rec-001"),
        ):
            with self.subTest(code=code):
                manifest = self.manifest()
                manifest["records"][index]["attempt"] = forged
                original = (self.out / "manifest.json").read_bytes()
                (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                try:
                    self.assertIn(code, verify_artifact(self.out, records)["errors"])
                finally:
                    (self.out / "manifest.json").write_bytes(original)

    def test_verifier_reports_but_never_raises_on_hostile_manifest_shapes(self):
        records = [_record("rec-001"), _record("rec-002")]
        script = Script()
        script.fail_calls[0] = RuntimeError("x")
        self.run_triage(records, script, records_per_batch=1)
        original = (self.out / "manifest.json").read_bytes()
        for field, hostile in (("attempt", 5), ("decision", ["x"]), ("consumer", "x"), ("attempt", None)):
            with self.subTest(field=field, hostile=hostile):
                manifest = self.manifest()
                manifest["records"][0][field] = hostile
                (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
                report = verify_artifact(self.out, records)  # must not raise
                self.assertFalse(report["verified"])
                self.assertIn("entry_unreadable:rec-001", report["errors"])
        (self.out / "manifest.json").write_bytes(original)
        manifest = self.manifest()
        manifest["records"][0]["decision"]["source"] = "jev"  # an unassessed record claiming a source
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("unassessed_carries_a_decision:rec-001", verify_artifact(self.out, records)["errors"])

    def test_verifier_fails_closed_when_a_hash_valid_action_file_lacks_queue_fields(self):
        import hashlib
        records = [_record("rec-001")]
        self.run_triage(records, Script())
        path = self.out / "actions" / "rec-001.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        del payload["queue"], payload["priority"]
        data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
        path.write_bytes(data)
        manifest = self.manifest()
        manifest["records"][0]["consumer"]["sha256"] = hashlib.sha256(data).hexdigest()
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        report = verify_artifact(self.out, records)  # must not raise
        self.assertFalse(report["verified"])
        self.assertIn("manifest_structure_invalid", report["errors"])

    def test_verifier_checks_action_file_decision_source_and_reports_invalid_records_accurately(self):
        records = [_record("rec-001")]
        self.run_triage(records, Script())
        path = self.out / "actions" / "rec-001.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["decision_source"] = "local_rule"
        data = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode("utf-8")
        path.write_bytes(data)
        import hashlib
        manifest = self.manifest()
        manifest["records"][0]["consumer"]["sha256"] = hashlib.sha256(data).hexdigest()
        (self.out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        self.assertIn("action_file_source_mismatch:rec-001", verify_artifact(self.out, records)["errors"])
        bad = [dict(records[0], data_class="private")]
        self.assertIn("records_invalid", verify_artifact(self.out, bad)["errors"])


class JevAssessParityTests(unittest.TestCase):
    def test_workflow_request_is_accepted_by_the_registered_jev_assess_handler(self):
        state, questions = record_triage.build_assessment_request([_record("rec-001"), _record("rec-002")])
        seen = []

        def transport(payload):
            seen.append(payload)
            return Script().transport(payload)

        class Context:
            def __init__(self):
                self.tools = {}

            def get_config(self, _key, default=None):
                return default

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_cli_command(self, *_a, **_k):
                pass

            def register_auxiliary_task(self, *_a, **_k):
                pass

            def register_skill(self, *_a, **_k):
                pass

            def register_hook(self, *_a, **_k):
                pass

        context = Context()
        with mock.patch.object(
            hermes_switchyard, "DecisionClient",
            lambda **_kwargs: DecisionClient(api_key="test-key", transport=transport),
        ), mock.patch.object(hermes_switchyard, "_secret", return_value="test-key"):
            hermes_switchyard.register(context)
            reply = json.loads(context.tools["jev_assess"]({"state": state, "questions": questions}))
        self.assertNotEqual(reply.get("status"), "error", reply)
        self.assertEqual(set(reply["answers"]), set(questions))
        self.assertEqual(seen[0]["state"], state)

    def test_questions_use_code_defined_alternatives_and_ordered_severity(self):
        _state, questions = record_triage.build_assessment_request([_record("rec-001")])
        disposition = questions["disposition__rec-001"]
        self.assertEqual(disposition["type"], "choice")
        self.assertEqual(set(disposition["criteria"]), set(record_triage.DISPOSITIONS))
        severity = questions["severity__rec-001"]
        self.assertEqual(severity["type"], "score")
        self.assertEqual(severity["criteria"], list(record_triage.SEVERITY_LEVELS))


if __name__ == "__main__":
    unittest.main()
