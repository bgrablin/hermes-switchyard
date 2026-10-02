"""User-facing contracts for actionable, bounded offline lint reports."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import types
import unittest
from unittest import mock

import hermes_switchyard as plugin
from hermes_switchyard import skill_lint


class ActionableSkillLintTests(unittest.TestCase):
    def cli(self, rows, *options):
        registry = types.ModuleType("tools.skills_tool")
        lookup = mock.Mock(return_value=json.dumps({"success": True, "skills": rows}))
        setattr(registry, "skills_list", lookup)
        parser = argparse.ArgumentParser()
        plugin._setup_cli(parser)
        args = parser.parse_args(["lint-skills", *options])
        output = io.StringIO()
        with mock.patch.dict(sys.modules, {"tools.skills_tool": registry}), contextlib.redirect_stdout(output):
            code = args.func(args)
        lookup.assert_called_once_with()
        return code, output.getvalue()

    def test_useful_imperative_descriptions_need_no_prescribed_prefix(self):
        rows = [{"name": "certificates", "description": "Inspect TLS certificate chains and renewal failures."}]
        code, text = self.cli(rows)
        self.assertEqual(code, 0)
        self.assertIn("No actionable findings", text)
        self.assertNotIn("missing_use_when", text)
        report = skill_lint.lint_catalog(rows)
        self.assertEqual(report["diagnostics"], [])
        self.assertFalse(report["style_enabled"])

    def test_style_is_explicit_and_never_fails_warning_gate(self):
        rows = [{"name": "certificates", "description": "Inspect TLS certificate chains and renewal failures."}]
        code, text = self.cli(rows, "--style", "--fail-on", "warning")
        self.assertEqual(code, 0)
        self.assertIn("missing_use_when", text)
        report = skill_lint.lint_catalog(rows, include_style=True)
        self.assertEqual(report["diagnostics"][0]["severity"], "info")
        self.assertEqual(report["diagnostics"][0]["category"], "style")

    def test_empty_and_low_information_are_not_cosmetic(self):
        rows = [
            {"name": "empty", "description": " \n\t"},
            {"name": "vague", "description": "Use when you need help with various general tasks."},
            {"name": "concise", "description": "Inspect TLS chains."},
        ]
        report = skill_lint.lint_catalog(rows)
        diagnostics = {d["code"]: d for d in report["diagnostics"]}
        self.assertEqual(diagnostics["empty_description"]["severity"], "error")
        self.assertEqual(diagnostics["low_information_description"]["names"], ["vague"])
        self.assertNotIn("concise", [name for d in diagnostics.values() for name in d["names"]])
        self.assertEqual(report["pairs"], [])
        self.assertEqual(self.cli(rows)[0], 0)
        self.assertEqual(self.cli(rows, "--fail-on", "error")[0], 2)

    def test_pairs_have_numeric_evidence_not_private_words(self):
        text = "Use when checking archive digests and validating recovery manifests. PRIVATE_SENTINEL"
        rows = [{"name": name, "description": text} for name in ("beta", "alpha")]
        report = skill_lint.lint_catalog(rows)
        pair = report["pairs"][0]
        self.assertEqual(pair["evidence"]["similarity"], 1.0)
        self.assertTrue(pair["evidence"]["exact_description"])
        self.assertGreater(pair["evidence"]["shared_tokens"], 2)
        self.assertEqual(pair["severity"], "warning")
        self.assertEqual(report["diagnostics"][0]["names"], ["alpha", "beta"])
        self.assertNotIn("PRIVATE_SENTINEL", json.dumps(report))
        rendered = skill_lint.format_report(report)
        self.assertEqual(rendered.count("alpha, beta"), 1)
        self.assertIn("1.000", rendered)
        self.assertIn("Do not merge", rendered)
        self.assertEqual(self.cli(rows, "--fail-on", "warning")[0], 2)
        self.assertEqual(self.cli(rows, "--fail-on", "error")[0], 0)

    def test_confusable_is_a_review_suggestion_not_a_warning(self):
        rows = [
            {"name": "one", "description": "Inspect certificate expiry renewal ownership health"},
            {"name": "two", "description": "Inspect certificate expiry renewal identity status"},
        ]
        report = skill_lint.lint_catalog(rows)
        self.assertEqual(report["pairs"][0]["kind"], "confusable")
        self.assertEqual(report["pairs"][0]["severity"], "info")
        self.assertEqual(self.cli(rows, "--fail-on", "warning")[0], 0)

    def test_single_word_overlap_does_not_claim_a_collision(self):
        rows = [{"name": name, "description": "Use when helping"} for name in ("one", "two")]
        self.assertEqual(skill_lint.lint_catalog(rows)["pairs"], [])

    def test_output_budget_does_not_truncate_analysis_or_ci_gate(self):
        rows = [{"name": f"empty-{i:03d}", "description": ""} for i in range(40)]
        code, text = self.cli(rows, "--limit", "3", "--fail-on", "error")
        self.assertEqual(code, 2)
        self.assertEqual(text.count("[error]"), 3)
        self.assertIn("37 more", text)
        self.assertIn("--all", text)
        self.assertEqual(self.cli(rows, "--all")[1].count("[error]"), 40)
        _, exported = self.cli(rows, "--json", "--limit", "3")
        self.assertEqual(len(json.loads(exported)["diagnostics"]), 40)
        self.assertEqual(self.cli(rows)[1].count("[error]"), 20)

    def test_invalid_coverage_is_explicit_and_reason_counts_reconcile(self):
        rows = [None, {"name": "bad\nname", "description": "secret"},
                {"name": "dup", "description": "a"}, {"name": "dup", "description": "b"},
                {"name": "nontext", "description": None},
                {"name": "large", "description": "x" * 4097},
                {"name": "many", "description": " ".join(f"word{i}" for i in range(129))}]
        report = skill_lint.lint_catalog(rows)
        self.assertEqual(report["status"], "partial")
        self.assertFalse(report["coverage_complete"])
        self.assertEqual(sum(report["invalid_reasons"].values()), len(rows))
        self.assertEqual(report["counts"]["invalid_rows"], len(rows))
        self.assertEqual(report["counts"]["input_rows"], len(rows))
        self.assertEqual(report["severity_counts"]["error"], 1)
        self.assertEqual(self.cli(rows, "--fail-on", "error")[0], 2)
        self.assertNotIn("bad\\nname", json.dumps(report))
        self.assertNotIn("secret", json.dumps(report))

    def test_severity_order_is_deterministic(self):
        rows = [{"name": "z-empty", "description": ""},
                {"name": "a-vague", "description": "Help with tasks"},
                {"name": "b-useful", "description": "Inspect TLS chains."}]
        first = skill_lint.lint_catalog(rows, include_style=True)
        self.assertEqual(first, skill_lint.lint_catalog(list(reversed(rows)), include_style=True))
        ranks = {"error": 0, "warning": 1, "info": 2}
        values = [ranks[d["severity"]] for d in first["diagnostics"]]
        self.assertEqual(values, sorted(values))
        self.assertEqual(sum(first["severity_counts"].values()), len(first["diagnostics"]))

    def test_non_ascii_text_is_not_mislabeled_empty_or_weak(self):
        rows = [{"name": "unicode", "description": "检查证书链并验证续期。"}]
        report = skill_lint.lint_catalog(rows)
        self.assertEqual(report["diagnostics"], [])
        self.assertEqual(report["counts"]["comparison_omitted"], 1)
        self.assertFalse(report["comparison_complete"])
        self.assertIn("ASCII", skill_lint.format_report(report))

    def test_empty_catalog_is_explicit_not_a_health_certificate(self):
        report = skill_lint.lint_catalog([])
        self.assertEqual(report["counts"]["skills"], 0)
        self.assertIn("No skills returned", skill_lint.format_report(report))

    def test_combining_marks_do_not_bypass_comparison_omission(self):
        for text in ("Inspect caf\u00e9 assets", "Inspect cafe\u0301 assets", "Inspect cafe\u0338 assets"):
            with self.subTest(text=text):
                rows = [{"name": name, "description": text} for name in ("one", "two")]
                report = skill_lint.lint_catalog(rows)
                self.assertEqual(report["diagnostics"], [])
                self.assertEqual(report["counts"]["comparison_omitted"], 2)
                self.assertFalse(report["comparison_complete"])
        punctuation = skill_lint.lint_catalog([{
            "name": "punctuation", "description": "Inspect TLS chains \u2014 check renewal failures.",
        }])
        self.assertTrue(punctuation["comparison_complete"])

    def test_non_ascii_numbers_are_omitted_not_compared_as_fragments(self):
        # Decimal, digit, and numeric-only Unicode forms, not just fullwidth.
        for number in ("\uff11\uff12\uff13", "\u0661", "\u00b2", "\u00bd", "\u2163"):
            with self.subTest(number=number):
                rows = [{"name": name, "description": f"Inspect certificate renewal {number}"}
                        for name in ("one", "two")]
                code, text = self.cli(rows, "--json", "--fail-on", "warning")
                report = json.loads(text)
                self.assertEqual(code, 0)
                self.assertEqual(report["pairs"], [])
                self.assertEqual(report["diagnostics"], [])
                self.assertEqual(report["counts"]["comparison_omitted"], 2)
                self.assertEqual(report["counts"]["compared_skills"], 0)
                self.assertFalse(report["comparison_complete"])
        ascii_report = skill_lint.lint_catalog([
            {"name": name, "description": "Inspect certificate renewal 123"} for name in ("one", "two")
        ])
        self.assertTrue(ascii_report["comparison_complete"])
        self.assertEqual(len(ascii_report["pairs"]), 1)

    def test_non_ascii_number_only_descriptions_are_not_mislabeled_weak(self):
        rows = [{"name": "numbers", "description": "\uff11\uff12\uff13"}]
        report = skill_lint.lint_catalog(rows)
        self.assertEqual(report["diagnostics"], [])
        self.assertEqual(report["counts"]["comparison_omitted"], 1)
        self.assertFalse(report["comparison_complete"])
        self.assertIn("ASCII comparison omitted 1", skill_lint.format_report(report))
        styled = skill_lint.lint_catalog(rows, include_style=True)
        self.assertTrue(styled["diagnostics"])
        self.assertTrue(all(d["category"] == "style" for d in styled["diagnostics"]))

    def test_limit_rejects_nonpositive_values_before_discovery(self):
        parser = argparse.ArgumentParser()
        plugin._setup_cli(parser)
        for value in ("0", "-1", "oops"):
            with self.subTest(value=value), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    parser.parse_args(["lint-skills", "--limit", value])
                self.assertEqual(error.exception.code, 2)

    def test_large_useful_catalog_is_quiet_without_changing_skills(self):
        rows = [{"name": f"domain-{i}",
                 "description": f"Inspect domain{i} assets{i} using procedure{i}."} for i in range(414)]
        original = json.dumps(rows)
        code, text = self.cli(rows)
        self.assertEqual(code, 0)
        self.assertIn("Checked 414/414", text)
        self.assertLess(len(text.splitlines()), 10)
        report = skill_lint.lint_catalog(rows, include_style=True)
        self.assertEqual(report["counts"]["missing_use_when"], 414)
        self.assertEqual(json.dumps(rows), original)
        self.assertEqual(report["pairs"], [])


if __name__ == "__main__":
    unittest.main()
