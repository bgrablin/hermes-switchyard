"""Receipt persistence must never break skill routing (issue #91).

On Windows, a receipt write error once escaped the pre_llm_call hook on
every turn, so no skill was recommended or loaded, and each failure left a
zero-byte ``.receipt-*.tmp`` file with an open handle. These tests inject
the same class of error on any platform. All fixtures are synthetic and
local; no test calls a hosted model.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from hermes_switchyard import receipt_state
from hermes_switchyard.automatic import build_pre_llm_call_hook, build_routing_receipt

_INJECTED = ModuleNotFoundError("No module named 'hermes_switchyard'")


def _skipped_receipt():
    return build_routing_receipt(
        {
            "status": "abstained",
            "selected": None,
            "source": "none",
            "abstention_reason": None,
            "hosted_skipped": "public_or_sanitized_data_ack_required",
            "hosted_attempted": False,
            "cache_hit": False,
            "local_score": 0.0,
        }
    )


def _open_descriptor_count() -> int | None:
    fd_dir = Path("/proc/self/fd")
    if not fd_dir.is_dir():
        return None
    return len(list(fd_dir.iterdir()))


class ReceiptWriteIsolationTests(unittest.TestCase):
    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.home = Path(self._directory.name)
        patcher = mock.patch.dict(os.environ, {"HERMES_HOME": str(self.home)}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._directory.cleanup)
        path = receipt_state._receipt_state_file()
        self.assertIsNotNone(path)
        # Fail loudly instead of touching a real Hermes home.
        self.assertTrue(str(path).startswith(str(self.home)), path)
        self.receipt_path = path
        self.data_dir = path.parent

    def _temporaries(self) -> list[Path]:
        if not self.data_dir.exists():
            return []
        return sorted(self.data_dir.glob(".receipt-*.tmp"))

    def test_non_oserror_during_write_is_contained_and_cleaned_up(self):
        before = _open_descriptor_count()
        with mock.patch.object(receipt_state, "_apply_private_permissions", side_effect=_INJECTED):
            with self.assertLogs("hermes_switchyard.receipt_state", level="WARNING") as captured:
                stored = receipt_state.store_latest_receipt(_skipped_receipt())
        self.assertFalse(stored)
        self.assertIn("ModuleNotFoundError", "\n".join(captured.output))
        self.assertEqual(self._temporaries(), [])
        self.assertFalse(self.receipt_path.exists())
        if before is not None:
            self.assertEqual(_open_descriptor_count(), before)

    def test_hook_still_loads_the_skill_when_receipt_write_raises(self):
        loaded = []

        def load_skill(name, task_id=None):
            loaded.append(name)
            return f"LOADED SKILL: {name}"

        hook = build_pre_llm_call_hook(
            configured_candidates=[
                {"name": "docker-management", "description": "Manage Docker containers and Compose services."},
                {"name": "network-printer-operations", "description": "Operate network printers and scanners."},
            ],
            routing_mode="local_only",
            consumer_mode="load",
            skill_loader=load_skill,
        )
        assert hook is not None
        with mock.patch.object(receipt_state, "_apply_private_permissions", side_effect=_INJECTED):
            with self.assertLogs("hermes_switchyard.receipt_state", level="WARNING"):
                result = hook(
                    user_message="Diagnose an exiting Docker Compose container",
                    session_id="session-91",
                    turn_id="turn-1",
                )

        self.assertEqual(loaded, ["docker-management"])
        self.assertEqual(result["context"], "LOADED SKILL: docker-management")
        self.assertEqual(result["metadata"]["skill_recommendation"]["status"], "loaded")
        self.assertEqual(self._temporaries(), [])

    def test_successful_write_sweeps_only_stale_temporaries(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        stale = self.data_dir / ".receipt-stale000.tmp"
        recent = self.data_dir / ".receipt-recent00.tmp"
        unrelated = self.data_dir / "notes.tmp"
        for path in (stale, recent, unrelated):
            path.write_bytes(b"")
        old = time.time() - 2 * receipt_state.STALE_TEMPORARY_SECONDS
        os.utime(stale, (old, old))
        os.utime(unrelated, (old, old))

        self.assertTrue(receipt_state.store_latest_receipt(_skipped_receipt()))

        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())
        self.assertTrue(unrelated.exists())
        self.assertTrue(self.receipt_path.exists())


if __name__ == "__main__":
    unittest.main()
