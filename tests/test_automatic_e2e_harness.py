"""Regression checks for automatic evaluation evidence classification."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evaluation.automatic_e2e import harness
from evaluation.automatic_e2e.harness import (
    HarnessInvalid,
    _forbidden_tool_calls,
    _parse_shebang_reexec_argv,
    _skill_load_metrics,
    build_skill_alias_map,
    canonicalize_skill_identifier,
)


class AutomaticEvaluationHarnessTests(unittest.TestCase):
    def test_skill_load_metrics_requires_exact_expected_identifier(self):
        correct, irrelevant, loaded_canonical, expected_canonical = _skill_load_metrics(
            ["docker-management-unsafe", "docker-management"],
            "docker-management",
        )
        self.assertTrue(correct)
        self.assertEqual(irrelevant, ["docker-management-unsafe"])
        self.assertEqual(expected_canonical, "docker-management")
        self.assertEqual(loaded_canonical, ["docker-management-unsafe", "docker-management"])

    def test_skill_load_metrics_reports_nonmatching_identifier(self):
        correct, irrelevant, _, _ = _skill_load_metrics(
            ["docker-management-unsafe"],
            "docker-management",
        )
        self.assertFalse(correct)
        self.assertEqual(irrelevant, ["docker-management-unsafe"])

    def test_namespaced_skill_load_matches_bare_expected_via_registry(self):
        """Regress issue #24: devops:network-printer-operations is a correct load."""
        aliases = build_skill_alias_map(
            [
                {
                    "name": "network-printer-operations",
                    "description": "Printers",
                    "category": "devops",
                },
                {
                    "name": "docker-management",
                    "description": "Docker",
                    "category": "devops",
                },
            ]
        )
        correct, irrelevant, loaded_canonical, expected_canonical = _skill_load_metrics(
            ["devops:network-printer-operations"],
            "network-printer-operations",
            aliases,
        )
        self.assertTrue(correct)
        self.assertEqual(irrelevant, [])
        self.assertEqual(loaded_canonical, ["network-printer-operations"])
        self.assertEqual(expected_canonical, "network-printer-operations")

        correct_slash, _, _, _ = _skill_load_metrics(
            ["devops/network-printer-operations"],
            "network-printer-operations",
            aliases,
        )
        self.assertTrue(correct_slash)

        # Category-qualified expected also matches a bare load.
        correct_inverse, _, _, _ = _skill_load_metrics(
            ["network-printer-operations"],
            "devops:network-printer-operations",
            aliases,
        )
        self.assertTrue(correct_inverse)

    def test_build_skill_alias_map_registers_bare_and_qualified_forms(self):
        aliases = build_skill_alias_map(
            [{"name": "log-triage", "category": "ops", "description": "Logs"}]
        )
        self.assertEqual(aliases["log-triage"], "log-triage")
        self.assertEqual(aliases["ops:log-triage"], "log-triage")
        self.assertEqual(aliases["ops/log-triage"], "log-triage")
        self.assertEqual(
            canonicalize_skill_identifier("ops:log-triage", aliases),
            "log-triage",
        )

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

    def test_prepare_home_copies_fixture_skills_not_operator_home(self):
        with tempfile.TemporaryDirectory() as directory:
            home, _ = harness._prepare_home(Path.cwd(), Path(directory), False)
            printer = home / "skills" / "devops" / "network-printer-operations" / "SKILL.md"
            logs = home / "skills" / "ops" / "log-triage" / "SKILL.md"
            self.assertTrue(printer.is_file())
            self.assertTrue(logs.is_file())
            self.assertIn("network-printer-operations", printer.read_text(encoding="utf-8"))

    def test_paired_suite_has_more_than_two_unique_tasks(self):
        ids = [task["id"] for task in harness.TASKS]
        self.assertGreaterEqual(len(set(ids)), 3)
        self.assertEqual(len(ids), len(set(ids)))


    def test_skills_from_public_registry_parses_skills_list_payload(self):
        payload = {
            "success": True,
            "skills": [
                {"name": "network-printer-operations", "category": "devops", "description": "Printers"},
                {"name": "plugin:helper", "description": "Plugin skill"},
            ],
        }
        fake_module = mock.Mock()
        fake_module.skills_list = mock.Mock(return_value=__import__("json").dumps(payload))
        with mock.patch.dict("sys.modules", {"tools.skills_tool": fake_module}):
            skills = harness._skills_from_public_registry()
        self.assertEqual(
            [item["name"] for item in skills],
            ["network-printer-operations", "plugin:helper"],
        )
        aliases = build_skill_alias_map(skills)
        self.assertEqual(aliases["devops:network-printer-operations"], "network-printer-operations")
        self.assertEqual(aliases["helper"], "plugin:helper")

    def test_skills_from_public_registry_fails_closed_on_empty(self):
        fake_module = mock.Mock()
        fake_module.skills_list = mock.Mock(
            return_value=__import__("json").dumps({"success": True, "skills": []})
        )
        with mock.patch.dict("sys.modules", {"tools.skills_tool": fake_module}):
            with self.assertRaises(HarnessInvalid):
                harness._skills_from_public_registry()

    def test_ensure_hermes_runtime_fails_closed_without_interpreter(self):
        with mock.patch.object(harness, "_hermes_imports_available", return_value=(False, "ImportError: hermes_state")):
            with mock.patch.object(harness, "resolve_hermes_reexec_argv", return_value=None):
                with self.assertRaises(HarnessInvalid) as ctx:
                    harness.ensure_hermes_runtime()
        self.assertIn("Hermes runtime imports unavailable", str(ctx.exception))

    def test_unknown_qualified_skill_does_not_match_bare_leaf(self):
        """Wrong namespace must not fall through to bare-leaf scoring (Copilot on #43)."""
        aliases = build_skill_alias_map(
            [
                {
                    "name": "network-printer-operations",
                    "description": "Printers",
                    "category": "devops",
                }
            ]
        )
        self.assertEqual(
            canonicalize_skill_identifier("wrong:network-printer-operations", aliases),
            "wrong:network-printer-operations",
        )
        self.assertEqual(
            canonicalize_skill_identifier("wrong/network-printer-operations", aliases),
            "wrong/network-printer-operations",
        )
        correct_colon, irrelevant_colon, loaded_colon, _ = _skill_load_metrics(
            ["wrong:network-printer-operations"],
            "network-printer-operations",
            aliases,
        )
        self.assertFalse(correct_colon)
        self.assertEqual(irrelevant_colon, ["wrong:network-printer-operations"])
        self.assertEqual(loaded_colon, ["wrong:network-printer-operations"])

        correct_slash, irrelevant_slash, _, _ = _skill_load_metrics(
            ["wrong/network-printer-operations"],
            "network-printer-operations",
            aliases,
        )
        self.assertFalse(correct_slash)
        self.assertEqual(irrelevant_slash, ["wrong/network-printer-operations"])

        # Registry-reported qualified forms still match.
        self.assertEqual(
            canonicalize_skill_identifier("devops:network-printer-operations", aliases),
            "network-printer-operations",
        )

    def test_env_shebang_parses_into_reexec_argv(self):
        argv = _parse_shebang_reexec_argv("#!/usr/bin/env python3")
        # On POSIX the shebang path is kept; on Windows without that path,
        # shutil.which("env") may supply a Git usr\bin\env location.
        self.assertTrue(Path(argv[0]).name.lower().startswith("env"), argv[0])
        self.assertEqual(argv[1:], ["python3"])

        argv_s = _parse_shebang_reexec_argv("#!/usr/bin/env -S python3 -u")
        self.assertTrue(Path(argv_s[0]).name.lower().startswith("env"), argv_s[0])
        self.assertIn("python3", argv_s)

    def test_direct_python_shebang_parses_into_reexec_argv(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_python = Path(directory) / "python3"
            fake_python.write_text("#!/bin/sh\n", encoding="utf-8")
            fake_python.chmod(0o755)
            argv = _parse_shebang_reexec_argv(f"#!{fake_python}")
            self.assertEqual(argv, [str(fake_python)])

    def test_unsupported_shebang_wrapper_raises_harness_invalid(self):
        with self.assertRaises(HarnessInvalid) as ctx:
            _parse_shebang_reexec_argv("#!/bin/sh")
        self.assertIn("unsupported wrapper", str(ctx.exception))

        with self.assertRaises(HarnessInvalid) as ctx_env:
            _parse_shebang_reexec_argv("#!/usr/bin/env bash")
        self.assertIn("env shebang", str(ctx_env.exception))


if __name__ == "__main__":
    unittest.main()
