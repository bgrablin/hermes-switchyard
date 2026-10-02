"""Observable preservation contracts for opt-in terminal compaction."""
from __future__ import annotations

import importlib.util
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from hermes_switchyard.output_pruning import OutputCompactionGuard, prune_terminal_result
from scripts.build_release import RELEASE_FILES


def result(**extra):
    return json.dumps({"output": "build checkpoint completed\n" * 300
                       + "artifact digest: REQUIRED_SYNTHETIC_VALUE\n"
                       + "build checkpoint completed\n" * 300,
                       "exit_code": 0, "stderr": "", **extra})


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.guard = OutputCompactionGuard()
        self.scope = {"session_id": "session", "task_id": "task", "turn_id": "turn"}
        self.raw = result()

    def capture(self, text="Report the artifact digest.", **extra):
        self.guard.capture(user_message=text, **{**self.scope, **extra})

    def prune(self, **extra):
        return self.guard.prune(tool_name="terminal", status="ok", result=self.raw,
                                **{**self.scope, **extra})

    def test_ordinary_request_keeps_digest_and_multiplicity(self):
        self.capture()
        out = self.prune()
        self.assertLess(len(out), len(self.raw))
        data = json.loads(out)
        self.assertIn("artifact digest: REQUIRED_SYNTHETIC_VALUE", data["output"])
        self.assertEqual(data["output"].count("299 additional times"), 2)
        self.assertEqual({k: v for k, v in data.items() if k != "output"},
                         {"exit_code": 0, "stderr": ""})

    def test_full_output_requests_preserve_original_bytes(self):
        for text in ("show full output", "keep the full dump", "show stdout verbatim",
                     "do not compact this", "show complete output", "show raw stdout",
                     "keep the exact output", "show untruncated output", "show me everything"):
            with self.subTest(text=text):
                self.capture(text)
                self.assertIsNone(self.prune())

    def test_cue_after_long_context_and_multimodal_text_is_preserved(self):
        self.capture([{"type": "image", "text": "ignored"},
                      {"type": "text", "text": "context " * 3000},
                      {"type": "input_text", "text": "Show full output."}])
        self.assertIsNone(self.prune())

    def test_unknown_capture_preserves_output(self):
        for text in (None, "", {}, [{"type": "image", "text": "ordinary"}],
                     [{"type": [], "text": "full output"}]):
            with self.subTest(text=text):
                self.capture(text)
                self.assertIsNone(self.prune())

    def test_next_turn_replaces_preservation_decision(self):
        self.capture("keep full output")
        self.capture("report digest", turn_id="next")
        self.assertIsNotNone(self.prune(turn_id="next"))
        self.assertIsNone(self.prune())

    def test_sibling_and_session_captures_cannot_override_full_request(self):
        self.capture("keep full output")
        self.capture("report digest", task_id="sibling")
        self.capture("report digest", task_id=None)
        self.assertIsNone(self.prune())
        self.assertIsNotNone(self.prune(task_id="sibling"))

    def test_equal_task_ids_in_different_sessions_are_isolated(self):
        self.capture("keep full output")
        self.capture("report digest", session_id="other")
        self.assertIsNone(self.prune())
        self.assertIsNotNone(self.prune(session_id="other"))

    def test_no_session_fallback_for_uncaptured_task(self):
        self.capture(task_id=None)
        self.assertIsNone(self.prune())

    def test_missing_identity_or_turn_capture_preserves_output(self):
        self.assertIsNone(self.prune())
        self.capture()
        self.assertIsNone(self.prune(turn_id=None))
        self.assertIsNone(self.prune(session_id=None, task_id=None))

    def test_command_preservation_cue_is_respected(self):
        self.capture()
        self.assertIsNone(self.prune(args={"command": "make # show full output"}))

    def test_expiry_and_eviction_preserve_uncertain_scope(self):
        with patch("hermes_switchyard.output_pruning.time.monotonic", return_value=0):
            self.capture()
        with patch("hermes_switchyard.output_pruning.time.monotonic", return_value=601):
            self.assertIsNone(self.prune())
        self.capture("keep full output")
        for n in range(129):
            self.capture(task_id=f"sibling-{n}")
        self.assertIsNone(self.prune())


class EnvelopeTests(unittest.TestCase):
    def prune(self, raw, **extra):
        return prune_terminal_result(tool_name="terminal", status="ok", result=raw, **extra)

    def test_stdout_envelope_retains_other_fields(self):
        data = json.loads(result())
        data["stdout"] = data.pop("output")
        data["duration"] = 2.0
        out = json.loads(self.prune(json.dumps(data)))
        self.assertEqual({k: v for k, v in out.items() if k != "stdout"},
                         {"exit_code": 0, "stderr": "", "duration": 2.0})
        self.assertIn("REQUIRED_SYNTHETIC_VALUE", out["stdout"])

    def test_ambiguous_unknown_and_failed_envelopes_are_preserved(self):
        for raw in (result(stdout=None), result(stdout="different"), result(exit_code=1),
                    result(status="failed"), result(status=[]), result(returncode=1),
                    result(exitcode="0"), result(stderr="warning"), result(truncated=True),
                    json.dumps({"processes": [{"output": "x" * 9000}]}),
                    "{broken JSON}", "[" * 2000 + "]" * 2000):
            with self.subTest(raw=raw[:60]):
                self.assertIsNone(self.prune(raw))

    def test_duplicate_keys_are_preserved_without_reinterpretation(self):
        raw = result()[:-1] + ', "exit_code": 1}'
        self.assertIsNone(self.prune(raw))

    def test_observer_failure_fields_preserve_output(self):
        for fields in ({"error": "failed"}, {"error_type": "failure"},
                       {"error_message": "warning"}, {"ok": False}):
            with self.subTest(fields=fields):
                self.assertIsNone(self.prune(result(), **fields))

    def test_serialization_failure_leaves_original_envelope(self):
        raw = result()
        with patch("hermes_switchyard.output_pruning.json.dumps", side_effect=TypeError):
            self.assertIsNone(self.prune(raw))

    def test_unique_middle_evidence_is_never_truncated(self):
        original = "".join(f"build step {n}: exact artifact ID {n * n}\n" for n in range(1000))
        self.assertIsNone(self.prune(json.dumps({"output": original, "exit_code": 0})))


def _native_child(plugin: Path) -> None:
    # The test module imports the checkout for unit tests. Native replay must
    # load the installed package afresh, rather than reuse those imports.
    for name in list(sys.modules):
        if name == "hermes_switchyard" or name.startswith("hermes_switchyard."):
            sys.modules.pop(name)
    checkout = Path(__file__).resolve().parents[1]
    sys.path[:] = [value for value in sys.path
                   if not Path(value or os.getcwd()).resolve().is_relative_to(checkout)]
    from hermes_cli.plugins import get_plugin_manager, invoke_hook
    from model_tools import _apply_transform_tool_result_hook, _CallIds

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    loaded = manager._plugins["hermes-switchyard"]
    assert loaded.enabled and loaded.error is None
    assert Path(loaded.module.__file__).resolve().is_relative_to(plugin.resolve())
    installed_output = importlib.import_module(loaded.module.register.__module__ + ".output_pruning")
    assert Path(installed_output.__file__).resolve().is_relative_to(plugin.resolve())
    assert len(manager._hooks["transform_tool_result"]) == 1
    raw = result()
    outputs = {}
    with patch("http.client.HTTPSConnection.request", side_effect=AssertionError("unexpected_request")) as wire:
        for task, message in (("full", "Show the full output verbatim."),
                              ("ordinary", "Report the artifact digest.")):
            scope = {"session_id": "shared", "task_id": task, "turn_id": "turn"}
            invoke_hook("pre_llm_call", user_message=message, **scope)
        for task in ("full", "ordinary", "uncaptured"):
            ids = _CallIds(task_id=task, session_id="shared", turn_id="turn", tool_call_id=task)
            out = _apply_transform_tool_result_hook("terminal", {"command": "build"}, raw, 1, ids)
            outputs[task] = {"chars": len(out), "unchanged": out == raw,
                             "digest_retained": "REQUIRED_SYNTHETIC_VALUE" in out}
        assert wire.call_count == 0
    assert outputs["full"]["unchanged"] and outputs["uncaptured"]["unchanged"]
    assert outputs["ordinary"]["chars"] < len(raw)
    assert all(item["digest_retained"] for item in outputs.values())
    manager.unload()
    print(json.dumps({"installed_plugin": True, "installed_output_module": True,
                      "native_result_transform": True,
                      "https_request_attempts": wire.call_count, "outputs": outputs}))


class NativePreservationTests(unittest.TestCase):
    def test_installed_plugin_preserves_full_output_through_native_transform(self):
        try:
            available = importlib.util.find_spec("hermes_cli.plugins") is not None
        except ModuleNotFoundError:
            available = False
        if not available:
            self.skipTest("Hermes is not installed")
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory(prefix="switchyard-output-") as temporary:
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
                "        adaptive_reasoning_effort: false\n        repeated_output_compaction: true\n"
                "        cross_tool_stuck_detection: true\n", encoding="utf-8")
            bundled = workspace / "bundled"
            bundled.mkdir()
            env = {key: value for key, value in os.environ.items()
                   if key.upper() in {"PATH", "PYTHONPATH", "TMPDIR", "LANG", "SYSTEMROOT", "WINDIR"}}
            env.update({"HOME": str(workspace), "HERMES_HOME": str(home),
                        "HERMES_BUNDLED_PLUGINS": str(bundled)})
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(plugin)],
                                   cwd=workspace, env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(child.returncode, 0, child.stderr[-3000:])
            proof = json.loads(child.stdout.splitlines()[-1])
            self.assertTrue(proof["installed_plugin"])
            self.assertTrue(proof["native_result_transform"])
            self.assertTrue(proof["installed_output_module"])
            self.assertEqual(proof["https_request_attempts"], 0)
            self.assertTrue(proof["outputs"]["full"]["unchanged"])


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _native_child(Path(sys.argv[2]))
    else:
        unittest.main()
