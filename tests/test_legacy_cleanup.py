"""Legacy jev-decision artifact detection and cleanup.

Every test runs against a fresh temporary HERMES_HOME and the real Hermes
config helpers. No test reads or writes the operator's Hermes home.
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import json
import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import hermes_switchyard as switchyard
from hermes_switchyard import legacy_cleanup as lc

HAVE_HERMES = importlib.util.find_spec("hermes_cli") is not None and importlib.util.find_spec("hermes_constants") is not None

SKILL_MD = "---\nname: jev-decision-operations\ndescription: legacy\n---\n\n# Legacy\n"
UNDERSCORE = "jev" + "_decision"
CONFIG = (
    "# operator comment survives\n"
    "model:\n  default: example\n"
    f"plugins:\n  enabled:\n  - hermes-switchyard\n  - jev-decision\n  - {UNDERSCORE}\n  - Jev-Decision\n"
    "  - ' jev-decision'\n  - jev-decision-extra\n"
)
def _valid_receipt() -> dict | None:
    """Return a receipt built and accepted by the plugin's own receipt code."""
    from hermes_switchyard import receipt_state
    from hermes_switchyard.automatic import build_routing_receipt

    receipt = build_routing_receipt(
        {
            "selected": "docker-management",
            "source": "local",
            "hosted_attempted": False,
            "hosted_skipped": "disabled",
            "cache_hit": False,
            "candidate_count": 1,
        }
    )
    return receipt_state.canonicalize_receipt(receipt)


@unittest.skipUnless(HAVE_HERMES, "Hermes Agent is not importable in this interpreter")
class LegacyCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="switchyard-legacy-")
        self.home = Path(self._tmp.name) / "hermes-home"
        self.home.mkdir()
        real_default = Path.home() / ".hermes"
        self.assertNotEqual(self.home.resolve(), real_default.resolve())
        env = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        self.addCleanup(self._tmp.cleanup)
        self.now = time.time()

    # -- fixtures ---------------------------------------------------------

    def write_config(self, text: str = CONFIG) -> Path:
        path = self.home / "config.yaml"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def make_skill(self, category: str | None = "autonomous-ai-agents", body: str = SKILL_MD) -> Path:
        base = self.home / "skills"
        if category:
            base = base / category
        path = base / lc.LEGACY_SKILL_NAME
        (path / "references").mkdir(parents=True)
        (path / "SKILL.md").write_text(body, encoding="utf-8")
        (path / "references" / "note.md").write_text("x", encoding="utf-8")
        return path

    def make_temp(self, directory: Path, name: str = ".receipt-abc123.tmp", age: float = 7200) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (self.now - age, self.now - age))
        return path

    def install_tree(self) -> Path:
        return self.home / "plugins" / lc.PLUGIN_NAME

    def data_dir(self) -> Path:
        return self.home / "plugin-data" / lc.PLUGIN_NAME

    def by_kind(self, result, kind):
        key = "findings" if "findings" in result else "actions"
        return [item for item in result[key] if item["kind"] == kind]

    def enabled(self) -> list:
        from hermes_cli.config import read_user_config_raw

        return read_user_config_raw(self.home / "config.yaml")["plugins"]["enabled"]

    def run_cli(self, *argv: str) -> tuple[int, str]:
        parser = argparse.ArgumentParser()
        switchyard._setup_cli(parser)
        args = parser.parse_args(argv)
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = args.func(args)
        return result, output.getvalue()

    def test_cli_status_warns_about_legacy_artifacts(self):
        self.write_config()
        self.make_skill()
        with (
            mock.patch.object(switchyard, "_secret", return_value=""),
            mock.patch.object(switchyard, "_tool_exposure_report", return_value=switchyard._unavailable_exposure("test")),
        ):
            code, output = self.run_cli("status", "--json")
        self.assertEqual(code, 0)
        warnings = json.loads(output)["legacy_warnings"]
        self.assertTrue(any("jev-decision" in line for line in warnings))
        self.assertNotIn(str(self.home), output)

    def test_cli_cleanup_dry_run_then_apply_archives_exact_targets(self):
        self.write_config()
        skill = self.make_skill()
        code, output = self.run_cli("cleanup")
        self.assertEqual(code, 0)
        self.assertIn("Dry run", output)
        self.assertIn("Nothing was changed", output)
        self.assertTrue(skill.is_dir())
        self.assertIn("jev-decision", self.enabled())
        code, output = self.run_cli("cleanup", "--apply")
        self.assertEqual(code, 0)
        self.assertIn("Apply", output)
        self.assertIn("Archive:", output)
        self.assertFalse(skill.exists())
        self.assertNotIn("jev-decision", self.enabled())

    def test_cli_reports_completed_archive_when_manifest_write_fails(self):
        skill = self.make_skill()
        with mock.patch.object(lc, "_write_manifest", side_effect=OSError("private failure detail")):
            code, output = self.run_cli("cleanup", "--apply")
        self.assertEqual(code, 1)
        self.assertIn("Archive:", output)
        self.assertIn("manifest could not be written", output)
        self.assertIn("applied", output)
        self.assertNotIn("private failure detail", output)
        self.assertFalse(skill.exists())

    # -- detection ----------------------------------------------------------

    def test_clean_home_reports_nothing(self):
        result = lc.detect_legacy_artifacts(self.home, now=self.now)
        self.assertFalse(result["legacy_artifacts"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(lc.status_warnings(self.home), [])

    def test_detection_is_exact_and_read_only(self):
        config = self.write_config()
        skill = self.make_skill()
        self.make_temp(self.install_tree())
        self.make_temp(self.data_dir(), ".receipt-def456.tmp")
        self.make_temp(self.data_dir(), ".receipt-new.tmp", age=5)
        (self.data_dir() / "receipt-x.tmp").write_text("", encoding="utf-8")  # wrong prefix
        before = sorted(str(p) for p in self.home.rglob("*"))
        config_bytes = config.read_bytes()

        result = lc.detect_legacy_artifacts(self.home, now=self.now)

        self.assertTrue(result["legacy_artifacts"])
        [cfg] = self.by_kind(result, lc.KIND_CONFIG)
        self.assertEqual(cfg["entries"], ["jev-decision", UNDERSCORE])
        self.assertEqual(cfg["status"], lc.STATUS_PLANNED)
        [skill_item] = self.by_kind(result, lc.KIND_SKILL)
        self.assertEqual(skill_item["target"], "$HERMES_HOME/skills/autonomous-ai-agents/jev-decision-operations")
        temps = {(i["target"].rsplit("/", 1)[1], i["status"]) for i in self.by_kind(result, lc.KIND_TEMP)}
        self.assertEqual(
            temps,
            {(".receipt-abc123.tmp", "planned"), (".receipt-def456.tmp", "planned"), (".receipt-new.tmp", "skipped")},
        )
        self.assertEqual(sorted(str(p) for p in self.home.rglob("*")), before)
        self.assertEqual(config.read_bytes(), config_bytes)
        self.assertTrue(skill.is_dir())
        warnings = lc.status_warnings(self.home)
        self.assertTrue(any("plugins_enabled_entry" in line for line in warnings))
        self.assertIn("hermes switchyard cleanup", warnings[-1])

    def test_skill_with_other_frontmatter_name_is_skipped(self):
        self.make_skill(body="---\nname: something-else\n---\n")
        [item] = self.by_kind(lc.detect_legacy_artifacts(self.home, now=self.now), lc.KIND_SKILL)
        self.assertEqual((item["status"], item["reason"]), ("skipped", "not_legacy_skill"))

    def test_top_level_skill_is_detected(self):
        self.make_skill(category=None)
        [item] = self.by_kind(lc.detect_legacy_artifacts(self.home, now=self.now), lc.KIND_SKILL)
        self.assertEqual(item["target"], "$HERMES_HOME/skills/jev-decision-operations")

    # -- dry run -----------------------------------------------------------

    def test_dry_run_is_default_and_changes_nothing(self):
        config = self.write_config()
        self.make_skill()
        self.make_temp(self.install_tree())
        before = sorted(str(p) for p in self.home.rglob("*"))
        data = config.read_bytes()

        result = lc.cleanup_legacy_artifacts(self.home, now=self.now)

        self.assertEqual((result["mode"], result["status"], result["archive"]), ("dry_run", "planned", None))
        self.assertEqual(sorted(str(p) for p in self.home.rglob("*")), before)
        self.assertEqual(config.read_bytes(), data)
        lines = lc.format_cleanup_report(result)
        self.assertIn("Re-run with --apply", lines[-1])
        self.assertEqual(len(lines), 2 + len(result["actions"]))

    # -- apply ---------------------------------------------------------------

    def test_apply_archives_exact_targets_with_backup_and_manifest(self):
        config = self.write_config()
        original = config.read_text(encoding="utf-8")
        skill = self.make_skill()
        temp_a = self.make_temp(self.install_tree())
        temp_b = self.make_temp(self.data_dir(), ".receipt-def456.tmp")
        fresh = self.make_temp(self.data_dir(), ".receipt-new.tmp", age=5)
        bystander = self.home / "skills" / "autonomous-ai-agents" / "keep-me"
        bystander.mkdir()

        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)

        self.assertEqual(result["status"], "applied", result)
        archive = self.home / result["archive"].removeprefix("$HERMES_HOME/")
        self.assertEqual(stat.S_IMODE(archive.stat().st_mode), 0o700)
        # Config: only the exact legacy entries are removed; comments and the rest survive.
        self.assertEqual(
            self.enabled(), ["hermes-switchyard", "Jev-Decision", " jev-decision", "jev-decision-extra"]
        )
        self.assertIn("# operator comment survives", config.read_text(encoding="utf-8"))
        self.assertEqual(stat.S_IMODE(config.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE((archive / "config" / "config.yaml").stat().st_mode), 0o600)
        self.assertEqual((archive / "config" / "config.yaml").read_text(encoding="utf-8"), original)
        # Skill moved, not deleted; bystander untouched.
        self.assertFalse(skill.exists())
        moved = archive / "files" / "skills" / "autonomous-ai-agents" / lc.LEGACY_SKILL_NAME
        self.assertEqual((moved / "SKILL.md").read_text(encoding="utf-8"), SKILL_MD)
        self.assertTrue((moved / "references" / "note.md").exists())
        self.assertTrue(bystander.is_dir())
        # Stale temps moved; the fresh temp remains for its writer.
        self.assertFalse(temp_a.exists() or temp_b.exists())
        self.assertTrue(fresh.exists())
        manifest = json.loads((archive / "manifest.json").read_text(encoding="utf-8"))
        applied = [a for a in manifest["actions"] if a["status"] == "applied"]
        self.assertEqual(len(applied), 4)
        self.assertTrue(all(a.get("archived_to") for a in applied))
        # Idempotent: a second run finds nothing actionable and creates no archive.
        again = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        self.assertIsNone(again["archive"])
        self.assertNotIn("planned", {a["status"] for a in again["actions"]})

    def test_apply_refuses_a_home_other_than_the_active_one(self):
        self.make_skill()
        other = Path(self._tmp.name) / "other-home"
        other.mkdir()
        with mock.patch.dict(os.environ, {"HERMES_HOME": str(other)}):
            result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        self.assertEqual(result["status"], "refused")
        self.assertEqual({a["reason"] for a in result["actions"]}, {"home_mismatch"})
        self.assertTrue((self.home / "skills" / "autonomous-ai-agents" / lc.LEGACY_SKILL_NAME).is_dir())
        self.assertFalse((self.home / "plugin-data").exists())

    def test_managed_config_is_refused(self):
        self.write_config()
        with mock.patch("hermes_cli.config.is_managed", return_value=True):
            result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        [cfg] = self.by_kind(result, lc.KIND_CONFIG)
        self.assertEqual((cfg["status"], cfg["reason"]), ("refused", "managed_config"))
        self.assertIn("jev-decision", self.enabled())

    def test_unparseable_config_is_refused(self):
        self.write_config("plugins: [unclosed\n")
        [cfg] = self.by_kind(lc.detect_legacy_artifacts(self.home, now=self.now), lc.KIND_CONFIG)
        self.assertEqual((cfg["status"], cfg["reason"]), ("refused", "config_unreadable"))

    # -- receipt ---------------------------------------------------------------

    def test_unmigrated_valid_legacy_receipt_is_kept(self):
        receipt = _valid_receipt()
        self.assertIsNotNone(receipt)
        legacy = self.install_tree() / "receipt.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text(json.dumps(receipt), encoding="utf-8")
        [item] = self.by_kind(lc.detect_legacy_artifacts(self.home, now=self.now), lc.KIND_RECEIPT)
        self.assertEqual((item["status"], item["reason"]), ("skipped", "not_migrated"))

    def test_migrated_or_invalid_legacy_receipt_is_archived(self):
        legacy = self.install_tree() / "receipt.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("not json", encoding="utf-8")
        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        [item] = self.by_kind(result, lc.KIND_RECEIPT)
        self.assertEqual(item["status"], "applied", result)
        self.assertFalse(legacy.exists())
        self.assertTrue(self.install_tree().is_dir())

    # -- fail closed: symlinks, ownership, races -------------------------------

    def test_symlinked_skill_is_refused_and_target_untouched(self):
        outside = Path(self._tmp.name) / "outside-skill"
        outside.mkdir()
        (outside / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        category = self.home / "skills" / "cat"
        category.mkdir(parents=True)
        (category / lc.LEGACY_SKILL_NAME).symlink_to(outside, target_is_directory=True)
        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        [item] = self.by_kind(result, lc.KIND_SKILL)
        self.assertEqual((item["status"], item["reason"]), ("refused", "symlink"))
        self.assertTrue((outside / "SKILL.md").exists())
        self.assertTrue((category / lc.LEGACY_SKILL_NAME).is_symlink())

    def test_symlinked_skills_root_and_install_tree_are_refused(self):
        outside = Path(self._tmp.name) / "outside"
        (outside / "cat" / lc.LEGACY_SKILL_NAME).mkdir(parents=True)
        (outside / "cat" / lc.LEGACY_SKILL_NAME / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")
        (self.home / "skills").symlink_to(outside, target_is_directory=True)
        dev_checkout = Path(self._tmp.name) / "dev-checkout"
        self.make_temp(dev_checkout)
        (dev_checkout / "receipt.json").write_text("x", encoding="utf-8")
        (self.home / "plugins").mkdir()
        self.install_tree().symlink_to(dev_checkout, target_is_directory=True)

        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)

        self.assertEqual(result["status"], "refused")
        self.assertEqual({a["reason"] for a in result["actions"]}, {"symlinked_parent"})
        self.assertTrue((outside / "cat" / lc.LEGACY_SKILL_NAME / "SKILL.md").exists())
        self.assertTrue((dev_checkout / "receipt.json").exists())
        self.assertTrue((dev_checkout / ".receipt-abc123.tmp").exists())

    def test_symlinked_temp_and_config_are_refused(self):
        outside = Path(self._tmp.name) / "outside.tmp"
        outside.write_text("keep", encoding="utf-8")
        self.data_dir().mkdir(parents=True)
        (self.data_dir() / ".receipt-link.tmp").symlink_to(outside)
        real_config = Path(self._tmp.name) / "real-config.yaml"
        real_config.write_text(CONFIG, encoding="utf-8")
        (self.home / "config.yaml").symlink_to(real_config)
        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        self.assertEqual({(a["kind"], a["reason"]) for a in result["actions"]},
                         {(lc.KIND_TEMP, "symlink"), (lc.KIND_CONFIG, "symlink")})
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")
        self.assertEqual(real_config.read_text(encoding="utf-8"), CONFIG)

    @unittest.skipUnless(hasattr(os, "geteuid"), "POSIX ownership only")
    def test_foreign_owned_targets_are_refused(self):
        self.write_config()
        self.make_skill()
        self.make_temp(self.data_dir())
        with mock.patch.object(lc.os, "geteuid", return_value=os.geteuid() + 1):
            result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        self.assertEqual({a["reason"] for a in result["actions"]}, {"not_owned"})
        self.assertIn("jev-decision", self.enabled())

    def test_skill_swapped_for_symlink_after_plan_is_refused(self):
        skill = self.make_skill()
        outside = Path(self._tmp.name) / "decoy"
        outside.mkdir()
        (outside / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

        def swap():
            os.rename(skill, Path(self._tmp.name) / "moved-away")
            skill.symlink_to(outside, target_is_directory=True)

        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now, _before_apply=swap)
        [item] = self.by_kind(result, lc.KIND_SKILL)
        self.assertEqual((item["status"], item["reason"]), ("refused", "symlink"))
        self.assertTrue(skill.is_symlink())
        self.assertTrue((outside / "SKILL.md").exists())

    def test_skill_replaced_by_different_directory_after_plan_is_refused(self):
        skill = self.make_skill()

        def replace():
            os.rename(skill, Path(self._tmp.name) / "moved-away")
            skill.mkdir()
            (skill / "SKILL.md").write_text(SKILL_MD, encoding="utf-8")

        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now, _before_apply=replace)
        [item] = self.by_kind(result, lc.KIND_SKILL)
        self.assertEqual((item["status"], item["reason"]), ("refused", "changed_since_plan"))
        self.assertTrue(skill.is_dir())

    def test_config_edited_after_plan_is_refused(self):
        config = self.write_config()

        def edit():
            config.write_text(CONFIG + "extra: 1\n", encoding="utf-8")
            os.utime(config, ns=(time.time_ns(), time.time_ns() + 5_000_000))

        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now, _before_apply=edit)
        [cfg] = self.by_kind(result, lc.KIND_CONFIG)
        self.assertEqual((cfg["status"], cfg["reason"]), ("refused", "changed_since_plan"))
        self.assertIn("jev-decision", self.enabled())

    def test_temp_removed_after_plan_is_refused_not_failed(self):
        temp = self.make_temp(self.data_dir())
        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now, _before_apply=temp.unlink)
        [item] = self.by_kind(result, lc.KIND_TEMP)
        self.assertEqual((item["status"], item["reason"]), ("refused", "changed_since_plan"))

    def test_symlinked_archive_root_is_refused(self):
        self.make_skill()
        outside = Path(self._tmp.name) / "archive-elsewhere"
        outside.mkdir()
        self.data_dir().mkdir(parents=True)
        (self.data_dir() / lc.ARCHIVE_DIRNAME).symlink_to(outside, target_is_directory=True)
        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        self.assertEqual(result["status"], "refused")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertTrue((self.home / "skills" / "autonomous-ai-agents" / lc.LEGACY_SKILL_NAME).is_dir())

    def test_output_contains_no_absolute_operator_paths(self):
        self.write_config()
        self.make_skill()
        result = lc.cleanup_legacy_artifacts(self.home, apply=True, now=self.now)
        rendered = "\n".join(lc.format_cleanup_report(result))
        self.assertNotIn(str(self.home) + "/", rendered)
        self.assertIn("$HERMES_HOME/", rendered)


if __name__ == "__main__":
    unittest.main()
