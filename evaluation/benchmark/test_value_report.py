import copy
import json
import tempfile
import unittest
from pathlib import Path

import benchmark
from collector_common import provenance
import value_report

PLUGIN = Path(__file__).resolve().parents[2]


class ValueReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book, cls.meta = benchmark.load_book()
        cls.routing, _client, cls.source = benchmark.import_plugin(PLUGIN)

    def _live_switchyard_payload(self):
        rows = []
        for case in self.book["heldout_fixtures"]:
            row = copy.deepcopy(
                benchmark.run_offline_case(case, self.meta, self.routing, self.source)["switchyard"]
            )
            row.update({
                "simulated": False,
                "actual_call": True,
                "measurement_status": "ok",
                "error": None,
                "provider": "openrouter",
                "provider_call_ms": 4.0,
                "wall_ms": 5.0,
                "timing_status": "actual_provider_observation",
            })
            row["measurement_provenance"] = provenance(
                arm="switchyard",
                collector=benchmark.LIVE_COLLECTORS["switchyard"],
                case=case,
                meta=self.meta,
                provider_calls=row["provider_call_count"],
                successful=True,
                wall_observed=True,
                provider_time_observed=True,
                usage_observed=False,
            )
            rows.append(row)
        return {
            "schema_version": 1,
            "measurement_schema_version": benchmark.MEASUREMENT_SCHEMA_VERSION,
            "collection_mode": "live",
            "arm": "switchyard",
            "dataset_hash": self.meta["dataset_hash"],
            "candidate_catalog_hash": self.meta["catalog_hash"],
            "public_synthetic_ack": True,
            "records": rows,
        }

    def test_live_value_report_compares_real_switchyard_receipts_to_local_baseline(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            path = Path(temp_dir) / "switchyard.json"
            path.write_text(json.dumps(self._live_switchyard_payload()), encoding="utf-8")
            report = value_report.build_report(
                plugin_path=PLUGIN,
                switchyard_input=path,
                public_synthetic_ack=True,
                max_requests=24,
            )
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["claim_level"], "live_selector_value")
        self.assertTrue(report["switchyard"]["timing"]["provider_timing_claimable"])
        self.assertGreater(
            report["paired_deltas"]["strict_top1_accuracy"],
            0,
        )
        self.assertEqual(report["switchyard"]["no_fit"]["false_positives"], 0)
        self.assertEqual(report["provider_call_count"], 24)
        self.assertEqual(report["report_hash"], value_report.report_hash(report))

    def test_value_report_refuses_missing_ack_or_partial_case_set(self):
        payload = self._live_switchyard_payload()
        payload["records"].pop()
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temp_dir:
            path = Path(temp_dir) / "switchyard.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaises(ValueError):
                value_report.build_report(
                    plugin_path=PLUGIN,
                    switchyard_input=path,
                    public_synthetic_ack=True,
                    max_requests=24,
                )
        with self.assertRaises(ValueError):
            value_report.build_report(
                plugin_path=PLUGIN,
                switchyard_input=Path("unused"),
                public_synthetic_ack=False,
                max_requests=24,
            )


if __name__ == "__main__":
    unittest.main()
