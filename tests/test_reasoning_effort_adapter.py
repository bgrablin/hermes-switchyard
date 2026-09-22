"""Offline tests for Jev adaptive reasoning-effort middleware (Hermes 0.21)."""
from __future__ import annotations

import unittest
from types import SimpleNamespace

from hermes_switchyard.reasoning_effort_adapter import (
    HERMES_REASONING_EFFORTS,
    ReasoningEffortController,
    apply_effort_to_request,
    choose_reasoning_effort,
    last_receipt,
    last_registration,
    normalize_effort,
    probe_llm_request_middleware_seam,
    register_reasoning_effort_adapter,
)


def _probs(winner: str) -> dict[str, float]:
    remaining = 1.0 - 0.90
    others = [level for level in HERMES_REASONING_EFFORTS if level != winner]
    share = remaining / len(others)
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
        return {
            "model": "typesafe/jev-1.13",
            "answers": {
                "reasoning_effort": {
                    "choice": winner,
                    "confidence": 0.90,
                    "probabilities": _probs(winner),
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
            "extra_body": {"reasoning": {"effort": "low", "enabled": True}},
            "reasoning_config": {"effort": "low", "enabled": True},
        }
        out = apply_effort_to_request(request, "xhigh")
        self.assertEqual(out["reasoning_effort"], "xhigh")
        self.assertEqual(out["extra_body"]["reasoning"]["effort"], "xhigh")
        self.assertEqual(out["reasoning_config"]["effort"], "xhigh")
        self.assertEqual(out["messages"], messages)
        self.assertIsNot(out, request)

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
            {"messages": [{"role": "user", "content": "hi"}], "reasoning_effort": "medium"}
        )
        self.assertEqual(first["request"]["reasoning_effort"], "minimal")
        receipt = last_receipt()
        self.assertTrue(receipt["applied"])
        self.assertEqual(receipt["reason_code"], "jev_selected")

        hook = controller.build_post_tool_call_hook()
        hook(tool_name="shell", error="failed")
        client.choice = "max"
        second = controller.on_llm_request(
            {"messages": [{"role": "user", "content": "try again"}]}
        )
        self.assertEqual(second["request"]["reasoning_effort"], "max")
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

    def test_register_disabled(self):
        ctx = SimpleNamespace(register_middleware=lambda *a, **k: None)
        receipt = register_reasoning_effort_adapter(ctx, enabled=False)
        self.assertEqual(receipt["mode"], "disabled")
        self.assertFalse(receipt["enabled"])


if __name__ == "__main__":
    unittest.main()
