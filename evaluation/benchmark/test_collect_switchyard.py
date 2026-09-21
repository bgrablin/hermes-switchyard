from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import benchmark
import collect_switchyard


ROOT = Path(__file__).resolve().parents[2]


class SwitchyardCollectorTests(unittest.TestCase):
    def test_runtime_scope_hydrates_before_build_and_cleans(self):
        events: list[str] = []
        agent = types.ModuleType("agent")
        agent.__path__ = []
        scope = types.ModuleType("agent.secret_scope")
        scope.build_profile_secret_scope = lambda home: events.append("build") or {"OPENROUTER_API_KEY": "fixture"}
        scope.set_secret_scope = lambda value: events.append("set") or object()
        scope.reset_secret_scope = lambda token: events.append("reset")
        scope.get_secret = lambda name: events.append("get") or "fixture"
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []
        env_loader = types.ModuleType("hermes_cli.env_loader")
        env_loader.hydrate_profile_secret_sources = lambda home: events.append("hydrate")
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path(tempfile.gettempdir())
        with mock.patch.dict(
            sys.modules,
            {
                "agent": agent,
                "agent.secret_scope": scope,
                "hermes_cli": hermes_cli,
                "hermes_cli.env_loader": env_loader,
                "hermes_constants": constants,
            },
        ), mock.patch.dict(os.environ, {"HERMES_HOME": tempfile.gettempdir()}):
            collect_switchyard._SECRET_SCOPE_TOKEN = None
            self.assertEqual(collect_switchyard._runtime_key(), "fixture")
            self.assertEqual(events[:3], ["hydrate", "build", "set"])
            collect_switchyard._reset_runtime_secret_scope()
        self.assertEqual(events[-1], "reset")

    def test_hydrate_helper_optional_when_scope_has_key(self):
        """Hermes 0.19 has secret_scope but no hydrate_profile_secret_sources."""
        events: list[str] = []
        agent = types.ModuleType("agent")
        agent.__path__ = []
        scope = types.ModuleType("agent.secret_scope")
        scope.build_profile_secret_scope = lambda home: events.append("build") or {
            "OPENROUTER_API_KEY": "fixture"
        }
        scope.set_secret_scope = lambda value: events.append("set") or object()
        scope.reset_secret_scope = lambda token: events.append("reset")
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []
        # Module exists but lacks hydrate_profile_secret_sources (ImportError path uses try/except ImportError
        # on from-import; AttributeError is also tolerated by our except Exception → but we raise
        # hermes_secret_scope_unavailable for generic Exception. So model missing attr via ImportError:
        # omit hermes_cli.env_loader entirely so `from hermes_cli.env_loader import ...` raises ImportError.
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path(tempfile.gettempdir())
        with mock.patch.dict(
            sys.modules,
            {
                "agent": agent,
                "agent.secret_scope": scope,
                "hermes_cli": hermes_cli,
                "hermes_constants": constants,
            },
            clear=False,
        ):
            # Ensure env_loader is not present
            sys.modules.pop("hermes_cli.env_loader", None)
            collect_switchyard._SECRET_SCOPE_TOKEN = None
            collect_switchyard._hydrate_runtime_secret_scope()
            self.assertIsNotNone(collect_switchyard._SECRET_SCOPE_TOKEN)
            self.assertEqual(events, ["build", "set"])
            collect_switchyard._reset_runtime_secret_scope()
        self.assertEqual(events[-1], "reset")

    def test_env_fallback_when_scope_lacks_openrouter_key(self):
        events: list[str] = []
        captured: dict = {}
        agent = types.ModuleType("agent")
        agent.__path__ = []
        scope = types.ModuleType("agent.secret_scope")
        scope.build_profile_secret_scope = lambda home: events.append("build") or {}
        def fake_set(value):
            events.append("set")
            captured.update(value)
            return object()
        scope.set_secret_scope = fake_set
        scope.reset_secret_scope = lambda token: events.append("reset")
        hermes_cli = types.ModuleType("hermes_cli")
        hermes_cli.__path__ = []
        constants = types.ModuleType("hermes_constants")
        constants.get_hermes_home = lambda: Path(tempfile.gettempdir())
        with mock.patch.dict(
            sys.modules,
            {
                "agent": agent,
                "agent.secret_scope": scope,
                "hermes_cli": hermes_cli,
                "hermes_constants": constants,
            },
            clear=False,
        ), mock.patch.dict(
            os.environ,
            {"OPENROUTER_API_KEY": "env-fallback-key"},
            clear=False,
        ):
            sys.modules.pop("hermes_cli.env_loader", None)
            collect_switchyard._SECRET_SCOPE_TOKEN = None
            collect_switchyard._hydrate_runtime_secret_scope()
            self.assertEqual(captured.get("OPENROUTER_API_KEY"), "env-fallback-key")
            collect_switchyard._reset_runtime_secret_scope()
        self.assertEqual(events, ["build", "set", "reset"])


    def test_collect_writes_24_actual_rows_and_honors_source_identity(self):
        book, meta = benchmark.load_book()
        source = benchmark.source_hashes(ROOT)
        calls = {"count": 0}

        class FakeDecisionClient:
            def __init__(self, **_kwargs):
                pass

            def _post(self, _payload):
                calls["count"] += 1
                return {}

            def decide(self, *_args, **_kwargs):
                self._post({})
                return {
                    "model": "typesafe/jev-1.13-20260917",
                    "latency_ms": 1.0,
                    "usage": {},
                    "answers": {},
                }

        fake_client_module = types.SimpleNamespace(
            DecisionClient=FakeDecisionClient,
            EXPECTED_MODEL="typesafe/jev-1.13",
        )

        class FakeRouting:
            @staticmethod
            def select_skill(*, task, candidates, client, public_or_sanitized_data_ack):
                self_result = client.decide({}, {}, public_or_sanitized_data_ack=public_or_sanitized_data_ack)
                return {
                    "status": "selected",
                    "selected": candidates[0]["name"],
                    "model": self_result["model"],
                    "latency_ms": self_result["latency_ms"],
                    "usage": self_result["usage"],
                    "abstention_reason": None,
                }

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "switchyard.json"
            args = argparse.Namespace(
                live=True,
                public_synthetic_ack=True,
                max_requests=24,
                plugin_path=str(ROOT),
                output=output,
                resume=False,
            )
            with mock.patch.object(collect_switchyard.benchmark, "import_plugin", return_value=(FakeRouting, fake_client_module, source)), \
                 mock.patch.object(collect_switchyard, "_runtime_key", return_value="fixture"):
                self.assertEqual(collect_switchyard.collect(args), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(calls["count"], 24)
        self.assertEqual(len(payload["records"]), 24)
        self.assertTrue(all(row["measurement_status"] == "ok" for row in payload["records"]))
        self.assertTrue(all(row["collector_source_hash"] == meta["collector_hashes"]["switchyard"] for row in payload["records"]))
        self.assertEqual(payload["collector_source_hash"], meta["collector_hashes"]["switchyard"])

    def test_provider_response_is_recorded_when_routing_validation_fails(self):
        book, _meta = benchmark.load_book()
        source = benchmark.source_hashes(ROOT)

        class FakeDecisionClient:
            def __init__(self, **_kwargs):
                pass

            def _post(self, _payload):
                return {"malformed": True}

            def decide(self, *_args, **_kwargs):
                return self._post({})

        fake_client_module = types.SimpleNamespace(
            DecisionClient=FakeDecisionClient,
            EXPECTED_MODEL="typesafe/jev-1.13",
        )

        class FakeRouting:
            @staticmethod
            def select_skill(*, client, **_kwargs):
                client.decide({})
                raise ValueError("provider_response_failed_validation")

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "switchyard.json"
            args = argparse.Namespace(
                live=True,
                public_synthetic_ack=True,
                max_requests=24,
                plugin_path=str(ROOT),
                output=output,
                resume=False,
            )
            with mock.patch.object(
                collect_switchyard.benchmark,
                "import_plugin",
                return_value=(FakeRouting, fake_client_module, source),
            ), mock.patch.object(collect_switchyard, "_runtime_key", return_value="fixture"):
                self.assertEqual(collect_switchyard.collect(args), 0)
            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(len(payload["records"]), len(book["heldout_fixtures"]))
        self.assertTrue(all(row["measurement_status"] == "failed" for row in payload["records"]))
        self.assertTrue(
            all(row["measurement_provenance"]["provider_response_observed"] for row in payload["records"])
        )


if __name__ == "__main__":
    unittest.main()
