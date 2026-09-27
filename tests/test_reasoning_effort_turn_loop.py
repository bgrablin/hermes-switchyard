"""End-to-end: the installed plugin inside the real Hermes turn loop (``AIAgent.run_conversation``).

A fake Jev decides only from the outbound ``current_request`` text. With the user's level at
``high``, a greeting and a thanks go out at ``low`` on the Anthropic (``output_config.effort``)
and Codex Responses (``reasoning.effort``) wire shapes, and a consequential request keeps
``high``. The opt-in receipt line reaches the final response through the Hermes
``transform_llm_output`` seam and never enters the stored conversation history.

All values are synthetic. The provider call is replaced at ``_interruptible_api_call``, so
nothing leaves the process.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts import live_effort_replay as replay
from scripts.build_release import RELEASE_FILES

ROUTINE = ("hi", "thanks!")
CONSEQUENTIAL = "delete the prod database backups"
ROUTES = {
    "anthropic": {"provider": "anthropic", "api_mode": "anthropic_messages", "model": "claude-opus-4-6"},
    "codex": {"provider": "openai-codex", "api_mode": "codex_responses", "model": "gpt-5-codex"},
}


def _child(plugin_dir: Path) -> None:
    """Run in an isolated HOME and HERMES_HOME; print one JSON line with the observed wire."""
    import types
    from types import SimpleNamespace

    for name, stub in (("fire", SimpleNamespace(Fire=lambda *a, **k: None)),
                       ("firecrawl", SimpleNamespace(Firecrawl=object)), ("fal_client", SimpleNamespace())):
        sys.modules.setdefault(name, stub if isinstance(stub, types.SimpleNamespace) else stub)

    import agent.anthropic_adapter as anthropic_adapter
    import model_tools
    import run_agent
    from hermes_cli.plugins import get_plugin_manager

    model_tools.get_tool_definitions = lambda **kwargs: [{"type": "function", "function": {
        "name": "synthetic_tool", "description": "synthetic", "parameters": {"type": "object", "properties": {}}}}]
    model_tools.check_toolset_requirements = lambda: {}
    anthropic_adapter.build_anthropic_client = lambda *a, **k: SimpleNamespace(close=lambda: None)

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    callbacks = manager._middleware.get("llm_request", [])
    assert len(callbacks) == 1, "installed_plugin_middleware_not_unique"
    controller = callbacks[0].__self__
    assert Path(sys.modules[type(controller).__module__].__file__).resolve().is_relative_to(plugin_dir.resolve()), \
        "controller_not_installed_copy"

    jev_states: list[dict] = []

    class TextOnlyJev:
        """Decides from ``current_request`` alone: a bare greeting or thanks is routine."""

        def decide(self, state, questions, **kwargs):
            jev_states.append(json.loads(json.dumps(state)))
            levels = list(questions["reasoning_effort"]["criteria"])
            routine = str(state.get("current_request", "")).strip().lower() in ROUTINE
            pick = "low" if routine and "low" in levels else levels[-1]
            return {"answers": {
                "reasoning_effort": {"choice": pick, "confidence": 1.0,
                                     "probabilities": {level: 1.0 if level == pick else 0.0 for level in levels}},
                "stakes": {"noul": 0.0},
            }}

    controller.client_factory = TextOnlyJev

    def run(route: str, text: str) -> dict:
        spec = ROUTES[route]
        agent = run_agent.AIAgent(
            model=spec["model"], provider=spec["provider"], api_mode=spec["api_mode"],
            api_key="test-key", base_url="http://localhost:1234/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True, max_iterations=2,
            reasoning_config={"enabled": True, "effort": "high"},
        )
        agent._cleanup_task_resources = agent._persist_session = lambda *a, **k: None
        agent._save_trajectory = lambda *a, **k: None
        agent._disable_streaming = True
        wire: list[dict] = []

        def provider_call(api_kwargs):
            wire.append(api_kwargs)
            if spec["api_mode"] == "anthropic_messages":
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="Synthetic answer.")],
                                       stop_reason="end_turn", model=spec["model"],
                                       usage=SimpleNamespace(input_tokens=1, output_tokens=1))
            return SimpleNamespace(
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="Synthetic answer.")])],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                status="completed", model=spec["model"])

        agent._interruptible_api_call = provider_call
        result = agent.run_conversation(text)
        assert len(wire) == 1, "unexpected_provider_call_count"
        sent = wire[0]
        if spec["api_mode"] == "anthropic_messages":
            effort = sent["output_config"]["effort"]
            assert (sent.get("thinking") or {}).get("type") == "adaptive", \
                "anthropic_thinking_changed: " + json.dumps(sent.get("thinking"))
        else:
            effort = sent["reasoning"]["effort"]
        assert "reasoning_effort" not in sent, "flat_reasoning_effort_on_native_wire"
        history = [m.get("content") for m in result["messages"] if m.get("role") == "assistant"]
        return {"route": route, "text": text, "sent": effort, "final": result["final_response"],
                "history": history, "transformed": bool(result.get("response_transformed"))}

    rows = [run(route, text) for route in ROUTES for text in (*ROUTINE, CONSEQUENTIAL)]
    controller.set_receipt_line(True)
    rows.append({**run("anthropic", "hi"), "receipt_line": True})
    rows.append({**run("codex", CONSEQUENTIAL), "receipt_line": True})
    print(json.dumps({"rows": rows, "jev_requests": [s.get("current_request") for s in jev_states],
                      "jev_keys": sorted({key for s in jev_states for key in s})}))
    manager.unload()


class HermesTurnLoopEffortTests(unittest.TestCase):
    def test_greeting_and_thanks_go_out_low_and_consequential_keeps_high(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="switchyard-turnloop-") as temporary:
            workspace = Path(temporary)
            home = workspace / "home"
            plugin = home / "plugins" / "hermes-switchyard"
            for relative in RELEASE_FILES:
                target = plugin / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(root / relative, target)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [hermes-switchyard]\n  entries:\n    hermes-switchyard:\n"
                "      settings:\n        automatic_skill_recommendation: false\n"
                "        jev_provider: openrouter\n", encoding="utf-8",
            )
            bundled = workspace / "bundled"
            bundled.mkdir()
            env = replay._sparse_child_env(("PATH", "PYTHONPATH", "TMPDIR", "LANG"))
            env.update({"HOME": str(workspace), "HERMES_HOME": str(home), "HERMES_BUNDLED_PLUGINS": str(bundled)})
            result = subprocess.run(
                [sys.executable, "-m", "tests.test_reasoning_effort_turn_loop", "--child", str(plugin)],
                cwd=root, env=env, text=True, capture_output=True, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stderr[-3000:])
            proof = json.loads(result.stdout.splitlines()[-1])

        rows = proof["rows"]
        by_case = {(row["route"], row["text"]): row for row in rows if not row.get("receipt_line")}
        for route in ROUTES:
            for text in ROUTINE:
                self.assertEqual(by_case[(route, text)]["sent"], "low", (route, text))
            self.assertEqual(by_case[(route, CONSEQUENTIAL)]["sent"], "high", route)
        # Receipt line is off by default: no reply was changed.
        for row in by_case.values():
            self.assertEqual(row["final"], "Synthetic answer.")
            self.assertFalse(row["transformed"])
        # Jev saw the real current request text, and only closed-set state fields.
        self.assertEqual(proof["jev_requests"][:6], [*ROUTINE, CONSEQUENTIAL] * 2)
        self.assertEqual(proof["jev_keys"], ["current_request", "latest_tool_failed", "recent_tool_statuses", "turn_phase"])

        lowered, kept = [row for row in rows if row.get("receipt_line")]
        self.assertEqual(lowered["sent"], "low")
        self.assertRegex(lowered["final"], r"^Synthetic answer\.\n\nswitchyard: effort high→low \(Jev \d+ ms\)$")
        self.assertTrue(lowered["transformed"])
        self.assertEqual(lowered["history"], ["Synthetic answer."], "receipt line entered model history")
        self.assertEqual(kept["sent"], "high")
        self.assertEqual(kept["final"], "Synthetic answer.", "unchanged effort must stay quiet")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _child(Path(sys.argv[2]))
    else:
        unittest.main()
