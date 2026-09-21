"""Hermes 0.19 compatibility: status selection when parse_config_string_list is absent."""

from __future__ import annotations

import sys
import unittest
from types import ModuleType, SimpleNamespace
from unittest import mock

import hermes_switchyard


def _install_fake_config(disabled_raw):
    hermes_cli = ModuleType("hermes_cli")
    config = ModuleType("hermes_cli.config")

    def load_config():
        return {"agent": {"disabled_toolsets": disabled_raw}}

    config.load_config = load_config  # type: ignore[attr-defined]
    hermes_cli.config = config  # type: ignore[attr-defined]
    return {
        "hermes_cli": hermes_cli,
        "hermes_cli.config": config,
        "agent.skill_utils": SimpleNamespace(),  # no parse_config_string_list
    }


class DisabledToolsetsCompatTests(unittest.TestCase):
    def test_falls_back_when_parse_config_string_list_missing(self):
        seams = hermes_switchyard._load_hermes_seams()
        with mock.patch.dict(sys.modules, _install_fake_config(["computer_use", "web"])):
            names = seams.disabled_toolsets()
        self.assertEqual(names, ["computer_use", "web"])

    def test_csv_string_fallback(self):
        seams = hermes_switchyard._load_hermes_seams()
        with mock.patch.dict(sys.modules, _install_fake_config("computer_use, terminal")):
            names = seams.disabled_toolsets()
        self.assertEqual(names, ["computer_use", "terminal"])

    def test_none_and_empty(self):
        seams = hermes_switchyard._load_hermes_seams()
        with mock.patch.dict(sys.modules, _install_fake_config(None)):
            self.assertEqual(seams.disabled_toolsets(), [])

    def test_default_selection_survives_missing_parse_helper(self):
        seams = hermes_switchyard._load_hermes_seams()
        seams.default_selection = lambda: (
            "platform_default",
            ["hermes_switchyard", "computer_use", "terminal"],
        )
        with mock.patch.dict(sys.modules, _install_fake_config([])):
            seams2 = hermes_switchyard._load_hermes_seams()
            seams2.default_selection = seams.default_selection
            selection = hermes_switchyard._resolve_selection(seams2, None)
        self.assertEqual(selection["source"], "platform_default")
        self.assertIn("hermes_switchyard", selection["enabled_toolsets"])
        self.assertEqual(selection["disabled_toolsets"], [])


if __name__ == "__main__":
    unittest.main()
