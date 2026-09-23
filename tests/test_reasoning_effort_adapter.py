"""Offline tests for Jev adaptive reasoning-effort middleware (Hermes 0.21)."""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace

from hermes_switchyard.reasoning_effort_adapter import (
    HERMES_REASONING_EFFORTS,
    ReasoningEffortController,
    apply_effort_to_request,
    choose_reasoning_effort,
    clamp_effort_for_provider,
    derive_tool_failure,
    last_receipt,
    last_registration,
    normalize_effort,
    probe_llm_request_middleware_seam,
    register_reasoning_effort_adapter,
    wire_efforts_for_provider,
)


def _probs(winner: str, levels: tuple[str, ...] | None = None) -> dict[str, float]:
    ladder = levels or HERMES_REASONING_EFFORTS
    remaining = 1.0 - 0.90
    others = [level for level in ladder if level != winner]
    share = remaining / max(len(others), 1)
    out = {level: share for level in others}
    out[winner] = 0.90
    return out


class FakeClient:
    def __init__(self, *, choice: str = "medium", error: Exception | None = None):
        self.choice = choice
        self.error = error
        self.calls: list[tuple] = []

    def decide(self, state, questions, *, public_or_sanitized_data_ack=True):
        self.calls.append((state, questions, public_or_sanitized_data_ack))
        if self.error is not None:
            raise self.error
        winner = self.choice
        criteria = questions["reasoning_effort"]["criteria"]
        levels = tuple(criteria.keys()) if isinstance(criteria, dict) else HERMES_REASONING_EFFORTS
        if winner not in levels and levels:
            winner = levels[0]
        return {
            "model": "typesafe/jev-1.13",
            "answers": {
                "reasoning_effort": {
                    "choice": winner,
                    "confidence": 0.90,
                    "probabilities": _probs(winner, levels),
                }
            },
            "usage": {"cost": 0.0001},
            "latency_ms": 2,
            "request_count": 1,
        }


class ReasoningEffortAdapterTests(unittest.TestCase):
    def test_normalize_effort_levels_and_aliases(self):
        self.assertEqual(normalize_effort("HIGH"), "high")
        self.assertEqual(normalize_effort(False), "none")
        self.assertEqual(normalize_effort("disabled"), "none")
        self.assertEqual(normalize_effort("nope", default="low"), "low")
        for level in HERMES_REASONING_EFFORTS:
            self.assertEqual(normalize_effort(level), level)

    def test_apply_effort_preserves_messages_for_prompt_cache(self):
        messages = [{"role": "user", "content": "hello"}]
        request = {
            "model": "gpt-test",
            "messages": messages,
            "reasoning_effort": "low",
            "extra_body": {"reasoning": {"effort": "low", "enabled": True}},
            "reasoning_config": {"effort": "low", "enabled": True},
        }
        out = apply_effort_to_request(request, "xhigh")
        self.assertEqual(out["reasoning_effort"], "xhigh")
        self.assertEqual(out["extra_body"]["reasoning"]["effort"], "xhigh")
        self.assertEqual(out["reasoning_config"]["effort"], "xhigh")
        self.assertEqual(out["messages"], messages)
        self.assertIsNot(out, request)

    def test_apply_effort_preserves_responses_input(self):
        payload = [{"role": "user", "content": "codex task"}]
        request = {"model": "gpt-5.6", "input": payload, "reasoning": {"effort": "low", "enabled": True}}
        out = apply_effort_to_request(
            request,
            "ultra",
            provider="openai",
            model="gpt-5.6",
            api_mode="codex_responses",
        )
        self.assertEqual(out["input"], payload)
        self.assertNotIn("reasoning_effort", out)
        self.assertEqual(out["reasoning"]["effort"], "max")  # ultra clamped for gpt-5.6

    def test_choose_raises_when_stuck_signal_present(self):
        client = FakeClient(choice="xhigh")
        result = choose_reasoning_effort(
            task="fix the flaky CI job",
            recent_tool_outcomes=[
                {"tool": "shell", "status": "error", "detail": "exit 1"},
            ],
            prior_effort="medium",
            client=client,
        )
        self.assertEqual(result["status"], "selected")
        self.assertEqual(result["effort"], "xhigh")
        self.assertEqual(result["reason_code"], "jev_selected")
        self.assertTrue(result["stuck_signal"])
        self.assertTrue(client.calls[0][2])

    def test_choose_fail_closed_keeps_previous_on_jev_error(self):
        client = FakeClient(error=RuntimeError("boom"))
        result = choose_reasoning_effort(
            task="routine summary",
            recent_tool_outcomes=[],
            prior_effort="low",
            client=client,
        )
        self.assertEqual(result["status"], "kept_previous")
        self.assertEqual(result["effort"], "low")
        self.assertEqual(result["reason_code"], "kept_previous_on_jev_failure")
        self.assertEqual(result["error_type"], "RuntimeError")

    def test_choose_fail_closed_without_ack(self):
        client = FakeClient(choice="high")
        result = choose_reasoning_effort(
            task="secret-ish",
            recent_tool_outcomes=[],
            prior_effort="medium",
            client=client,
            public_or_sanitized_data_ack=False,
        )
        self.assertEqual(result["reason_code"], "kept_previous_ack_required")
        self.assertEqual(result["effort"], "medium")
        self.assertEqual(client.calls, [])

    def test_controller_applies_via_llm_request_and_rereads_after_tools(self):
        client = FakeClient(choice="minimal")
        controller = ReasoningEffortController(
            client_factory=lambda: client,
            default_effort="medium",
        )
        first = controller.on_llm_request(
            {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "medium"},
            session_id="s1",
            turn_id="t1",
        )
        self.assertEqual(first["request"]["reasoning_effort"], "minimal")
        receipt = last_receipt()
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["reason_code"], "jev_selected")

        hook = controller.build_post_tool_call_hook()
        hook(
            tool_name="shell",
            status="error",
            error_type="tool_error",
            error_message="failed",
            session_id="s1",
        )
        client.choice = "max"
        second = controller.on_llm_request(
            {"messages": [{"role": "user", "content": "try again"}]},
            session_id="s1",
            turn_id="t1",
        )
        self.assertEqual(second["request"]["reasoning_effort"], "max")
        self.assertTrue(client.calls[-1][0]["stuck_signal"])

    def test_per_session_state_is_isolated(self):
        client = FakeClient(choice="low")
        controller = ReasoningEffortController(
            client_factory=lambda: client,
            default_effort="medium",
        )
        controller.on_llm_request(
            {"messages": [{"role": "user", "content": "a"}]},
            session_id="alpha",
            turn_id="t1",
        )
        hook = controller.build_post_tool_call_hook()
        hook(
            tool_name="shell",
            status="error",
            error_message="boom",
            session_id="alpha",
        )
        client.choice = "high"
        controller.on_llm_request(
            {"messages": [{"role": "user", "content": "b"}]},
            session_id="beta",
            turn_id="t1",
        )
        # beta must not inherit alpha's stuck signal
        beta_state = client.calls[-1][0]
        self.assertFalse(beta_state["stuck_signal"])
        self.assertEqual(beta_state["recent_tool_outcomes"], [])

        client.choice = "xhigh"
        controller.on_llm_request(
            {"messages": [{"role": "user", "content": "a2"}]},
            session_id="alpha",
            turn_id="t1",
        )
        self.assertTrue(client.calls[-1][0]["stuck_signal"])

    def test_responses_input_string_and_list_feed_jev_task(self):
        client = FakeClient(choice="high")
        controller = ReasoningEffortController(client_factory=lambda: client)
        controller.on_llm_request(
            {"input": "direct string task about hard debugging", "model": "gpt-5"},
            session_id="s",
            turn_id="t1",
            api_mode="codex_responses",
        )
        self.assertIn("direct string task", client.calls[-1][0]["task"])

        client.choice = "medium"
        controller.on_llm_request(
            {
                "input": [
                    {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "list form task"}]},
                ],
                "model": "gpt-5",
            },
            session_id="s",
            turn_id="t2",
            api_mode="codex_responses",
        )
        self.assertIn("list form task", client.calls[-1][0]["task"])

    def test_provider_aware_clamp_drops_ultra_from_wire(self):
        self.assertEqual(
            clamp_effort_for_provider("ultra", model="gpt-5.6", api_mode="codex_responses"),
            "max",
        )
        self.assertEqual(
            clamp_effort_for_provider("ultra", provider="xai", api_mode="codex_responses", model="grok"),
            "high",
        )
        self.assertEqual(
            clamp_effort_for_provider("ultra", provider="openrouter", model="openai/gpt-4o"),
            "max",
        )
        self.assertEqual(
            clamp_effort_for_provider("minimal", api_mode="codex_responses"),
            "low",
        )
        self.assertNotIn(
            "ultra",
            wire_efforts_for_provider(provider="openrouter", model="openai/o3", api_mode="chat_completions"),
        )

        out = apply_effort_to_request(
            {"model": "gpt-4o"},
            "ultra",
            provider="openrouter",
            model="openai/gpt-4o",
            api_mode="chat_completions",
        )
        self.assertEqual(out["reasoning_effort"], "max")
        self.assertNotEqual(out["reasoning_effort"], "ultra")

        codex = apply_effort_to_request(
            {"model": "gpt-5.6", "input": "hi"},
            "ultra",
            provider="openai",
            model="gpt-5.6",
            api_mode="codex_responses",
        )
        self.assertNotIn("reasoning_effort", codex)
        self.assertEqual(codex["reasoning"]["effort"], "max")

    def test_codex_astra_maps_none_and_minimal_to_low(self):
        # openai-codex / Responses / Astra reject reasoning_effort=none (HTTP 400).
        cases = [
            {"provider": "openai-codex", "model": "gpt-6-astra", "api_mode": "codex_responses"},
            {"provider": "openai", "model": "gpt-6-astra", "api_mode": "responses"},
            {"provider": "openai", "model": "gpt-6-astra", "api_mode": None},
            {"provider": "openai-codex", "model": "gpt-5.4", "api_mode": "codex_responses"},
        ]
        for kwargs in cases:
            with self.subTest(**{k: v for k, v in kwargs.items() if v is not None}):
                self.assertEqual(clamp_effort_for_provider("none", **kwargs), "low")
                self.assertEqual(clamp_effort_for_provider("minimal", **kwargs), "low")
                wire = wire_efforts_for_provider(**kwargs)
                self.assertNotIn("none", wire)
                self.assertNotIn("minimal", wire)
                self.assertIn("low", wire)
                for level in ("low", "medium", "high", "xhigh", "max"):
                    self.assertIn(level, wire)

                applied = apply_effort_to_request(
                    {"model": kwargs["model"], "reasoning_effort": "medium"},
                    "none",
                    **kwargs,
                )
                self.assertEqual(applied["reasoning_effort"], "low")
                self.assertNotEqual(applied["reasoning_effort"], "none")

                nested = apply_effort_to_request(
                    {"model": kwargs["model"], "input": "hi"},
                    "none",
                    **{**kwargs, "api_mode": kwargs.get("api_mode") or "codex_responses"},
                )
                self.assertNotIn("reasoning_effort", nested)
                self.assertEqual(nested["reasoning"]["effort"], "low")
                self.assertNotIn("enabled", nested["reasoning"])

        # Non-Codex / non-Astra may still keep internal none (disabled).
        self.assertEqual(
            clamp_effort_for_provider("none", provider="openrouter", model="openai/gpt-4o"),
            "none",
        )
        self.assertIn(
            "none",
            wire_efforts_for_provider(provider="openrouter", model="openai/gpt-4o", api_mode="chat_completions"),
        )

    def test_codex_wire_rewrite_does_not_forward_internal_enabled(self):
        payload = [{"role": "user", "content": "ping"}]
        for model in ("gpt-6-luna-900k", "unannounced-next-model-900k"):
            with self.subTest(model=model):
                request = {
                    "model": model,
                    "input": payload,
                    "reasoning": {"effort": "medium", "summary": "auto", "enabled": True},
                }
                out = apply_effort_to_request(
                    request, "high", provider="openai-codex",
                    model=model, api_mode="codex_responses",
                )
                self.assertIs(out["input"], payload)
                self.assertEqual(out["reasoning"], {"effort": "high", "summary": "auto"})
                self.assertEqual(request["reasoning"]["enabled"], True)

                injected = apply_effort_to_request(
                    {"model": model, "input": payload}, "high",
                    provider="openai-codex", model=model, api_mode="codex_responses",
                )
                self.assertEqual(injected["reasoning"], {"effort": "high"})

                extra = apply_effort_to_request(
                    {"model": model, "extra_body": {"reasoning": {"enabled": True}}},
                    "high", provider="openai-codex", model=model, api_mode="codex_responses",
                )
                self.assertEqual(extra["extra_body"]["reasoning"], {"effort": "high"})

    def test_turn_id_invalidates_cache_retries_reuse(self):
        client = FakeClient(choice="low")
        controller = ReasoningEffortController(client_factory=lambda: client)
        controller.on_llm_request(
            {"messages": [{"role": "user", "content": "turn one"}]},
            session_id="s",
            turn_id="turn-1",
        )
        calls_after_first = len(client.calls)
        # Same turn retry: cached, no new Jev call
        controller.on_llm_request(
            {"messages": [{"role": "user", "content": "turn one retry"}]},
            session_id="s",
            turn_id="turn-1",
        )
        self.assertEqual(len(client.calls), calls_after_first)
        self.assertEqual(last_receipt()["reason_code"], "cached")

        client.choice = "high"
        controller.on_llm_request(
            {"messages": [{"role": "user", "content": "turn two"}]},
            session_id="s",
            turn_id="turn-2",
        )
        self.assertEqual(len(client.calls), calls_after_first + 1)
        self.assertEqual(last_receipt()["effort"], "high")
        self.assertEqual(last_receipt()["reason_code"], "jev_selected")

    def test_derive_tool_failure_from_hermes_fields_and_json_result(self):
        failed, detail = derive_tool_failure(
            status="error",
            error_type="tool_error",
            error_message="no such file",
        )
        self.assertTrue(failed)
        self.assertIn("no such file", detail)

        failed, _ = derive_tool_failure(result=json.dumps({"error": "boom", "ok": False}))
        self.assertTrue(failed)

        failed, detail = derive_tool_failure(result=json.dumps({"ok": True, "status": "done"}))
        self.assertFalse(failed)
        self.assertEqual(detail, "done")

        controller = ReasoningEffortController(client_factory=lambda: FakeClient(choice="medium"))
        hook = controller.build_post_tool_call_hook()
        hook(
            tool_name="browser",
            result='{"error": "timeout waiting"}',
            status="error",
            error_type="tool_error",
            error_message="timeout waiting",
            session_id="s-fail",
        )
        client = FakeClient(choice="xhigh")
        controller.client_factory = lambda: client
        controller.on_llm_request(
            {"messages": [{"role": "user", "content": "retry"}]},
            session_id="s-fail",
            turn_id="t1",
        )
        self.assertTrue(client.calls[-1][0]["stuck_signal"])

    def test_probe_and_register_noop_without_middleware_seam(self):
        ctx = SimpleNamespace()
        seam = probe_llm_request_middleware_seam(ctx)
        self.assertFalse(seam["available"])
        receipt = register_reasoning_effort_adapter(ctx, enabled=True)
        self.assertEqual(receipt["mode"], "noop_seam_unavailable")
        self.assertFalse(receipt["can_apply"])
        self.assertEqual(last_registration()["mode"], "noop_seam_unavailable")

    def test_register_binds_llm_request_and_post_tool_call(self):
        calls: list[tuple] = []

        def register_middleware(kind, callback):
            calls.append(("middleware", kind, callback))

        def register_hook(name, callback):
            calls.append(("hook", name, callback))

        ctx = SimpleNamespace(
            register_middleware=register_middleware,
            register_hook=register_hook,
        )
        client = FakeClient(choice="low")
        receipt = register_reasoning_effort_adapter(
            ctx,
            enabled=True,
            client_factory=lambda: client,
            default_effort="medium",
        )
        self.assertEqual(receipt["mode"], "llm_request_middleware")
        self.assertTrue(receipt["can_apply"])
        self.assertTrue(receipt["post_tool_call_registered"])
        kinds = [(entry[0], entry[1]) for entry in calls]
        self.assertIn(("middleware", "llm_request"), kinds)
        self.assertIn(("hook", "post_tool_call"), kinds)

        # Exercise the bound middleware callback.
        mw = next(cb for tag, kind, cb in calls if tag == "middleware")
        out = mw({"messages": [{"role": "user", "content": "ping"}]})
        self.assertEqual(out["request"]["reasoning_effort"], "low")
        codex = mw(
            {"model": "future-openai-model", "input": "ping", "reasoning": {"effort": "medium", "summary": "auto"}},
            session_id="codex-wire", provider="openai-codex", model="future-openai-model",
            api_mode="codex_responses",
        )
        self.assertEqual(codex["request"]["reasoning"], {"effort": "low", "summary": "auto"})

    def test_register_disabled(self):
        ctx = SimpleNamespace(register_middleware=lambda *a, **k: None)
        receipt = register_reasoning_effort_adapter(ctx, enabled=False)
        self.assertEqual(receipt["mode"], "disabled")
        self.assertFalse(receipt["enabled"])


if __name__ == "__main__":
    unittest.main()
