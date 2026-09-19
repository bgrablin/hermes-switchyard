"""Regression tests for the complete PR 18 Astra review."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from unittest import mock

import hermes_switchyard
from hermes_switchyard.client import DecisionClient
from hermes_switchyard.computer_use import _semantic_action_state
from hermes_switchyard.routing import select_skills


class AstraReviewRegressionTests(unittest.TestCase):
    def test_provider_answer_type_discriminator_is_validated_then_removed(self):
        def transport(_payload):
            return {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "pick": {
                        "type": "choice",
                        "choice": "a",
                        "probabilities": {"a": 1.0},
                        "confidence": 1.0,
                    }
                },
                "usage": {},
            }

        result = DecisionClient(api_key="fixture", transport=transport).decide(
            {},
            {
                "pick": {
                    "type": "choice",
                    "instructions": "Choose one.",
                    "criteria": {"a": "A"},
                }
            },
            public_or_sanitized_data_ack=True,
        )
        self.assertEqual(
            result["answers"]["pick"],
            {"choice": "a", "probabilities": {"a": 1.0}, "confidence": 1.0},
        )

    def test_mismatched_provider_answer_type_fails_closed(self):
        def transport(_payload):
            return {
                "model": "typesafe/jev-1.13",
                "answers": {
                    "pick": {
                        "type": "score",
                        "choice": "a",
                        "probabilities": {"a": 1.0},
                        "confidence": 1.0,
                    }
                },
                "usage": {},
            }

        with self.assertRaisesRegex(ValueError, "type"):
            DecisionClient(api_key="fixture", transport=transport).decide(
                {},
                {
                    "pick": {
                        "type": "choice",
                        "instructions": "Choose one.",
                        "criteria": {"a": "A"},
                    }
                },
                public_or_sanitized_data_ack=True,
            )

    def test_native_hermes_string_effect_and_dict_verdict_are_preserved(self):
        state = _semantic_action_state(
            {
                "ok": True,
                "effect": "confirmed",
                "verified": True,
                "verdict": {"decision": "done"},
            }
        )
        self.assertEqual(state["effect_confirmed"], True)
        self.assertEqual(state["effect_status"], "confirmed")
        self.assertEqual(state["verdict"], "done")
        self.assertEqual(state["escalation"], "none")

        escalated = _semantic_action_state(
            {"effect": "unconfirmed", "verdict": {"decision": "escalate"}}
        )
        self.assertEqual(escalated["effect_confirmed"], False)
        self.assertEqual(escalated["verdict"], "escalate")
        self.assertEqual(escalated["escalation"], "required")

    def test_multi_skill_questions_bind_each_exact_candidate(self):
        seen = []

        class Client:
            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                seen.append((state, questions))
                return {
                    "model": "typesafe/jev-1.13",
                    "answers": {name: {"noul": 0.9} for name in questions},
                    "usage": {},
                }

        candidates = [
            {"name": "skill-a", "description": "Alpha capability"},
            {"name": "skill-b", "description": "Beta capability"},
        ]
        select_skills(
            task="public task",
            candidates=candidates,
            client=Client(),
            public_or_sanitized_data_ack=True,
        )
        instructions = [
            question["instructions"]
            for _state, questions in seen
            for question in questions.values()
        ]
        self.assertTrue(any("skill-a" in text and "Alpha capability" in text for text in instructions))
        self.assertTrue(any("skill-b" in text and "Beta capability" in text for text in instructions))
        self.assertEqual(len(set(instructions)), 2)

    def test_registered_multi_skill_default_deadline_uses_client_constant(self):
        class Context:
            def __init__(self):
                self.tools = {}

            def get_config(self, _key, default=None):
                return default

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_auxiliary_task(self, *_args, **_kwargs):
                pass

            def register_skill(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

        context = Context()
        hermes_switchyard.register(context)
        fake_result = {
            "status": "selected",
            "selected": ["skill-a"],
            "scores": {"skill-a": 1.0},
        }
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture"), \
             mock.patch.object(hermes_switchyard, "select_skills", return_value=fake_result) as select:
            result = json.loads(context.tools["jev_skill_select_many"]({
                "task": "public task",
                "candidates": [{"name": "skill-a", "description": "A"}],
                "public_or_sanitized_data_ack": True,
            }))
        self.assertEqual(result["status"], "selected")
        self.assertEqual(select.call_args.kwargs["deadline_seconds"], hermes_switchyard.DEFAULT_OPERATION_DEADLINE_SECONDS)

    def test_redacted_metadata_retains_bounded_routing_receipt_fields(self):
        from hermes_switchyard.automatic import _copy_redacted_jev_metadata

        result = {}
        _copy_redacted_jev_metadata(
            result,
            {
                "model": "typesafe/jev-1.13",
                "request_id": "req_public_123",
                "shortlist_policy": "recursive_partition",
                "usage": {"input_tokens": 10, "cost": None},
                "total_usage": {"input_tokens": 20, "cost": None},
            },
        )
        self.assertEqual(result["jev_model"], "typesafe/jev-1.13")
        self.assertEqual(result["jev_request_id"], "req_public_123")
        self.assertEqual(result["jev_shortlist_policy"], "recursive_partition")
        self.assertEqual(result["jev_usage"]["input_tokens"], 10)
        self.assertIsNone(result["jev_usage"]["cost"])
        self.assertEqual(result["jev_total_usage"]["input_tokens"], 20)
        self.assertIsNone(result["jev_total_usage"]["cost"])

    def test_registered_handlers_forward_explicit_aggregate_deadlines(self):
        class Context:
            def __init__(self):
                self.tools = {}

            def get_config(self, _key, default=None):
                return default

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_auxiliary_task(self, *_args, **_kwargs): pass
            def register_skill(self, *_args, **_kwargs): pass
            def register_hook(self, *_args, **_kwargs): pass

        context = Context()
        hermes_switchyard.register(context)
        args = {
            "task": "public task",
            "candidates": [{"name": "skill-a", "description": "A"}],
            "requirements": {},
            "public_or_sanitized_data_ack": True,
            "deadline_seconds": 12.5,
        }
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture"), \
             mock.patch.object(hermes_switchyard, "select_skill", return_value={"status": "selected"}) as one, \
             mock.patch.object(hermes_switchyard, "route_model", return_value={"status": "selected"}) as route:
            context.tools["jev_skill_select"](args)
            context.tools["jev_model_route"](args)
        self.assertEqual(one.call_args.kwargs["deadline_seconds"], 12.5)
        self.assertEqual(route.call_args.kwargs["deadline_seconds"], 12.5)

    def test_registered_computer_dispatch_preserves_host_context_kwargs(self):
        forwarded = []

        class Context:
            def __init__(self):
                self.tools = {}

            def get_config(self, _key, default=None):
                return default

            def register_tool(self, *, name, handler, **_kwargs):
                self.tools[name] = handler

            def register_auxiliary_task(self, *_args, **_kwargs): pass
            def register_skill(self, *_args, **_kwargs): pass
            def register_hook(self, *_args, **_kwargs): pass

            def dispatch_tool(self, tool_name, args, **kwargs):
                forwarded.append((tool_name, args, kwargs))
                return {"ok": True}

        def fake_run(**kwargs):
            kwargs["dispatch"]("computer_use", {"action": "capture", "app": "Chrome"})
            self.assertEqual(kwargs["deadline_seconds"], 9.0)
            return {"status": "blocked"}

        context = Context()
        hermes_switchyard.register(context)
        parent = object()
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture"), \
             mock.patch.object(hermes_switchyard, "run_computer_goal", side_effect=fake_run):
            context.tools["jev_computer_use"](
                {
                    "goal": "public goal",
                    "app": "Chrome",
                    "deadline_seconds": 9.0,
                    "public_or_sanitized_data_ack": True,
                },
                parent_agent=parent,
                session_id="session-1",
            )
        self.assertEqual(forwarded[0][2]["parent_agent"], parent)
        self.assertEqual(forwarded[0][2]["session_id"], "session-1")

    def test_registered_cache_identity_uses_resolved_route_profile_and_secret_hash(self):
        captured = {}

        class Context:
            def __init__(self):
                self.settings = {
                    "jev_provider": "openrouter",
                    "api_endpoint": hermes_switchyard.DEFAULT_ENDPOINT,
                    "jev_model": "typesafe/jev-1.13",
                }

            def get_config(self, key, default=None):
                return self.settings.get(key, default)

            def register_auxiliary_task(self, *_args, **_kwargs): pass
            def register_tool(self, **_kwargs): pass
            def register_skill(self, *_args, **_kwargs): pass
            def register_hook(self, *_args, **_kwargs): pass

        def fake_hook(**kwargs):
            captured.update(kwargs)
            return None

        active = {"secret": "secret-a"}
        with mock.patch.object(hermes_switchyard, "build_pre_llm_call_hook", side_effect=fake_hook), \
             mock.patch.object(hermes_switchyard, "_secret", side_effect=lambda _provider: active["secret"]), \
             mock.patch.dict("os.environ", {"HERMES_PROFILE": "profile-a"}, clear=False):
            hermes_switchyard.register(Context())
            first = captured["cache_identity"]()
            active["secret"] = "secret-b"
            second = captured["cache_identity"]()

        self.assertEqual(first["provider"], "openrouter")
        self.assertEqual(first["endpoint"], hermes_switchyard.DEFAULT_ENDPOINT)
        self.assertEqual(first["model"], "typesafe/jev-1.13")
        self.assertEqual(first["profile"], "profile-a")
        self.assertRegex(first["credential_sha256"], r"^[0-9a-f]{64}$")
        self.assertNotEqual(first["credential_sha256"], second["credential_sha256"])
        self.assertNotIn("secret-a", json.dumps(first, sort_keys=True))
        self.assertNotIn("secret-b", json.dumps(second, sort_keys=True))

    def test_cli_live_test_makes_one_explicit_billed_synthetic_request_and_closes(self):
        instances = []

        class Client:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.calls = []
                self.closed = False
                instances.append(self)

            def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
                self.calls.append((state, questions, public_or_sanitized_data_ack))
                return {
                    "model": self.kwargs["model"],
                    "answers": {"connectivity": {"noul": 1.0}},
                    "usage": {"cost": None},
                    "latency_ms": 12.0,
                }

            def close(self):
                self.closed = True

        args = SimpleNamespace(
            switchyard_command="test",
            live=True,
            public_or_sanitized_data_ack=True,
            provider="openrouter",
        )
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture"), \
             mock.patch.object(hermes_switchyard, "DecisionClient", Client):
            self.assertEqual(hermes_switchyard._cli_handler(args), 0)

        self.assertEqual(len(instances), 1)
        self.assertEqual(len(instances[0].calls), 1)
        state, questions, ack = instances[0].calls[0]
        self.assertEqual(state["data_class"], "public_synthetic")
        self.assertEqual(set(questions), {"connectivity"})
        self.assertIs(ack, True)
        self.assertTrue(instances[0].closed)


if __name__ == "__main__":
    unittest.main()
