"""Regression checks for automatic evaluation evidence classification."""
from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest import mock

from evaluation.automatic_e2e import harness
from evaluation.automatic_e2e.harness import _forbidden_tool_calls, _skill_load_metrics


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

    def test_forbidden_tool_calls_include_browser_and_web_tools(self):
        calls = [
            {"name": "browser_exec"},
            {"name": "web_search"},
            {"name": "web_extract"},
            {"name": "skill_view"},
        ]
        self.assertEqual(
            _forbidden_tool_calls(calls),
            ["browser_exec", "web_search", "web_extract"],
        )

    def test_observer_candidate_flags_ignore_system_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(harness, "_copy_skill"):
                home, _ = harness._prepare_home(Path.cwd(), Path(directory), True)
            namespace = {}
            source = home / "plugins" / "auto-e2e-observer" / "__init__.py"
            exec(compile(source.read_text(encoding="utf-8"), str(source), "exec"), namespace)
            messages = [
                {"role": "system", "content": "docker-management network-printer-operations"},
                {"role": "user", "content": "Diagnose the container"},
            ]
            namespace["on_pre_api_request"](request_messages=messages)
            record = namespace["records"][-1]
            self.assertFalse(record["recommendation_present"])
            self.assertFalse(record["docker_recommendation_present"])

            messages[-1]["content"] += "\nAdvisory skill recommendation: docker-management"
            namespace["on_pre_api_request"](request_messages=messages)
            record = namespace["records"][-1]
            self.assertTrue(record["docker_recommendation_present"])
            self.assertFalse(record["printer_recommendation_present"])


if __name__ == "__main__":
    unittest.main()
