"""Shared isolation boundary for tests that may touch Hermes home state."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermes_switchyard as switchyard
from hermes_switchyard import receipt_state


class HermesHomeTestCase(unittest.TestCase):
    """Give each test an owned home and refuse receipt writes to a real home."""

    def setUp(self):
        super().setUp()
        owned = tempfile.TemporaryDirectory(prefix="switchyard-test-home-")
        self.addCleanup(owned.cleanup)
        self.hermes_home = Path(owned.name).resolve()
        environment = dict(os.environ)
        environment.pop("HERMES_PROFILE", None)
        environment["HERMES_HOME"] = str(self.hermes_home)
        patch_env = mock.patch.dict(os.environ, environment, clear=True)
        patch_env.start()
        self.addCleanup(patch_env.stop)

        original_data_dir = receipt_state._plugin_data_dir
        scratch_root = Path(tempfile.gettempdir()).resolve()

        def guarded_data_dir():
            configured = os.environ.get("HERMES_HOME")
            if not configured:
                raise AssertionError("a test tried to access plugin data without HERMES_HOME")
            home = Path(configured).resolve()
            if not home.is_relative_to(scratch_root):
                raise AssertionError("a test tried to access plugin data outside the temp root")
            result = original_data_dir()
            if result is not None and not result.resolve().is_relative_to(home):
                raise AssertionError("plugin data escaped the test-owned Hermes home")
            return result

        patch_data_dir = mock.patch.object(receipt_state, "_plugin_data_dir", side_effect=guarded_data_dir)
        patch_data_dir.start()
        self.addCleanup(patch_data_dir.stop)
        self._real_skill_loader = switchyard._load_skill_context
        loader_guard = mock.patch.object(
            switchyard,
            "_load_skill_context",
            side_effect=AssertionError("test must supply a synthetic skill loader"),
        )
        loader_guard.start()
        self.addCleanup(loader_guard.stop)
        self.assertNotIn("HERMES_PROFILE", os.environ)
