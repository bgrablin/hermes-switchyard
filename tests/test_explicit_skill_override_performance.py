"""Freeze explicit-skill semantics and verify avoided work without timing gates."""
from __future__ import annotations

import unittest
from unittest import mock

from hermes_switchyard import automatic
from test_support import HermesHomeTestCase


class ExplicitSkillOverrideTests(unittest.TestCase):
    def test_absent_names_skip_all_regex_searches(self):
        candidates = tuple({"name": f"topic-{index:04d}"} for index in range(600))
        for task in ("Diagnose a container restart loop.", "hi", "Use unknown-skill"):
            with self.subTest(task=task):
                with mock.patch.object(automatic.re, "search", wraps=automatic.re.search) as search:
                    self.assertIsNone(automatic._explicit_skill_override(task, candidates))
                self.assertEqual(search.call_count, 0)

    def test_present_names_still_use_regex_predicates(self):
        candidates = ("absent-before", "alpha", "absent-after")
        cases = (
            ("/alpha inspect", "alpha", 1),
            ("Use alpha", "alpha", 2),
            ("Load the skill `alpha`", "alpha", 2),
            ("mention alpha only", None, 2),
            ("/alpha.", None, 2),
            ("misuse alpha", None, 2),
        )
        for task, expected, searches in cases:
            with self.subTest(task=task):
                with mock.patch.object(automatic.re, "search", wraps=automatic.re.search) as search:
                    self.assertEqual(automatic._explicit_skill_override(task, candidates), expected)
                self.assertEqual(search.call_count, searches)

    def test_literal_escaping_and_existing_boundaries(self):
        cases = (
            ("Use x.y", "x.y", "x.y"),
            ("Use xay", "x.y", None),
            ("Use a+b", "a+b", "a+b"),
            ("Use aaab", "a+b", None),
            ("Use '[ab]'", "[ab]", "[ab]"),
            ("Use a", "[ab]", None),
            ("Use 'c++'", "c++", "c++"),
            ("Use c++", "c++", None),
            ("/c++ inspect", "c++", "c++"),
            ("Use alpha-suffix", "alpha", "alpha"),
            ("Use alphasuffix", "alpha", None),
            ("Do not use alpha", "alpha", "alpha"),
        )
        for task, name, expected in cases:
            with self.subTest(task=task, name=name):
                self.assertEqual(automatic._explicit_skill_override(task, (name,)), expected)

    def test_unicode_and_case_preserve_lower_not_ignorecase_semantics(self):
        # No IGNORECASE flag: long s is not ASCII s, but Kelvin lowercases to k.
        cases = (
            ("LOAD THE SKILL `ALPHA`", "AlPhA", "AlPhA"),
            ("Use ſkill", "skill", None),
            ("Use skill", "ſkill", None),
            ("Use ſkill", "ſkill", "ſkill"),
            ("Use Kelvin", "kelvin", "kelvin"),
            ("Use kelvin", "Kelvin", "Kelvin"),
            ("Use ıtem", "item", None),
            ("Use 'İ'", "İ", "İ"),
            ("Use 'i'", "İ", None),
            ("Use STRASSE", "Straße", None),
            ("Use STRAẞE", "Straße", "Straße"),
            ("Use ÉCLAIR", "éclair", "éclair"),
        )
        for task, name, expected in cases:
            with self.subTest(task=task, name=name):
                self.assertEqual(automatic._explicit_skill_override(task, (name,)), expected)

    def test_candidate_order_and_original_name_win(self):
        cases = (
            ("Use alpha-beta", ("alpha", "alpha-beta"), "alpha"),
            ("Use alpha-beta", ("alpha-beta", "alpha"), "alpha-beta"),
            ("Use beta then use alpha", ("alpha", "beta"), "alpha"),
            ("Use beta then use alpha", ({"name": "BETA"}, "alpha"), "BETA"),
        )
        for task, candidates, expected in cases:
            with self.subTest(task=task, candidates=candidates):
                self.assertEqual(automatic._explicit_skill_override(task, candidates), expected)

    def test_typed_and_truncated_text(self):
        cases = (
            ([{"type": "text", "text": "Use alpha"}], "alpha"),
            ([{"type": "input_text", "text": "Use alpha"}], "alpha"),
            ([{"type": "image", "text": "Use alpha"}], None),
            (["Use alpha", {"text": "Use alpha"}], None),
            ([{"type": "text", "text": "Use"}, {"type": "text", "text": "alpha"}], "alpha"),
            ("x" * (automatic.MAX_TASK_CHARS - len(" Use alpha")) + " Use alpha", "alpha"),
            ("x" * (automatic.MAX_TASK_CHARS - len(" Use alpha") + 1) + " Use alpha", None),
            ([{"type": "text", "text": "x" * automatic.MAX_TASK_CHARS},
              {"type": "text", "text": "Use alpha"}], None),
        )
        for task, expected in cases:
            with self.subTest(task=task):
                self.assertEqual(automatic._explicit_skill_override(task, ("alpha",)), expected)

    def test_invalid_entries_and_empty_inputs(self):
        candidates = (None, {}, 1, {"name": None}, {"name": ""}, "", "alpha")
        self.assertEqual(automatic._explicit_skill_override("Use alpha", candidates), "alpha")
        for task in (None, "", {"text": "Use alpha"}):
            with self.subTest(task=task):
                self.assertIsNone(automatic._explicit_skill_override(task, candidates))
        self.assertIsNone(automatic._explicit_skill_override("Use alpha", None))


class ExplicitSkillRegistryTests(HermesHomeTestCase):
    def test_full_registry_override_and_configured_candidate_precedence(self):
        configured = {"name": "configured-skill", "description": "Synthetic configured utility."}
        registry_only = {"name": "registry-only", "description": "Synthetic registry utility."}
        cases = (
            ("Use registry-only", "registry-only"),
            ("Use registry-only then use configured-skill", "configured-skill"),
        )
        for task, expected in cases:
            with self.subTest(task=task):
                client = mock.Mock(side_effect=AssertionError("explicit overrides must not call a provider"))
                loader = mock.Mock(side_effect=AssertionError("explicit overrides must not load a skill"))
                hook = automatic.build_pre_llm_call_hook(
                    configured_candidates=[configured],
                    routing_mode="hosted_sanitized",
                    public_or_sanitized_data_ack=True,
                    client_factory=client,
                    consumer_mode="load",
                    skill_loader=loader,
                    environ={},
                )
                assert hook is not None
                with mock.patch.object(
                    automatic, "discover_available_skill_candidates",
                    return_value=(registry_only, configured),
                ):
                    result = hook(user_message=task, platform="cli")
                assert result is not None
                recommendation = result["metadata"]["skill_recommendation"]
                self.assertEqual(recommendation["status"], "explicit_override")
                self.assertEqual(recommendation["explicit_skill"], expected)
                self.assertEqual(hook.last_receipt["request_count"], 0)
                client.assert_not_called()
                loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
