"""v0.5.5 routing lane: redact, do not block; local-first bypass; receipt fields.

Synthetic DecisionClient transports only. No network, no private text. Secret
values are built at run time from inert fragments so the hygiene scanner and
readers never see a credential-shaped literal.
"""
from __future__ import annotations

import json
import threading
import unittest

from hermes_switchyard import egress_redaction, receipt_state
from hermes_switchyard.automatic import AutomaticSkillRecommender, build_routing_receipt
from hermes_switchyard.client import EXPECTED_MODEL, DecisionClient
from hermes_switchyard.two_stage_routing import TwoStageConfig
from test_support import HermesHomeTestCase

NONE = "__jev_none_of_these__"
CATALOG = [
    {"name": "docker-management", "description": "Manage Docker containers and Compose"},
    {"name": "github-pr-workflow", "description": "Open, review, and merge pull requests"},
    {"name": "local-secret-lifecycle-operations", "description": "Rotate local service secrets"},
]
# Inert synthetic values, assembled at run time.
SECRET_VALUE = "Zq9w" + "Xr7v" + "Kp3m"
TOKEN_VALUE = "gh" + "p_" + "Q7zX" * 9


class Transport:
    def __init__(self, pick: str = "docker-management"):
        self.payloads: list[dict] = []
        self.pick = pick
        self.lock = threading.Lock()

    def __call__(self, payload):
        with self.lock:
            self.payloads.append(json.loads(json.dumps(payload)))
        answers = {}
        for name, question in payload["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"noul": 0.95}
                continue
            keys = list(question["criteria"])
            pick = self.pick if self.pick in keys else (NONE if NONE in keys else keys[0])
            rest = [key for key in keys if key != pick]
            probabilities = {key: 0.02 / len(rest) for key in rest} if rest else {}
            probabilities[pick] = 0.98 if rest else 1.0
            answers[name] = {"choice": pick, "probabilities": probabilities, "confidence": 0.98}
        return {"model": EXPECTED_MODEL, "answers": answers, "usage": {}, "latency_ms": 5.0}

    def wire(self) -> str:
        return json.dumps(self.payloads, sort_keys=True)


def recommender(transport=None, *, factory=None, two_stage=None):
    if factory is None:
        factory = lambda: DecisionClient(api_key="fixture-key", transport=transport)  # noqa: E731
    return AutomaticSkillRecommender(
        configured_candidates=CATALOG,
        routing_mode="hosted_sanitized",
        hosted_mode="always",
        public_or_sanitized_data_ack=True,
        client_factory=factory,
        cache_seconds=0.0,
        two_stage=two_stage,
    )


def forbidden_factory(calls):
    def factory():
        calls.append(True)
        raise AssertionError("hosted client must not be constructed")

    return factory


class RedactNotBlockTests(HermesHomeTestCase):
    def setUp(self):
        super().setUp()
        self.assertTrue(egress_redaction.redaction_available(), "pinned Hermes must supply agent.redact")

    def test_topic_words_no_longer_block_hosted_routing(self):
        for task in (
            "rotate the password on the staging docker registry",
            "review the confidential rollout notes for the docker upgrade",
            "where is the credential helper for docker login",
            "the want ads site docker build fails",
        ):
            with self.subTest(task=task):
                transport = Transport()
                result = recommender(transport).recommend(task)
                self.assertTrue(result["hosted_attempted"], result.get("hosted_skipped"))
                self.assertGreaterEqual(len(transport.payloads), 1)
                self.assertIn(task.split()[0], transport.wire())

    def test_contact_identifiers_are_not_masked(self):
        task = "email ops@example.com or call 256-555-0100 about the docker outage"
        transport = Transport()
        result = recommender(transport).recommend(task)
        self.assertTrue(result["hosted_attempted"], result.get("hosted_skipped"))
        wire = transport.wire()
        self.assertIn("ops@example.com", wire)
        self.assertIn("256-555-0100", wire)

    def test_secret_values_are_masked_in_the_exact_outbound_jev_state(self):
        task = (
            f"deploy the docker stack with password={SECRET_VALUE} and token {TOKEN_VALUE} "
            "then pay with card 4111 1111 1111 1111"
        )
        for two_stage in (None, TwoStageConfig()):
            with self.subTest(two_stage=two_stage is not None):
                transport = Transport()
                result = recommender(transport, two_stage=two_stage).recommend(task)
                self.assertTrue(result["hosted_attempted"], result.get("hosted_skipped"))
                wire = transport.wire()
                self.assertNotIn(SECRET_VALUE, wire)
                self.assertNotIn(TOKEN_VALUE, wire)
                self.assertNotIn("4111 1111 1111 1111", wire)
                self.assertIn("password=***", wire)
                self.assertIn("[card]", wire)
                self.assertIn("deploy the docker stack", wire)
                receipt = build_routing_receipt(result)
                self.assertNotIn(SECRET_VALUE, json.dumps(receipt))

    def test_restricted_marking_stays_local_before_client_construction(self):
        for task in (
            "company confidential docker migration plan",
            "summarize this client confidential docker report",
            "employer confidential docker hardening checklist",
        ):
            with self.subTest(task=task):
                calls: list[bool] = []
                result = recommender(factory=forbidden_factory(calls)).recommend(task)
                self.assertEqual(calls, [])
                self.assertFalse(result["hosted_attempted"])
                self.assertEqual(result["hosted_skipped"], "local_scan_restricted_marking")
                receipt = build_routing_receipt(result)
                self.assertEqual(receipt["hosted_skip_reason"], "local_scan_restricted_marking")

    def test_marking_rule_is_shared_with_the_effort_path(self):
        from hermes_switchyard import automatic
        from hermes_switchyard.reasoning_effort_adapter import _effort_scan_reason

        self.assertIs(automatic._effort_scan_reason, _effort_scan_reason)

    def test_without_hermes_redactor_no_text_is_sent_and_local_routing_applies(self):
        egress_redaction._reset_for_tests(None, loaded=True)
        self.addCleanup(egress_redaction._reset_for_tests)
        calls: list[bool] = []
        result = recommender(factory=forbidden_factory(calls)).recommend(
            "please look into why the staging job keeps failing"
        )
        self.assertEqual(calls, [])
        self.assertFalse(result["hosted_attempted"])
        self.assertEqual(result["hosted_skipped"], "redaction_unavailable")
        # Local-confident turns never need the redactor.
        rec = AutomaticSkillRecommender(
            configured_candidates=CATALOG,
            routing_mode="hosted_sanitized",
            hosted_mode="uncertain_only",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory(calls),
            cache_seconds=0.0,
        )
        confident = rec.recommend("docker management")
        self.assertEqual(confident["source"], "local")
        self.assertEqual(calls, [])

    def test_oversized_message_stays_local(self):
        calls: list[bool] = []
        task = "docker " * 10_000
        result = recommender(factory=forbidden_factory(calls)).recommend(task)
        self.assertEqual(calls, [])
        self.assertEqual(result["hosted_skipped"], "local_scan_oversized")


class TrivialBypassTests(HermesHomeTestCase):
    def test_greetings_and_acknowledgements_skip_the_hosted_call(self):
        for task in ("hi", "thanks!", "Thank you so much", "ok, sounds good", "lgtm", "👍", "?"):
            with self.subTest(task=task):
                calls: list[bool] = []
                rec = recommender(factory=forbidden_factory(calls))
                result = rec.recommend(task)
                self.assertEqual(calls, [])
                self.assertFalse(result["hosted_attempted"])
                self.assertEqual(result["bypass_reason"], "trivial_turn")
                self.assertEqual(rec.last_receipt["bypass_reason"], "trivial_turn")
                self.assertEqual(rec.last_receipt["hosted_skip_reason"], "trivial_turn")
                self.assertEqual(rec.last_receipt["input_chars"], 0)

    def test_short_task_requests_still_reach_the_hosted_selector(self):
        for task in ("fix ci", "status?", "rebase it", "deploy"):
            with self.subTest(task=task):
                transport = Transport()
                result = recommender(transport).recommend(task)
                self.assertTrue(result["hosted_attempted"], result.get("hosted_skipped"))
                self.assertNotIn("bypass_reason", result)


class ReceiptFieldTests(HermesHomeTestCase):
    def test_hosted_receipt_records_input_chars_and_candidate_count(self):
        transport = Transport()
        rec = recommender(transport)
        task = f"deploy the docker stack with password={SECRET_VALUE}"
        rec.recommend(task)
        receipt = rec.last_receipt
        self.assertTrue(receipt["hosted_attempted"])
        self.assertEqual(receipt["candidate_count"], len(CATALOG))
        self.assertEqual(receipt["input_chars"], len("deploy the docker stack with password=***"))
        self.assertIsNone(receipt["bypass_reason"])
        self.assertGreater(receipt["latency_ms"], 0.0)
        self.assertEqual(receipt_state.canonicalize_receipt(receipt), receipt)

    def test_local_confident_receipt_records_bypass_reason(self):
        rec = AutomaticSkillRecommender(
            configured_candidates=CATALOG,
            routing_mode="hosted_sanitized",
            hosted_mode="uncertain_only",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_factory([]),
            cache_seconds=0.0,
        )
        rec.recommend("docker management")
        self.assertEqual(rec.last_receipt["bypass_reason"], "local_confident")
        self.assertEqual(rec.last_receipt["input_chars"], 0)

    def test_receipt_validation_rejects_bad_new_fields(self):
        transport = Transport()
        rec = recommender(transport)
        rec.recommend("deploy the docker stack")
        good = dict(rec.last_receipt)
        self.assertTrue(receipt_state.validate_receipt(good))
        for key, value in (
            ("bypass_reason", "free text"),
            ("bypass_reason", 3),
            ("input_chars", -1),
            ("input_chars", "12"),
        ):
            with self.subTest(key=key, value=value):
                self.assertFalse(receipt_state.validate_receipt({**good, key: value}))
        skipped = {**good, "hosted_attempted": False, "hosted_succeeded": False, "input_chars": 5}
        self.assertFalse(receipt_state.validate_receipt(skipped))

    def test_old_receipts_without_new_fields_stay_valid(self):
        transport = Transport()
        rec = recommender(transport)
        rec.recommend("deploy the docker stack")
        legacy = {k: v for k, v in rec.last_receipt.items() if k not in {"bypass_reason", "input_chars"}}
        self.assertTrue(receipt_state.validate_receipt(legacy))


if __name__ == "__main__":
    unittest.main()
