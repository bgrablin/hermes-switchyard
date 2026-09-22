"""Hermes PluginContext host compatibility for get_config-less 0.19 hosts."""

from __future__ import annotations

import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

from hermes_switchyard import host_compat
import hermes_switchyard as plugin


def _install_fake_hermes_cli(fake_cfg: dict):
    """Install a temporary hermes_cli.config.load_config for fallback tests."""
    hermes_cli = ModuleType("hermes_cli")
    config = ModuleType("hermes_cli.config")

    def load_config():
        return fake_cfg

    config.load_config = load_config  # type: ignore[attr-defined]
    hermes_cli.config = config  # type: ignore[attr-defined]
    return {
        "hermes_cli": hermes_cli,
        "hermes_cli.config": config,
    }


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
        with mock.patch.dict(sys.modules, _install_fake_hermes_cli({"plugins": {"entries": {}}})):
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "computer_max_steps", default=100),
                100,
            )

    def test_settings_from_entry_prefers_settings_then_legacy_config(self):
        self.assertEqual(
            host_compat._settings_from_entry(
                {
                    "allow_tool_override": False,
                    "settings": {"computer_max_steps": 7},
                    "config": {"computer_max_steps": 9},
                }
            ),
            {"computer_max_steps": 7},
        )
        self.assertEqual(
            host_compat._settings_from_entry(
                {"config": {"jev_provider": "typesafe"}, "allow_tool_override": True}
            ),
            {"jev_provider": "typesafe"},
        )
        self.assertEqual(
            host_compat._settings_from_entry({"allow_tool_override": False}),
            {},
        )

    def test_reads_nested_settings_from_load_config(self):
        """Fallback must use plugins.entries.<id>.settings, not the outer entry."""
        ctx = SimpleNamespace(
            manifest=SimpleNamespace(key="hermes-switchyard", name="hermes-switchyard")
        )
        fake_cfg = {
            "plugins": {
                "entries": {
                    "hermes-switchyard": {
                        "enabled": True,
                        "allow_tool_override": False,
                        "settings": {
                            "jev_provider": "openrouter",
                            "computer_max_steps": 42,
                            "public_or_sanitized_data_ack": False,
                        },
                    }
                }
            }
        }
        with mock.patch.dict(sys.modules, _install_fake_hermes_cli(fake_cfg)):
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "computer_max_steps", default=100),
                42,
            )
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "jev_provider", default="auto"),
                "openrouter",
            )
            self.assertIs(
                host_compat.ctx_get_config(
                    ctx, "public_or_sanitized_data_ack", default=True
                ),
                False,
            )
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "missing", default="x"),
                "x",
            )
            # Outer host field must not be treated as a plugin setting.
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "allow_tool_override", default="unset"),
                "unset",
            )

    def test_reads_legacy_config_mapping(self):
        ctx = SimpleNamespace(
            manifest=SimpleNamespace(key="hermes-switchyard", name="hermes-switchyard")
        )
        fake_cfg = {
            "plugins": {
                "entries": {
                    "hermes-switchyard": {
                        "config": {"computer_max_steps": 3},
                    }
                }
            }
        }
        with mock.patch.dict(sys.modules, _install_fake_hermes_cli(fake_cfg)):
            self.assertEqual(
                host_compat.ctx_get_config(ctx, "computer_max_steps", default=100),
                3,
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
            "jev_session_search_rerank",
        ):
            self.assertIn(name, registered["tools"])


class RegisterAuxiliaryTaskTests(unittest.TestCase):
    def test_returns_false_when_host_lacks_api(self):
        self.assertFalse(
            host_compat.register_auxiliary_task(SimpleNamespace(), "writer")
        )

    def test_calls_register_auxiliary_task_once(self):
        calls = []

        class Ctx:
            def register_auxiliary_task(self, key, **kwargs):
                calls.append((key, kwargs))

        ok = host_compat.register_auxiliary_task(
            Ctx(), "hermes_switchyard_writer", display_name="x"
        )
        self.assertTrue(ok)
        self.assertEqual(calls, [("hermes_switchyard_writer", {"display_name": "x"})])


if __name__ == "__main__":
    unittest.main()
