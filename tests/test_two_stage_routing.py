"""Two-stage routing planner tests (issue #94).

All hosted calls use a synthetic ``DecisionClient`` transport. No network,
no credentials, no private text.
"""
from __future__ import annotations

import json
import sys
import threading
import time
import unittest
from pathlib import Path

from hermes_switchyard import receipt_state
from hermes_switchyard.automatic import _copy_redacted_jev_metadata, build_routing_receipt
from hermes_switchyard.client import (
    EXPECTED_MODEL,
    DeadlineExceeded,
    DecisionClient,
    HostCancelled,
    LateResultDiscarded,
    PartialAccountingError,
    host_cancel_scope,
)
from hermes_switchyard.two_stage_routing import (
    DEFAULT_HOSTED_DETAIL,
    DEFAULT_RECHECK_TOP_K,
    HOSTED_DETAIL_DESCRIPTIONS,
    HOSTED_DETAIL_EXCERPT,
    HOSTED_DETAIL_NAMES,
    MAX_DETAIL_EXCERPT_CHARS,
    NONINTERACTIVE_PLATFORMS,
    PLATFORM_REASON_KANBAN_WORKER,
    PLATFORM_REASON_NONINTERACTIVE,
    PLATFORM_REASON_NOT_LISTED,
    SHORTLIST_POLICY_TWO_STAGE,
    SHORTLIST_POLICY_TWO_STAGE_EARLY_STOP,
    SHORTLIST_POLICY_TWO_STAGE_SINGLE,
    TwoStageConfig,
    build_stage2_skills,
    detect_kanban_worker,
    plan_two_stage,
    platform_decision,
    rank_stage1,
    run_two_stage,
    skill_excerpt,
)

ROOT = Path(__file__).resolve().parents[1]
NONE = "__jev_none_of_these__"
DESCRIPTION_MARKER = "SYNTHETIC_CANDIDATE_DESCRIPTION_MARKER"
TASK = "Render a mel spectrogram for this public wav file"


def catalog(size: int = 403, *, target: str = "songsee", at: int = 350) -> list[dict[str, str]]:
    rows = [
        {"name": f"filler-skill-{index:03d}", "description": f"{DESCRIPTION_MARKER} filler {index}"}
        for index in range(size)
    ]
    rows[at] = {"name": target, "description": f"{DESCRIPTION_MARKER} audio spectrogram features"}
    return rows


class Responder:
    """Synthetic transport that favors ``target`` wherever it is offered."""

    def __init__(self, *, target: str = "songsee", needs: float = 0.95, stage2_choice: str | None = None,
                 delay: float = 0.0, barrier: threading.Barrier | None = None) -> None:
        self.target = target
        self.needs = needs
        self.stage2_choice = stage2_choice
        self.delay = delay
        self.barrier = barrier
        self.payloads: list[dict] = []
        self._lock = threading.Lock()

    def __call__(self, payload: dict) -> dict:
        with self._lock:
            self.payloads.append(json.loads(json.dumps(payload)))
        if self.barrier is not None and payload["state"].get("partition", 0) > 0:
            self.barrier.wait(timeout=2.0)
        if self.delay:
            time.sleep(self.delay)
        answers = {}
        for name, question in payload["questions"].items():
            if question["type"] == "noul":
                answers[name] = {"noul": self.needs}
                continue
            keys = list(question["criteria"])
            wanted = self.stage2_choice if name == "skill" and self.stage2_choice is not None else self.target
            pick = wanted if wanted in keys else NONE
            rest = [key for key in keys if key != pick]
            probabilities = {key: 0.02 / len(rest) for key in rest} if rest else {}
            probabilities[pick] = 0.98 if rest else 1.0
            answers[name] = {"choice": pick, "probabilities": probabilities, "confidence": 0.98}
        return {"model": EXPECTED_MODEL, "answers": answers, "usage": {"cost": 0.0001}, "latency_ms": 10.0}


def responder(**kwargs) -> Responder:
    return Responder(**kwargs)


def client_for(transport) -> DecisionClient:
    return DecisionClient(api_key="test-key", transport=transport)


class PlatformGateTests(unittest.TestCase):
    def test_default_skips_noninteractive_and_routes_interactive(self):
        config = TwoStageConfig()
        for platform in NONINTERACTIVE_PLATFORMS:
            self.assertEqual(platform_decision(platform, config), (False, PLATFORM_REASON_NONINTERACTIVE))
        for platform in ("cli", "telegram", "discord", "tui", "", None, "API_SERVER "[:0]):
            self.assertTrue(platform_decision(platform, config)[0], platform)
        self.assertEqual(platform_decision(" Cron ", config), (False, PLATFORM_REASON_NONINTERACTIVE))

    def test_kanban_worker_skipped_by_default_and_detected_from_env(self):
        self.assertTrue(detect_kanban_worker({"HERMES_KANBAN_TASK": "t_1"}))
        self.assertFalse(detect_kanban_worker({"HERMES_KANBAN_TASK": "  "}))
        self.assertFalse(detect_kanban_worker({}))
        self.assertEqual(
            platform_decision("cli", TwoStageConfig(), kanban_worker=True),
            (False, PLATFORM_REASON_KANBAN_WORKER),
        )

    def test_override_all_and_allowlist(self):
        every = TwoStageConfig.from_mapping({"automatic_skill_platforms": "all"})
        self.assertTrue(platform_decision("api_server", every)[0])
        self.assertTrue(platform_decision("cli", every, kanban_worker=True)[0])
        schema_all = TwoStageConfig.from_mapping({"automatic_skill_platforms": ["all"]})
        self.assertTrue(platform_decision("api_server", schema_all)[0])
        self.assertTrue(platform_decision("cli", schema_all, kanban_worker=True)[0])
        listed = TwoStageConfig.from_mapping({"automatic_skill_platforms": ["cli", "cron"]})
        self.assertTrue(platform_decision("cron", listed)[0])
        self.assertEqual(platform_decision("telegram", listed), (False, PLATFORM_REASON_NOT_LISTED))
        self.assertFalse(platform_decision("cli", listed, kanban_worker=True)[0])
        with_kanban = TwoStageConfig.from_mapping({"automatic_skill_platforms": ["cli", "kanban"]})
        self.assertTrue(platform_decision("cli", with_kanban, kanban_worker=True)[0])

    def test_invalid_platform_config_falls_back_to_interactive_default(self):
        for raw in ([], [1], "sometimes", {"cli": True}, [""]):
            config = TwoStageConfig.from_mapping({"automatic_skill_platforms": raw})
            self.assertEqual(config.platform_policy, TwoStageConfig().platform_policy, raw)


class ConfigTests(unittest.TestCase):
    def test_defaults_keep_names_only_boundary(self):
        self.assertEqual(DEFAULT_HOSTED_DETAIL, HOSTED_DETAIL_NAMES)
        self.assertEqual(TwoStageConfig.from_mapping(None).hosted_detail, HOSTED_DETAIL_NAMES)
        self.assertEqual(TwoStageConfig.from_mapping({}).recheck_top_k, DEFAULT_RECHECK_TOP_K)

    def test_invalid_values_use_defaults_and_bounds(self):
        config = TwoStageConfig.from_mapping(
            {
                "automatic_skill_hosted_detail": "full_body",
                "automatic_skill_recheck_top_k": 999,
                "automatic_skill_early_stop": "yes",
                "automatic_skill_early_stop_threshold": float("nan"),
                "automatic_skill_parallel_requests": True,
            }
        )
        self.assertEqual(config.hosted_detail, HOSTED_DETAIL_NAMES)
        self.assertLessEqual(config.recheck_top_k, 8)
        self.assertTrue(config.early_stop)
        self.assertEqual(config.early_stop_threshold, TwoStageConfig().early_stop_threshold)
        self.assertEqual(config.parallel_requests, TwoStageConfig().parallel_requests)


class PlanTests(unittest.TestCase):
    def test_large_catalog_plans_partitions_plus_one_recheck(self):
        plan = plan_two_stage(TASK, catalog(), TwoStageConfig())
        self.assertGreater(plan.stage1_requests, 1)
        self.assertEqual(sum(len(part) for part in plan.partitions), 403)
        self.assertTrue(plan.stage2_enabled)
        self.assertEqual(plan.max_requests, plan.stage1_requests + 1)

    def test_single_partition_names_only_needs_no_stage2(self):
        plan = plan_two_stage(TASK, catalog(20, at=5), TwoStageConfig())
        self.assertEqual((plan.stage1_requests, plan.stage2_enabled, plan.max_requests), (1, False, 1))
        detailed = plan_two_stage(
            TASK, catalog(20, at=5), TwoStageConfig(hosted_detail=HOSTED_DETAIL_DESCRIPTIONS)
        )
        self.assertTrue(detailed.stage2_enabled)

    def test_rejects_duplicates_and_reserved_names(self):
        with self.assertRaises(ValueError):
            plan_two_stage(TASK, [{"name": "a"}, {"name": "a"}], TwoStageConfig())
        with self.assertRaises(ValueError):
            run_two_stage(task=TASK, candidates=[{"name": NONE}, {"name": "b"}],
                          client=client_for(responder()), config=TwoStageConfig())

    def test_rank_stage1_is_global_and_excludes_none(self):
        ranked = rank_stage1(
            [
                (("a", "b"), {"a": 0.2, "b": 0.1, NONE: 0.7}),
                (("c", "d"), {"c": 0.9, "d": 0.04, NONE: 0.06}),
            ],
            top_k=3,
            min_probability=0.05,
        )
        self.assertEqual(ranked, ["c", "a", "b"])


class DataBoundaryTests(unittest.TestCase):
    def test_names_default_sends_no_description_anywhere(self):
        transport = responder()
        result = run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport))
        self.assertEqual(result["selected"], "songsee")
        self.assertEqual(result["hosted_detail"], HOSTED_DETAIL_NAMES)
        self.assertEqual(result["detail_sent_count"], 0)
        blob = json.dumps(transport.payloads)
        self.assertNotIn(DESCRIPTION_MARKER, blob)
        for payload in transport.payloads:
            for row in payload["state"]["skills"]:
                self.assertEqual(set(row), {"name"})

    def test_descriptions_opt_in_reach_only_top_k_in_stage2(self):
        transport = responder()
        loader_calls: list[str] = []
        result = run_two_stage(
            task=TASK,
            candidates=catalog(),
            client=client_for(transport),
            config=TwoStageConfig(hosted_detail=HOSTED_DETAIL_DESCRIPTIONS),
            excerpt_loader=lambda name: loader_calls.append(name) or "unused",
        )
        self.assertEqual(result["selected"], "songsee")
        stage1 = [p for p in transport.payloads if p["state"].get("stage") != 2]
        stage2 = [p for p in transport.payloads if p["state"].get("stage") == 2]
        self.assertNotIn(DESCRIPTION_MARKER, json.dumps(stage1))
        self.assertEqual(len(stage2), 1)
        rows = stage2[0]["state"]["skills"]
        self.assertLessEqual(len(rows), DEFAULT_RECHECK_TOP_K)
        self.assertTrue(all("description" in row for row in rows))
        self.assertEqual(loader_calls, [], "descriptions level must not load excerpts")

    def test_excerpt_is_bounded_and_loaded_only_for_finalists(self):
        transport = responder()
        loader_calls: list[str] = []

        def loader(name: str) -> str:
            loader_calls.append(name)
            return "step " * 5000

        run_two_stage(
            task=TASK,
            candidates=catalog(),
            client=client_for(transport),
            config=TwoStageConfig(hosted_detail=HOSTED_DETAIL_EXCERPT, recheck_top_k=2),
            excerpt_loader=loader,
        )
        self.assertLessEqual(len(loader_calls), 2)
        stage2 = [p for p in transport.payloads if p["state"].get("stage") == 2][0]
        for row in stage2["state"]["skills"]:
            self.assertLessEqual(len(row.get("excerpt", "")), MAX_DETAIL_EXCERPT_CHARS)

    def test_restricted_or_failing_detail_is_withheld(self):
        rows, withheld = build_stage2_skills(
            ["a", "b", "c"],
            {
                "a": {"name": "a", "description": "Rotate the admin password on a host"},
                "b": {"name": "b", "description": "Plain public description"},
                "c": {"name": "c", "description": "c"},
            },
            HOSTED_DETAIL_EXCERPT,
            excerpt_loader=lambda name: (_ for _ in ()).throw(OSError("no file")) if name == "b" else "contact me at x@example.com",
        )
        self.assertEqual(rows[0], {"name": "a"})
        self.assertEqual(rows[1], {"name": "b", "description": "Plain public description"})
        self.assertEqual(rows[2], {"name": "c"})
        self.assertEqual(withheld, 3)

    def test_skill_excerpt_strips_frontmatter_and_bounds(self):
        content = "---\nname: x\ndescription: secret frontmatter\n---\n# Title\n\n" + "body " * 1000
        excerpt = skill_excerpt(content) or ""
        self.assertNotIn("frontmatter", excerpt)
        self.assertTrue(excerpt.startswith("# Title body"))
        self.assertLessEqual(len(excerpt), MAX_DETAIL_EXCERPT_CHARS)
        self.assertIsNone(skill_excerpt(None))
        self.assertIsNone(skill_excerpt("---\nunterminated"))

    def test_ack_false_refuses_before_any_request(self):
        transport = responder()
        with self.assertRaises(PermissionError):
            run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport),
                          public_or_sanitized_data_ack=False)
        self.assertEqual(transport.payloads, [])


class EarlyStopTests(unittest.TestCase):
    def test_low_needs_skill_stops_after_one_request(self):
        transport = responder(needs=0.05)
        result = run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport))
        self.assertEqual(len(transport.payloads), 1)
        self.assertIsNone(result["selected"])
        self.assertTrue(result["early_stop"])
        self.assertEqual(result["shortlist_policy"], SHORTLIST_POLICY_TWO_STAGE_EARLY_STOP)
        self.assertIn("needs_skill_early_stop", result["abstention_reason"])
        self.assertEqual(result["request_count"], 1)

    def test_early_stop_question_asks_about_the_whole_catalog(self):
        transport = responder(needs=0.05)
        run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport))
        question = transport.payloads[0]["questions"]["needs_skill"]
        self.assertIn("full catalog", question["instructions"])
        self.assertEqual(transport.payloads[0]["state"]["candidate_count"], 403)

    def test_disabled_early_stop_runs_every_partition(self):
        transport = responder(needs=0.05)
        config = TwoStageConfig(early_stop=False)
        plan = plan_two_stage(TASK, catalog(), config)
        result = run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport), config=config)
        self.assertGreaterEqual(len(transport.payloads), plan.stage1_requests)
        self.assertIsNone(result["selected"])  # final needs gate still applies
        self.assertIn("needs_skill_below_threshold", result["abstention_reason"])


class ExecutionTests(unittest.TestCase):
    def test_request_count_never_exceeds_plan(self):
        for detail in (HOSTED_DETAIL_NAMES, HOSTED_DETAIL_DESCRIPTIONS):
            transport = responder()
            config = TwoStageConfig(hosted_detail=detail)
            plan = plan_two_stage(TASK, catalog(), config)
            result = run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport), config=config)
            self.assertEqual(len(transport.payloads), plan.max_requests)
            self.assertEqual(result["request_count"], len(transport.payloads))
            self.assertEqual(result["shortlist_policy"], SHORTLIST_POLICY_TWO_STAGE)

    def test_partitions_run_concurrently_with_client_pool(self):
        plan = plan_two_stage(TASK, catalog(), TwoStageConfig())
        parties = plan.stage1_requests - 1  # partition 0 runs first for early stop
        self.assertGreaterEqual(parties, 2)
        barrier = threading.Barrier(parties)
        transports = [responder(barrier=barrier) for _ in range(parties + 1)]
        clients = [client_for(item) for item in transports]
        result = run_two_stage(
            task=TASK, candidates=catalog(), client=clients[0], client_pool=clients[1:],
            deadline_seconds=5.0,
        )
        # A broken barrier would raise inside the transport and fail the run.
        self.assertEqual(result["selected"], "songsee")
        self.assertFalse(barrier.broken)
        self.assertEqual(result["parallel_clients"], parties + 1)

    def test_parallel_wall_time_beats_sequential_sum(self):
        delay = 0.3
        transports = [responder(delay=delay) for _ in range(4)]
        clients = [client_for(item) for item in transports]
        started = time.monotonic()
        result = run_two_stage(task=TASK, candidates=catalog(), client=clients[0],
                               client_pool=clients[1:], deadline_seconds=10.0)
        elapsed = time.monotonic() - started
        sequential = result["request_count"] * delay
        self.assertLess(elapsed, sequential - delay * 0.5)

    def test_deadline_bounds_wall_time_and_reports_partial_accounting(self):
        transport = responder(delay=0.4)
        started = time.monotonic()
        with self.assertRaises((DeadlineExceeded, LateResultDiscarded, PartialAccountingError)) as caught:
            run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport),
                          deadline_seconds=0.6)
        self.assertLess(time.monotonic() - started, 1.5)
        if isinstance(caught.exception, PartialAccountingError):
            self.assertGreaterEqual(len(caught.exception.partial), 1)

    def test_host_cancel_propagates_into_workers(self):
        transports = [responder(delay=0.2) for _ in range(3)]
        clients = [client_for(item) for item in transports]
        state = {"calls": 0}

        def cancel() -> bool:
            state["calls"] += 1
            return state["calls"] > 3

        with self.assertRaises((HostCancelled, PartialAccountingError)):
            with host_cancel_scope(cancel):
                run_two_stage(task=TASK, candidates=catalog(), client=clients[0],
                              client_pool=clients[1:], deadline_seconds=5.0)

    def test_stage2_failure_keeps_stage1_accounting(self):
        base = responder()

        def failing(payload: dict) -> dict:
            if payload["state"].get("stage") == 2:
                raise OSError("synthetic stage-2 failure")
            return base(payload)

        with self.assertRaises(PartialAccountingError) as caught:
            run_two_stage(task=TASK, candidates=catalog(), client=client_for(failing))
        plan = plan_two_stage(TASK, catalog(), TwoStageConfig())
        self.assertEqual(len(caught.exception.partial), plan.stage1_requests)

    def test_stage2_none_choice_abstains(self):
        transport = responder(stage2_choice=NONE)
        result = run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport))
        self.assertIsNone(result["selected"])
        self.assertIn("hosted_none_option", result["abstention_reason"])

    def test_single_partition_uses_one_request(self):
        transport = responder()
        result = run_two_stage(task=TASK, candidates=catalog(40, at=7), client=client_for(transport))
        self.assertEqual(len(transport.payloads), 1)
        self.assertEqual(result["selected"], "songsee")
        self.assertEqual(result["shortlist_policy"], SHORTLIST_POLICY_TWO_STAGE_SINGLE)

    def test_result_feeds_a_valid_privacy_safe_receipt(self):
        transport = responder()
        hosted = run_two_stage(task=TASK, candidates=catalog(), client=client_for(transport),
                               config=TwoStageConfig(hosted_detail=HOSTED_DETAIL_DESCRIPTIONS))
        result = {
            "status": "selected", "selected": hosted["selected"], "source": "jev",
            "hosted_attempted": True, "candidate_count": 403, "cache_hit": False,
            "routing_mode": "hosted_sanitized",
        }
        _copy_redacted_jev_metadata(result, hosted)
        receipt = build_routing_receipt(result)
        self.assertTrue(receipt_state.validate_receipt(receipt))
        self.assertEqual(receipt["terminal_state"], "hosted_selection")
        self.assertEqual(receipt["request_count"], hosted["request_count"])
        self.assertEqual(receipt["shortlist_policy"], SHORTLIST_POLICY_TWO_STAGE)
        self.assertNotIn(DESCRIPTION_MARKER, json.dumps(receipt))
        self.assertNotIn(TASK, json.dumps(receipt))


class OfflineBenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "evaluation" / "two_stage"))
        try:
            import benchmark as two_stage_benchmark  # type: ignore[import-not-found]
        finally:
            sys.path.pop(0)
        cls.module = two_stage_benchmark
        cls.report = two_stage_benchmark.run(two_stage_benchmark.load_fixtures())

    def test_report_is_marked_simulation_and_not_claimable(self):
        self.assertTrue(self.report["simulation"])
        self.assertFalse(self.report["claimable"])
        self.assertEqual(set(self.report["metrics"]), set(self.module.ARMS))

    def test_names_only_arms_send_no_detail_and_detailed_arms_do(self):
        metrics = self.report["metrics"]
        self.assertEqual(metrics["names_full_fan_out"]["description_bytes_sent"], 0)
        self.assertEqual(metrics["two_stage_names"]["description_bytes_sent"], 0)
        self.assertGreater(metrics["two_stage_descriptions"]["description_bytes_sent"], 0)

    def test_two_stage_uses_fewer_sequential_requests_than_full_fan_out(self):
        metrics = self.report["metrics"]
        for arm in ("two_stage_names", "two_stage_descriptions"):
            self.assertLess(
                metrics[arm]["critical_path_requests_mean"],
                metrics["names_full_fan_out"]["critical_path_requests_mean"],
            )
            self.assertLessEqual(metrics[arm]["requests_max"], metrics["names_full_fan_out"]["requests_max"])

    def test_every_case_has_one_record_per_arm(self):
        cases = {case["id"] for case in self.module.load_fixtures()["cases"]}
        for rows in self.report["records"].values():
            self.assertEqual([row["case_id"] for row in rows].__len__(), len(cases))
            self.assertEqual({row["case_id"] for row in rows}, cases)

    def test_fixtures_are_public_synthetic_and_labels_resolve(self):
        fixtures = self.module.load_fixtures()
        self.assertTrue(fixtures["public_synthetic"])
        names = {item["name"] for item in self.module.build_catalog(fixtures)}
        self.assertEqual(len(names), fixtures["catalog_size"])
        for case in fixtures["cases"]:
            if case["kind"] == "positive":
                self.assertIn(case["expected"], names)


if __name__ == "__main__":
    unittest.main()
