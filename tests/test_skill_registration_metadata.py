"""Bundled skill metadata reaches compatible hosts without breaking old hosts."""
from pathlib import Path
import unittest
from unittest import mock

import hermes_switchyard as plugin


SKILL_PATH = Path(plugin.__file__).parent / "skills" / "hermes-switchyard-operations" / "SKILL.md"
EXPECTED_DESCRIPTION = "Use when evaluating bounded Jev decisions or integrating this plugin safely."


class BaseContext:
    def __init__(self):
        self.skills = []
        self.tools = []

    def get_config(self, key, default=None):
        return default

    def register_tool(self, **kwargs):
        self.tools.append(kwargs["name"])


class CurrentContext(BaseContext):
    def register_skill(self, name, path, description="", frontmatter=None):
        self.skills.append((name, path, description, frontmatter))


class LegacyContext(BaseContext):
    def register_skill(self, name, path):
        self.skills.append((name, path))


class SkillRegistrationMetadataTests(unittest.TestCase):
    def setUp(self):
        plugin.reset_runtime_status()
        self.addCleanup(plugin.reset_runtime_status)

    def test_bundled_description_is_registered_exactly(self):
        ctx = CurrentContext()
        plugin.register(ctx)
        self.assertEqual(ctx.skills, [
            ("hermes-switchyard-operations", SKILL_PATH, EXPECTED_DESCRIPTION, None),
        ])
        self.assertIn("jev_assess", ctx.tools)

    def test_legacy_host_keeps_two_argument_registration(self):
        ctx = LegacyContext()
        plugin.register(ctx)
        self.assertEqual(ctx.skills, [("hermes-switchyard-operations", SKILL_PATH)])

    def test_description_is_read_at_registration_time(self):
        ctx = CurrentContext()
        with mock.patch.object(Path, "read_text", return_value="---\nname: example\ndescription: New trigger.\n---\n"):
            plugin.register(ctx)
        self.assertEqual(ctx.skills[0][2], "New trigger.")

    def test_missing_description_does_not_break_registration(self):
        ctx = CurrentContext()
        with mock.patch.object(Path, "read_text", return_value="---\nname: example\n---\ndescription: body text\n"):
            plugin.register(ctx)
        self.assertEqual(ctx.skills, [("hermes-switchyard-operations", SKILL_PATH, "", None)])
        self.assertIn("jev_assess", ctx.tools)

    def test_unreadable_skill_does_not_break_registration(self):
        for error in (FileNotFoundError(), PermissionError(), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")):
            with self.subTest(error=type(error).__name__):
                ctx = CurrentContext()
                with mock.patch.object(Path, "read_text", side_effect=error):
                    plugin.register(ctx)
                self.assertEqual(ctx.skills, [("hermes-switchyard-operations", SKILL_PATH, "", None)])

    def test_real_registration_errors_are_not_hidden(self):
        class FailingContext(CurrentContext):
            def register_skill(self, name, path, description="", frontmatter=None):
                raise TypeError("host registration failed")

        with self.assertRaisesRegex(TypeError, "host registration failed"):
            plugin.register(FailingContext())


if __name__ == "__main__":
    unittest.main()
