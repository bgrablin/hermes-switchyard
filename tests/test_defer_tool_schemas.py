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
    filter_switchyard_tool_schemas,
    history_has_switchyard_tool_call,
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


if __name__ == "__main__":
    unittest.main()
