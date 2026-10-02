"""Keep the bundled guide discoverable and its examples executable offline."""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import shlex
import unittest

from hermes_switchyard import _setup_cli
from scripts.build_release import RELEASE_FILES

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "hermes_switchyard/skills/hermes-switchyard-operations/SKILL.md"


class OperationsSkillTests(unittest.TestCase):
    def setUp(self):
        self.text = SKILL.read_text(encoding="utf-8")

    def test_discovery_metadata_has_a_short_complete_trigger(self):
        self.assertTrue(self.text.startswith("---\n"))
        frontmatter, body = self.text[4:].split("\n---\n", 1)
        fields = dict(re.findall(r"^([a-z]+): (.+)$", frontmatter, re.MULTILINE))
        self.assertEqual(fields["name"], "hermes-switchyard-operations")
        description = fields["description"].strip("\"'")
        self.assertTrue(description.startswith("Use when "))
        self.assertTrue(description.endswith("."))
        self.assertLessEqual(len(description), 60)
        self.assertTrue(body.strip())

    def test_terminal_examples_parse_with_the_real_command_parser(self):
        examples = re.findall(r"^terminal\(command=.+\)$", self.text, re.MULTILINE)
        self.assertTrue(examples, "the guide needs runnable terminal examples")
        plugin_commands = []
        for example in examples:
            with self.subTest(example=example):
                call = ast.parse(example, mode="eval").body
                self.assertIsInstance(call, ast.Call)
                self.assertEqual(call.func.id, "terminal")
                command = ast.literal_eval(next(item.value for item in call.keywords if item.arg == "command"))
                argv = shlex.split(command)
                if argv[:2] == ["hermes", "switchyard"]:
                    parser = argparse.ArgumentParser()
                    _setup_cli(parser)
                    parsed = parser.parse_args(argv[2:])
                    self.assertIsNotNone(parsed.switchyard_command)
                    plugin_commands.append(parsed.switchyard_command)
                elif argv[:3] == ["hermes", "config", "set"]:
                    self._assert_plugin_setting(argv)
                else:
                    self.fail(f"no validator for documented command: {command}")
        self.assertTrue(plugin_commands)

    def _assert_plugin_setting(self, argv):
        """Check a documented setting against the plugin.yaml settings schema."""
        self.assertEqual(len(argv), 5, argv)
        prefix = "plugins.entries.hermes-switchyard.settings."
        self.assertTrue(argv[3].startswith(prefix), argv[3])
        name, value = argv[3][len(prefix):], argv[4]
        manifest = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        declared = re.search(
            r"^  " + re.escape(name) + r": \{type: (\w+), default: [^,]+, description: \"([^\"]*)\"",
            manifest, re.MULTILINE,
        )
        self.assertIsNotNone(declared, f"{name} is not a declared plugin setting")
        kind, description = declared.groups()
        if kind == "bool":
            self.assertIn(value, {"true", "false"})
        else:
            self.assertEqual(kind, "str", f"{name}: no validator for type {kind}")
            self.assertRegex(description, r"\b" + re.escape(value) + r"\b")

    def test_linked_guides_exist_in_the_release_payload(self):
        links = re.findall(r"\]\(([^)]+)\)", self.text)
        self.assertTrue(links, "detailed guidance must remain reachable offline")
        for link in links:
            if "://" in link or link.startswith("#"):
                continue
            with self.subTest(link=link):
                target = (SKILL.parent / link.split("#", 1)[0]).resolve()
                relative = target.relative_to(ROOT).as_posix()
                self.assertIn(relative, RELEASE_FILES)
                self.assertTrue(target.is_file())
        self.assertIn(SKILL.relative_to(ROOT).as_posix(), RELEASE_FILES)


if __name__ == "__main__":
    unittest.main()
