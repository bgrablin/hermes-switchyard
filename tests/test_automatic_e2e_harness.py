"""Regression checks for automatic evaluation evidence classification."""
from __future__ import annotations

import unittest

from evaluation.automatic_e2e.harness import _skill_load_metrics


class AutomaticEvaluationHarnessTests(unittest.TestCase):
    def test_skill_load_metrics_requires_exact_expected_identifier(self):
        correct, irrelevant = _skill_load_metrics(
            ["docker-management-unsafe", "docker-management"],
            "docker-management",
        )
        self.assertTrue(correct)
        self.assertEqual(irrelevant, ["docker-management-unsafe"])

    def test_skill_load_metrics_reports_nonmatching_identifier(self):
        correct, irrelevant = _skill_load_metrics(
            ["docker-management-unsafe"],
            "docker-management",
        )
        self.assertFalse(correct)
        self.assertEqual(irrelevant, ["docker-management-unsafe"])


if __name__ == "__main__":
    unittest.main()
