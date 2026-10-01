"""Opt-in deferral of hermes_switchyard decision-tool schemas (default off).

Flag ``defer_switchyard_tool_schemas``: when on, skill-route-only / light oneshots
omit Switchyard decision-tool schemas from the main model tools list while
``pre_llm_call`` skill routing stays unchanged. Fail-open keeps schemas when
tools may be needed.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from hermes_switchyard.defer_tool_schemas import (
    DEFERRED_SWITCHYARD_TOOL_NAMES,
    build_defer_tool_schemas_middleware,
    compose_llm_request_middleware,
    filter_switchyard_tool_schemas,
    history_has_switchyard_tool_call,
    latest_user_text,
    register_defer_tool_schemas_middleware,
    should_omit_switchyard_tool_schemas,
    tool_definition_name,
    user_requests_switchyard_tools,
)


def _openai_tool(name: str, *, description: str = "fixture") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _responses_tool(name: str) -> dict:
    return {
        "type": "function",
        "name": name,
        "description": "fixture",
        "parameters": {"type": "object", "properties": {}},
    }


def _skill_route_request(*, user: str = "Fix the network printer that is unreachable.") -> dict:
    """Skill-route-only oneshot: Switchyard + skills/terminal tools, no jev_* ask."""
    tools = [
        _openai_tool("skill_view"),
        _openai_tool("terminal"),
        *[_openai_tool(name) for name in sorted(DEFERRED_SWITCHYARD_TOOL_NAMES)],
    ]
    return {
        "model": "fixture-model",
        "messages": [{"role": "user", "content": user}],
        "tools": tools,
    }


class ToolDefHelpersTests(unittest.TestCase):
    def test_tool_definition_name_openai_and_responses(self):
        self.assertEqual(tool_definition_name(_openai_tool("jev_assess")), "jev_assess")
        self.assertEqual(tool_definition_name(_responses_tool("jev_skill_select")), "jev_skill_select")
        self.assertIsNone(tool_definition_name({"type": "web_search"}))

    def test_filter_removes_only_deferred_switchyard_tools(self):
        tools = [
            _openai_tool("terminal"),
            _openai_tool("jev_assess"),
            _openai_tool("jev_computer_use"),
            _openai_tool("jev_skill_select"),
            _responses_tool("web_search"),
        ]
        kept = filter_switchyard_tool_schemas(tools)
        names = [tool_definition_name(t) for t in kept]
        self.assertEqual(names, ["terminal", "jev_computer_use", "web_search"])


class PredicateTests(unittest.TestCase):
    def test_flag_off_never_omits(self):
        request = _skill_route_request()
        self.assertFalse(
            should_omit_switchyard_tool_schemas(enabled=False, request=request)
        )

    def test_flag_on_omits_on_skill_route_only(self):
        request = _skill_route_request()
        self.assertTrue(
            should_omit_switchyard_tool_schemas(enabled=True, request=request)
        )

    def test_flag_on_omits_on_light_greeting(self):
        request = _skill_route_request(user="thanks")
        self.assertTrue(
            should_omit_switchyard_tool_schemas(enabled=True, request=request)
        )

    def test_flag_on_keeps_when_user_asks_for_jev_tool(self):
        request = _skill_route_request(
            user="Please call jev_skill_select to choose a skill for this printer task."
        )
        self.assertFalse(
            should_omit_switchyard_tool_schemas(enabled=True, request=request)
        )
        self.assertTrue(user_requests_switchyard_tools(request["messages"][0]["content"]))

    def test_flag_on_keeps_when_history_called_switchyard_tool(self):
        request = {
            "messages": [
                {"role": "user", "content": "rerank these sessions"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "1",
                            "type": "function",
                            "function": {
                                "name": "jev_session_search_rerank",
                                "arguments": "{}",
                            },
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": "1", "content": "{}"},
                {"role": "user", "content": "try again"},
            ],
            "tools": [
                _openai_tool("terminal"),
                _openai_tool("jev_session_search_rerank"),
            ],
        }
        self.assertTrue(history_has_switchyard_tool_call(request))
        self.assertFalse(
            should_omit_switchyard_tool_schemas(enabled=True, request=request)
        )

    def test_flag_on_keeps_when_toolset_is_switchyard_primary(self):
        request = {
            "messages": [{"role": "user", "content": "pick a skill"}],
            "tools": [_openai_tool(name) for name in sorted(DEFERRED_SWITCHYARD_TOOL_NAMES)],
        }
        self.assertFalse(
            should_omit_switchyard_tool_schemas(enabled=True, request=request)
        )


class MiddlewareTests(unittest.TestCase):
    def test_middleware_none_when_flag_off(self):
        self.assertIsNone(build_defer_tool_schemas_middleware(enabled=False))

    def test_middleware_flag_off_path_via_register(self):
        ctx = SimpleNamespace(register_middleware=lambda *_a, **_k: None)
        status = register_defer_tool_schemas_middleware(ctx, enabled=False)
        self.assertEqual(status["registered"], False)
        self.assertEqual(status["reason"], "flag_off")

    def test_middleware_omits_schemas_when_flag_on_skill_route(self):
        callback = build_defer_tool_schemas_middleware(enabled=True)
        self.assertIsNotNone(callback)
        request = _skill_route_request()
        before = [tool_definition_name(t) for t in request["tools"]]
        self.assertTrue(set(before) & DEFERRED_SWITCHYARD_TOOL_NAMES)
        result = callback(request=request)
        self.assertIsNotNone(result)
        self.assertEqual(result["reason"], "defer_switchyard_tool_schemas")
        after = [tool_definition_name(t) for t in result["request"]["tools"]]
        self.assertEqual(set(after) & DEFERRED_SWITCHYARD_TOOL_NAMES, set())
        self.assertIn("terminal", after)
        self.assertIn("skill_view", after)

    def test_middleware_keeps_schemas_when_tools_needed(self):
        callback = build_defer_tool_schemas_middleware(enabled=True)
        request = _skill_route_request(
            user="Use jev_model_route to recommend a model for this task."
        )
        result = callback(request=request)
        self.assertIsNone(result)

    def test_middleware_preserves_jev_computer_use(self):
        callback = build_defer_tool_schemas_middleware(enabled=True)
        request = {
            "messages": [{"role": "user", "content": "list cwd read-only"}],
            "tools": [
                _openai_tool("terminal"),
                _openai_tool("jev_assess"),
                _openai_tool("jev_computer_use"),
            ],
        }
        result = callback(request=request)
        self.assertIsNotNone(result)
        names = [tool_definition_name(t) for t in result["request"]["tools"]]
        self.assertEqual(names, ["terminal", "jev_computer_use"])

    def test_register_hooks_middleware_when_enabled(self):
        seen = []

        def register_middleware(kind, callback):
            seen.append((kind, callback))

        ctx = SimpleNamespace(register_middleware=register_middleware)
        status = register_defer_tool_schemas_middleware(ctx, enabled=True)
        self.assertTrue(status["registered"])
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], "llm_request")
        self.assertTrue(callable(seen[0][1]))


class ScalarResponsesInputTests(unittest.TestCase):
    def test_scalar_input_string_is_visible_to_cue_detection(self):
        request = {
            "model": "fixture-model",
            "input": "Use jev_model_route to pick a cheaper model for this turn.",
            "tools": [
                _openai_tool("terminal"),
                *[_openai_tool(name) for name in sorted(DEFERRED_SWITCHYARD_TOOL_NAMES)],
            ],
        }
        self.assertIn("jev_model_route", latest_user_text(request))
        # Explicit cue => fail-open keep schemas.
        self.assertFalse(
            should_omit_switchyard_tool_schemas(enabled=True, request=request)
        )

    def test_scalar_input_without_cue_may_omit(self):
        request = {
            "model": "fixture-model",
            "input": "Fix the network printer that is unreachable.",
            "tools": [
                _openai_tool("terminal"),
                *[_openai_tool(name) for name in sorted(DEFERRED_SWITCHYARD_TOOL_NAMES)],
            ],
        }
        self.assertTrue(should_omit_switchyard_tool_schemas(enabled=True, request=request))


class ComposeMiddlewareTests(unittest.TestCase):
    def test_compose_applies_effort_then_schema_filter(self):
        def effort_mw(request=None, **_):
            out = dict(request or {})
            out["reasoning_effort"] = "low"
            out["effort_receipt"] = "adapted"
            return {"request": out, "source": "adaptive_effort"}

        defer_mw = build_defer_tool_schemas_middleware(enabled=True)
        composed = compose_llm_request_middleware(effort_mw, defer_mw)
        request = _skill_route_request()
        request["reasoning_effort"] = "high"
        result = composed(request=request)
        self.assertIsNotNone(result)
        updated = result["request"]
        # Both transforms must land: effort rewrite kept, schemas omitted.
        self.assertEqual(updated.get("reasoning_effort"), "low")
        self.assertEqual(updated.get("effort_receipt"), "adapted")
        names = [tool_definition_name(t) for t in updated.get("tools") or []]
        self.assertNotIn("jev_assess", names)
        self.assertIn("terminal", names)

    def test_hermes_last_wins_without_compose_drops_effort(self):
        """Document why composition is required under last-callback-wins."""
        def effort_mw(request=None, **_):
            out = dict(request or {})
            out["reasoning_effort"] = "low"
            return {"request": out}

        defer_mw = build_defer_tool_schemas_middleware(enabled=True)
        original = _skill_route_request()
        original["reasoning_effort"] = "high"
        # Simulate Hermes: each callback sees the original payload.
        first = effort_mw(request=original)
        second = defer_mw(request=original)
        last = second or first
        # Defer returns a shallow copy of the original → effort rewrite lost.
        self.assertEqual(last["request"].get("reasoning_effort"), "high")


class ProviderBoundaryTests(unittest.TestCase):
    def test_anthropic_tool_result_preserves_original_explicit_request(self):
        request = _skill_route_request(user="Use jev_model_route after reading the file.")
        request["messages"].extend([
            {"role": "assistant", "content": [{"type": "tool_use", "id": "read-1", "name": "read_file", "input": {}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "read-1", "content": "read result"}]},
        ])
        self.assertIn("jev_model_route", latest_user_text(request))
        self.assertIsNone(build_defer_tool_schemas_middleware(enabled=True)(request=request))

    def test_non_text_block_with_text_cannot_mask_prompt(self):
        request = _skill_route_request(user="Use jev_model_route after reading the file.")
        request["messages"].append({"role": "user", "content": [
            {"type": "tool_result", "text": "read result", "tool_use_id": "read-1"},
        ]})
        self.assertIn("jev_model_route", latest_user_text(request))
        self.assertIsNone(build_defer_tool_schemas_middleware(enabled=True)(request=request))

    def test_clarification_keeps_unfulfilled_explicit_tool_request(self):
        request = _skill_route_request(user="Use jev_model_route after reading the file.")
        request["messages"].append({"role": "user", "content": "README.md"})
        self.assertEqual(latest_user_text(request), "README.md")
        self.assertIsNone(build_defer_tool_schemas_middleware(enabled=True)(request=request))

    def test_anthropic_prior_tool_use_preserves_schema(self):
        request = _skill_route_request(user="Continue.")
        request["messages"].insert(0, {"role": "assistant", "content": [
            {"type": "tool_use", "id": "jev-1", "name": "jev_assess", "input": {}},
        ]})
        self.assertTrue(history_has_switchyard_tool_call(request))
        self.assertIsNone(build_defer_tool_schemas_middleware(enabled=True)(request=request))

    def test_forced_tool_choices_and_unknown_input_fail_open(self):
        callback = build_defer_tool_schemas_middleware(enabled=True)
        for choice in ({"type": "function", "function": {"name": "jev_assess"}},
                       {"type": "function", "name": "jev_assess"},
                       {"type": "tool", "name": "jev_assess"}):
            request = {**_skill_route_request(), "tool_choice": choice}
            self.assertIsNone(callback(request=request))
        request = _skill_route_request()
        request["messages"] = [{"role": "user", "content": [{"type": "image", "source": {}}]}]
        self.assertIsNone(callback(request=request))

    def test_mixed_multimodal_inputs_keep_all_schemas(self):
        callback = build_defer_tool_schemas_middleware(enabled=True)
        for block in ({"type": "image_url", "image_url": {}},
                      {"type": "image", "source": {}},
                      {"type": "document", "source": {}},
                      {"type": "unknown", "text": "not a text block"}):
            request = _skill_route_request()
            request["messages"] = [{"role": "user", "content": [
                {"type": "text", "text": "Explain this."}, block,
            ]}]
            self.assertIsNone(callback(request=request))

    def test_full_switchyard_and_computer_pin_keeps_decision_tools(self):
        request = _skill_route_request()
        request["tools"] = [_openai_tool(name) for name in (
            *sorted(DEFERRED_SWITCHYARD_TOOL_NAMES), "jev_computer_use", "computer_use",
        )]
        self.assertIsNone(build_defer_tool_schemas_middleware(enabled=True)(request=request))

    def test_real_registration_composes_and_reports_effective_effort_seam(self):
        import json
        import tempfile
        from unittest.mock import patch
        import hermes_switchyard as plugin
        from hermes_switchyard import reasoning_effort_adapter as adapter
        from test_readme_capabilities import _RecordingContext

        class Context(_RecordingContext):
            def __init__(self):
                super().__init__()
                self.callbacks = []
                self.hook_callbacks = []

            def get_config(self, key, default=None):
                if key == "defer_switchyard_tool_schemas":
                    return True
                return default

            def register_middleware(self, name, callback, **kwargs):
                super().register_middleware(name, callback, **kwargs)
                self.callbacks.append((name, callback))

            def register_hook(self, name, callback):
                super().register_hook(name, callback)
                self.hook_callbacks.append((name, callback))

        with tempfile.TemporaryDirectory() as home, patch.dict("os.environ", {"HERMES_HOME": home}):
            ctx = Context()
            plugin.register(ctx)
            callbacks = [cb for name, cb in ctx.callbacks if name == "llm_request"]
            self.assertEqual(len(callbacks), 1)
            self.assertTrue(plugin._RUNTIME_STATUS["reasoning_effort_adapter"]["llm_request_registered"])
            self.assertTrue(adapter.last_registration()["llm_request_registered"])
            json.dumps(plugin._RUNTIME_STATUS)
            import contextlib
            import io
            output = io.StringIO()
            with patch.object(plugin, "_secret", return_value=None), \
                    patch.object(plugin, "_tool_exposure_report", return_value=plugin._unavailable_exposure("test")), \
                    patch.object(plugin, "_toolset_composition", return_value={}), \
                    patch.object(plugin.legacy_cleanup, "status_warnings", return_value=[]), \
                    contextlib.redirect_stdout(output):
                plugin._cli_handler(SimpleNamespace(switchyard_command="status", json_output=True))
            status = json.loads(output.getvalue())
            self.assertTrue(status["defer_tool_schemas"]["registered"])
            self.assertTrue(status["defer_tool_schemas"]["composed_with_prior"])

            # Exercise the real effort controller and composition, not a fake rewrite.
            capture = [cb for name, cb in ctx.hook_callbacks if name == "pre_llm_call"][-1]
            capture(session_id="schema-test", turn_id="turn-1", user_message="thanks")
            request = _skill_route_request(user="thanks")
            request["reasoning_effort"] = "high"
            updated = callbacks[0](request=request, session_id="schema-test", turn_id="turn-1")["request"]
            self.assertEqual(updated["reasoning_effort"], "minimal")
            self.assertNotIn("jev_assess", [tool_definition_name(t) for t in updated["tools"]])
            self.assertEqual(request["reasoning_effort"], "high")


if __name__ == "__main__":
    unittest.main()
