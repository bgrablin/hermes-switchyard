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
import re
from unittest import mock

from scripts import check_portability


ROOT = Path(__file__).resolve().parent.parent


def _manifest_version() -> str:
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^version:\s*(\S+)", text)
    assert match is not None, "plugin.yaml declares no version"
    return match.group(1)


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
        self.assertEqual(parsed.name, "hermes-switchyard")
        self.assertEqual(parsed.version, _manifest_version())
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
                import hermes_switchyard

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
                hermes_switchyard.register(context)
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
            expected = relocated / "hermes_switchyard" / "skills" / "hermes-switchyard-operations" / "SKILL.md"
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
        import hermes_switchyard

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
            self.assertEqual(hermes_switchyard._secret(), "fixture-profile-a")
            active_profile["name"] = "B"
            self.assertEqual(hermes_switchyard._secret(), "fixture-profile-b")

    def test_unknown_suffix_text_is_scanned_but_binary_branding_is_not(self):
        self.assertIsNone(check_portability._text_from_bytes(b"\x89PNG\x00binary"))
        failures = check_portability._content_failures(
            "fixtures.data", "OPENROUTER_API_KEY=" + "definitely-not-a-fixture-value"
        )
        self.assertTrue(any("credential-shaped assignment" in failure for failure in failures))
        self.assertEqual(check_portability._credential_path_failures(".env"), ["credential file is tracked in .env"])
        self.assertEqual(check_portability._credential_path_failures(".env.example"), [])

    def test_content_scan_allows_loopback_bind_and_private_rejection_fixtures(self):
        self.assertEqual(
            check_portability._content_failures(
                "hermes_switchyard/browser_use.py",
                'sock.bind(("127.0.0.1", 0))\ncache = Path.home() / ".cache"\n',
            ),
            [],
        )
        self.assertEqual(
            check_portability._content_failures(
                "tests/test_browser_use.py",
                'infer_start_url("https://192.168.0.1/", "open this")\n',
            ),
            [],
        )
        self.assertTrue(
            any(
                "IPv4 address" in item
                for item in check_portability._content_failures(
                    "hermes_switchyard/browser_use.py",
                    'url = "https://8.8.8.8/"\n',
                )
            )
        )
        self.assertTrue(
            any(
                "private hostname suffix" in item
                for item in check_portability._content_failures(
                    "README.md",
                    "Do not ship example.lan addresses.\n",
                )
            )
        )

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

    def test_history_exceptions_do_not_hide_new_content_in_known_paths(self):
        from scripts.check_portability import _history_content_failures

        self.assertTrue(any(
            "credential-shaped token" in item
            for item in _history_content_failures(
                "tests/test_session_search_rerank.py",
                b"api_key=sk-" + b"z" * 26,
            )
        ))
        self.assertTrue(any(
            "host-specific absolute path" in item
            for item in _history_content_failures(
                "docs/benchmarks/feature-battery-c8e6008.json",
                b'{"source": "' + bytes((47, 116, 109, 112, 47)) + b'unknown-new-report"}',
            )
        ))
        with mock.patch("scripts.check_portability.hashlib.sha256") as sha256:
            sha256.return_value.hexdigest.return_value = (
                "3b76ca9f7e4ee91030d61adc0a8fd049149a2b1d258687dd7a8972528eeaac61"
            )
            failures = _history_content_failures(
                "docs/benchmarks/feature-battery-c8e6008.json",
                b'{"source": "example.lan"}',
            )
        self.assertEqual(
            failures,
            ["private hostname suffix in docs/benchmarks/feature-battery-c8e6008.json"],
        )


if __name__ == "__main__":
    unittest.main()
