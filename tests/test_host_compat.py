"""Hermes PluginContext host compatibility for get_config-less 0.19 hosts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from hermes_switchyard import host_compat
import hermes_switchyard as plugin


class CtxGetConfigTests(unittest.TestCase):
    def test_prefers_native_get_config(self):
        ctx = SimpleNamespace(
            get_config=lambda key, default=None: f"native:{key}:{default}"
        )
        self.assertEqual(
            host_compat.ctx_get_config(ctx, "jev_provider", default="auto"),
            "native:jev_provider:auto",
        )

    def test_default_when_host_lacks_get_config(self):
        ctx = SimpleNamespace(
            manifest=SimpleNamespace(key="hermes-switchyard", name="hermes-switchyard")
        )
        self.assertEqual(
            host_compat.ctx_get_config(ctx, "computer_max_steps", default=100),
            100,
        )

    def test_reads_plugins_entries_fallback(self):
        ctx = SimpleNamespace(
            manifest=SimpleNamespace(key="hermes-switchyard", name="hermes-switchyard")
        )
        settings = {
            "allow_tool_override": False,
            "jev_provider": "openrouter",
            "computer_max_steps": 42,
        }
        with mock.patch.object(host_compat, "_entry_settings", return_value=settings):
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "computer_max_steps", default=100),
                42,
            )
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "jev_provider", default="auto"),
                "openrouter",
            )
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "missing", default="x"),
                "x",
            )


class RegisterWithoutGetConfigTests(unittest.TestCase):
    def setUp(self):
        if hasattr(plugin, "reset_runtime_status"):
            plugin.reset_runtime_status()
            self.addCleanup(plugin.reset_runtime_status)

    def test_register_completes_without_get_config(self):
        registered = {"tools": [], "hooks": [], "cli": []}

        class Context:
            manifest = SimpleNamespace(key="hermes-switchyard", name="hermes-switchyard")

            def register_tool(self, **kwargs):
                registered["tools"].append(kwargs["name"])

            def register_hook(self, name, callback):
                registered["hooks"].append(name)

            def register_cli_command(self, **kwargs):
                registered["cli"].append(kwargs["name"])

            def register_auxiliary_task(self, *args, **kwargs):
                registered["aux"] = args[0] if args else True

            def register_skill(self, *args, **kwargs):
                pass

        with mock.patch.object(
            plugin, "_secret", side_effect=RuntimeError("no secret in unit test")
        ):
            plugin.register(Context())

        self.assertIn("switchyard", registered["cli"])
        self.assertGreaterEqual(len(registered["tools"]), 5)
        for name in (
            "jev_assess",
            "jev_computer_use",
            "jev_skill_select",
            "jev_skill_select_many",
            "jev_model_route",
        ):
            self.assertIn(name, registered["tools"])


if __name__ == "__main__":
    unittest.main()
