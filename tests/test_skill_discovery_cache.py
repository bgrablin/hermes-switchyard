"""In-process skill registry discovery cache (no behavior change on miss)."""
from __future__ import annotations

import json
import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

from hermes_switchyard import automatic
from hermes_switchyard.automatic import (
    clear_skill_discovery_cache,
    discover_available_skill_candidates,
)
from test_support import HermesHomeTestCase


def _payload(*names: str) -> str:
    return json.dumps(
        {
            "success": True,
            "skills": [
                {"name": name, "description": f"{name} description"} for name in names
            ],
        }
    )


class SkillDiscoveryCacheTests(HermesHomeTestCase):
    def setUp(self):
        super().setUp()
        clear_skill_discovery_cache()
        self.addCleanup(clear_skill_discovery_cache)

    def _patch_skills_list(self, skills_list):
        fake_module = types.ModuleType("tools.skills_tool")
        fake_module.skills_list = skills_list
        return mock.patch.dict(sys.modules, {"tools.skills_tool": fake_module})

    def test_second_discover_reuses_skills_list_without_rescan(self):
        skills_list = mock.Mock(return_value=_payload("docker-management"))
        with self._patch_skills_list(skills_list):
            first = discover_available_skill_candidates()
            second = discover_available_skill_candidates()
        self.assertEqual([c["name"] for c in first], ["docker-management"])
        self.assertEqual(first, second)
        self.assertEqual(skills_list.call_count, 1)

    def test_force_refresh_bypasses_cache(self):
        skills_list = mock.Mock(
            side_effect=[
                _payload("docker-management"),
                _payload("network-printer-operations"),
            ]
        )
        with self._patch_skills_list(skills_list):
            first = discover_available_skill_candidates()
            second = discover_available_skill_candidates(force_refresh=True)
        self.assertEqual(first[0]["name"], "docker-management")
        self.assertEqual(second[0]["name"], "network-printer-operations")
        self.assertEqual(skills_list.call_count, 2)

    def test_skills_dir_mtime_change_invalidates_cache(self):
        skills_list = mock.Mock(
            side_effect=[
                _payload("docker-management"),
                _payload("systematic-debugging"),
            ]
        )
        with self._patch_skills_list(skills_list):
            first = discover_available_skill_candidates()
            self.assertEqual(first[0]["name"], "docker-management")
            skills = self.hermes_home / "skills"
            skills.mkdir(parents=True, exist_ok=True)
            (skills / "new-skill").mkdir()
            time.sleep(0.01)
            Path(skills / "new-skill" / "SKILL.md").write_text("# skill\n", encoding="utf-8")
            second = discover_available_skill_candidates()
        self.assertEqual(second[0]["name"], "systematic-debugging")
        self.assertEqual(skills_list.call_count, 2)

    def test_max_age_expiry_refreshes_cache(self):
        skills_list = mock.Mock(
            side_effect=[
                _payload("docker-management"),
                _payload("network-printer-operations"),
            ]
        )
        with self._patch_skills_list(skills_list):
            with mock.patch.object(automatic, "_DISCOVERY_CACHE_MAX_AGE_SECONDS", 0.01):
                first = discover_available_skill_candidates()
                time.sleep(0.02)
                second = discover_available_skill_candidates()
        self.assertEqual(first[0]["name"], "docker-management")
        self.assertEqual(second[0]["name"], "network-printer-operations")
        self.assertEqual(skills_list.call_count, 2)

    def test_discovery_failure_is_not_cached(self):
        skills_list = mock.Mock(
            side_effect=[RuntimeError("boom"), _payload("docker-management")]
        )
        with self._patch_skills_list(skills_list):
            first = discover_available_skill_candidates()
            second = discover_available_skill_candidates()
        self.assertEqual(first, ())
        self.assertEqual(second[0]["name"], "docker-management")
        self.assertEqual(skills_list.call_count, 2)

    def test_fingerprint_never_embeds_in_receipt_helpers(self):
        # Guard: discovery helpers return only hashes / candidates, never raw
        # absolute paths to callers that might serialize receipts.
        fp = automatic._discovery_fingerprint()
        self.assertRegex(fp, r"^[0-9a-f]{64}$")
        self.assertNotIn("/", fp)
        self.assertNotIn("home", fp)


if __name__ == "__main__":
    unittest.main()
