import json
import copy
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import benchmark
from collector_common import provenance


# The benchmark lives under evaluation/benchmark; the plugin is the repository package.
PLUGIN = Path(__file__).resolve().parents[2]


class BenchmarkContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book, cls.meta = benchmark.load_book()
        cls.routing, _client, cls.source = benchmark.import_plugin(PLUGIN)

    def test_frozen_balanced_public_heldout_book(self):
        heldout = self.book["heldout_fixtures"]
        self.assertEqual(len(heldout), 24)
        counts = {category: sum(case["category"] == category for case in heldout) for category in benchmark.CATEGORIES}
        self.assertEqual(counts, {category: 6 for category in benchmark.CATEGORIES})
        self.assertTrue(self.book["label_frozen"])
        self.assertGreaterEqual(len(self.book["candidate_catalog"]), 8)
        self.assertEqual(len(self.book["development_fixtures"]), 4)

    def test_offline_uses_actual_plugin_and_has_three_complete_arms(self):
        rows = {
            case["id"]: benchmark.run_offline_case(case, self.meta, self.routing, self.source)
            for case in self.book["heldout_fixtures"]
        }
        self.assertEqual(len(rows), 24)
        for arms in rows.values():
            self.assertEqual(set(arms), {"lexical", "luna", "switchyard"})
            self.assertEqual(arms["switchyard"]["selector_invocation_count"], 1)
            self.assertEqual(arms["switchyard"]["provider_call_count"], 1)
            self.assertTrue(arms["switchyard"]["simulated"])
            self.assertTrue(all(value is None for value in arms["luna"]["usage"].values()))

    def test_offline_report_scores_without_claiming_timing(self):
        rows = {case["id"]: benchmark.run_offline_case(case, self.meta, self.routing, self.source)
                for case in self.book["heldout_fixtures"]}
        report = benchmark.summarize(
            self.book, self.meta,
            {arm: {cid: rows[cid][arm] for cid in rows} for arm in ("lexical", "luna", "switchyard")},
            mode="offline", source=self.source,
        )
        self.assertEqual(report["status"], "ok")
        self.assertFalse(report["timing_claims_allowed"])
        self.assertEqual(report["heldout_case_count"], 24)
        self.assertEqual(report["arms"]["switchyard"]["no_fit"]["cases"], 6)
        self.assertEqual(report["arms"]["lexical"]["timing"]["provider_timing_observation_count"], 0)

    def test_missing_case_refuses_comparative_summary(self):
        rows = {case["id"]: benchmark.run_offline_case(case, self.meta, self.routing, self.source)
                for case in self.book["heldout_fixtures"]}
        arms = {arm: {cid: rows[cid][arm] for cid in rows} for arm in ("lexical", "luna", "switchyard")}
        del arms["luna"][next(iter(arms["luna"]))]
        with self.assertRaises(benchmark.BenchmarkRefusal):
            benchmark.summarize(self.book, self.meta, arms, mode="offline", source=self.source)

    def test_live_mode_requires_ack_cap_and_normalized_inputs(self):
        parser = benchmark.argparse.ArgumentParser()
        # Exercise the actual guard through run(), without provider calls.
        args = parser.parse_args([])
        args.mode = "live"
        args.dataset = "heldout"
        args.plugin_path = str(PLUGIN)
        args.luna_input = None
        args.switchyard_input = None
        args.lexical_input = None
        args.public_synthetic_ack = False
        args.max_requests = None
        args.output = None
        with self.assertRaises(ValueError):
            benchmark.run(args)

    def test_prompt_is_public_and_simulations_are_not_usage_claims(self):
        self.assertIn("Return only this JSON object", (Path(benchmark.PROMPT)).read_text(encoding="utf-8"))
        for case in self.book["heldout_fixtures"]:
            self.assertNotIn("usage", case["offline_simulation"]["luna"])

    def test_live_ingest_rejects_copied_offline_records(self):
        rows = {case["id"]: benchmark.run_offline_case(case, self.meta, self.routing, self.source)["luna"]
                for case in self.book["heldout_fixtures"]}
        payload = {
            "schema_version": 1,
            "arm": "luna",
            "collection_mode": "live",
            "dataset_hash": self.meta["dataset_hash"],
            "candidate_catalog_hash": self.meta["catalog_hash"],
            "public_synthetic_ack": True,
            "records": list(rows.values()),
        }
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            path = Path(temp_dir) / "copied.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                benchmark.ingest(path, "luna", self.book["heldout_fixtures"], self.meta, live=True, max_requests=24)

    def test_summary_requires_exact_arm_source_and_dataset_targets(self):
        rows = {case["id"]: benchmark.run_offline_case(case, self.meta, self.routing, self.source)
                for case in self.book["heldout_fixtures"]}
        arms = {arm: {cid: rows[cid][arm] for cid in rows} for arm in ("lexical", "luna", "switchyard")}
        arms["luna"][next(iter(arms["luna"]))]["source_hash"] = self.source["benchmark"]
        with self.assertRaises(benchmark.BenchmarkRefusal):
            benchmark.summarize(self.book, self.meta, arms, mode="offline", source=self.source)

        arms = {arm: {cid: rows[cid][arm] for cid in rows} for arm in ("lexical", "luna", "switchyard")}
        for row in arms["luna"].values():
            row["dataset_hash"] = "wrong-dataset"
        with self.assertRaises(benchmark.BenchmarkRefusal):
            benchmark.summarize(self.book, self.meta, arms, mode="offline", source=self.source)

    def test_abstention_reason_is_typed_and_bounded(self):
        case = self.book["heldout_fixtures"][0]
        row = copy.deepcopy(benchmark.run_offline_case(case, self.meta, self.routing, self.source)["luna"])
        row["abstention_reason"] = {"unexpected": "object"}
        with self.assertRaises(ValueError):
            benchmark.validate_record(row, "luna", case, self.meta, live=False)
        row["abstention_reason"] = "x" * (benchmark.MAX_ABSTENTION_REASON_CHARS + 1)
        with self.assertRaises(ValueError):
            benchmark.validate_record(row, "luna", case, self.meta, live=False)

    def test_switchyard_request_and_collector_identity_are_not_luna_prompt_identity(self):
        case = self.book["heldout_fixtures"][0]
        rows = benchmark.run_offline_case(case, self.meta, self.routing, self.source)
        self.assertEqual(rows["luna"]["request_identity"]["template_hash"], self.meta["template_hash"])
        self.assertIsNone(rows["switchyard"]["request_identity"]["template_hash"])
        self.assertNotEqual(rows["luna"]["request_hash"], rows["switchyard"]["request_hash"])
        self.assertEqual(rows["switchyard"]["collector_source_hash"], self.meta["collector_hashes"]["switchyard"])
        rows["switchyard"]["collector_source_hash"] = "wrong-collector"
        with self.assertRaises(ValueError):
            benchmark.validate_record(rows["switchyard"], "switchyard", case, self.meta, live=False)

    def test_live_collector_fingerprints_include_shared_benchmark_contract(self):
        for files in benchmark.COLLECTOR_SOURCE_FILES.values():
            self.assertIn("benchmark.py", files)

    def test_plugin_fingerprint_includes_root_entrypoint(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            plugin = Path(temp_dir)
            package = plugin / "jev_decision"
            package.mkdir()
            (package / "routing.py").write_text("ROUTE = 1\n", encoding="utf-8")
            (plugin / "plugin.yaml").write_text("name: test\n", encoding="utf-8")
            entrypoint = plugin / "__init__.py"
            entrypoint.write_text("REGISTER = 1\n", encoding="utf-8")

            before = benchmark.source_hashes(plugin)
            self.assertIn("__init__.py", before["plugin_files"])

            entrypoint.write_text("REGISTER = 2\n", encoding="utf-8")
            after = benchmark.source_hashes(plugin)
            self.assertNotEqual(before["plugin"], after["plugin"])

    def test_live_validation_enforces_collector_specific_timing_status(self):
        case = self.book["heldout_fixtures"][0]
        row = copy.deepcopy(benchmark.run_offline_case(case, self.meta, self.routing, self.source)["switchyard"])
        row.update({
            "simulated": False,
            "actual_call": True,
            "measurement_status": "ok",
            "provider": "openrouter",
            "wall_ms": 10.0,
            "provider_call_ms": 4.0,
            "timing_status": "actual_end_to_end_process",
            "error": None,
        })
        row["measurement_provenance"] = provenance(
            arm="switchyard",
            collector=benchmark.LIVE_COLLECTORS["switchyard"],
            case=case,
            meta=self.meta,
            provider_calls=1,
            successful=True,
            wall_observed=True,
            provider_time_observed=True,
            usage_observed=False,
        )
        with self.assertRaises(ValueError):
            benchmark.validate_record(row, "switchyard", case, self.meta, live=True)

    def test_failed_contract_can_record_an_observed_provider_response(self):
        case = self.book["heldout_fixtures"][0]
        row = copy.deepcopy(benchmark.run_offline_case(case, self.meta, self.routing, self.source)["luna"])
        row.update({
            "status": "failed",
            "selected": None,
            "selected_skills": [],
            "simulated": False,
            "actual_call": True,
            "measurement_status": "failed",
            "wall_ms": 10.0,
            "provider_call_ms": None,
            "timing_status": "provider_error",
            "usage": {key: None for key in row["usage"]},
            "error": {"type": "ValueError"},
        })
        row["measurement_provenance"] = {
            **provenance(
                arm="luna",
                collector=benchmark.LIVE_COLLECTORS["luna"],
                case=case,
                meta=self.meta,
                provider_calls=1,
                successful=False,
                wall_observed=True,
                provider_time_observed=False,
                usage_observed=False,
            ),
            "provider_response_observed": True,
        }
        benchmark.validate_record(row, "luna", case, self.meta, live=True)

    def test_switchyard_ingest_rejects_multiple_selected_skills(self):
        rows = {case["id"]: benchmark.run_offline_case(case, self.meta, self.routing, self.source)["switchyard"]
                for case in self.book["heldout_fixtures"]}
        row = next(row for row in rows.values() if row["status"] == "selected")
        row["selected_skills"] = [row["selected"], next(name for name in self.meta["names"] if name != row["selected"])]
        payload = {
            "schema_version": 1,
            "arm": "switchyard",
            "dataset_hash": self.meta["dataset_hash"],
            "candidate_catalog_hash": self.meta["catalog_hash"],
            "records": list(rows.values()),
        }
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            path = Path(temp_dir) / "multiple.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                benchmark.ingest(path, "switchyard", self.book["heldout_fixtures"], self.meta, live=False, max_requests=None)

    def test_luna_strict_top1_does_not_reward_a_multi_selection(self):
        case = next(case for case in self.book["heldout_fixtures"] if case["expected"]["label_type"] == "single")
        row = copy.deepcopy(benchmark.run_offline_case(case, self.meta, self.routing, self.source)["luna"])
        row["selected_skills"].append(next(name for name in self.meta["names"] if name != row["selected"]))
        result = benchmark.outcome(case, row)
        self.assertFalse(result["top1_correct"])

    def test_required_set_over_selection_is_not_complete(self):
        case = next(case for case in self.book["heldout_fixtures"] if case["expected"]["label_type"] == "required_set")
        row = copy.deepcopy(benchmark.run_offline_case(case, self.meta, self.routing, self.source)["luna"])
        row.update({
            "status": "selected",
            "selected": case["expected"]["required_skills"][0],
            "selected_skills": list(self.meta["names"]),
        })
        result = benchmark.outcome(case, row)
        self.assertEqual(result["coverage"], 1.0)
        self.assertFalse(result["required_set_complete"])
        self.assertTrue(result["positive_miss"])

    def test_partial_provider_timing_is_null_with_coverage(self):
        rows = [copy.deepcopy(benchmark.run_offline_case(case, self.meta, self.routing, self.source)["switchyard"])
                for case in self.book["heldout_fixtures"][:2]]
        for index, row in enumerate(rows):
            row.update({"measurement_status": "ok", "actual_call": True, "simulated": False})
            row["measurement_provenance"] = {
                "wall_time_observed": True,
                "provider_time_observed": index == 0,
            }
        rows[0]["provider_call_ms"] = 10.0
        rows[1]["provider_call_ms"] = None
        summary = benchmark.timing_summary(rows, "live")
        self.assertIsNone(summary["total_provider_call_ms"])
        self.assertEqual(summary["provider_timing_observation_count"], 1)
        self.assertEqual(summary["provider_timing_case_count"], 2)
        self.assertFalse(summary["provider_timing_coverage_complete"])

    def test_luna_prompt_contains_request_data_but_not_ground_truth(self):
        case = self.book["heldout_fixtures"][0]
        prompt = benchmark.render_luna_prompt(case, self.meta)
        self.assertIn(case["task"], prompt)
        self.assertIn(case["expected"]["selected"], prompt)
        self.assertNotIn('"label_type"', prompt)
        self.assertNotIn('"offline_simulation"', prompt)
        mutated = copy.deepcopy(case)
        mutated["task"] += " Mutated after the frozen request was hashed."
        with self.assertRaises(ValueError):
            benchmark.render_luna_prompt(mutated, self.meta)

    def test_live_success_requires_an_observed_provider_response(self):
        case = self.book["heldout_fixtures"][0]
        row = copy.deepcopy(benchmark.run_offline_case(case, self.meta, self.routing, self.source)["switchyard"])
        row.update({
            "simulated": False,
            "actual_call": True,
            "measurement_status": "ok",
            "provider": "openrouter",
            "wall_ms": 10.0,
            "provider_call_ms": 4.0,
            "timing_status": "actual_provider_observation",
            "error": None,
        })
        row["measurement_provenance"] = {
            **provenance(
                arm="switchyard",
                collector=benchmark.LIVE_COLLECTORS["switchyard"],
                case=case,
                meta=self.meta,
                provider_calls=1,
                successful=True,
                wall_observed=True,
                provider_time_observed=True,
                usage_observed=False,
            ),
            "provider_response_observed": False,
        }
        with self.assertRaises(ValueError):
            benchmark.validate_record(row, "switchyard", case, self.meta, live=True)

    def test_ingest_rejects_non_object_json_root(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            path = Path(temp_dir) / "array.json"
            path.write_text("[]", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "arm_input_root"):
                benchmark.ingest(path, "lexical", self.book["heldout_fixtures"], self.meta, live=False, max_requests=None)

    def test_multi_skill_capability_excludes_ambiguous_cases(self):
        rows = {case["id"]: benchmark.run_offline_case(case, self.meta, self.routing, self.source)
                for case in self.book["heldout_fixtures"]}
        report = benchmark.summarize(
            self.book, self.meta,
            {arm: {cid: rows[cid][arm] for cid in rows} for arm in ("lexical", "luna", "switchyard")},
            mode="offline", source=self.source,
        )
        required_count = sum(case["expected"]["label_type"] == "required_set" for case in self.book["heldout_fixtures"])
        ambiguous_count = sum(case["expected"]["label_type"] == "ambiguous" for case in self.book["heldout_fixtures"])
        self.assertGreater(ambiguous_count, 0)
        self.assertEqual(report["arms"]["switchyard"]["multi_skill_capability"]["cases"], required_count)

    def test_ambiguous_case_rejects_multiple_selected_skills(self):
        case = {
            "expected": {
                "label_type": "ambiguous",
                "acceptable_skills": ["docker-management", "kubernetes-patterns"],
            },
        }
        row = {
            "status": "selected",
            "selected": None,
            "selected_skills": ["docker-management", "kubernetes-patterns"],
            "fixture_hash": "fixture",
            "request_hash": "request",
        }
        result = benchmark.outcome(case, row)
        self.assertFalse(result["ambiguous_hit"])
        self.assertTrue(result["positive_miss"])

    def test_output_cannot_overwrite_input_evidence(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            receipt = Path(temp_dir) / "luna.json"
            receipt.write_text('{"retained": true}\n', encoding="utf-8")
            args = benchmark.argparse.Namespace(
                mode="live", dataset="heldout", plugin_path=str(PLUGIN), lexical_input=None,
                luna_input=str(receipt), switchyard_input="switchyard.json",
                public_synthetic_ack=True, max_requests=48, output=receipt,
            )
            with mock.patch.object(benchmark.argparse.ArgumentParser, "parse_args", return_value=args):
                self.assertEqual(benchmark.main(), 2)
            self.assertEqual(receipt.read_text(encoding="utf-8"), '{"retained": true}\n')

    def test_refusal_output_creates_parent_directory(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            output = Path(temp_dir) / "nested" / "refusal.json"
            args = benchmark.argparse.Namespace(
                mode="live", dataset="heldout", plugin_path=str(PLUGIN), lexical_input=None,
                luna_input=None, switchyard_input=None, public_synthetic_ack=False,
                max_requests=None, output=output,
            )
            with mock.patch.object(benchmark.argparse.ArgumentParser, "parse_args", return_value=args):
                self.assertEqual(benchmark.main(), 2)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["status"], "refused")

    def test_live_run_computes_local_lexical_arm_and_claims_only_provider_timing(self):
        paths = {}
        payloads = {}
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            for arm in ("luna", "switchyard"):
                rows = []
                for case in self.book["heldout_fixtures"]:
                    row = copy.deepcopy(benchmark.run_offline_case(case, self.meta, self.routing, self.source)[arm])
                    row.update({"simulated": False, "actual_call": True, "measurement_status": "ok", "error": None, "wall_ms": 10.0})
                    if arm == "switchyard":
                        row["provider"] = "openrouter"
                        row["provider_call_ms"] = 4.0
                        row["timing_status"] = "actual_provider_observation"
                    else:
                        row["provider"] = "openai-codex"
                        row["timing_status"] = "actual_end_to_end_process"
                    row["measurement_provenance"] = provenance(
                        arm=arm,
                        collector=benchmark.LIVE_COLLECTORS[arm],
                        case=case,
                        meta=self.meta,
                        provider_calls=row["provider_call_count"],
                        successful=True,
                        wall_observed=row["wall_ms"] is not None,
                        provider_time_observed=row["provider_call_ms"] is not None,
                        usage_observed=False,
                    )
                    rows.append(row)
                payload = {
                    "schema_version": 1,
                    "measurement_schema_version": benchmark.MEASUREMENT_SCHEMA_VERSION,
                    "collection_mode": "live",
                    "arm": arm,
                    "dataset_hash": self.meta["dataset_hash"],
                    "candidate_catalog_hash": self.meta["catalog_hash"],
                    "public_synthetic_ack": True,
                    "records": rows,
                }
                if arm == "luna":
                    payload["hermes_runtime_identity"] = "Hermes Agent test"
                payloads[arm] = payload
                path = Path(temp_dir) / f"{arm}.json"
                path.write_text(json.dumps(payload), encoding="utf-8")
                paths[arm] = str(path)
            args = benchmark.argparse.Namespace(
                mode="live", dataset="heldout", plugin_path=str(PLUGIN), lexical_input=None,
                luna_input=paths["luna"], switchyard_input=paths["switchyard"],
                public_synthetic_ack=True, max_requests=48, output=None,
            )
            luna_path = Path(paths["luna"])
            luna_payload = copy.deepcopy(payloads["luna"])
            del luna_payload["hermes_runtime_identity"]
            luna_path.write_text(json.dumps(luna_payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "hermes_runtime_identity_type"):
                benchmark.ingest(
                    luna_path,
                    "luna",
                    self.book["heldout_fixtures"],
                    self.meta,
                    live=True,
                    max_requests=24,
                )
            luna_path.write_text(json.dumps(payloads["luna"]), encoding="utf-8")
            report = benchmark.run(args)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(set(report["arms"]), benchmark.COMPARATIVE_ARMS)
        self.assertTrue(report["timing_claims_allowed"])
        self.assertFalse(report["arms"]["lexical"]["timing"]["claimable"])
        self.assertEqual(report["provider_call_count_total"], 48)

        luna_rows = copy.deepcopy(payloads["luna"]["records"])
        luna_rows[0]["wall_ms"] = None
        luna_rows[0]["measurement_provenance"]["wall_time_observed"] = False
        arms = {
            "lexical": {
                case["id"]: benchmark.run_offline_case(case, self.meta, self.routing, self.source)["lexical"]
                for case in self.book["heldout_fixtures"]
            },
            "luna": {row["case_id"]: row for row in luna_rows},
            "switchyard": {
                row["case_id"]: row
                for row in payloads["switchyard"]["records"]
            },
        }
        report = benchmark.summarize(self.book, self.meta, arms, mode="live", source=self.source)
        self.assertFalse(report["arms"]["luna"]["timing"]["claimable"])
        self.assertFalse(report["timing_claims_allowed"])


if __name__ == "__main__":
    unittest.main()
