"""Paired synthetic benchmarks for automatic routing prefilter (issue #20).

Uses delayed DecisionClient transports only. No live provider calls. Evidence is
request count, intervention latency, approximate tokens, and a cost proxy versus
forced full-partition fan-out on the same held-out cases.
"""
from __future__ import annotations

import json
import statistics
import time
import unittest

from hermes_switchyard.automatic import (
    AutomaticSkillRecommender,
    SHORTLIST_POLICY_FULL_FAN_OUT,
    SHORTLIST_POLICY_NO_SKILL_GATE,
    SHORTLIST_POLICY_PREFILTER,
    _catalog_identity,
    _cached_candidate_tokens,
    _rank_candidates,
    build_routing_receipt,
    plan_hosted_prefilter,
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
        # Normalize residual drift.
        total = sum(probabilities.values())
        probabilities = {key: value / total for key, value in probabilities.items()}
    return {"choice": selected, "confidence": confidence, "probabilities": probabilities}


class _DelayedCatalogClient:
    """Synthetic provider with fixed per-call delay and usage accounting."""

    def __init__(self, *, prefer: str | None, delay_ms: float = 5.0):
        self.prefer = prefer
        self.delay_ms = delay_ms
        self.calls = []
        self.payload_bytes = 0

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        if public_or_sanitized_data_ack is not True:
            raise AssertionError("test client requires acknowledgement")
        started = time.monotonic()
        time.sleep(self.delay_ms / 1000.0)
        wire = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
        self.payload_bytes += len(wire.encode("utf-8"))
        self.calls.append((state, questions))
        answers = {}
        if "needs_skill" in questions:
            # Low needs when prefer is None (no-fit / abstention cases).
            answers["needs_skill"] = {"noul": 0.10 if self.prefer is None else 0.99}
        if "skill" in questions:
            criteria = questions["skill"]["criteria"]
            selected = self.prefer if self.prefer in criteria else next(iter(criteria))
            if self.prefer is None:
                # Force abstention via low confidence / needs in select_skill thresholds.
                answers["skill"] = _choice(criteria, selected, confidence=0.50)
                answers["needs_skill"] = {"noul": 0.10}
            else:
                answers["skill"] = _choice(criteria, selected, confidence=0.99)
        for name, question in questions.items():
            if not name.startswith("skill_chunk_"):
                continue
            criteria = question["criteria"]
            if self.prefer is not None and self.prefer in criteria:
                selected = self.prefer
            else:
                # Prefer none-of-these when the target is outside this partition.
                selected = "__jev_none_of_these__" if "__jev_none_of_these__" in criteria else next(iter(criteria))
            answers[name] = _choice(criteria, selected, confidence=0.99)
        elapsed_ms = (time.monotonic() - started) * 1000.0
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


def _large_catalog(*, winner: str | None = "docker-management", size: int = 300):
    candidates = [
        {"name": f"skill-{index}", "description": f"generic public utility {index}"}
        for index in range(size - (1 if winner else 0))
    ]
    if winner:
        candidates.append(
            {
                "name": winner,
                "description": "Manage Docker containers and Compose services.",
            }
        )
    return candidates


def _percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    weight = rank - low
    return float(ordered[low] * (1.0 - weight) + ordered[high] * weight)


class PrefilterUnitTests(unittest.TestCase):
    def test_no_skill_gate_when_overlap_near_zero(self):
        catalog = tuple(_large_catalog(winner=None, size=64))
        ranked = _rank_candidates("hello how are you today", catalog)
        policy, subset = plan_hosted_prefilter(ranked, catalog_size=len(catalog))
        self.assertEqual(policy, SHORTLIST_POLICY_NO_SKILL_GATE)
        self.assertEqual(subset, ())

    def test_clear_winner_shortlists_without_dropping_margin_safety(self):
        catalog = tuple(_large_catalog(size=300))
        ranked = _rank_candidates("Diagnose a Docker container restart loop", catalog)
        policy, subset = plan_hosted_prefilter(ranked, catalog_size=len(catalog))
        self.assertEqual(policy, SHORTLIST_POLICY_PREFILTER)
        assert subset is not None
        names = {item["name"] for item in subset}
        self.assertIn("docker-management", names)
        self.assertLessEqual(len(subset), 32)
        self.assertLess(len(subset), len(catalog))

    def test_ambiguous_cluster_fails_closed_to_full_fan_out(self):
        catalog = tuple(
            {
                "name": f"docker-skill-{index}",
                "description": "Manage Docker containers and Compose services.",
            }
            for index in range(300)
        )
        ranked = _rank_candidates("Diagnose a Docker container", catalog)
        policy, subset = plan_hosted_prefilter(ranked, catalog_size=len(catalog))
        self.assertEqual(policy, SHORTLIST_POLICY_FULL_FAN_OUT)
        self.assertIsNone(subset)

    def test_small_catalog_passes_through_without_shortlist_rewrite(self):
        catalog = tuple(_large_catalog(size=12))
        ranked = _rank_candidates("Diagnose a Docker container", catalog)
        policy, subset = plan_hosted_prefilter(ranked, catalog_size=len(catalog))
        self.assertEqual(policy, SHORTLIST_POLICY_FULL_FAN_OUT)
        self.assertIsNone(subset)

    def test_catalog_token_features_are_cached_by_hash(self):
        catalog = tuple(_large_catalog(size=40))
        first = _cached_candidate_tokens(catalog)
        second = _cached_candidate_tokens(catalog)
        self.assertIs(first, second)
        self.assertEqual(len(first), len(catalog))
        self.assertEqual(len(_catalog_identity(catalog)), 64)


class PrefilterIntegrationTests(unittest.TestCase):
    def _recommend(self, *, task, candidates, prefer, delay_ms=3.0, shortlist_size=32, hosted_mode="always"):
        client = _DelayedCatalogClient(prefer=prefer, delay_ms=delay_ms)

        def factory():
            return client

        recommender = AutomaticSkillRecommender(
            configured_candidates=candidates,
            routing_mode="hosted_sanitized",
            hosted_mode=hosted_mode,
            public_or_sanitized_data_ack=True,
            client_factory=factory,
            adoption_capable=True,
            cache_seconds=0,
            prefilter_shortlist_size=shortlist_size,
            deadline_seconds=30.0,
        )
        started = time.monotonic()
        result = recommender.recommend(task, turn_egress_policy=_allowed_policy(task))
        wall_ms = (time.monotonic() - started) * 1000.0
        return result, client, wall_ms

    def test_no_skill_gate_skips_hosted_requests(self):
        candidates = _large_catalog(winner=None, size=120)
        result, client, _wall = self._recommend(
            task="hello how are you today",
            candidates=candidates,
            prefer=None,
            hosted_mode="uncertain_only",
        )
        self.assertFalse(result["hosted_attempted"])
        self.assertEqual(result["hosted_skipped"], SHORTLIST_POLICY_NO_SKILL_GATE)
        self.assertEqual(result["shortlist_policy"], SHORTLIST_POLICY_NO_SKILL_GATE)
        self.assertEqual(result.get("request_count", 0), 0)
        self.assertEqual(client.calls, [])
        receipt = build_routing_receipt(result)
        self.assertEqual(receipt["shortlist_policy"], SHORTLIST_POLICY_NO_SKILL_GATE)

    def test_shortlist_reduces_fan_out_and_preserves_winner(self):
        candidates = _large_catalog(size=300)
        result, client, _wall = self._recommend(
            task="Diagnose a Docker container restart loop",
            candidates=candidates,
            prefer="docker-management",
        )
        self.assertTrue(result["hosted_attempted"])
        self.assertEqual(result["selected"], "docker-management")
        self.assertEqual(result["source"], "jev")
        self.assertEqual(result["shortlist_policy"], SHORTLIST_POLICY_PREFILTER)
        self.assertEqual(result["jev_shortlist_policy"], SHORTLIST_POLICY_PREFILTER)
        self.assertLess(result["jev_offered_count"], 300)
        self.assertGreater(result["jev_excluded_count"], 0)
        self.assertEqual(result["jev_request_count"], len(client.calls))
        self.assertEqual(len(client.calls), 1)
        offered = {
            name
            for _state, questions in client.calls
            for question in questions.values()
            for name in (question.get("criteria") or {})
            if name != "__jev_none_of_these__"
        }
        # Single Choice path uses "skill" criteria, not partitions.
        self.assertIn("docker-management", offered)
        self.assertLessEqual(len(offered), 32)

    def test_ambiguous_cases_keep_full_catalog_recall(self):
        candidates = [
            {
                "name": f"docker-skill-{index}",
                "description": "Manage Docker containers and Compose services.",
            }
            for index in range(300)
        ]
        prefer = "docker-skill-299"
        result, client, _wall = self._recommend(
            task="Diagnose a Docker container",
            candidates=candidates,
            prefer=prefer,
            delay_ms=1.0,
        )
        self.assertTrue(result["hosted_attempted"])
        self.assertEqual(result.get("jev_shortlist_policy") or result.get("shortlist_policy"), SHORTLIST_POLICY_FULL_FAN_OUT)
        self.assertGreater(len(client.calls), 1)
        offered = {
            name
            for _state, questions in client.calls
            for question_name, question in questions.items()
            if question_name.startswith("skill_chunk_")
            for name in question["criteria"]
            if name != "__jev_none_of_these__"
        }
        self.assertEqual(offered, {item["name"] for item in candidates})
        self.assertEqual(result["selected"], prefer)

    def test_paired_benchmark_reports_latency_tokens_cost(self):
        cases = [
            {
                "label": "single_skill",
                "task": "Diagnose a Docker container restart loop",
                "candidates": _large_catalog(size=300),
                "prefer": "docker-management",
                "expect_policy": SHORTLIST_POLICY_PREFILTER,
            },
            {
                "label": "no_fit",
                "task": "hello how are you today",
                "candidates": _large_catalog(winner=None, size=300),
                "prefer": None,
                "expect_policy": SHORTLIST_POLICY_NO_SKILL_GATE,
                "hosted_mode": "uncertain_only",
            },
            {
                "label": "ambiguous",
                "task": "Diagnose a Docker container",
                "candidates": [
                    {
                        "name": f"docker-skill-{index}",
                        "description": "Manage Docker containers and Compose services.",
                    }
                    for index in range(256)
                ],
                "prefer": "docker-skill-255",
                "expect_policy": SHORTLIST_POLICY_FULL_FAN_OUT,
            },
        ]

        prefilter_rows = []
        full_rows = []
        for case in cases:
            # Prefilter-enabled recommender (production defaults).
            result, client, wall_ms = self._recommend(
                task=case["task"],
                candidates=case["candidates"],
                prefer=case["prefer"],
                delay_ms=2.0,
                shortlist_size=32,
                hosted_mode=case.get("hosted_mode", "always"),
            )
            policy = result.get("shortlist_policy") or result.get("jev_shortlist_policy")
            self.assertEqual(policy, case["expect_policy"], case["label"])
            usage = result.get("jev_total_usage") or result.get("jev_usage") or {}
            prefilter_rows.append(
                {
                    "label": case["label"],
                    "policy": policy,
                    "request_count": int(result.get("jev_request_count") or result.get("request_count") or 0),
                    "latency_ms": float(result.get("jev_total_latency_ms") or result.get("jev_latency_ms") or wall_ms),
                    "wall_ms": wall_ms,
                    "prompt_tokens": float(usage.get("prompt_tokens") or 0),
                    "cost": float(usage.get("cost") or 0),
                    "payload_bytes": client.payload_bytes,
                    "selected": result.get("selected"),
                }
            )

            # Forced full-catalog arm: shortlist_size >= catalog disables shortlist
            # and no-skill threshold 0 disables the gate so both arms stay paired.
            full_client = _DelayedCatalogClient(prefer=case["prefer"], delay_ms=2.0)
            full = AutomaticSkillRecommender(
                configured_candidates=case["candidates"],
                routing_mode="hosted_sanitized",
                hosted_mode="always",
                public_or_sanitized_data_ack=True,
                client_factory=lambda c=full_client: c,
                adoption_capable=True,
                cache_seconds=0,
                prefilter_shortlist_size=10_000,
                prefilter_no_skill_threshold=0.0,
                prefilter_min_score=1.0,
                deadline_seconds=60.0,
            )
            started = time.monotonic()
            full_result = full.recommend(case["task"], turn_egress_policy=_allowed_policy(case["task"]))
            full_wall = (time.monotonic() - started) * 1000.0
            full_usage = full_result.get("jev_total_usage") or full_result.get("jev_usage") or {}
            full_rows.append(
                {
                    "label": case["label"],
                    "request_count": int(
                        full_result.get("jev_request_count") or full_result.get("request_count") or len(full_client.calls)
                    ),
                    "latency_ms": float(
                        full_result.get("jev_total_latency_ms")
                        or full_result.get("jev_latency_ms")
                        or full_wall
                    ),
                    "wall_ms": full_wall,
                    "prompt_tokens": float(full_usage.get("prompt_tokens") or 0),
                    "cost": float(full_usage.get("cost") or 0),
                    "payload_bytes": full_client.payload_bytes,
                    "selected": full_result.get("selected"),
                }
            )

        # Recall / abstention safety: prefilter must not invent winners on no-fit,
        # and must match full-fan-out selection on single-skill / ambiguous cases.
        by_label_pre = {row["label"]: row for row in prefilter_rows}
        by_label_full = {row["label"]: row for row in full_rows}
        self.assertIsNone(by_label_pre["no_fit"]["selected"])
        self.assertEqual(by_label_pre["single_skill"]["selected"], by_label_full["single_skill"]["selected"])
        self.assertEqual(by_label_pre["ambiguous"]["selected"], by_label_full["ambiguous"]["selected"])

        # Measurable hosted reduction on the clear single-skill case.
        self.assertLess(
            by_label_pre["single_skill"]["request_count"],
            by_label_full["single_skill"]["request_count"],
        )
        self.assertEqual(by_label_pre["no_fit"]["request_count"], 0)
        self.assertGreater(by_label_full["no_fit"]["request_count"], 0)

        pre_lat = [row["latency_ms"] for row in prefilter_rows]
        full_lat = [row["latency_ms"] for row in full_rows]
        report = {
            "prefilter": {
                "p50_intervention_latency_ms": _percentile(pre_lat, 50),
                "p95_intervention_latency_ms": _percentile(pre_lat, 95),
                "request_count_sum": sum(row["request_count"] for row in prefilter_rows),
                "prompt_tokens_sum": sum(row["prompt_tokens"] for row in prefilter_rows),
                "cost_sum": sum(row["cost"] for row in prefilter_rows),
                "rows": prefilter_rows,
            },
            "full_partition": {
                "p50_intervention_latency_ms": _percentile(full_lat, 50),
                "p95_intervention_latency_ms": _percentile(full_lat, 95),
                "request_count_sum": sum(row["request_count"] for row in full_rows),
                "prompt_tokens_sum": sum(row["prompt_tokens"] for row in full_rows),
                "cost_sum": sum(row["cost"] for row in full_rows),
                "rows": full_rows,
            },
        }
        self.assertLess(report["prefilter"]["request_count_sum"], report["full_partition"]["request_count_sum"])
        self.assertLess(report["prefilter"]["prompt_tokens_sum"], report["full_partition"]["prompt_tokens_sum"])
        # Synthetic wall-clock p50 can invert under OS timer noise (seen on
        # Windows CI). Prefer request/token reduction as the durable metric;
        # only require latencies to be finite and non-negative here.
        self.assertGreaterEqual(report["prefilter"]["p50_intervention_latency_ms"], 0.0)
        self.assertGreaterEqual(report["full_partition"]["p50_intervention_latency_ms"], 0.0)
        # Keep the synthetic report attached for operator-visible unittest output.
        print(json.dumps(report, sort_keys=True, indent=2))
        self.assertGreaterEqual(report["prefilter"]["p95_intervention_latency_ms"], 0.0)
        self.assertTrue(statistics.fmean(pre_lat) >= 0.0)


if __name__ == "__main__":
    unittest.main()
