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
        environment["HERMES_SHARED_AUTH_DIR"] = str(self.hermes_home / "shared")
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


class SharedAuthIsolationTests(unittest.TestCase):
    def test_inherited_shared_auth_store_is_not_read_or_copied(self):
        with tempfile.TemporaryDirectory(prefix="switchyard-external-auth-fixture-") as directory:
            external = Path(directory) / "shared"
            external.mkdir()
            (external / "nous_auth.json").write_text(
                '{"access_token":"synthetic", "refresh_token":"synthetic"}\n',
                encoding="utf-8",
            )
            (external / "auth.json").write_text('{"fixture":"synthetic"}\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"HERMES_SHARED_AUTH_DIR": str(external)}):
                case = HermesHomeTestCase()
                try:
                    case.setUp()
                    shared = Path(os.environ["HERMES_SHARED_AUTH_DIR"]).resolve()
                    self.assertEqual(shared, case.hermes_home / "shared")
                    self.assertFalse((shared / "nous_auth.json").exists())
                    try:
                        from hermes_cli.auth import _auth_file_path, _load_auth_store, _read_shared_nous_state
                    except ImportError:
                        pass  # Standalone tests still enforce the shared-path isolation contract.
                    else:
                        read_text = Path.read_text

                        def reject_external_read(path, *args, **kwargs):
                            if path.is_relative_to(external):
                                raise AssertionError("test attempted to read an external auth store")
                            return read_text(path, *args, **kwargs)

                        with mock.patch.object(Path, "read_text", reject_external_read):
                            self.assertIsNone(_read_shared_nous_state())
                            self.assertEqual(_auth_file_path(), case.hermes_home / "auth.json")
                            self.assertEqual(_load_auth_store()["providers"], {})
                    self.assertFalse((case.hermes_home / "auth.json").exists())
                finally:
                    case.doCleanups()
