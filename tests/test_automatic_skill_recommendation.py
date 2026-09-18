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
    build_pre_llm_call_hook,
    extract_available_skill_candidates,
)
from jev_decision.client import DecisionClient


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

    def test_local_lifecycle_hook_selects_relevant_skill_and_abstains_on_no_fit(self):
        import jev_decision

        context = _Context()
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
        self.assertIsNone(no_fit)

    def test_available_skill_parser_uses_system_index_only(self):
        candidates = extract_available_skill_candidates(self._history())
        self.assertEqual(
            [candidate["name"] for candidate in candidates],
            ["docker-management", "network-printer-operations"],
        )
        serialized = json.dumps(candidates)
        self.assertNotIn("PRIVATE_HISTORY_MARKER", serialized)

    def test_available_skill_catalog_survives_long_system_prefix(self):
        prefix = "System context. " * 400
        history = [{
            "role": "system",
            "content": prefix + (
                "\n<available_skills>\n"
                "  devops:\n"
                "    - docker-management: Manage Docker containers and Compose services.\n"
                "</available_skills>"
            ),
        }]
        candidates = extract_available_skill_candidates(history)
        self.assertEqual([candidate["name"] for candidate in candidates], ["docker-management"])

    def test_available_skill_catalog_close_tag_can_follow_large_catalog(self):
        entries = [
            f"    - synthetic-skill-{index}: " + ("public catalog description " * 12)
            for index in range(40)
        ]
        entries.append("    - docker-management: Manage Docker containers and Compose services.")
        content = "prefix\n<available_skills>\n" + "\n".join(entries) + "\n</available_skills>"
        self.assertGreater(len(content), 4_000)
        candidates = extract_available_skill_candidates([{"role": "system", "content": content}])
        self.assertIn("docker-management", [candidate["name"] for candidate in candidates])

    def test_realistic_hermes_available_skills_payload_is_discovered(self):
        content = (
            "## Hermes Agent\n\n"
            "Available capabilities and policy context.\n\n"
            "<available_skills>\n"
            "  devops:\n"
            "    - docker-management: Manage Docker containers, images, and Compose.\n"
            "  smart-home:\n"
            "    - network-printer-operations: Operate network printers and scanners.\n"
            "</available_skills>\n"
        )
        candidates = extract_available_skill_candidates([{"role": "system", "content": content}])
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
            public_or_sanitized_data_ack=True,
            client_factory=lambda: DecisionClient(api_key="fixture-key", transport=transport),
        )
        assert hook is not None
        result = hook(
            user_message="public Docker maintenance request",
            conversation_history=[
                {
                    "role": "system",
                    "content": (
                        "<available_skills>\n"
                        "    - docker-management: SYNTHETIC_PROMPT_DESCRIPTION_MARKER\n"
                        "</available_skills>"
                    ),
                },
                {"role": "user", "content": "PRIVATE_HISTORY_MARKER"},
            ],
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
            public_or_sanitized_data_ack=True,
            client_factory=client_factory,
        )
        result = hook(
            user_message="public Docker maintenance request",
            conversation_history=[
                {"role": "system", "content": "PRIVATE_HISTORY_MARKER"},
                {"role": "assistant", "content": "private prior turn"},
            ],
        )
        self.assertIsNotNone(result)
        self.assertIn("docker-management", result["context"])
        self.assertEqual(len(payloads), 1)
        wire = json.dumps(payloads[0], sort_keys=True)
        self.assertNotIn("PRIVATE_HISTORY_MARKER", wire)
        self.assertNotIn("private prior turn", wire)
        self.assertNotIn("SYNTHETIC_CONFIGURED_DESCRIPTION_MARKER", wire)
        self.assertIn("docker-management", wire)
        self.assertIn("public Docker maintenance request", wire)
        self.assertEqual(payloads[0]["provider"], {"allow_fallbacks": False})

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
        self.assertEqual(constructed, [])

    def test_real_hermes_loader_registers_hook_when_available(self):
        try:
            from hermes_cli.plugins import PluginManager
        except ImportError as exc:
            self.skipTest(f"Hermes loader unavailable in standalone test environment: {exc}")

        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            home = workspace / "hermes"
            plugin = home / "plugins" / "jev-decision"
            plugin.parent.mkdir(parents=True)
            shutil.copytree(root, plugin)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled:\n    - jev-decision\n", encoding="utf-8"
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
                        conversation_history=self._history(),
                    )
                    self.assertTrue(any("docker-management" in str(item) for item in results))
                finally:
                    manager.unload()


if __name__ == "__main__":
    unittest.main()
