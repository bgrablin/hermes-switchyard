"""Offline behavior checks for standalone layout and portable resource resolution."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
from scripts import check_portability


def _offline_env() -> dict[str, str]:
    """Do not pass profile or GitHub credentials into offline child processes."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in {"OPENROUTER_API_KEY", "GH_TOKEN", "GITHUB_TOKEN"}
    }


class PortabilityTests(unittest.TestCase):
    def test_root_native_entrypoint_exports_register(self):
        script = textwrap.dedent(
            """
            import importlib.util
            from pathlib import Path
            import sys

            root = Path(sys.argv[1])
            spec = importlib.util.spec_from_file_location(
                "native_jev", root / "__init__.py", submodule_search_locations=[str(root)]
            )
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            assert callable(module.register)
            """
        )
        completed = subprocess.run(
            [sys.executable, "-c", script, str(ROOT)],
            cwd=Path(tempfile.gettempdir()),
            env=_offline_env(),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_native_manifest_passes_hermes_parser_and_installer_when_available(self):
        try:
            from hermes_cli.plugins_cmd import _check_manifest_version, _read_manifest
            from hermes_cli.plugins_manifest import parse_manifest_file
        except ImportError as exc:
            self.skipTest(f"Hermes native parser unavailable: {exc}")

        raw_manifest = _read_manifest(ROOT)
        parsed = parse_manifest_file(ROOT / "plugin.yaml", ROOT, source="project", prefix="")
        self.assertIsNotNone(parsed)
        _check_manifest_version(raw_manifest, parsed.name)
        self.assertEqual(parsed.name, "jev-decision")
        self.assertEqual(parsed.version, "0.3.2")
        self.assertEqual(parsed.manifest_version, 1)
        self.assertIsNone(parsed.api_version)

    def test_skill_resource_uses_relocated_package_and_ignores_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            relocated = workspace / "relocated"
            shutil.copytree(ROOT, relocated)
            different_cwd = workspace / "different-cwd"
            different_cwd.mkdir()
            script = textwrap.dedent(
                """
                from pathlib import Path
                import jev_decision

                class Context:
                    def __init__(self):
                        self.skill_path = None

                    def get_config(self, _key, default=None):
                        return default

                    def register_auxiliary_task(self, *_args, **_kwargs):
                        pass

                    def register_tool(self, **_kwargs):
                        pass

                    def register_skill(self, _name, path, *_args, **_kwargs):
                        self.skill_path = Path(path)

                context = Context()
                jev_decision.register(context)
                assert context.skill_path is not None
                assert context.skill_path.is_file()
                print(context.skill_path)
                """
            )
            env = _offline_env()
            env["PYTHONPATH"] = str(relocated)
            completed = subprocess.run(
                [sys.executable, "-c", script],
                cwd=different_cwd,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            resource = Path(completed.stdout.strip())
            expected = relocated / "jev_decision" / "skills" / "jev-decision-operations" / "SKILL.md"
            self.assertEqual(resource, expected)

    def test_evaluation_default_parent_and_report_are_portable(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "synthetic-results.json"
            completed = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "evaluation" / "evaluate.py"),
                    "--validate",
                    "--output",
                    str(output),
                ],
                cwd=Path(directory),
                env=_offline_env(),
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
            report = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["plugin_parent"], ".")
            self.assertTrue(all(not Path(name).is_absolute() for name in report["source_hashes"]))
            self.assertNotIn(str(ROOT), output.read_text(encoding="utf-8"))

    def test_secret_resolution_is_deferred_to_profile_scoped_provider(self):
        import jev_decision

        active_profile = {"name": "A"}
        values = {"A": "fixture-profile-a", "B": "fixture-profile-b"}
        secret_scope = types.ModuleType("agent.secret_scope")
        setattr(
            secret_scope,
            "get_secret",
            lambda name: values[active_profile["name"]] if name == "OPENROUTER_API_KEY" else None,
        )
        agent_package = types.ModuleType("agent")
        agent_package.__path__ = []
        with mock.patch.dict(
            sys.modules,
            {"agent": agent_package, "agent.secret_scope": secret_scope},
        ):
            self.assertEqual(jev_decision._secret(), "fixture-profile-a")
            active_profile["name"] = "B"
            self.assertEqual(jev_decision._secret(), "fixture-profile-b")

    def test_unknown_suffix_text_is_scanned_but_binary_branding_is_not(self):
        self.assertIsNone(check_portability._text_from_bytes(b"\x89PNG\x00binary"))
        failures = check_portability._content_failures(
            "fixtures.data", "OPENROUTER_API_KEY=" + "definitely-not-a-fixture-value"
        )
        self.assertTrue(any("credential-shaped assignment" in failure for failure in failures))
        self.assertEqual(check_portability._credential_path_failures(".env"), ["credential file is tracked in .env"])
        self.assertEqual(check_portability._credential_path_failures(".env.example"), [])

    def test_history_uses_nul_paths_and_fails_closed_on_unreadable_blob(self):
        responses = [
            subprocess.CompletedProcess([], 0, stdout="commit\n", stderr=""),
            subprocess.CompletedProcess([], 0, stdout=b"odd\nname.data\0", stderr=b""),
            subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"missing"),
        ]
        with mock.patch("scripts.check_portability.subprocess.run", side_effect=responses) as run:
            with self.assertRaises(RuntimeError):
                check_portability._history_failures(Path("/repo"), set())
        self.assertEqual(run.call_args_list[1].args[0][4:8], ["-r", "-z", "--name-only", "commit"])


if __name__ == "__main__":
    unittest.main()
