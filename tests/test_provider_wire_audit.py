import json
import unittest

from hermes_switchyard.reasoning_effort_adapter import (
    ReasoningEffortController,
    apply_effort_to_request,
    choose_reasoning_effort,
    clamp_effort_for_provider,
)


class CaptureClient:
    def __init__(self, error=None):
        self.error = error
        self.calls = []

    def decide(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        if self.error:
            raise self.error
        levels = tuple(questions["reasoning_effort"]["criteria"])
        selected = "high" if "high" in levels else levels[0]
        others = [level for level in levels if level != selected]
        probs = {selected: 0.9, **{level: 0.1 / len(others) for level in others}} if others else {selected: 1.0}
        return {"answers": {"reasoning_effort": {"choice": selected, "confidence": 0.9, "probabilities": probs}}}


class ProviderWireAuditTests(unittest.TestCase):
    def test_unmarked_responses_request_does_not_invent_reasoning(self):
        request = {"model": "future-unknown", "input": []}
        result = apply_effort_to_request(
            request,
            "high",
            provider="openai-codex",
            model="future-unknown",
            api_mode="codex_responses",
        )
        self.assertEqual(result, request)

    def test_codex_responses_strips_unsupported_reasoning_aliases(self):
        payload = [{"role": "user", "content": "synthetic input"}]
        request = {
            "input": payload,
            "reasoning_effort": "low",
            "reasoning_config": {"enabled": True, "effort": "low"},
            "extra_body": {"reasoning_effort": "low", "marker": "preserve"},
            "reasoning": {"effort": "medium", "summary": "auto", "enabled": True},
        }
        result = apply_effort_to_request(request, "high", provider="openai-codex", model="unknown-future", api_mode="codex_responses")
        self.assertNotIn("reasoning_effort", result)
        self.assertNotIn("reasoning_config", result)
        self.assertNotIn("reasoning_effort", result["extra_body"])
        self.assertEqual(result["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertEqual(result["extra_body"]["marker"], "preserve")
        self.assertIs(result["input"], payload)

    def test_codex_drops_extra_body_reasoning_alias_and_preserves_safe_sibling(self):
        request = {"model": "future-codex", "input": [], "reasoning_effort": "medium", "reasoning_config": {"enabled": True}, "reasoning": {"enabled": True, "effort": "medium", "summary": "auto"}, "extra_body": {"reasoning_effort": "medium", "reasoning": {"enabled": True, "effort": "medium"}, "safe_setting": "keep"}}
        result = apply_effort_to_request(request, "high", provider="openai-codex", model="future-codex", api_mode="codex_responses")
        self.assertEqual(result["reasoning"], {"effort": "high", "summary": "auto"})
        self.assertEqual(result["extra_body"], {"safe_setting": "keep"})
        self.assertIn("reasoning_effort", request)

    def test_anthropic_adaptive_and_manual_thinking(self):
        request = {"model": "future-claude", "messages": [], "thinking": {"type": "adaptive", "display": "summarized"}, "output_config": {"effort": "medium"}, "reasoning_effort": "low", "reasoning_config": {"enabled": True}}
        result = apply_effort_to_request(request, "high", provider="anthropic", model="future-claude", api_mode="anthropic_messages")
        self.assertEqual(result["output_config"], {"effort": "high"})
        self.assertEqual(result["thinking"], request["thinking"])
        self.assertNotIn("reasoning_effort", result)
        self.assertNotIn("reasoning_config", result)
        manual = {"model": "manual-claude", "thinking": {"type": "enabled", "budget_tokens": 8192}}
        self.assertEqual(apply_effort_to_request(manual, "high", provider="anthropic", api_mode="anthropic_messages"), manual)

    def test_bedrock_and_generic_chat_without_host_effort_unchanged(self):
        bedrock = {"modelId": "some-model", "messages": [], "reasoning_effort": "high", "reasoning_config": {"enabled": True}}
        self.assertEqual(apply_effort_to_request(bedrock, "low", provider="bedrock", api_mode="bedrock_converse"), bedrock)
        request = {"model": "future-chat", "messages": []}
        self.assertEqual(apply_effort_to_request(request, "high", provider="openai", model="future", api_mode="chat_completions"), request)

    def test_codex_capability_respects_installed_host_function_unknown_models(self):
        try:
            from agent.reasoning_effort import codex_supported_efforts
        except ImportError:
            self.skipTest("Hermes host capability helper unavailable")
        for model in ("gpt-6-luna-900k", "unknown-future-model"):
            allowed = codex_supported_efforts(model)
            for effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
                actual = clamp_effort_for_provider(effort, provider="openai-codex", model=model, api_mode="codex_responses")
                self.assertIn(actual, allowed)

    def test_explicit_host_effort_preserved_on_jev_failure(self):
        client = CaptureClient(error=RuntimeError("synthetic failure"))
        controller = ReasoningEffortController(client_factory=lambda: client)
        request = {"model": "future-codex", "input": "synthetic prompt", "reasoning": {"effort": "high", "summary": "auto"}}
        result = controller.on_llm_request(request, session_id="synthetic", provider="openai-codex", model="future-codex", api_mode="codex_responses")
        self.assertIsNone(result)
        self.assertEqual(request["reasoning"]["effort"], "high")
        preserved = controller._state_for(session_id="synthetic").last_choice
        self.assertFalse(preserved["applied"])
        self.assertEqual(preserved["effort"], "high")
        self.assertEqual(preserved["source"], "host_request_preserved")

    def test_unsupported_route_skips_jev(self):
        client = CaptureClient()
        controller = ReasoningEffortController(client_factory=lambda: client)
        result = controller.on_llm_request({"modelId": "model", "messages": []}, session_id="synthetic", provider="bedrock", api_mode="bedrock_converse")
        self.assertIsNone(result)
        self.assertEqual(client.calls, [])

    def test_jev_receives_no_raw_task_or_tool_error_details(self):
        client = CaptureClient()
        choose_reasoning_effort(task="synthetic-private-marker", recent_tool_outcomes=[{"tool": "shell", "status": "error", "detail": "synthetic-secret-error"}], prior_effort="medium", client=client)
        sent = json.dumps(client.calls[0][0])
        self.assertNotIn("synthetic-private-marker", sent)
        self.assertNotIn("synthetic-secret-error", sent)
        self.assertIn("medium", sent)


if __name__ == "__main__":
    unittest.main()
