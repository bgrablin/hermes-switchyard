"""Offline skill-routability command and frozen synthetic evaluation."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import socket
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import hermes_switchyard as plugin
from hermes_switchyard import skill_lint

FIXTURE = Path(__file__).resolve().parents[1] / "evaluation" / "lint-skills" / "fixture.json"


class SkillLintTests(unittest.TestCase):
    def _run_cli(self, catalog, *options):
        skills_tool = types.ModuleType("tools.skills_tool")
        setattr(skills_tool, "skills_list", mock.Mock(return_value=json.dumps({"success": True, "skills": catalog})))
        parser = argparse.ArgumentParser()
        plugin._setup_cli(parser)
        args = parser.parse_args(["lint-skills", *options])
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"tools.skills_tool": skills_tool}):
            with contextlib.redirect_stdout(output):
                code = args.func(args)
        skills_tool.skills_list.assert_called_once_with()  # type: ignore[attr-defined]
        return code, output.getvalue()

    def test_oversized_catalog_refuses_partial_report(self):
        oversized = json.loads(FIXTURE.read_text(encoding="utf-8"))["oversized"]
        rows = [
            {"name": f"{oversized['name_prefix']}{index}", "description": oversized["description"]}
            for index in range(oversized["count"])
        ]
        code, text = self._run_cli(rows, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text), {
            "schema": "switchyard.lint_skills.v1", "status": "unavailable", "reason": "catalog_too_large",
        })
        self.assertNotIn("synthetic-", text)

    def test_oversized_serialized_response_is_refused_before_decoding(self):
        skills_tool = types.ModuleType("tools.skills_tool")
        marker = "OVERSIZED_CONTENT_CANARY_NOT_FOR_EXPORT"
        huge = '{"success":true,"skills":[],"content":"' + marker + ("x" * (8 * 1024 * 1024)) + '"}'
        setattr(skills_tool, "skills_list", lambda: huge)
        parser = argparse.ArgumentParser()
        plugin._setup_cli(parser)
        args = parser.parse_args(["lint-skills", "--json"])
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"tools.skills_tool": skills_tool}):
            with contextlib.redirect_stdout(output):
                code = args.func(args)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["reason"], "catalog_too_large")
        self.assertNotIn(marker, output.getvalue())

    def test_dense_catalog_refuses_unbounded_pair_report(self):
        catalog = [
            {"name": f"dense-{index:03d}", "description": "Use when checking synthetic reference entries and counting identical labels."}
            for index in range(100)
        ]
        code, text = self._run_cli(catalog, "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(text)["reason"], "catalog_too_confusable")
        self.assertNotIn("dense-", text)

    def test_text_report_stable_order_and_private_fields_absent(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        code, text = self._run_cli(fixture["catalog"])
        code_reversed, reversed_text = self._run_cli(list(reversed(fixture["catalog"])))
        self.assertEqual((code, code_reversed), (0, 0))
        self.assertEqual(text, reversed_text)
        self.assertIn("certificate-alpha: confusable with certificate-beta", text)
        self.assertIn("tiny-description: short_description, missing_use_when", text)
        self.assertIn("invalid_rows: 1", text)
        self.assertNotIn('"schema"', text)
        for canary in fixture["export_canaries"]:
            self.assertNotIn(canary, text)

    def test_frozen_catalog_finds_planted_pairs_and_named_peers(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report = skill_lint.lint_catalog(fixture["catalog"])
        self.assertEqual(report["counts"]["invalid_rows"], 1)
        self.assertEqual(report["counts"]["skills"], 12)
        actual = {"|".join(pair["names"]): pair["kind"] for pair in report["pairs"]}
        self.assertEqual(actual, fixture["truth_kinds"])
        findings = {entry["name"]: entry for entry in report["findings"]}
        for name, issues in fixture["description_issues"].items():
            if "invalid_row" not in issues:
                self.assertEqual(findings[name]["issues"], issues)
        for name in fixture["distinct_names"]:
            self.assertFalse(findings.get(name, {}).get("peers"))
        for pair in fixture["truth_pairs"]:
            left, right = pair
            self.assertIn({"name": right, "kind": actual["|".join(pair)]}, findings[left]["peers"])
            self.assertIn({"name": left, "kind": actual["|".join(pair)]}, findings[right]["peers"])
        self.assertEqual([group["names"] for group in report["clusters"]], fixture["truth_pairs"])
        exported = json.dumps(report, sort_keys=True)
        for canary in fixture["export_canaries"]:
            self.assertNotIn(canary, exported)
        self.assertNotIn("description", report)

    def test_frozen_fixture_precision_recall_and_distinct_guard(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report = skill_lint.lint_catalog(fixture["catalog"])
        expected = {tuple(pair) for pair in fixture["truth_pairs"]}
        predicted = {tuple(pair["names"]) for pair in report["pairs"]}
        true_positive = len(expected & predicted)
        false_positive = len(predicted - expected)
        false_negative = len(expected - predicted)
        self.assertGreaterEqual(true_positive / (true_positive + false_positive), 0.90)
        self.assertGreaterEqual(true_positive / (true_positive + false_negative), 0.80)
        self.assertEqual(false_positive, 0)
        self.assertEqual(
            [pair for pair in predicted if fixture["distinct_names"] and set(pair) & set(fixture["distinct_names"])],
            [],
        )
        # An exact-description baseline finds only the planted identical pair.
        normalized = {
            row["name"]: " ".join(row["description"].casefold().split())
            for row in fixture["catalog"] if row["name"] in {n for p in fixture["truth_pairs"] for n in p}
        }
        baseline = {pair for pair in expected if normalized[pair[0]] == normalized[pair[1]]}
        self.assertGreater(true_positive, len(baseline))
        self.assertEqual(len(expected), 3)

    def test_json_order_and_round_trip_ignore_input_order(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        code, output = self._run_cli(fixture["catalog"], "--json")
        reverse_code, reverse_output = self._run_cli(list(reversed(fixture["catalog"])), "--json")
        self.assertEqual((code, reverse_code), (0, 0))
        self.assertEqual(output, reverse_output)
        self.assertEqual(json.loads(json.dumps(json.loads(output))), json.loads(output))
        for canary in fixture["export_canaries"]:
            self.assertNotIn(canary, output)

    def test_registry_error_and_non_string_description_never_echo_raw_data(self):
        marker = "REGISTRY_ERROR_CANARY_NOT_FOR_EXPORT"
        rows = [{"name": "broken", "description": {"content": marker}}]
        code, output = self._run_cli(rows, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["counts"]["invalid_rows"], 1)
        self.assertNotIn(marker, output)
        skills_tool = types.ModuleType("tools.skills_tool")
        setattr(skills_tool, "skills_list", mock.Mock(side_effect=RuntimeError(marker)))
        parser = argparse.ArgumentParser()
        plugin._setup_cli(parser)
        args = parser.parse_args(["lint-skills", "--json"])
        buffer = io.StringIO()
        with mock.patch.dict(sys.modules, {"tools.skills_tool": skills_tool}):
            with contextlib.redirect_stdout(buffer):
                code = args.func(args)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buffer.getvalue())["reason"], "catalog_unavailable")
        self.assertNotIn(marker, buffer.getvalue())

    def test_catalog_error_from_registry_cannot_inject_a_reason(self):
        marker = "REGISTRY_REASON_CANARY_NOT_FOR_EXPORT"
        parser = argparse.ArgumentParser()
        plugin._setup_cli(parser)
        args = parser.parse_args(["lint-skills", "--json"])
        output = io.StringIO()
        with mock.patch.object(skill_lint, "discover_report", side_effect=skill_lint.CatalogError(marker)):
            with contextlib.redirect_stdout(output):
                code = args.func(args)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["reason"], "catalog_unavailable")
        self.assertNotIn(marker, output.getvalue())

    def test_lint_path_reads_only_registry_names_and_descriptions_offline(self):
        class RestrictedRow(dict):
            def get(self, key, default=None):
                if key not in {"name", "description"}:
                    raise AssertionError("unexpected field access")
                return super().get(key, default)

        row = RestrictedRow({
            "name": "synthetic-safe", "description": "Use when testing local metadata without loading a skill body.",
            "content": object(), "supporting_files": object(),
        })
        with mock.patch.object(plugin, "DecisionClient", side_effect=AssertionError("provider call")):
            with mock.patch.object(socket.socket, "connect", side_effect=AssertionError("network call")):
                report = skill_lint.lint_catalog([row])
        self.assertEqual(report["counts"]["skills"], 1)
        self.assertEqual(report["pairs"], [])

    def test_json_command_reports_duplicate_names_without_description_text(self):
        catalog = [
            {"name": "alpha", "description": "Use when checking sample manifests before recovery and validating restore receipts."},
            {"name": "beta", "description": "Use when checking sample manifests before recovery and validating restore receipts."},
        ]
        skills_tool = types.ModuleType("tools.skills_tool")
        setattr(skills_tool, "skills_list", mock.Mock(return_value=json.dumps({"success": True, "skills": catalog})))
        parser = argparse.ArgumentParser()
        plugin._setup_cli(parser)
        args = parser.parse_args(["lint-skills", "--json"])
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"tools.skills_tool": skills_tool}):
            with contextlib.redirect_stdout(output):
                code = args.func(args)
        self.assertEqual(code, 0)
        report = json.loads(output.getvalue())
        self.assertEqual(report["schema"], "switchyard.lint_skills.v1")
        self.assertEqual(report["counts"]["skills"], 2)
        self.assertEqual(report["pairs"], [{"names": ["alpha", "beta"], "kind": "near_duplicate"}])
        self.assertNotIn("checking sample manifests", output.getvalue())
        skills_tool.skills_list.assert_called_once_with()  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
