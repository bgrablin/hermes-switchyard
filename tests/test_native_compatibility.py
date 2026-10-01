"""Exercise native compatibility against the installed Hermes source and registry."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from scripts.ci.check_native_hermes import inspect_native_plugin


class NativeCompatibilityTests(unittest.TestCase):
    def test_installed_hermes_registration_reports_interpreter_support(self):
        spec = importlib.util.find_spec("hermes_cli.plugins")
        if spec is None or spec.origin is None:
            self.skipTest("Hermes is not installed")
        upstream = Path(spec.origin).resolve().parents[1]
        if not (upstream / ".git").exists():
            self.skipTest("Hermes is not installed from a source checkout")
        sha = subprocess.check_output(
            ["git", "-C", str(upstream), "rev-parse", "HEAD"], text=True
        ).strip()
        plugin = Path(__file__).resolve().parents[1]
        result = inspect_native_plugin(plugin, upstream_root=upstream, upstream_sha=sha)
        self.assertTrue(result["ok"])
        self.assertEqual(result["hermes_source_sha"], sha)
        self.assertEqual(result["python"], ".".join(str(x) for x in sys.version_info[:3]))
        self.assertEqual(len(result["registered_tools"]), 8)
        self.assertIsInstance(result["python_in_declared_range"], bool)
        self.assertEqual(result["registered_tools"], result["manifest_tools"])


if __name__ == "__main__":
    unittest.main()
