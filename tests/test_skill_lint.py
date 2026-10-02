"""Offline skill-routability command and frozen synthetic evaluation."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
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
    def test_frozen_evaluation_provenance_matches_recorded_hashes(self):
        expected = {
            "PLAN.md": "99db874094409c53a4d94a2c7bac742c5c406077e20560aa1e75d98ca580ee7a",
            "fixture.json": "1594daa52d22cc188d8f100ed0a1ff21dc0c3060a30122b39f64974469083d57",
        }
        results = FIXTURE.with_name("RESULTS.md").read_text(encoding="utf-8")
        for name, digest in expected.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256(FIXTURE.with_name(name).read_bytes()).hexdigest(), digest)
                self.assertIn(digest, results)

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
            "schema": "switchyard.lint_skills.v2", "status": "unavailable", "reason": "catalog_too_large",
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
        self.assertIn("certificate-alpha, certificate-beta: confusable", text)
        self.assertIn("tiny-description: low_information_description", text)
        self.assertNotIn("missing_use_when", text)
        self.assertIn("invalid_rows: 1", text)
        self.assertNotIn('"schema"', text)
        for canary in fixture["export_canaries"]:
            self.assertNotIn(canary, text)

    def test_frozen_catalog_finds_planted_pairs_and_named_peers(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        report = skill_lint.lint_catalog(fixture["catalog"], include_style=True)
        self.assertEqual(report["counts"]["invalid_rows"], 1)
        self.assertEqual(report["counts"]["skills"], 12)
        actual = {"|".join(pair["names"]): pair["kind"] for pair in report["pairs"]}
        self.assertEqual(actual, fixture["truth_kinds"])
        findings = {entry["name"]: entry for entry in report["findings"]}
        for name, issues in fixture["description_issues"].items():
            if "invalid_row" not in issues:
                # The frozen v1 style oracle remains unchanged and is opt-in in v2.
                legacy_style = [code for code in findings[name]["issues"] if code in {
                    "short_description", "long_description", "missing_use_when",
                }]
                self.assertEqual(legacy_style, issues)
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

    def test_use_when_trigger_rejects_unicode_lookalikes(self):
        description = "Uſe when checking synthetic entries before preparing a local report."
        report = skill_lint.lint_catalog([{"name": "lookalike", "description": description}], include_style=True)
        self.assertEqual(report["counts"]["missing_use_when"], 1)
        self.assertEqual(report["findings"][0]["issues"], ["missing_use_when"])
        mixed_case = skill_lint.lint_catalog([{
            "name": "ascii", "description": "uSe WhEn checking synthetic entries before preparing a local report.",
        }], include_style=True)
        self.assertEqual(mixed_case["counts"]["missing_use_when"], 0)

    def test_duplicate_names_are_all_invalid_regardless_of_input_order(self):
        shared = "Use when checking synthetic library entries and validating revisions."
        rows = [
            {"name": "shared", "description": shared},
            {"name": "shared", "description": "Use when planning public exhibits and cataloging star maps."},
            {"name": "peer", "description": shared},
        ]
        forward = skill_lint.lint_catalog(rows)
        backward = skill_lint.lint_catalog(list(reversed(rows)))
        self.assertEqual(forward, backward)
        self.assertEqual(forward["counts"]["skills"], 1)
        self.assertEqual(forward["counts"]["invalid_rows"], 2)
        self.assertEqual(forward["pairs"], [])
        self.assertEqual(forward["clusters"], [])
        self.assertEqual(forward["findings"], [])
        malformed = skill_lint.lint_catalog([
            rows[0], {"name": "shared", "description": {"content": "never export"}}, rows[2],
        ])
        self.assertEqual(malformed["counts"]["invalid_rows"], 2)
        self.assertEqual(malformed["counts"]["skills"], 1)
        self.assertNotIn("shared", json.dumps(malformed))

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
        self.assertEqual(report["schema"], "switchyard.lint_skills.v2")
        self.assertEqual(report["counts"]["skills"], 2)
        self.assertEqual(len(report["pairs"]), 1)
        self.assertEqual(report["pairs"][0]["names"], ["alpha", "beta"])
        self.assertEqual(report["pairs"][0]["kind"], "near_duplicate")
        self.assertTrue(report["pairs"][0]["evidence"]["exact_description"])
        self.assertNotIn("checking sample manifests", output.getvalue())
        skills_tool.skills_list.assert_called_once_with()  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
