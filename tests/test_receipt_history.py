"""Receipt history, routing stats, and git source SHA tests (issue #93).

All tests use an isolated temporary data directory and synthetic receipts.
No test contacts a provider or spawns git for the code under test.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

from hermes_switchyard import receipt_history as rh
from hermes_switchyard import receipt_state

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
NOW = datetime(2026, 9, 24, 12, 0, 0, tzinfo=timezone.utc)


def _receipt(**overrides):
    identity = {"plugin": receipt_state.PLUGIN_NAME, "version": "0.5.3", "source_sha": "unavailable"}
    receipt = {
        "terminal_state": "hosted_skipped",
        "source": "none",
        "selected": None,
        "hosted_attempted": False,
        "hosted_succeeded": False,
        "hosted_error": None,
        "hosted_skip_reason": "local_confident",
        "abstention_reason": None,
        "jev_model": None,
        "request_id": None,
        "request_count": 0,
        "latency_ms": 0.0,
        "total_latency_ms": 0.0,
        "total_usage": {},
        "candidate_count": 3,
        "offered_count": 0,
        "excluded_count": 0,
        "shortlist_policy": None,
        "verified": False,
        "advisory_only": True,
        "plugin_identity": identity,
        "source_sha": "unavailable",
    }
    receipt.update(overrides)
    return receipt


def _local_selection(latency=5.0):
    return _receipt(
        terminal_state="local_selection", source="local", selected="docker-management",
        hosted_skip_reason=None, latency_ms=latency, total_latency_ms=latency,
    )


def _hosted_selection(latency=800.0, cost=0.002):
    return _receipt(
        terminal_state="hosted_selection", source="jev", selected="network-printer-operations",
        hosted_attempted=True, hosted_succeeded=True, hosted_skip_reason=None,
        jev_model="jev-small", request_id="req-1", request_count=1,
        latency_ms=latency, total_latency_ms=latency, total_usage={"cost": cost, "total_tokens": 100.0},
    )


def _hosted_failure(code="deadline_exceeded", latency=3000.0):
    return _receipt(
        terminal_state="hosted_failure", source="none", hosted_attempted=True,
        hosted_error=code, hosted_skip_reason=None, request_count=1,
        latency_ms=latency, total_latency_ms=latency, total_usage={"cost": None},
    )


def _hosted_abstention():
    return _receipt(
        terminal_state="hosted_abstention", source="none", hosted_attempted=True,
        hosted_succeeded=True, hosted_skip_reason=None, abstention_reason="no_confident_match",
        request_count=1, latency_ms=400.0, total_latency_ms=400.0, total_usage={"cost": 0.001},
    )


class _Isolated(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rh-test-")
        self.data = Path(self._tmp.name) / "plugin-data"
        home = Path(self._tmp.name) / "home"
        self._env = mock.patch.dict(os.environ, {"HERMES_HOME": str(home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    @property
    def path(self) -> Path:
        path = rh.history_path(self.data)
        assert path is not None
        return path

    def append(self, receipt, **kwargs):
        kwargs.setdefault("now", NOW)
        return rh.append_receipt_history(receipt, data_dir=self.data, **kwargs)


class SanitizerTests(unittest.TestCase):
    def test_identifiers(self):
        self.assertEqual(rh.sanitize_session_id("20260924_034047_ae311e"), "20260924_034047_ae311e")
        for bad in ("", "has space", "a" * 200, "../../etc", None, 5, "x\ny", "caf\u00e9"):
            self.assertIsNone(rh.sanitize_session_id(bad), bad)
        self.assertIsNone(rh.sanitize_turn_id("turn with text"))

    def test_platform(self):
        self.assertEqual(rh.sanitize_platform(" Discord "), "discord")
        self.assertEqual(rh.sanitize_platform("api_server"), "api_server")
        for bad in ("", "9cli", "a" * 40, "tele gram", None, 1):
            self.assertIsNone(rh.sanitize_platform(bad))

    def test_timestamps_and_since(self):
        self.assertEqual(rh.format_timestamp(NOW), "2026-09-24T12:00:00Z")
        cdt = NOW.astimezone(timezone(timedelta(hours=-5)))
        self.assertEqual(rh.format_timestamp(cdt), "2026-09-24T12:00:00Z")
        self.assertEqual(rh.parse_timestamp("2026-09-24T12:00:00Z"), NOW)
        self.assertIsNone(rh.parse_timestamp("2026-13-40T99:00:00Z"))
        self.assertIsNone(rh.parse_timestamp("2026-09-24 12:00:00"))
        self.assertEqual(rh.parse_since("24h"), timedelta(hours=24))
        self.assertEqual(rh.parse_since("7d"), timedelta(days=7))
        self.assertEqual(rh.parse_since("30m"), timedelta(minutes=30))
        for bad in ("0h", "h", "24", "-1d", "1y", None, "9999999w"):
            self.assertIsNone(rh.parse_since(bad), bad)


class HistoryStorageTests(_Isolated):
    def test_append_and_session_lookup(self):
        self.assertTrue(self.append(_local_selection(), session_id="s1", turn_id="t1", platform="cli"))
        self.assertTrue(self.append(_hosted_selection(), session_id="s2", turn_id="t1", platform="discord"))
        self.assertTrue(self.append(_receipt(), session_id="s1", turn_id="t2", platform="cli"))
        s1 = rh.session_receipts("s1", data_dir=self.data)
        self.assertEqual([r["turn_id"] for r in s1], ["t1", "t2"])
        self.assertTrue(all(r["session_id"] == "s1" and r["platform"] == "cli" for r in s1))
        self.assertEqual(s1[0]["recorded_at"], "2026-09-24T12:00:00Z")
        self.assertEqual(len(rh.session_receipts("missing", data_dir=self.data)), 0)
        self.assertEqual(rh.session_receipts("bad id", data_dir=self.data), [])
        last = rh.last_receipts(2, data_dir=self.data)
        self.assertEqual([(r["session_id"], r["turn_id"]) for r in last], [("s2", "t1"), ("s1", "t2")])
        self.assertEqual(rh.last_receipts(0, data_dir=self.data), [])
        self.assertEqual(len(rh.read_history(data_dir=self.data, platform="discord")), 1)

    def test_invalid_receipt_and_metadata_are_not_stored_raw(self):
        self.assertFalse(self.append({"terminal_state": "local_selection"}))
        self.assertFalse(self.append(_receipt(task="SYNTHETIC_TASK_MARKER")))
        self.assertFalse(self.append(_receipt(abstention_reason="Provider said: SECRET text")))
        self.assertTrue(self.append(
            _receipt(), session_id="SYNTHETIC_TASK_MARKER please help", turn_id="t 1",
            platform="PRIVATE_HISTORY_MARKER platform",
        ))
        raw = self.path.read_text(encoding="ascii")
        for marker in ("SYNTHETIC_TASK_MARKER", "PRIVATE_HISTORY_MARKER", "SECRET", "please help"):
            self.assertNotIn(marker, raw)
        record = json.loads(raw.splitlines()[0])
        self.assertEqual(set(record), rh.HISTORY_RECORD_FIELDS)
        self.assertIsNone(record["session_id"])
        self.assertIsNone(record["turn_id"])
        self.assertIsNone(record["platform"])
        self.assertTrue(rh.validate_history_record(record))

    def test_same_turn_rewrite_replaces_prior_record(self):
        self.append(_receipt(), session_id="s1", turn_id="t1")
        self.append(_local_selection(), session_id="s1", turn_id="t1")
        self.append(_receipt(), turn_id="t1")
        self.append(_receipt(), turn_id="t1")
        records = rh.read_history(data_dir=self.data)
        self.assertEqual(len(records), 3)
        self.assertEqual(records[0]["receipt"]["terminal_state"], "local_selection")

    def test_bounded_by_record_count(self):
        for index in range(12):
            self.append(_receipt(), session_id="s", turn_id=f"t{index}", max_records=5)
        records = rh.read_history(data_dir=self.data)
        self.assertEqual([r["turn_id"] for r in records], [f"t{i}" for i in range(7, 12)])

    def test_bounded_by_bytes(self):
        sample = rh.build_history_record(_receipt(), session_id="s", turn_id="t00", now=NOW)
        assert sample is not None
        one = len(rh._encode(sample)) + 1
        limit = one * 3 + one // 2
        for index in range(10):
            self.append(_receipt(), session_id="s", turn_id=f"t{index:02d}", max_bytes=limit)
        path = self.path
        self.assertLessEqual(path.stat().st_size, limit)
        self.assertEqual([r["turn_id"] for r in rh.read_history(data_dir=self.data)], ["t07", "t08", "t09"])
        self.assertFalse(self.append(_receipt(), max_bytes=10))

    def test_corrupt_lines_are_skipped_and_dropped_on_rewrite(self):
        self.append(_receipt(), session_id="s", turn_id="t1")
        path = self.path
        with open(path, "a", encoding="ascii") as handle:
            handle.write("not json\n")
            handle.write(json.dumps({"schema": 1, "recorded_at": "x"}) + "\n")
            tampered = rh.build_history_record(_receipt(), session_id="s", turn_id="t9", now=NOW)
            assert tampered is not None
            tampered["receipt"]["extra"] = "SYNTHETIC_TASK_MARKER"
            handle.write(json.dumps(tampered) + "\n")
            handle.write('{"partial":')
        self.assertEqual(len(rh.read_history(data_dir=self.data)), 1)
        self.append(_receipt(), session_id="s", turn_id="t2")
        text = path.read_text(encoding="ascii")
        self.assertNotIn("SYNTHETIC_TASK_MARKER", text)
        self.assertEqual(len(text.splitlines()), 2)

    def test_deeply_nested_invalid_line_does_not_block_reads_or_repair(self):
        self.assertTrue(self.append(_receipt(), session_id="s", turn_id="t1"))
        nested = "[" * 1200 + "0" + "]" * 1200
        with open(self.path, "a", encoding="ascii") as handle:
            handle.write(nested + "\n")
        self.assertEqual([r["turn_id"] for r in rh.read_history(data_dir=self.data)], ["t1"])
        self.assertTrue(self.append(_receipt(), session_id="s", turn_id="t2"))
        self.assertNotIn(nested, self.path.read_text(encoding="ascii"))
        self.assertEqual([r["turn_id"] for r in rh.read_history(data_dir=self.data)], ["t1", "t2"])

    def test_ordinary_appends_do_not_rewrite_or_fsync_the_retained_tail(self):
        self.assertTrue(self.append(_receipt(), session_id="s", turn_id="t0", max_records=100))
        with mock.patch.object(rh, "_write_lines", side_effect=AssertionError("unexpected compaction")), mock.patch.object(
            rh.os, "fsync", side_effect=AssertionError("unexpected per-turn fsync")
        ):
            for index in range(1, 8):
                self.assertTrue(self.append(_receipt(), session_id="s", turn_id=f"t{index}", max_records=100))
        self.assertEqual(len(rh.read_history(data_dir=self.data)), 8)

    @unittest.skipIf(os.name == "nt", "symlink privileges vary on Windows")
    def test_append_rejects_a_symlinked_history_path(self):
        self.assertTrue(self.append(_receipt(), session_id="s", turn_id="t0"))
        outside = Path(self._tmp.name) / "outside.jsonl"
        original = self.path.read_bytes()
        outside.write_bytes(original)
        self.path.unlink()
        self.path.symlink_to(outside)
        self.assertFalse(self.append(_receipt(), session_id="s", turn_id="t1"))
        self.assertEqual(rh.read_history(data_dir=self.data), [])
        self.assertEqual(outside.read_bytes(), original)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_private_permissions_and_no_temp_leftovers(self):
        self.append(_receipt(), session_id="s", turn_id="t")
        path = self.path
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        leftovers = [p.name for p in self.data.iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_storage_failure_returns_false(self):
        with mock.patch.object(rh.os, "replace", side_effect=OSError("disk")):
            self.assertFalse(self.append(_receipt(), session_id="s", turn_id="t"))
        self.assertEqual([p for p in self.data.iterdir() if p.suffix == ".tmp"], [])
        with mock.patch.object(rh, "history_path", return_value=None):
            self.assertFalse(self.append(_receipt()))
        self.assertEqual(rh.read_history(data_dir=self.data / "absent"), [])

    def test_lock_timeout_fails_closed(self):
        with mock.patch.object(rh._HistoryLock, "__enter__", return_value=False):
            self.assertFalse(self.append(_receipt()))

    def test_concurrent_appends_keep_every_record(self):
        errors = []

        def worker(offset):
            for index in range(10):
                if not self.append(_receipt(), session_id=f"w{offset}", turn_id=f"t{index}"):
                    errors.append((offset, index))

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        records = rh.read_history(data_dir=self.data)
        # A bounded lock wait may reject a write, but never corrupts the file.
        self.assertEqual(len(records), 40 - len(errors))
        self.assertLessEqual(len(errors), 4)

    def test_default_path_uses_profile_plugin_data(self):
        path = rh.history_path()
        assert path is not None
        self.assertEqual(path.name, rh.HISTORY_FILE_NAME)
        self.assertEqual(path.parent, receipt_state._plugin_data_dir())
        repo_root = Path(rh.__file__).resolve().parent.parent
        self.assertFalse(str(path).startswith(str(repo_root)))
        self.assertTrue(rh.record_turn_receipt(_receipt(), session_id="s", turn_id="t", platform="cli"))
        self.assertEqual(len(rh.read_history()), 1)

    def test_since_filter(self):
        self.append(_receipt(), session_id="s", turn_id="old", now=NOW - timedelta(hours=30))
        self.append(_receipt(), session_id="s", turn_id="new", now=NOW - timedelta(hours=1))
        records = rh.read_history(data_dir=self.data, since=timedelta(hours=24), now=NOW)
        self.assertEqual([r["turn_id"] for r in records], ["new"])


class StatsTests(_Isolated):
    def test_empty(self):
        stats = rh.compute_stats([])
        self.assertEqual(stats["turns"], 0)
        self.assertIsNone(stats["selection_rate"])
        self.assertIsNone(stats["latency_ms_p50"])
        self.assertEqual(stats["cost_total_known"], 0.0)

    def test_routing_stats(self):
        loaded = dict(
            _local_selection(), consumer_status="loaded", loaded_skill="docker-management",
            loaded_source="local", skill_load_verified=True, advisory_only=False,
            delivery_status="delivered", adoption_status="adopted", outcome_status="unverified",
        )
        failed_load = dict(
            _hosted_selection(), consumer_status="load_failed", loaded_skill=None,
            loaded_source=None, skill_load_verified=False,
        )
        entries = [
            (loaded, "cli"),
            (failed_load, "discord"),
            (_hosted_failure("deadline_exceeded"), "discord"),
            (_hosted_failure("transport_or_execution_failure"), "cli"),
            (_hosted_abstention(), "cli"),
            (_receipt(), "cli"),
        ]
        for index, (receipt, platform) in enumerate(entries):
            self.assertTrue(self.append(receipt, session_id=f"s{index % 2}", turn_id=f"t{index}", platform=platform))
        stats = rh.routing_stats(data_dir=self.data, since=timedelta(hours=24), now=NOW)
        self.assertEqual(stats["turns"], 6)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["window_seconds"], 86400)
        self.assertEqual(stats["platforms"], {"cli": 4, "discord": 2})
        self.assertEqual(stats["selections"], 2)
        self.assertEqual(stats["selection_rate"], round(2 / 6, 4))
        self.assertEqual(stats["no_selections"], 4)
        self.assertEqual(stats["abstentions"], 1)
        self.assertEqual(stats["abstention_rate"], round(1 / 6, 4))
        self.assertEqual(stats["abstention_reasons"], {"no_confident_match": 1})
        self.assertEqual(stats["hosted_attempts"], 4)
        self.assertEqual(stats["hosted_failures"], 2)
        self.assertEqual(stats["hosted_failure_rate"], 0.5)
        self.assertEqual(
            stats["hosted_failures_by_code"],
            {"deadline_exceeded": 1, "transport_or_execution_failure": 1},
        )
        self.assertEqual(stats["hosted_skip_reasons"], {"local_confident": 1})
        self.assertEqual(stats["consumer_statuses"], {"load_failed": 1, "loaded": 1})
        self.assertEqual(stats["skill_loads"], 1)
        self.assertEqual(stats["skill_load_rate"], 0.5)
        self.assertEqual(stats["requests"], 4)
        self.assertAlmostEqual(stats["requests_per_turn"], round(4 / 6, 4))
        # latencies: 5, 800, 3000, 3000, 400, 0 -> sorted 0,5,400,800,3000,3000
        self.assertEqual(stats["latency_ms_p50"], 400.0)
        self.assertEqual(stats["latency_ms_p95"], 3000.0)
        self.assertEqual(stats["hosted_latency_ms_p50"], 800.0)
        # Known cost: loaded(0, no request), hosted 0.002, abstention 0.001, skipped 0.
        # Two failures report cost None and stay unknown, never zero.
        self.assertAlmostEqual(stats["cost_total_known"], 0.003)
        self.assertEqual(stats["cost_known_turns"], 4)
        self.assertEqual(stats["cost_unknown_turns"], 2)
        self.assertAlmostEqual(stats["cost_per_turn_known"], 0.00075)
        json.dumps(stats, allow_nan=False)

    def test_failure_stats_prefer_closed_set_subcode_with_legacy_fallback(self):
        detailed = _hosted_failure("transport_or_execution_failure")
        detailed["hosted_error_detail"] = "http_429"
        self.assertTrue(self.append(detailed, session_id="s", turn_id="t1"))
        self.assertTrue(self.append(_hosted_failure("deadline_exceeded"), session_id="s", turn_id="t2"))
        stats = rh.routing_stats(data_dir=self.data)
        self.assertEqual(stats["hosted_failures_by_code"], {"http_429": 1, "deadline_exceeded": 1})

    def test_stats_session_filter(self):
        self.append(_hosted_selection(), session_id="a", turn_id="1")
        self.append(_receipt(), session_id="b", turn_id="1")
        stats = rh.routing_stats(data_dir=self.data, session_id="a")
        self.assertEqual(stats["turns"], 1)
        self.assertEqual(stats["selection_rate"], 1.0)
        self.assertIsNone(stats["window_seconds"])


class GitSourceShaTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rh-git-")
        self.root = Path(self._tmp.name) / "repo"
        self.git = self.root / ".git"
        (self.git / "refs" / "heads").mkdir(parents=True)

    def tearDown(self):
        self._tmp.cleanup()

    def test_detached_head(self):
        (self.git / "HEAD").write_text(SHA_A + "\n")
        self.assertEqual(rh.resolve_git_source_sha(self.root), SHA_A)

    def test_loose_ref(self):
        (self.git / "HEAD").write_text("ref: refs/heads/main\n")
        (self.git / "refs" / "heads" / "main").write_text(SHA_B + "\n")
        self.assertEqual(rh.resolve_git_source_sha(self.root), SHA_B)

    def test_packed_ref(self):
        (self.git / "HEAD").write_text("ref: refs/heads/main\n")
        (self.git / "packed-refs").write_text(
            "# pack-refs with: peeled fully-peeled sorted \n"
            f"{SHA_A} refs/heads/other\n{SHA_C} refs/heads/main\n^{SHA_B}\n"
        )
        self.assertEqual(rh.resolve_git_source_sha(self.root), SHA_C)

    def test_linked_worktree(self):
        common = Path(self._tmp.name) / "main.git"
        (common / "refs" / "heads").mkdir(parents=True)
        wt_git = common / "worktrees" / "93"
        wt_git.mkdir(parents=True)
        (wt_git / "commondir").write_text("../..\n")
        (wt_git / "HEAD").write_text("ref: refs/heads/feature\n")
        (common / "refs" / "heads" / "feature").write_text(SHA_B + "\n")
        worktree = Path(self._tmp.name) / "wt"
        worktree.mkdir()
        (worktree / ".git").write_text(f"gitdir: {wt_git}\n")
        self.assertEqual(rh.resolve_git_source_sha(worktree), SHA_B)
        (common / "refs" / "heads" / "feature").unlink()
        (common / "packed-refs").write_text(f"{SHA_C} refs/heads/feature\n")
        self.assertEqual(rh.resolve_git_source_sha(worktree), SHA_C)
        (wt_git / "HEAD").write_text(SHA_A + "\n")
        self.assertEqual(rh.resolve_git_source_sha(worktree), SHA_A)

    def test_relative_gitdir(self):
        real = self.root / "store"
        real.mkdir()
        (real / "HEAD").write_text(SHA_C + "\n")
        shutil.rmtree(self.git)
        (self.root / ".git").write_text("gitdir: store\n")
        self.assertEqual(rh.resolve_git_source_sha(self.root), SHA_C)

    def test_unavailable_cases(self):
        unavailable = receipt_state.RECEIPT_SOURCE_SHA_UNAVAILABLE
        self.assertEqual(rh.resolve_git_source_sha(Path(self._tmp.name) / "none"), unavailable)
        for head in (
            "", "garbage\n", "ref: refs/heads/missing\n", "ref: refs/../../etc/passwd\n",
            "ref: HEAD\n", "A" * 40 + "\n", "a" * 64 + "\n", "x" * 5000,
        ):
            (self.git / "HEAD").write_text(head)
            self.assertEqual(rh.resolve_git_source_sha(self.root), unavailable, head[:40])
        (self.git / "HEAD").write_text("ref: refs/heads/main\n")
        (self.git / "refs" / "heads" / "main").write_text("not-a-sha\n")
        self.assertEqual(rh.resolve_git_source_sha(self.root), unavailable)

    def test_no_subprocess(self):
        (self.git / "HEAD").write_text("ref: refs/heads/main\n")
        (self.git / "refs" / "heads" / "main").write_text(SHA_A + "\n")
        with mock.patch.object(subprocess, "Popen", side_effect=AssertionError("subprocess")), \
                mock.patch.object(os, "system", side_effect=AssertionError("system")):
            self.assertEqual(rh.resolve_git_source_sha(self.root), SHA_A)

    def test_manifest_takes_precedence(self):
        (self.git / "HEAD").write_text(SHA_A + "\n")
        self.assertEqual(rh.resolve_receipt_source_sha(self.root), SHA_A)
        with mock.patch.object(receipt_state, "resolve_source_sha", return_value=SHA_B):
            self.assertEqual(rh.resolve_receipt_source_sha(self.root), SHA_B)

    def test_plugin_identity_prefers_manifest_then_git(self):
        (self.git / "HEAD").write_text("ref: refs/heads/main\n")
        (self.git / "refs" / "heads" / "main").write_text(SHA_A + "\n")
        self.assertEqual(receipt_state.plugin_identity(self.root)["source_sha"], SHA_A)
        (self.root / "SOURCE-MANIFEST.json").write_text(
            json.dumps(
                {
                    "files": [],
                    "format": 1,
                    "manifest_version": 1,
                    "plugin": "hermes-switchyard",
                    "source_sha": SHA_B,
                    "version": "0.5.3",
                }
            ),
            encoding="utf-8",
        )
        self.assertEqual(receipt_state.plugin_identity(self.root)["source_sha"], SHA_B)

    def test_matches_real_checkout(self):
        repo = Path(rh.__file__).resolve().parent.parent
        if not (repo / ".git").exists() or shutil.which("git") is None:
            self.skipTest("not a git checkout")
        expected = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=False
        ).stdout.strip()
        if not receipt_state.SOURCE_SHA_RE.fullmatch(expected):
            self.skipTest("git rev-parse unavailable")
        self.assertEqual(rh.resolve_git_source_sha(repo), expected)


if __name__ == "__main__":
    unittest.main()
