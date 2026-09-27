"""End-to-end: the installed plugin inside the real Hermes turn loop (``AIAgent.run_conversation``).

A fake Jev decides only from the outbound ``current_request`` text. With the user's level at
``high``, a greeting and a thanks go out at ``low`` on the Anthropic (``output_config.effort``)
and Codex Responses (``reasoning.effort``) wire shapes, and a consequential request keeps
``high``. The receipt line is on by default: it reaches the final response through the Hermes
``transform_llm_output`` seam for the lowered and the kept turn, and never enters the stored
conversation history. ``/switchyard effort receipt off`` removes it.

All values are synthetic. The provider call is replaced at ``_interruptible_api_call``, so
nothing leaves the process.
"""
from __future__ import annotations

import json
import os
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
# Local-first bypass: trivial by the shared closed list, so no Jev call at all.
TRIVIAL = ("hi", "thanks", "ok thanks!", "👍")
# Not trivial: these still reach Jev (task words, a code block, a URL, or a path).
NOT_TRIVIAL = ("hi, delete the prod backups", "thanks, now deploy", "ok\n```\nls -la\n```",
               "thanks https://example.com", "ok ~/done/")
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
                "stakes": {"noul": 0.0 if routine else 1.0},
            }}

    controller.client_factory = TextOnlyJev

    def make_agent(route: str):
        spec = ROUTES[route]
        return run_agent.AIAgent(
            model=spec["model"], provider=spec["provider"], api_mode=spec["api_mode"],
            api_key="test-key", base_url="http://localhost:1234/v1", quiet_mode=True,
            skip_context_files=True, skip_memory=True, max_iterations=2,
            reasoning_config={"enabled": True, "effort": "high"},
        )

    def run(route: str, text: str, output_tokens: int = 1, agent=None) -> dict:
        spec = ROUTES[route]
        agent = agent or make_agent(route)
        agent._cleanup_task_resources = agent._persist_session = lambda *a, **k: None
        agent._save_trajectory = lambda *a, **k: None
        agent._disable_streaming = True
        wire: list[dict] = []

        def provider_call(api_kwargs):
            wire.append(api_kwargs)
            if spec["api_mode"] == "anthropic_messages":
                return SimpleNamespace(content=[SimpleNamespace(type="text", text="Synthetic answer.")],
                                       stop_reason="end_turn", model=spec["model"],
                                       usage=SimpleNamespace(input_tokens=1, output_tokens=output_tokens))
            return SimpleNamespace(
                output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="output_text", text="Synthetic answer.")])],
                usage=SimpleNamespace(input_tokens=1, output_tokens=1, total_tokens=2),
                status="completed", model=spec["model"])

        agent._interruptible_api_call = provider_call
        result = agent.run_conversation(text, conversation_history=[])
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
                "history": history, "transformed": bool(result.get("response_transformed")),
                "session": agent.session_id}

    rows = [run(route, text) for route in ROUTES for text in (*ROUTINE, CONSEQUENTIAL)]
    # Bypass matrix on both wire shapes: count Jev calls per turn.
    bypass = []
    for route in ROUTES:
        for text in (*TRIVIAL, *NOT_TRIVIAL):
            before = len(jev_states)
            row = run(route, text)
            bypass.append({**row, "jev_calls": len(jev_states) - before})
    # Pinned sessions stay pinned: the CLI command names the live session, as Hermes does.
    pinned = []
    for route in ROUTES:
        pin_agent = make_agent(route)
        os.environ["HERMES_SESSION_ID"] = pin_agent.session_id
        pin_reply = controller.handle_command("effort pin")
        before = len(jev_states)
        row = run(route, "thanks", agent=pin_agent)
        pinned.append({**row, "jev_calls": len(jev_states) - before, "pin_reply": pin_reply})
    os.environ.pop("HERMES_SESSION_ID", None)
    default_on = controller.receipt_line
    off_reply = controller.handle_command("effort receipt off")
    rows.append({**run("anthropic", "hi"), "receipt_off": True})
    # The CLI command context names the live session the way Hermes does.
    os.environ["HERMES_SESSION_ID"] = rows[-1]["session"]
    summary = controller.handle_command("effort summary")
    # Measured basis through the real post_api_request hook: three consequential turns at the
    # user's level give the baseline, then a lowered greeting reports the measured difference.
    controller.handle_command("effort receipt on")
    session_agent = make_agent("anthropic")
    measured = [run("anthropic", CONSEQUENTIAL, output_tokens=value, agent=session_agent) for value in (900, 700, 800)]
    measured.append(run("anthropic", "hi", output_tokens=300, agent=session_agent))
    os.environ["HERMES_SESSION_ID"] = session_agent.session_id
    measured_summary = controller.handle_command("effort summary")
    print(json.dumps({"rows": rows, "bypass": bypass, "pinned": pinned, "measured": measured, "measured_summary": measured_summary, "jev_requests": [s.get("current_request") for s in jev_states],
                      "jev_keys": sorted({key for s in jev_states for key in s}),
                      "default_on": default_on, "off_reply": off_reply, "summary": summary}))
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
        self.assertTrue(proof["default_on"], "receipt line must be on by default")
        by_case = {(row["route"], row["text"]): row for row in rows if not row.get("receipt_off")}
        for route in ROUTES:
            for text in ROUTINE:
                row = by_case[(route, text)]
                self.assertEqual(row["sent"], "low", (route, text))
                self.assertEqual(row["final"], "Synthetic answer.\n\nswitchyard: effort high→low · local (no Jev call)")
            kept = by_case[(route, CONSEQUENTIAL)]
            self.assertEqual(kept["sent"], "high", route)
            self.assertRegex(
                kept["final"], r"^Synthetic answer\.\n\nswitchyard: effort high \(kept: consequential\) · Jev \d+ ms$"
            )
        # The default-on line reaches every foreground reply and never enters stored history.
        for row in by_case.values():
            self.assertTrue(row["transformed"], row)
            self.assertEqual(row["history"], ["Synthetic answer."], "receipt line entered model history")
        # Jev saw only the consequential request text (greetings stayed local), and only
        # closed-set state fields.
        self.assertEqual(proof["jev_requests"][:2], [CONSEQUENTIAL] * 2)
        self.assertEqual(proof["jev_keys"], ["current_request", "latest_tool_failed", "recent_tool_statuses", "turn_phase"])

        # Local-first bypass through the real turn loop on both wire shapes.
        for row in proof["bypass"]:
            if row["text"] in TRIVIAL:
                self.assertEqual((row["sent"], row["jev_calls"]), ("low", 0), (row["route"], row["text"]))
                self.assertTrue(row["final"].endswith("switchyard: effort high→low · local (no Jev call)"), row)
            else:
                self.assertEqual(row["jev_calls"], 1, (row["route"], row["text"]))
                self.assertNotIn("local (no Jev call)", row["final"], row)
        self.assertEqual(len(proof["bypass"]), 2 * (len(TRIVIAL) + len(NOT_TRIVIAL)))
        for row in proof["pinned"]:
            self.assertIn("pinned", row["pin_reply"])
            self.assertEqual((row["sent"], row["jev_calls"]), ("high", 0), row)
            self.assertEqual(row["final"], "Synthetic answer.", row)

        self.assertIn("off", proof["off_reply"])
        (quiet,) = [row for row in rows if row.get("receipt_off")]
        self.assertEqual(quiet["sent"], "low")
        self.assertEqual(quiet["final"], "Synthetic answer.")
        self.assertFalse(quiet["transformed"])

        summary = proof["summary"]
        self.assertIn("Switchyard effort summary (this session)", summary)
        # The receipt-off greeting was decided locally: no Jev call, one local decision.
        self.assertIn("Jev calls: 0, p50 n/a, p95 n/a", summary)
        self.assertIn("local decisions (no Jev call): 1", summary)
        self.assertIn("local decisions (no Jev call): 1", proof["measured_summary"])
        self.assertRegex(proof["measured_summary"], r"Jev calls: 3, p50 \d+ ms, p95 \d+ ms")
        measured = proof["measured"]
        self.assertEqual([row["sent"] for row in measured], ["high", "high", "high", "low"])
        self.assertRegex(
            measured[-1]["final"],
            r"^Synthetic answer\.\n\nswitchyard: effort high→low · local \(no Jev call\) · ~500 output tokens saved \(est\.\)$",
        )
        self.assertNotIn("switchyard:", json.dumps(measured[-1]["history"]), "receipt line entered model history")
        self.assertIn("estimated tokens saved: ~500 output (est., 1 of 1 lowered requests measured)",
                      proof["measured_summary"])
        print("E2E measured summary sample:\n" + proof["measured_summary"])
        print("\nE2E measured receipt sample:", measured[-1]["final"].splitlines()[-1])
        print("E2E receipt samples:", by_case[("anthropic", "hi")]["final"].splitlines()[-1], "|",
              by_case[("codex", CONSEQUENTIAL)]["final"].splitlines()[-1])
        print("E2E summary sample:\n" + summary)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _child(Path(sys.argv[2]))
    else:
        unittest.main()
