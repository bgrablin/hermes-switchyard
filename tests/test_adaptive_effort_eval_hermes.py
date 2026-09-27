"""Run the adaptive-effort harness through real Hermes middleware (issue #121).

GREEN: plumbing gates. The effort is read from the provider SDK wire payload
after Hermes ``llm_request`` middleware, never from a TUI label or receipt.

Contrast: same-length routine and consequential tasks must reach Jev as
different states. This was an expected failure on base ebe1409 (metadata-only
state). The issue #121 adapter sends the bounded current request, so the test
is now a plain assertion.

The Jev responder is the offline ``synthetic_lowest`` responder. These tests
make no accuracy claim.
"""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

from evaluation import adaptive_effort_eval as harness

ROOT = Path(__file__).resolve().parent.parent
HERMES_AVAILABLE = all(importlib.util.find_spec(name) is not None
                       for name in ("hermes_cli", "agent", "anthropic", "openai", "httpx"))


@unittest.skipUnless(HERMES_AVAILABLE, "Hermes runtime is not importable")
class AdaptiveEffortHermesEvaluationTests(unittest.TestCase):
    report: dict

    @classmethod
    def setUpClass(cls):
        cls.report = harness.run_evaluation(
            plugin_parent=ROOT, fixtures_path=ROOT / "evaluation" / "adaptive_effort_fixtures.json",
            split="dev", responder="synthetic_lowest", max_jev_calls=128, timeout=300)

    def test_run_completes_and_measures_every_fixture_on_every_route(self):
        self.assertEqual(self.report["status"], "ok", self.report.get("error"))
        book = harness.load_fixture_book(ROOT / "evaluation" / "adaptive_effort_fixtures.json")
        dev = [item for item in book["fixtures"] if item["split"] == "dev"]
        self.assertEqual(len(self.report["fixtures"]), len(dev) * len(book["routes"]))
        self.assertFalse(self.report["jev_cap_reached"])
        self.assertFalse(self.report["summary"]["accuracy_claim_allowed"])

    def test_effort_is_read_from_provider_wire_field(self):
        for record in self.report["fixtures"]:
            for request in record["requests"]:
                self.assertIn(request["requested_wire"], harness.LEVEL_ORDER, record["id"])
                self.assertIn(request["sent_wire"], harness.LEVEL_ORDER, record["id"])
            self.assertNotEqual(record["outcome"], "unmeasured", record["id"])
            self.assertNotIn("wire_effort_missing", record["missing_evidence"], record["id"])

    def test_plumbing_gates_are_green(self):
        gates = self.report["acceptance"]["gates"]
        for gate in ("never_above_cap", "no_sidecar_or_tool_leak", "prompt_bytes_unchanged"):
            self.assertEqual(gates[gate], "GREEN", gate)
        self.assertEqual(gates["holdout_safety"], "NOT_MEASURED")

    def test_jev_calls_carry_usage_latency_and_request_id(self):
        calls = [call for record in self.report["fixtures"] for call in record["jev"]["calls"]]
        self.assertTrue(calls)
        for call in calls:
            self.assertTrue(call["ok"])
            self.assertEqual(call["response_source"], "synthetic_lowest")
            self.assertIsInstance(call["usage"].get("total_tokens"), (int, float))
            self.assertIsInstance(call["wire_latency_ms"], (int, float))
            self.assertTrue(call["request_id"])

    def test_same_length_routine_and_consequential_tasks_are_distinguishable(self):
        contrasts = self.report["contrasts"]
        self.assertGreater(contrasts["measurable"], 0)
        self.assertTrue(contrasts["all_distinguishable"], contrasts["groups"])


if __name__ == "__main__":
    unittest.main()
