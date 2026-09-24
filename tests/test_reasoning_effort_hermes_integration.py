"""Exercise the installed Hermes middleware and SDK wire shape in an isolated home."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from scripts.build_release import RELEASE_FILES


def _isolated_replay(plugin_dir: Path) -> None:
    # Imports happen only after the child has an isolated HOME and HERMES_HOME.
    import anthropic
    import httpx
    import openai
    from agent.transports.codex import _reasoning_fields
    from gateway.session_context import scoped_current_session_id
    from hermes_cli.commands import resolve_command
    from hermes_cli.middleware import apply_llm_request_middleware
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    loaded = manager._plugins.get("hermes-switchyard")
    assert loaded is not None and loaded.enabled and loaded.error is None, loaded
    callbacks = manager._middleware.get("llm_request", [])
    assert len(callbacks) == 1, callbacks
    controller = callbacks[0].__self__
    assert Path(sys.modules[controller.__class__.__module__].__file__).resolve().is_relative_to(plugin_dir)
    assert resolve_command("switchyard") is None
    command = manager._plugin_commands["switchyard"]["handler"]

    class SyntheticClient:
        def __init__(self):
            self.calls = []

        def decide(self, state, questions, **kwargs):
            self.calls.append((state, questions))
            levels = tuple(questions["reasoning_effort"]["criteria"])
            selected = "low" if "low" in levels else levels[0]
            probabilities = {level: 1.0 if level == selected else 0.0 for level in levels}
            return {"answers": {"reasoning_effort": {
                "choice": selected, "confidence": 1.0, "probabilities": probabilities,
            }}}

    fake = SyntheticClient()
    controller.client_factory = lambda: fake  # Never construct a credential-backed client.
    model = "gpt-6-astra-900k"

    def codex(effort, session="s1", turn="t1"):
        fields = _reasoning_fields(
            model, {}, effort=effort, enabled=True, replay_encrypted_reasoning=False,
            is_xai_responses=False, is_github_responses=False,
        )
        request = {"model": model, "input": "synthetic", **fields}
        middleware = apply_llm_request_middleware(
            request, provider="openai-codex", model=model, api_mode="codex_responses",
            session_id=session, turn_id=turn,
        )
        return request, middleware

    first, lowered = codex("high")
    assert first["reasoning"]["effort"] == "high"
    assert lowered.changed and lowered.payload["reasoning"]["effort"] == "low"
    assert lowered.payload["input"] == first["input"]
    assert lowered.trace[0]["source"] == "hermes-switchyard"
    jev_state = fake.calls[0][0]
    assert set(jev_state) == {
        "task_present", "task_length_bucket", "requested_effort", "turn_phase",
        "recent_tool_outcomes", "stuck_signal", "policy",
    }
    assert "synthetic" not in json.dumps(jev_state)
    _, cached = codex("high")
    assert cached.payload["reasoning"]["effort"] == "low" and len(fake.calls) == 1
    _, capped = codex("low", session="s-low")
    assert not capped.changed and capped.payload["reasoning"]["effort"] == "low"
    assert len(fake.calls) == 1
    _, pinned = codex("medium")
    assert not pinned.changed and pinned.payload["reasoning"]["effort"] == "medium"
    assert controller.session_status("s1")["mode"] == "pinned"
    assert len(fake.calls) == 1
    with scoped_current_session_id("s1"):
        assert "mode: pinned" in command("effort status")

    codex_wire = []

    def capture_codex(request):
        codex_wire.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "resp_synthetic", "object": "response", "model": model,
                                          "created_at": 0, "output": [], "status": "completed"})

    with openai.OpenAI(api_key="fixture-key", base_url="https://example.invalid/v1",
                       http_client=httpx.Client(transport=httpx.MockTransport(capture_codex))) as client:
        client.responses.create(**lowered.payload)
    assert codex_wire[0]["reasoning"] == {"effort": "low", "summary": "auto"}
    assert codex_wire[0]["input"] == "synthetic"
    assert "reasoning_effort" not in codex_wire[0]

    anthropic_request = {
        "model": "claude-opus-5-5", "messages": [{"role": "user", "content": "synthetic"}],
        "max_tokens": 32, "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
    }
    result = apply_llm_request_middleware(
        anthropic_request, provider="anthropic", model="claude-opus-5-5",
        api_mode="anthropic_messages", session_id="s-anthropic", turn_id="t1",
    )
    assert result.changed and result.payload["output_config"]["effort"] == "low"
    anthropic_wire = []

    def capture_anthropic(request):
        anthropic_wire.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg_synthetic", "type": "message", "role": "assistant",
                                          "model": "claude-opus-5-5", "content": [], "stop_reason": "end_turn",
                                          "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    with anthropic.Anthropic(api_key="fixture-key", base_url="https://example.invalid",
                             http_client=httpx.Client(transport=httpx.MockTransport(capture_anthropic))) as client:
        client.messages.create(**result.payload)
    assert anthropic_wire[0]["output_config"]["effort"] == "low"
    assert anthropic_wire[0]["thinking"] == {"type": "adaptive"}
    assert "reasoning_effort" not in anthropic_wire[0]
    print(json.dumps({"plugin_path": str(plugin_dir), "codex_wire_effort": codex_wire[0]["reasoning"]["effort"],
                      "anthropic_wire_effort": anthropic_wire[0]["output_config"]["effort"],
                      "jev_calls": len(fake.calls), "command_registered": True}))
    manager.unload()


class InstalledHermesEffortIntegrationTests(unittest.TestCase):
    def test_fresh_plugin_manager_middleware_and_sdk_wire(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="switchyard-effort-") as temporary:
            workspace = Path(temporary)
            home = workspace / "home"
            plugin = home / "plugins" / "hermes-switchyard"
            for relative in RELEASE_FILES:
                source = root / relative
                self.assertTrue(source.is_file() and not source.is_symlink(), relative)
                target = plugin / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [hermes-switchyard]\n  entries:\n    hermes-switchyard:\n"
                "      settings:\n        automatic_skill_recommendation: false\n"
                "        jev_provider: openrouter\n", encoding="utf-8",
            )
            bundled = workspace / "bundled"
            bundled.mkdir()
            env = {key: os.environ[key] for key in ("PATH", "PYTHONPATH", "TMPDIR", "LANG") if key in os.environ}
            env.update({"HOME": str(workspace), "HERMES_HOME": str(home),
                        "HERMES_BUNDLED_PLUGINS": str(bundled)})
            result = subprocess.run(
                [sys.executable, "-m", "tests.test_reasoning_effort_hermes_integration", "--child", str(plugin)],
                cwd=root, env=env, text=True, capture_output=True, timeout=90,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            proof = json.loads(result.stdout.splitlines()[-1])
            self.assertEqual(proof["plugin_path"], str(plugin))
            self.assertEqual(proof["codex_wire_effort"], "low")
            self.assertEqual(proof["anthropic_wire_effort"], "low")
            self.assertEqual(proof["jev_calls"], 2)
            self.assertTrue(proof["command_registered"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _isolated_replay(Path(sys.argv[2]))
    else:
        unittest.main()
