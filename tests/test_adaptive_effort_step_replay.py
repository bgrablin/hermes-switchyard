"""Offline checks for the step-level adaptive effort replay (evaluation/adaptive_effort_step_replay.py)."""
from __future__ import annotations

import json
import unittest

from evaluation import adaptive_effort_step_replay as step_replay


class StepReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.book = step_replay.load_fixtures()
        cls.report = step_replay.build_report(cls.book)

    def test_fixture_is_frozen_and_aggregate_only(self):
        self.assertEqual(sum(self.book["turn_request_counts"]), 2000)
        self.assertTrue(all(isinstance(value, int) for value in self.book["jev_latency_ms"]))
        text = json.dumps(self.book)
        for marker in ("session_id", "turn_id", "current_request", "read_file"):
            self.assertNotIn(marker, text)

    def test_never_above_cap_and_never_more_than_one_level_below(self):
        for run in self.report["runs"]:
            self.assertEqual(run["above_cap"], 0, run["scenario"])
            self.assertEqual(run["below_cap_minus_one"], 0, run["scenario"])

    def test_extra_calls_are_bounded(self):
        for run in self.report["runs"]:
            self.assertLessEqual(run["extra_jev_calls_per_turn"]["max"], 4, run["scenario"])

    def test_off_switch_makes_no_step_asks(self):
        off = [run for run in self.report["runs"] if not run["step_adaptation"]]
        self.assertEqual(len(off), 1)
        self.assertEqual((off[0]["step_asks"], off[0]["lowered_steps"]), (0, 0))

    def test_report_is_deterministic(self):
        self.assertEqual(step_replay.build_report(self.book), self.report)


if __name__ == "__main__":
    unittest.main()
