"""Offline negative controls for the Windows downloaded-archive CI gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

from scripts.build_release import build_release
from scripts.ci.check_windows_installed_archive import (
    WindowsGateError,
    install_archive,
    invoke_synthetic_assess,
    validate_loaded_assess,
    verify_downloaded_artifact,
    verify_installed_files,
)

ROOT = Path(__file__).resolve().parent.parent


class WindowsInstalledArchiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scratch = tempfile.TemporaryDirectory(prefix="switchyard-windows-archive-")
        cls.base = Path(cls.scratch.name)
        cls.source_sha = subprocess.check_output(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        cls.archive = build_release(ROOT, cls.base / "dist", cls.source_sha)
        cls.digest = hashlib.sha256(cls.archive.read_bytes()).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.scratch.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="switchyard-artifact-test-")
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.download = self.folder / "download" / "dist"
        self.download.mkdir(parents=True)
        shutil.copy2(self.archive, self.download / self.archive.name)
        receipt = self.folder / "download" / "receipts" / "archive-sha256.json"
        receipt.parent.mkdir(parents=True)
        receipt.write_text(json.dumps({
            "source_sha": self.source_sha,
            "archive_sha256": self.digest,
        }), encoding="utf-8")
        self.receipt = receipt

    def _verified_bytes(self) -> bytes:
        path, data, digest = verify_downloaded_artifact(
            self.folder / "download", ROOT, self.source_sha
        )
        self.assertEqual(path.name, "hermes-switchyard-0.5.4.zip")
        self.assertEqual(digest, self.digest)
        return data

    def test_source_verified_download_installs_exact_payload(self):
        data = self._verified_bytes()
        installed = self.folder / "home" / "plugins" / "hermes-switchyard"
        hashes = install_archive(data, installed)
        self.assertEqual(hashes, verify_installed_files(installed, data))
        self.assertEqual(len(hashes), 50)
        self.assertEqual(
            (installed / "plugin.yaml").read_bytes(),
            (ROOT / "plugin.yaml").read_bytes(),
        )

    def test_wrong_source_and_outer_digest_are_rejected(self):
        with self.assertRaises(WindowsGateError):
            verify_downloaded_artifact(self.folder / "download", ROOT, "0" * 40)
        receipt = json.loads(self.receipt.read_text(encoding="utf-8"))
        receipt["archive_sha256"] = "0" * 64
        self.receipt.write_text(json.dumps(receipt), encoding="utf-8")
        with self.assertRaises(WindowsGateError):
            self._verified_bytes()

    def test_missing_and_duplicate_downloads_are_rejected(self):
        copy = self.download / "second" / self.archive.name
        copy.parent.mkdir()
        shutil.copy2(self.archive, copy)
        with self.assertRaises(WindowsGateError):
            self._verified_bytes()
        copy.unlink()
        (self.download / self.archive.name).unlink()
        with self.assertRaises(WindowsGateError):
            self._verified_bytes()

    def test_installed_member_changed_missing_or_extra_is_rejected(self):
        data = self._verified_bytes()
        root = self.folder / "home" / "plugins" / "hermes-switchyard"
        install_archive(data, root)
        readme = root / "README.md"
        original = readme.read_bytes()
        readme.write_bytes(original + b"edited")
        with self.assertRaises(WindowsGateError):
            verify_installed_files(root, data)
        readme.write_bytes(original)
        readme.unlink()
        with self.assertRaises(WindowsGateError):
            verify_installed_files(root, data)
        readme.write_bytes(original)
        (root / "extra.txt").write_text("not packaged", encoding="utf-8")
        with self.assertRaises(WindowsGateError):
            verify_installed_files(root, data)

    def _fake_loaded(self, installed: Path):
        module_name = "hermes_plugins.hermes_switchyard"
        client_name = module_name + ".hermes_switchyard.client"
        client = SimpleNamespace(__file__=str(installed / "hermes_switchyard" / "client.py"))
        loaded = SimpleNamespace(
            enabled=True, error=None, manifest=SimpleNamespace(
                source="user", path=str(installed), name="hermes-switchyard",
                version="0.5.4", provides_tools=["jev_assess"],
            ), module=SimpleNamespace(__file__=str(installed / "__init__.py"), __name__=module_name),
            tools_registered=["jev_assess"],
        )
        manager = SimpleNamespace(_plugins={"hermes-switchyard": loaded}, scope_key="synthetic")
        entry = SimpleNamespace(handler=lambda _args: '{}')
        registry = SimpleNamespace(get_entry=lambda name, *, scope: entry if name == "jev_assess" else None)
        return manager, registry, client_name, client

    def test_wrong_module_root_disabled_and_misregistered_tool_reject(self):
        root = self.folder / "home" / "plugins" / "hermes-switchyard"
        manager, registry, client_name, client = self._fake_loaded(root)
        with mock.patch.dict(sys.modules, {client_name: client}):
            self.assertIsNotNone(validate_loaded_assess(manager, root, registry)[0])
            manager._plugins["hermes-switchyard"].module.__file__ = str(ROOT / "__init__.py")
            with self.assertRaises(WindowsGateError):
                validate_loaded_assess(manager, root, registry)
            manager._plugins["hermes-switchyard"].module.__file__ = str(root / "__init__.py")
            client.__file__ = str(root / "wrong-client.py")
            with self.assertRaises(WindowsGateError):
                validate_loaded_assess(manager, root, registry)
            client.__file__ = str(root / "hermes_switchyard" / "client.py")
            manager._plugins["hermes-switchyard"].enabled = False
            with self.assertRaises(WindowsGateError):
                validate_loaded_assess(manager, root, registry)
            manager._plugins["hermes-switchyard"].enabled = True
            manager._plugins["hermes-switchyard"].tools_registered.clear()
            with self.assertRaises(WindowsGateError):
                validate_loaded_assess(manager, root, registry)

    def test_upstream_sha_mismatch_fails_before_loading_runtime(self):
        from scripts.ci.check_windows_installed_archive import _check_pinned_runtime

        with self.assertRaises(WindowsGateError) as raised:
            _check_pinned_runtime(ROOT, "0" * 40)
        self.assertEqual(str(raised.exception), "hermes_source_mismatch")

    def test_child_environment_drops_inherited_credentials(self):
        from scripts.ci.check_windows_installed_archive import _child_environment

        with mock.patch.dict(os.environ, {
            "OPENROUTER_API_KEY": "do-not-copy", "GITHUB_TOKEN": "do-not-copy",
            "HERMES_HOME": "/real-user-home", "TYPESAFE_API_KEY": "do-not-copy",
        }):
            env = _child_environment(self.folder / "sandbox")
        self.assertNotIn("OPENROUTER_API_KEY", env)
        self.assertNotIn("GITHUB_TOKEN", env)
        self.assertNotIn("TYPESAFE_API_KEY", env)
        self.assertEqual(env["HERMES_HOME"], str(self.folder / "sandbox"))

    def test_unsupported_host_writes_a_failed_sanitized_receipt(self):
        from scripts.ci.check_windows_installed_archive import main

        report = self.folder / "reports" / "windows.json"
        with mock.patch.object(sys, "platform", "linux"):
            status = main([
                "--artifact-dir", str(self.folder / "download"),
                "--source-root", str(ROOT), "--source-sha", self.source_sha,
                "--upstream-root", str(ROOT),
                "--upstream-sha", "8503ee4459316ce092b5d69b7d396c27aa03d0be",
                "--hermes-home", str(self.folder / "nonexistent-home"),
                "--report", str(report),
            ])
        self.assertEqual(status, 1)
        receipt = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(receipt["error_code"], "not_windows")
        self.assertFalse(receipt["ok"])
        self.assertEqual(receipt["tool"]["status"], "not_run")
        self.assertFalse((self.folder / "nonexistent-home").exists())

    def test_windows_sandbox_must_be_under_runner_temp(self):
        from scripts.ci.check_windows_installed_archive import run_gate

        args = argparse.Namespace(
            source_sha=self.source_sha,
            upstream_sha="8503ee4459316ce092b5d69b7d396c27aa03d0be",
            hermes_home=self.folder / "outside",
            artifact_dir=self.folder / "download",
            source_root=ROOT,
            upstream_root=ROOT,
        )
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"RUNNER_TEMP": str(self.folder / "runner")}
        ):
            with self.assertRaises(WindowsGateError) as raised:
                run_gate(args, {})
        self.assertEqual(str(raised.exception), "sandbox_outside_runner_temp")

    def test_generated_routing_mode_is_a_valid_string_not_yaml_boolean(self):
        from scripts.ci.check_windows_installed_archive import run_gate

        home = self.folder / "runner-temp" / "candidate-home"
        args = argparse.Namespace(
            source_sha=self.source_sha,
            upstream_sha="8503ee4459316ce092b5d69b7d396c27aa03d0be",
            hermes_home=home,
            artifact_dir=self.folder / "download",
            source_root=ROOT,
            upstream_root=ROOT,
        )
        native_result = {
            "ok": True, "hermes_version": "0.21.3", "module_root": "installed:__init__.py",
            "client_root": "installed:hermes_switchyard/client.py",
            "tool": {"name": "jev_assess", "status": "assessed",
                     "synthetic_request_count": 1, "real_network_connects": 0},
        }
        real_run = subprocess.run

        def child_or_git(command, **kwargs):
            if "--child-probe" in command:
                return subprocess.CompletedProcess(command, 0, json.dumps(native_result), "")
            return real_run(command, **kwargs)

        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(
            os.environ, {"RUNNER_TEMP": str(home.parent)}
        ), mock.patch(
            "scripts.ci.check_windows_installed_archive.subprocess.run", side_effect=child_or_git
        ):
            report = {}
            run_gate(args, report)

        self.assertTrue(report["ok"])
        settings = yaml.safe_load((home / "config.yaml").read_text(encoding="utf-8"))[
            "plugins"
        ]["entries"]["hermes-switchyard"]["settings"]
        manifest = yaml.safe_load(
            (home / "plugins" / "hermes-switchyard" / "plugin.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["config_schema"]["automatic_skill_routing_mode"]["type"], "str")
        self.assertIs(type(settings["automatic_skill_routing_mode"]), str)
        self.assertEqual(settings["automatic_skill_routing_mode"], "off")
        try:
            from hermes_cli.plugins_manifest import validate_config_schema
        except ImportError:
            if os.environ.get("SWITCHYARD_REQUIRE_HERMES") == "1":
                self.fail("required pinned Hermes config validator is unavailable")
        else:
            self.assertEqual(validate_config_schema(
                "hermes-switchyard", manifest["config_schema"], settings
            ), [])

    def test_socket_escape_is_denied_not_counted_as_a_synthetic_call(self):
        client = SimpleNamespace(http=SimpleNamespace(client=__import__("http.client", fromlist=["client"])))

        def escaped(_args):
            with socket.socket() as sock:
                sock.connect(("127.0.0.1", 9))
            return json.dumps({"status": "assessed", "answers": {"fit": {"noul": 0.95}}})

        with self.assertRaises(WindowsGateError):
            invoke_synthetic_assess(SimpleNamespace(handler=escaped), client)

    def test_handler_error_is_not_an_assessment(self):
        client = SimpleNamespace(http=SimpleNamespace(client=__import__("http.client", fromlist=["client"])))
        with self.assertRaises(WindowsGateError):
            invoke_synthetic_assess(SimpleNamespace(handler=lambda _args: '{"status":"error"}'), client)


if __name__ == "__main__":
    unittest.main()
