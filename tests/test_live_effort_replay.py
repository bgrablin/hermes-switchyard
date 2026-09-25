"""Offline safety checks for the manual exact-source effort replay."""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import live_effort_replay as replay
from scripts.build_release import build_release

ROOT = Path(__file__).resolve().parent.parent


class LiveEffortReplayTests(unittest.TestCase):
    def test_manual_replay_child_keeps_only_actual_windows_systemroot(self):
        """The hosted entry point must not pass credentials with its Windows loader root."""
        source_sha = "a" * 40
        tree = "b" * 40
        parent = {
            "PATH": "synthetic-path", "LANG": "synthetic-lang", "TMPDIR": "synthetic-temp",
            "sYsTeMrOoT": "synthetic-systemroot", "HOME": "private-parent-home",
            "HERMES_HOME": "private-parent-hermes", "OPENROUTER_API_KEY": "synthetic-provider",
            "TYPESAFE_API_KEY": "synthetic-other-provider", "ANTHROPIC_API_KEY": "synthetic-anthropic",
            "HERMES_SHARED_AUTH_DIR": "private-parent-shared-auth", "GITHUB_TOKEN": "synthetic-github",
            "GH_TOKEN": "synthetic-gh", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc",
        }
        windows_os = SimpleNamespace(name="nt", environ=parent)
        child_report = {"ok": True, "source_sha": source_sha, "source_tree": tree,
                        "jev_transport": "hosted", "receipts": []}
        def prepared(_archive, _source_root, _source_sha, home):
            home.mkdir(parents=True)
            return tree
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "report.json"
            completed = SimpleNamespace(returncode=0, stdout=json.dumps(child_report), stderr="")
            with (patch.object(replay, "prepare", side_effect=prepared),
                  patch.object(replay, "os", windows_os),
                  patch.object(replay.subprocess, "run", return_value=completed) as run,
                  redirect_stdout(io.StringIO())):
                outcome = replay.main(["--archive", "unused.zip", "--source-root", str(ROOT),
                    "--source-sha", source_sha, "--secret-home", directory,
                    "--provider", "openrouter", "--report", str(report), "--allow-hosted"])
            self.assertEqual(outcome, 0, json.loads(report.read_text()).get("error"))
            env = run.call_args.kwargs["env"]
            self.assertIn("sYsTeMrOoT", env)
            self.assertEqual(env["sYsTeMrOoT"], parent["sYsTeMrOoT"])
            self.assertEqual(set(env), {"PATH", "LANG", "TMPDIR", "sYsTeMrOoT",
                "HOME", "HERMES_HOME", "HERMES_BUNDLED_PLUGINS"})
            self.assertNotEqual(env["HOME"], parent["HOME"])
            self.assertNotEqual(env["HERMES_HOME"], parent["HERMES_HOME"])

    def test_synthetic_wire_child_keeps_only_actual_windows_systemroot(self):
        """Capture the real installed-test subprocess boundary without launching it."""
        parent = {"PATH": "synthetic-path", "LANG": "synthetic-lang",
                  "TMPDIR": "synthetic-temp", "PYTHONPATH": "synthetic-pythonpath",
                  "sYsTeMrOoT": "synthetic-systemroot", "HOME": "private-parent-home",
                  "HERMES_HOME": "private-parent-hermes", "OPENROUTER_API_KEY": "synthetic-provider",
                  "TYPESAFE_API_KEY": "synthetic-other-provider", "ANTHROPIC_API_KEY": "synthetic-anthropic",
                  "HERMES_SHARED_AUTH_DIR": "private-parent-shared-auth", "GITHUB_TOKEN": "synthetic-github",
                  "GH_TOKEN": "synthetic-gh", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc"}
        windows_os = SimpleNamespace(name="nt", environ=parent)
        original_run = subprocess.run
        captured = {}

        class ChildEnvCaptured(Exception):
            pass

        def intercept(command, *args, **kwargs):
            if command[:2] == [sys.executable, "-c"]:
                captured.update(kwargs)
                raise ChildEnvCaptured()
            return original_run(command, *args, **kwargs)

        with (patch.object(sys.modules[__name__], "os", windows_os),
              patch.object(replay, "os", windows_os),
              patch.object(subprocess, "run", side_effect=intercept)):
            with self.assertRaises(ChildEnvCaptured):
                self.test_real_installed_seven_step_wire_replay_with_synthetic_jev()
        env = captured["env"]
        self.assertIn("sYsTeMrOoT", env)
        self.assertEqual(env["sYsTeMrOoT"], parent["sYsTeMrOoT"])
        self.assertEqual(set(env), {"PATH", "LANG", "TMPDIR", "sYsTeMrOoT",
            "HOME", "HERMES_HOME", "HERMES_BUNDLED_PLUGINS", "PYTHONPATH"})
        self.assertNotEqual(env["HOME"], parent["HOME"])
        self.assertNotEqual(env["HERMES_HOME"], parent["HERMES_HOME"])

    def test_windows_missing_or_empty_systemroot_refuses_child(self):
        """Do not guess a loader path or invoke the child when Windows lacks one."""
        source_sha = "a" * 40
        def prepared(_archive, _source_root, _source_sha, home):
            home.mkdir(parents=True)
            return "b" * 40
        for root in (None, "", "   "):
            with self.subTest(root_kind="missing" if root is None else "empty"):
                parent = {"PATH": "synthetic-path", "OPENROUTER_API_KEY": "synthetic-provider"}
                if root is not None:
                    parent["sYsTeMrOoT"] = root
                with tempfile.TemporaryDirectory() as directory:
                    report = Path(directory) / "report.json"
                    with (patch.object(replay, "prepare", side_effect=prepared),
                          patch.object(replay, "os", SimpleNamespace(name="nt", environ=parent)),
                          patch.object(replay.subprocess, "run") as run,
                          redirect_stdout(io.StringIO())):
                        outcome = replay.main(["--archive", "unused.zip", "--source-root", str(ROOT),
                            "--source-sha", source_sha, "--secret-home", directory,
                            "--provider", "openrouter", "--report", str(report), "--allow-hosted"])
                    self.assertEqual(outcome, 1)
                    self.assertEqual(json.loads(report.read_text()), {"ok": False,
                        "error": "Windows SystemRoot is unavailable for the isolated child"})
                    run.assert_not_called()

    def test_requires_explicit_hosted_gate_before_install_or_secret_resolution(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(replay, "prepare") as prepare:
            report = Path(directory) / "report.json"
            with redirect_stdout(io.StringIO()):
                outcome = replay.main(["--archive", "missing.zip", "--source-root", str(ROOT),
                    "--source-sha", "a" * 40, "--secret-home", directory,
                    "--provider", "typesafe", "--report", str(report)])
            self.assertEqual(outcome, 1)
            self.assertFalse(json.loads(report.read_text())["ok"])
            prepare.assert_not_called()

    def test_rejects_wrong_source_sha_before_archive_install(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(replay.ReplayError, "differs"):
                replay.prepare(Path(directory) / "missing.zip", ROOT, "a" * 40, Path(directory) / "home")
            self.assertFalse((Path(directory) / "home").exists())

    def test_exact_archive_install_and_child_credential_boundary(self):
        source_sha = replay._git(ROOT, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as directory:
            archive = build_release(ROOT, Path(directory) / "archive", source_sha)
            report = Path(directory) / "report.json"
            secret_home = Path(directory) / "profile"
            secret_home.mkdir()
            captured = {}

            def child(command, **kwargs):
                captured.update(command=command, **kwargs)
                receipt = {"ok": True, "source_sha": source_sha,
                    "source_tree": replay._git(ROOT, "rev-parse", "HEAD^{tree}"),
                    "jev_transport": "hosted", "receipts": []}
                from types import SimpleNamespace
                return SimpleNamespace(returncode=0, stdout=json.dumps(receipt), stderr="")

            original_run = replay.subprocess.run
            def run(command, **kwargs):
                return child(command, **kwargs) if command[0] == replay.sys.executable else original_run(command, **kwargs)
            with patch.object(replay.subprocess, "run", side_effect=run):
                with patch.dict(os.environ, {"OPENROUTER_API_KEY": "synthetic-secret-do-not-log"}):
                    with redirect_stdout(io.StringIO()):
                        outcome = replay.main(["--archive", str(archive), "--source-root", str(ROOT),
                            "--source-sha", source_sha, "--secret-home", str(secret_home),
                            "--provider", "typesafe", "--report", str(report), "--allow-hosted"])
            self.assertEqual(outcome, 0)
            self.assertEqual(json.loads(report.read_text())["source_sha"], source_sha)
            self.assertNotIn("OPENROUTER_API_KEY", captured["env"])
            self.assertNotIn("synthetic-secret-do-not-log", report.read_text())
            self.assertTrue(Path(captured["cwd"]).is_absolute())

    def test_wrong_or_modified_archive_fails_closed(self):
        source_sha = replay._git(ROOT, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as directory:
            archive = build_release(ROOT, Path(directory) / "archive", source_sha)
            with zipfile.ZipFile(archive, "a") as bundle:
                bundle.writestr("unexpected.txt", "synthetic")
            with self.assertRaisesRegex(Exception, "outside the release boundary"):
                replay.prepare(archive, ROOT, source_sha, Path(directory) / "install")

    def test_real_child_fails_closed_without_profile_credential(self):
        source_sha = replay._git(ROOT, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as directory:
            archive = build_release(ROOT, Path(directory) / "archive", source_sha)
            secret_home = Path(directory) / "empty-profile"
            secret_home.mkdir()
            report = Path(directory) / "report.json"
            with redirect_stdout(io.StringIO()):
                outcome = replay.main(["--archive", str(archive), "--source-root", str(ROOT),
                    "--source-sha", source_sha, "--secret-home", str(secret_home),
                    "--provider", "typesafe", "--report", str(report), "--allow-hosted"])
            self.assertEqual(outcome, 1)
            self.assertIn("no TYPESAFE_API_KEY", report.read_text())
            self.assertNotIn("Traceback", report.read_text())

    def test_real_installed_seven_step_wire_replay_with_synthetic_jev(self):
        source_sha = replay._git(ROOT, "rev-parse", "HEAD")
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            archive = build_release(ROOT, work / "archive", source_sha)
            home = work / "hermes"
            tree = replay.prepare(archive, ROOT, source_sha, home)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [hermes-switchyard]\n  entries:\n    hermes-switchyard:\n"
                "      settings:\n        jev_provider: openrouter\n"
                "        automatic_skill_recommendation: false\n", encoding="utf-8")
            secret_home = work / "synthetic-profile"
            secret_home.mkdir()
            (secret_home / ".env").write_text("OPENROUTER_API_KEY=test-key\n", encoding="utf-8")
            bundled = work / "bundled"
            bundled.mkdir()
            code = """
import json, sys
from pathlib import Path
from scripts.live_effort_replay import run_installed
class FakeJev:
    def decide(self, state, questions, **kwargs):
        levels = list(questions['reasoning_effort']['criteria'])
        return {'answers': {'reasoning_effort': {'choice': levels[0],
                'confidence': 0.9, 'probabilities': {level: 1.0 / len(levels) for level in levels}}}}
result = run_installed(Path(sys.argv[1]), sys.argv[2], sys.argv[3], 'openrouter',
                       Path(sys.argv[4]), synthetic_client_factory=FakeJev)
print(json.dumps(result))
"""
            env = replay._sparse_child_env(("PATH", "LANG", "TMPDIR"))
            env.update({"HOME": str(work), "HERMES_HOME": str(home),
                "HERMES_BUNDLED_PLUGINS": str(bundled), "PYTHONPATH": str(ROOT)})
            result = subprocess.run([sys.executable, "-c", code,
                str(home / "plugins" / "hermes-switchyard"), source_sha, tree, str(secret_home)],
                cwd=work, env=env, capture_output=True, text=True, timeout=90)
            self.assertEqual(result.returncode, 0, result.stderr)
            proof = json.loads(result.stdout.splitlines()[-1])
            self.assertTrue(proof["ok"])
            self.assertEqual(proof["jev_transport"], "synthetic")
            self.assertEqual(len(proof["receipts"]), 16)
            self.assertEqual(sum(row["jev_called"] for row in proof["receipts"]), 2)
            self.assertEqual({row["provider"] for row in proof["receipts"]}, {"openai-codex", "anthropic"})
            self.assertTrue(all(row["source_sha"] == source_sha and row["source_tree"] == tree
                for row in proof["receipts"]))


if __name__ == "__main__":
    unittest.main()
