"""Offline safety checks for the manual exact-source effort replay."""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from scripts import live_effort_replay as replay
from scripts.build_release import build_release

ROOT = Path(__file__).resolve().parent.parent


class LiveEffortReplayTests(unittest.TestCase):
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
                    "source_tree": replay._git(ROOT, "rev-parse", "HEAD^{tree}"), "receipts": []}
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


if __name__ == "__main__":
    unittest.main()
