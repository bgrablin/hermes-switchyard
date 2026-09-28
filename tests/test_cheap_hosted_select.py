"""Opt-in cheap hosted skill shortlist (fail-open).

Flag ``automatic_skill_cheap_hosted_select`` (default off): when a local
lexical shortlist is decisive, host only that shortlist; abstention or a
winner outside the shortlist fail-opens to the full catalog. Synthetic
DecisionClient only — no live provider calls.
"""
from __future__ import annotations

import json
import time
import unittest

from hermes_switchyard import egress_redaction
from hermes_switchyard.automatic import (
    AutomaticSkillRecommender,
    SHORTLIST_POLICY_CHEAP_FAIL_OPEN,
    SHORTLIST_POLICY_CHEAP_HOSTED,
    SHORTLIST_POLICY_FULL_FAN_OUT,
    SHORTLIST_POLICY_PREFILTER,
    build_routing_receipt,
    plan_cheap_hosted_shortlist,
    plan_hosted_prefilter,
    _rank_candidates,
)


def _allowed_policy(payload="SANITIZED_TASK_MARKER"):
    return {
        "version": 1,
        "decision": "allow",
        "data_class": "sanitized",
        "reason_code": "synthetic_fixture_allowed",
        "allowed_payload": payload,
    }


def _choice(criteria, selected, *, confidence=0.99):
    remaining = [name for name in criteria if name != selected]
    if not remaining:
        probabilities = {selected: 1.0}
    else:
        leftover = 1.0 - confidence
        each = leftover / len(remaining)
        probabilities = {selected: confidence, **{name: each for name in remaining}}
        total = sum(probabilities.values())
        probabilities = {key: value / total for key, value in probabilities.items()}
    return {"choice": selected, "confidence": confidence, "probabilities": probabilities}


class _SelectClient:
    """Synthetic provider: prefer a skill when offered, else abstain."""

    def __init__(self, *, prefer: str | None, delay_ms: float = 1.0):
        self.prefer = prefer
        self.delay_ms = delay_ms
        self.calls = []
        self.offered_per_call: list[set[str]] = []

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        if public_or_sanitized_data_ack is not True:
            raise AssertionError("test client requires acknowledgement")
        started = time.monotonic()
        time.sleep(self.delay_ms / 1000.0)
        self.calls.append((state, questions))
        answers = {}
        offered: set[str] = set()
        if "needs_skill" in questions:
            answers["needs_skill"] = {"noul": 0.10 if self.prefer is None else 0.99}
        if "skill" in questions:
            criteria = questions["skill"]["criteria"]
            offered.update(name for name in criteria if name != "__jev_none_of_these__")
            if self.prefer is not None and self.prefer in criteria:
                answers["skill"] = _choice(criteria, self.prefer, confidence=0.99)
                answers["needs_skill"] = {"noul": 0.99}
            else:
                selected = next(iter(criteria))
                answers["skill"] = _choice(criteria, selected, confidence=0.50)
                answers["needs_skill"] = {"noul": 0.10}
        for name, question in questions.items():
            if not name.startswith("skill_chunk_"):
                continue
            criteria = question["criteria"]
            offered.update(n for n in criteria if n != "__jev_none_of_these__")
            if self.prefer is not None and self.prefer in criteria:
                selected = self.prefer
            else:
                selected = (
                    "__jev_none_of_these__"
                    if "__jev_none_of_these__" in criteria
                    else next(iter(criteria))
                )
            answers[name] = _choice(criteria, selected, confidence=0.99)
        self.offered_per_call.append(offered)
        elapsed_ms = (time.monotonic() - started) * 1000.0
        wire = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
        input_tokens = max(1, len(wire) // 4)
        return {
            "model": "typesafe/jev-1.13",
            "answers": answers,
            "usage": {
                "prompt_tokens": input_tokens,
                "completion_tokens": 32,
                "total_tokens": input_tokens + 32,
                "cost": round(input_tokens * 0.00000002, 12),
            },
            "latency_ms": elapsed_ms,
            "request_id": f"synth-{len(self.calls)}",
        }


def _catalog_with_printer_logs(*, size: int = 120):
    pad = [
        {"name": f"skill-{index}", "description": f"generic helper utility {index}"}
        for index in range(size - 3)
    ]
    return [
        *pad,
        {
            "name": "network-printer-operations",
            "description": "Operate network printers and scanners. Diagnose unreachable printers.",
        },
        {
            "name": "log-triage",
            "description": "Triage application and system logs during startup errors.",
        },
        {
            "name": "systematic-debugging",
            "description": "Systematic debugging methodology for complex software failures.",
        },
    ]


PRINTER_PROMPT = (
    "This is a public synthetic evaluation task. A network printer is unreachable. "
    "Identify likely causes and list safe read-only checks. Use a relevant available "
    "skill if one is appropriate, then finish with EVAL_SKILL=<identifier>."
)
LOGS_PROMPT = (
    "This is a public synthetic evaluation task. An application repeatedly logs ERROR "
    "lines during startup. Identify likely causes and list safe read-only checks. "
    "Use a relevant available skill if one is appropriate (prefer systematic debugging "
    "/ log triage style skills), then finish with EVAL_SKILL=<identifier>."
)


class CheapPlanUnitTests(unittest.TestCase):
    def test_decisive_shortlist_without_cutoff_margin(self):
        catalog = tuple(_catalog_with_printer_logs(size=80))
        ranked = _rank_candidates(
            "Diagnose an unreachable network printer queue jam",
            catalog,
        )
        # Default prefilter may or may not fire depending on margin; cheap plan
        # accepts any eligible band with min_score.
        cheap_policy, cheap_subset = plan_cheap_hosted_shortlist(
            ranked, catalog_size=len(catalog)
        )
        self.assertEqual(cheap_policy, SHORTLIST_POLICY_PREFILTER)
        assert cheap_subset is not None
        names = {item["name"] for item in cheap_subset}
        self.assertIn("network-printer-operations", names)
        self.assertLessEqual(len(cheap_subset), 32)

    def test_below_min_score_fails_open_to_full(self):
        catalog = tuple(
            {"name": f"skill-{i}", "description": f"unrelated topic {i}"} for i in range(64)
        )
        ranked = _rank_candidates("completely orthogonal xyzzy quux", catalog)
        policy, subset = plan_cheap_hosted_shortlist(ranked, catalog_size=len(catalog))
        self.assertIn(policy, {SHORTLIST_POLICY_FULL_FAN_OUT, "local_no_skill_gate"})
        if policy == SHORTLIST_POLICY_FULL_FAN_OUT:
            self.assertIsNone(subset)


class CheapHostedSelectIntegrationTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        # Identity redactor so hosted path runs without a Hermes install.
        egress_redaction._reset_for_tests(lambda text: text, loaded=True)
        self.addCleanup(egress_redaction._reset_for_tests)

    def _recommend(
        self,
        *,
        task,
        candidates,
        prefer,
        cheap=True,
        delay_ms=1.0,
    ):
        client = _SelectClient(prefer=prefer, delay_ms=delay_ms)

        def factory():
            return client

        recommender = AutomaticSkillRecommender(
            configured_candidates=candidates,
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=factory,
            adoption_capable=True,
            cache_seconds=0,
            prefilter_shortlist_size=32,
            cheap_hosted_select=cheap,
            deadline_seconds=30.0,
        )
        result = recommender.recommend(task, turn_egress_policy=_allowed_policy(task))
        return result, client

    def test_flag_default_off_preserves_prefilter_policy_name(self):
        candidates = _catalog_with_printer_logs(size=100)
        result, _client = self._recommend(
            task="Diagnose an unreachable network printer queue jam",
            candidates=candidates,
            prefer="network-printer-operations",
            cheap=False,
        )
        self.assertTrue(result["hosted_attempted"])
        self.assertEqual(result["selected"], "network-printer-operations")
        # Flag off keeps historical local_prefilter_shortlist when decisive.
        self.assertEqual(result.get("shortlist_policy"), SHORTLIST_POLICY_PREFILTER)

    def test_decisive_shortlist_records_cheap_path(self):
        candidates = _catalog_with_printer_logs(size=100)
        result, client = self._recommend(
            task="Diagnose an unreachable network printer queue jam",
            candidates=candidates,
            prefer="network-printer-operations",
            cheap=True,
        )
        self.assertTrue(result["hosted_attempted"])
        self.assertEqual(result["selected"], "network-printer-operations")
        self.assertEqual(result["source"], "jev")
        self.assertEqual(result["shortlist_policy"], SHORTLIST_POLICY_CHEAP_HOSTED)
        self.assertLess(result["offered_count"], len(candidates))
        self.assertGreater(result["excluded_count"], 0)
        # Single shortlist look — no fail-open expand.
        all_offered = set().union(*client.offered_per_call) if client.offered_per_call else set()
        self.assertIn("network-printer-operations", all_offered)
        self.assertLess(len(all_offered), len(candidates))
        receipt = build_routing_receipt(result)
        self.assertEqual(receipt["shortlist_policy"], SHORTLIST_POLICY_CHEAP_HOSTED)

    def test_fail_open_when_shortlist_misses_winner(self):
        # Prefer a skill that scores below the shortlist so the first look
        # abstains / cannot select it; expand must offer the full catalog.
        candidates = _catalog_with_printer_logs(size=100)
        result, client = self._recommend(
            task="Diagnose an unreachable network printer queue jam",
            candidates=candidates,
            prefer="systematic-debugging",  # not in printer shortlist
            cheap=True,
        )
        self.assertTrue(result["hosted_attempted"])
        self.assertEqual(result["shortlist_policy"], SHORTLIST_POLICY_CHEAP_FAIL_OPEN)
        self.assertEqual(result["selected"], "systematic-debugging")
        self.assertEqual(result["source"], "jev")
        # Fail-open hosts the full catalog on the expand look.
        self.assertEqual(result["offered_count"], len(candidates))
        self.assertEqual(result["excluded_count"], 0)
        # At least two decision rounds (shortlist then full), and full offers winner.
        self.assertGreaterEqual(len(client.calls), 2)
        last_offered = client.offered_per_call[-1]
        self.assertIn("systematic-debugging", last_offered)
        self.assertIn("network-printer-operations", last_offered)
        receipt = build_routing_receipt(result)
        self.assertEqual(receipt["shortlist_policy"], SHORTLIST_POLICY_CHEAP_FAIL_OPEN)

    def test_uncertain_local_scores_use_full_catalog_path(self):
        # Flat near-zero overlap: cheap plan is not decisive → full path once.
        candidates = [
            {"name": f"skill-{i}", "description": f"unrelated topic {i}"} for i in range(80)
        ]
        result, client = self._recommend(
            task="xyzzy quux completely orthogonal request",
            candidates=candidates,
            prefer="skill-0",
            cheap=True,
        )
        self.assertTrue(result["hosted_attempted"])
        # Not a cheap shortlist accept; either full fan-out or no-skill override
        # under always (honor gate off) → full catalog offered.
        self.assertEqual(result.get("shortlist_policy"), SHORTLIST_POLICY_FULL_FAN_OUT)
        all_offered = set().union(*client.offered_per_call) if client.offered_per_call else set()
        self.assertGreaterEqual(len(all_offered), min(80, len(candidates) - 5))

    def test_printer_fixture_reaches_hosted_selection_candidates(self):
        candidates = _catalog_with_printer_logs(size=100)
        result, client = self._recommend(
            task=PRINTER_PROMPT,
            candidates=candidates,
            prefer="network-printer-operations",
            cheap=True,
        )
        self.assertTrue(result["hosted_attempted"])
        all_offered = set().union(*client.offered_per_call) if client.offered_per_call else set()
        self.assertIn("network-printer-operations", all_offered)
        self.assertEqual(result["selected"], "network-printer-operations")
        self.assertIn(
            result["shortlist_policy"],
            {
                SHORTLIST_POLICY_CHEAP_HOSTED,
                SHORTLIST_POLICY_CHEAP_FAIL_OPEN,
                SHORTLIST_POLICY_FULL_FAN_OUT,
                SHORTLIST_POLICY_PREFILTER,
            },
        )

    def test_logs_fixture_reaches_hosted_selection_candidates(self):
        candidates = _catalog_with_printer_logs(size=100)
        result, client = self._recommend(
            task=LOGS_PROMPT,
            candidates=candidates,
            prefer="systematic-debugging",
            cheap=True,
        )
        self.assertTrue(result["hosted_attempted"])
        all_offered = set().union(*client.offered_per_call) if client.offered_per_call else set()
        # Capability-first: systematic-debugging and/or log-triage must be
        # offered on some hosted look (shortlist and/or fail-open expand).
        self.assertTrue(
            "systematic-debugging" in all_offered or "log-triage" in all_offered,
            f"logs fixtures must reach hosted candidates; offered={sorted(all_offered)[:20]}",
        )
        self.assertEqual(result["selected"], "systematic-debugging")


if __name__ == "__main__":
    unittest.main()
