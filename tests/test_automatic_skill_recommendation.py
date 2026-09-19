"""Mocked lifecycle tests for automatic skill recommendations.

Hosted calls in this file use a synthetic DecisionClient transport. No test sends
private conversation history, calls a real endpoint, or loads a skill.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from jev_decision.automatic import (
    AutomaticSkillRecommender,
    _coerce_bounded_text,
    _config_float,
    build_pre_llm_call_hook,
    discover_available_skill_candidates,
)
from jev_decision.client import DecisionClient
from jev_decision.egress import evaluate_turn_egress_policy


class _Context:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.hooks = {}
        self.tools = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_hook(self, name, callback):
        self.hooks[name] = callback

    def register_tool(self, *, name, handler, **kwargs):
        self.tools[name] = handler

    def register_auxiliary_task(self, *_args, **_kwargs):
        pass

    def register_skill(self, *_args, **_kwargs):
        pass


class AutomaticRecommendationTests(unittest.TestCase):
    def test_multimodal_text_is_bounded_while_blocks_are_read(self):
        value = [
            {"type": "text", "text": "a" * 3_000},
            {"type": "text", "text": "b" * 3_000},
            {"type": "text", "text": "must-not-be-read"},
        ]
        result = _coerce_bounded_text(value, 4_000)
        self.assertEqual(len(result), 4_000)
        self.assertNotIn("must-not-be-read", result)

    @staticmethod
    def _skills_api():
        try:
            from tools import skills_tool
        except ImportError as exc:
            raise unittest.SkipTest(f"Hermes skills API unavailable: {exc}") from exc
        return skills_tool

    @staticmethod
    def _history():
        return [
            {
                "role": "system",
                "content": (
                    "<available_skills>\n"
                    "  devops:\n"
                    "    - docker-management: Manage Docker containers and Compose services.\n"
                    "    - network-printer-operations: Operate network printers and scanners.\n"
                    "</available_skills>"
                ),
            },
            {"role": "user", "content": "PRIVATE_HISTORY_MARKER"},
        ]

    @staticmethod
    def _allowed_policy(payload="SANITIZED_TASK_MARKER"):
        return {
            "version": 1,
            "decision": "allow",
            "data_class": "sanitized",
            "reason_code": "synthetic_fixture_allowed",
            "allowed_payload": payload,
        }

    def test_local_lifecycle_hook_selects_relevant_skill_and_abstains_on_no_fit(self):
        import jev_decision

        context = _Context({
            "automatic_skill_candidates": [
                {"name": "docker-management", "description": "Manage Docker containers and Compose services."},
                {"name": "network-printer-operations", "description": "Operate network printers and scanners."},
            ]
        })
        with mock.patch.object(jev_decision, "_secret", side_effect=AssertionError("hosted path must stay off")):
            jev_decision.register(context)
        hook = context.hooks["pre_llm_call"]
        self.assertIsNotNone(hook)
        assert hook is not None
        recommendation = hook(
            user_message="Diagnose an exiting Docker Compose container",
            conversation_history=self._history(),
        )
        self.assertIsNotNone(recommendation)
        assert recommendation is not None
        self.assertIn("docker-management", recommendation["context"])
        self.assertIn("did not load it", recommendation["context"])
        self.assertIn("Mandatory skills", recommendation["context"])

        no_fit = hook(
            user_message="Explain the difference between Python lists and tuples",
            conversation_history=self._history(),
        )
        self.assertIsNotNone(no_fit)
        self.assertEqual(no_fit["metadata"]["routing_reason"], "per_turn_policy_missing")

    def test_skill_registry_discovery_uses_public_response_schema(self):
        payload = {
            "success": True,
            "skills": [
                {"name": "docker-management", "description": "Manage Docker containers", "category": "devops"},
                {"name": "network-printer-operations", "description": "Operate network printers", "category": "devops"},
            ],
            "categories": ["devops"],
            "count": 2,
            "hint": "Use skill_view(name)",
        }
        with mock.patch.object(self._skills_api(), "skills_list", return_value=json.dumps(payload)):
            candidates = discover_available_skill_candidates()
        self.assertEqual(
            [candidate["name"] for candidate in candidates],
            ["docker-management", "network-printer-operations"],
        )

    def test_skill_registry_discovery_bounds_large_catalog(self):
        payload = {
            "success": True,
            "skills": [
                {"name": f"synthetic-{index}", "description": "public skill"}
                for index in range(300)
            ],
        }
        with mock.patch.object(self._skills_api(), "skills_list", return_value=json.dumps(payload)):
            candidates = discover_available_skill_candidates()
        self.assertEqual(len(candidates), 300)
        self.assertEqual(candidates[-1]["name"], "synthetic-299")

    def test_realistic_hermes_skill_registry_schema_is_used(self):
        payload = {
            "success": True,
            "skills": [
                {"name": "docker-management", "description": "Manage Docker containers, images, and Compose.", "category": "devops"},
                {"name": "network-printer-operations", "description": "Operate network printers and scanners.", "category": "devops"},
            ],
            "categories": ["devops"],
            "count": 2,
            "hint": "Use skill_view(name) to see full content, tags, and linked files",
        }
        with mock.patch.object(self._skills_api(), "skills_list", return_value=json.dumps(payload)):
            candidates = discover_available_skill_candidates()
        self.assertEqual(
            [candidate["name"] for candidate in candidates],
            ["docker-management", "network-printer-operations"],
        )

    def test_recommendation_cache_is_bounded_and_observable(self):
        recommender = AutomaticSkillRecommender(
            configured_candidates=[
                {"name": "docker-management", "description": "Manage Docker containers"},
            ],
            cache_seconds=30,
        )
        first = recommender.recommend("Diagnose a Docker container")
        second = recommender.recommend("Diagnose a Docker container")
        self.assertEqual(first["selected"], "docker-management")
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])

    def test_config_float_clamps_unbounded_integer_before_float_conversion(self):
        self.assertEqual(
            _config_float(10**400, 0.2, minimum=0.0, maximum=1.0),
            1.0,
        )
        self.assertEqual(
            _config_float(-(10**400), 0.2, minimum=0.0, maximum=1.0),
            0.0,
        )

    def test_prompt_catalog_descriptions_stay_local_to_hosted_boundary(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            return {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "skill": {
                        "choice": "docker-management",
                        "confidence": 0.95,
                        "probabilities": {"docker-management": 1.0},
                    },
                    "needs_skill": {"noul": 0.95},
                },
                "usage": {},
            }

        hook = build_pre_llm_call_hook(
            hosted_enabled=True,
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=transport),
        )
        assert hook is not None
        registry_payload = {
            "success": True,
            "skills": [{
                "name": "docker-management",
                "description": "SYNTHETIC_PROMPT_DESCRIPTION_MARKER",
            }],
        }
        with mock.patch.object(self._skills_api(), "skills_list", return_value=json.dumps(registry_payload)):
            result = hook(
                user_message="public Docker maintenance request",
                conversation_history=[{"role": "user", "content": "PRIVATE_HISTORY_MARKER"}],
                turn_egress_policy=self._allowed_policy(),
            )
        self.assertIsNotNone(result)
        wire = json.dumps(payloads[0], sort_keys=True)
        self.assertNotIn("SYNTHETIC_PROMPT_DESCRIPTION_MARKER", wire)
        self.assertNotIn("PRIVATE_HISTORY_MARKER", wire)
        self.assertIn("docker-management", wire)

    def test_hosted_path_is_explicit_and_uses_mocked_transport_without_history(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            return {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "skill": {
                        "choice": "docker-management",
                        "confidence": 0.95,
                        "probabilities": {"docker-management": 0.95, "printer": 0.05},
                    },
                    "needs_skill": {"noul": 0.95},
                },
                "usage": {},
            }

        def client_factory():
            return DecisionClient(api_key="fixture-key", transport=transport)

        hook = build_pre_llm_call_hook(
            configured_candidates=[
                {"name": "docker-management", "description": "SYNTHETIC_CONFIGURED_DESCRIPTION_MARKER private operator metadata"},
                {"name": "printer", "description": "Operate network printers"},
            ],
            hosted_enabled=True,
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=client_factory,
        )
        result = hook(
            user_message="public Docker maintenance request",
            conversation_history=[
                {"role": "system", "content": "PRIVATE_HISTORY_MARKER"},
                {"role": "assistant", "content": "private prior turn"},
            ],
            turn_egress_policy=self._allowed_policy(),
        )
        self.assertIsNotNone(result)
        self.assertIn("docker-management", result["context"])
        self.assertEqual(len(payloads), 1)
        wire = json.dumps(payloads[0], sort_keys=True)
        self.assertNotIn("PRIVATE_HISTORY_MARKER", wire)
        self.assertNotIn("private prior turn", wire)
        self.assertNotIn("SYNTHETIC_CONFIGURED_DESCRIPTION_MARKER", wire)
        self.assertIn("docker-management", wire)
        self.assertIn("SANITIZED_TASK_MARKER", wire)
        self.assertNotIn("public Docker maintenance request", wire)
        self.assertEqual(payloads[0]["provider"], {"allow_fallbacks": False})

    def test_valid_hosted_abstention_does_not_fallback_to_local_winner(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            return {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "skill": {
                        "choice": "docker-management",
                        "confidence": 0.60,
                        "probabilities": {"docker-management": 0.60, "printer": 0.40},
                    },
                    "needs_skill": {"noul": 0.60},
                },
                "usage": {},
            }

        recommender = AutomaticSkillRecommender(
            configured_candidates=[
                {"name": "docker-management", "description": "Manage Docker containers"},
                {"name": "printer", "description": "Operate network printers"},
            ],
            hosted_enabled=True,
            hosted_mode="always",
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=transport),
        )
        result = recommender.recommend(
            "Diagnose a Docker container",
            turn_egress_policy=self._allowed_policy("SANITIZED_DOCKER_TASK"),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["status"], "abstained")
        self.assertIsNone(result["selected"])
        self.assertEqual(result["source"], "none")
        self.assertEqual(result["abstention_reason"], "hosted_abstention")

    def test_hosted_default_calls_jev_even_when_local_match_is_confident(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            return {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "skill": {
                        "choice": "docker-management",
                        "confidence": 0.99,
                        "probabilities": {"docker-management": 1.0},
                    },
                    "needs_skill": {"noul": 0.99},
                },
                "usage": {},
            }

        recommender = AutomaticSkillRecommender(
            configured_candidates=[
                {"name": "docker-management", "description": "Manage Docker containers"},
            ],
            hosted_enabled=True,
            public_or_sanitized_data_ack=False,
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=transport),
        )
        result = recommender.recommend(
            "Diagnose a Docker container",
            turn_egress_policy=self._allowed_policy("SANITIZED_DOCKER_TASK"),
        )
        self.assertEqual(len(calls), 1)
        self.assertTrue(result["hosted_attempted"])
        self.assertEqual(result["source"], "jev")

    def test_hosted_path_without_attestation_does_not_construct_client(self):
        constructed = []

        def forbidden_client():
            constructed.append(True)
            raise AssertionError("hosted client must not be constructed")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            hosted_enabled=True,
            public_or_sanitized_data_ack=False,
            client_factory=forbidden_client,
        )
        result = recommender.recommend("Docker maintenance")
        self.assertEqual(result["selected"], "docker-management")
        self.assertEqual(result["source"], "local")
        self.assertEqual(result["hosted_skipped"], "per_turn_policy_missing")
        self.assertEqual(constructed, [])

    def test_legacy_ack_cannot_authorize_restricted_turn(self):
        constructed = []

        def forbidden_client():
            constructed.append(True)
            raise AssertionError("restricted turn must not construct hosted client")

        hook = build_pre_llm_call_hook(
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            hosted_enabled=True,
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_client,
        )
        assert hook is not None
        hook(
            user_message="private Docker maintenance request",
            conversation_history=self._history(),
            turn_egress_policy={
                "version": 1,
                "decision": "deny",
                "data_class": "private",
                "reason_code": "private_turn",
            },
        )
        self.assertEqual(constructed, [])
        self.assertEqual(hook.last_result["routing_reason"], "restricted_data_class")

    def test_egress_contract_is_fail_closed_and_metadata_never_contains_payload(self):
        cases = [
            (None, "per_turn_policy_missing"),
            ({"version": 1, "decision": "unknown", "data_class": "unknown"}, "per_turn_policy_unknown"),
            ({
                "version": 1,
                "decision": "allow",
                "data_class": "private",
                "allowed_payload": "SYNTHETIC_RESTRICTED_PAYLOAD",
            }, "restricted_data_class"),
            ({
                "version": 1,
                "decision": "allow",
                "data_class": "sanitized",
            }, "per_turn_policy_invalid"),
            ({
                "version": 2,
                "decision": "allow",
                "data_class": "sanitized",
                "allowed_payload": "SYNTHETIC_RESTRICTED_PAYLOAD",
            }, "per_turn_policy_invalid"),
        ]
        for policy, reason in cases:
            with self.subTest(reason=reason):
                evaluation = evaluate_turn_egress_policy(policy)
                self.assertFalse(evaluation.allowed)
                self.assertEqual(evaluation.reason_code, reason)
                self.assertNotIn("SYNTHETIC_RESTRICTED_PAYLOAD", json.dumps(evaluation.metadata))

    def test_explicit_routing_modes_are_observable_and_bounded(self):
        constructed = []

        def forbidden_client():
            constructed.append(True)
            raise AssertionError("non-hosted mode must not construct hosted client")

        candidates = [{"name": "docker-management", "description": "Docker"}]
        off = AutomaticSkillRecommender(
            configured_candidates=candidates,
            routing_mode="off",
            client_factory=forbidden_client,
        ).recommend("Docker maintenance", turn_egress_policy=self._allowed_policy())
        self.assertEqual(off["routing_status"], "disabled")
        self.assertEqual(off["routing_reason"], "routing_mode_off")

        local = AutomaticSkillRecommender(
            configured_candidates=candidates,
            routing_mode="local_only",
            client_factory=forbidden_client,
        ).recommend("Docker maintenance", turn_egress_policy=self._allowed_policy())
        self.assertEqual(local["source"], "local")
        self.assertEqual(local["hosted_skipped"], "routing_mode_local_only")
        self.assertEqual(constructed, [])

    def test_unknown_and_restricted_turns_fail_before_client_construction(self):
        constructed = []

        def forbidden_client():
            constructed.append(True)
            raise AssertionError("policy-denied turn must not construct hosted client")

        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "docker-management", "description": "Docker"}],
            routing_mode="hosted_sanitized",
            public_or_sanitized_data_ack=True,
            client_factory=forbidden_client,
        )
        policies = [
            {
                "version": 1,
                "decision": "unknown",
                "data_class": "unknown",
                "reason_code": "synthetic_unknown",
            },
            {
                "version": 1,
                "decision": "allow",
                "data_class": "employer",
                "reason_code": "synthetic_restricted",
                "allowed_payload": "SYNTHETIC_RESTRICTED_PAYLOAD",
            },
        ]
        reasons = ["per_turn_policy_unknown", "restricted_data_class"]
        for policy, reason in zip(policies, reasons):
            with self.subTest(reason=reason):
                result = recommender.recommend("Docker maintenance", turn_egress_policy=policy)
                self.assertEqual(result["routing_reason"], reason)
                self.assertEqual(result["hosted_attempted"], False)
        self.assertEqual(constructed, [])

    def test_redacted_metadata_excludes_local_and_provider_text(self):
        payloads = []

        def transport(payload):
            payloads.append(payload)
            return {
                "answers": {
                    "skill": {
                        "choice": "docker-management",
                        "confidence": 0.99,
                        "probabilities": {"docker-management": 1.0},
                    },
                    "needs_skill": {"noul": 0.99},
                },
                "usage": {"prompt_tokens": 3, "error": "SYNTHETIC_PROVIDER_ERROR"},
            }

        hook = build_pre_llm_call_hook(
            configured_candidates=[{
                "name": "docker-management",
                "description": "SYNTHETIC_CANDIDATE_DESCRIPTION_MARKER",
            }],
            routing_mode="hosted_sanitized",
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=transport),
        )
        assert hook is not None
        result = hook(
            user_message="SYNTHETIC_TASK_MARKER",
            conversation_history=[{"role": "user", "content": "PRIVATE_HISTORY_MARKER"}],
            turn_egress_policy=self._allowed_policy("SYNTHETIC_SANITIZED_PAYLOAD"),
        )
        serialized_metadata = json.dumps(hook.last_metadata, sort_keys=True)
        serialized_result = json.dumps(hook.last_result, sort_keys=True)
        self.assertIn("metadata", result)
        for marker in (
            "SYNTHETIC_TASK_MARKER",
            "SYNTHETIC_CANDIDATE_DESCRIPTION_MARKER",
            "PRIVATE_HISTORY_MARKER",
            "SYNTHETIC_PROVIDER_ERROR",
        ):
            self.assertNotIn(marker, serialized_metadata)
            self.assertNotIn(marker, serialized_result)
        self.assertIn("SYNTHETIC_SANITIZED_PAYLOAD", json.dumps(payloads[0]))

    def test_real_hermes_loader_registers_hook_when_available(self):
        try:
            from hermes_cli.plugins import PluginManager
        except ImportError as exc:
            self.skipTest(f"Hermes loader unavailable in standalone test environment: {exc}")

        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            home = workspace / "hermes"
            plugin = home / "plugins" / "hermes-switchyard"
            plugin.parent.mkdir(parents=True)
            skill = home / "skills" / "devops" / "docker-management"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(
                "---\nname: docker-management\ndescription: Manage Docker containers.\n---\n# Docker\n",
                encoding="utf-8",
            )
            shutil.copytree(root, plugin)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled:\n    - hermes-switchyard\n", encoding="utf-8"
            )
            empty_bundled = workspace / "empty-bundled"
            empty_bundled.mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "HERMES_HOME": str(home),
                    "HERMES_BUNDLED_PLUGINS": str(empty_bundled),
                },
                clear=False,
            ):
                manager = PluginManager(scope_key=str(home))
                try:
                    manager.discover_and_load()
                    results = manager.invoke_hook(
                        "pre_llm_call",
                        user_message="Diagnose a Docker Compose container",
                        conversation_history=[],
                    )
                    self.assertTrue(any("docker-management" in str(item) for item in results))
                finally:
                    manager.unload()


if __name__ == "__main__":
    unittest.main()
