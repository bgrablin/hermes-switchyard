"""Issue #93 wiring tests: hook turn records, CLI history/stats, and identity.

Every test runs against an isolated temporary HERMES_HOME with synthetic
receipts and a fixture skill loader. No test contacts a provider, stores real
conversation text, or spawns git for the code under test.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import hermes_switchyard
from hermes_switchyard import receipt_history as rh
from hermes_switchyard import receipt_state
from hermes_switchyard.automatic import build_pre_llm_call_hook, build_routing_receipt

CANDIDATES = [{"name": "docker-management", "description": "Docker containers and Compose services"}]
SMOKE_TASK = "Diagnose an exiting Docker container"


def _fixture_skill_loader(name, task_id=None):
    return f"# {name}\nSynthetic fixture skill body."


def _local_result():
    return {
        "status": "selected",
        "selected": "docker-management",
        "source": "local",
        "hosted_attempted": False,
        "hosted_skipped": "local_confident",
        "cache_hit": False,
        "candidate_count": 2,
    }


class _IsolatedHome(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="rwiring-")
        home = Path(self._tmp.name) / "home"
        self._env = mock.patch.dict(os.environ, {"HERMES_HOME": str(home)})
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    @staticmethod
    def build_hook():
        hook = build_pre_llm_call_hook(
            configured_candidates=CANDIDATES,
            routing_mode="local_only",
            consumer_mode="load",
            skill_loader=_fixture_skill_loader,
        )
        assert hook is not None
        return hook


class HookTurnHistoryTests(_IsolatedHome):
    def test_each_turn_appends_one_record_with_turn_metadata(self):
        hook = self.build_hook()
        hook(user_message=SMOKE_TASK, session_id="s1", turn_id="t1", platform="Discord")
        records = rh.read_history()
        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertTrue(rh.validate_history_record(record))
        self.assertEqual(record["session_id"], "s1")
        self.assertEqual(record["turn_id"], "t1")
        self.assertEqual(record["platform"], "discord")
        self.assertEqual(record["receipt"]["terminal_state"], "local_selection")
        self.assertEqual(record["receipt"]["selected"], "docker-management")
        self.assertTrue(receipt_state.validate_receipt(record["receipt"]))
        # A replay of the same identified turn reuses the first result and
        # never adds a second record.
        hook(user_message=SMOKE_TASK, session_id="s1", turn_id="t1", platform="discord")
        self.assertEqual(len(rh.read_history()), 1)
        hook(user_message=SMOKE_TASK, session_id="s1", turn_id="t2", platform="discord")
        self.assertEqual([r["turn_id"] for r in rh.read_history()], ["t1", "t2"])

    def test_unrepresentable_metadata_is_stored_as_null(self):
        hook = self.build_hook()
        hook(
            user_message=SMOKE_TASK,
            session_id="s1",
            turn_id="t1",
            platform="PRIVATE_HISTORY_MARKER platform",
        )
        record = rh.read_history()[0]
        self.assertIsNone(record["platform"])
        self.assertEqual(record["session_id"], "s1")
        hook(user_message=SMOKE_TASK)
        records = rh.read_history()
        self.assertEqual(len(records), 2)
        self.assertIsNone(records[-1]["session_id"])
        self.assertIsNone(records[-1]["turn_id"])

    def test_explicit_override_turn_records_a_zero_request_skip(self):
        hook = self.build_hook()
        hook(user_message="Load docker-management now", session_id="s2", turn_id="t1", platform="cli")
        record = rh.read_history()[0]
        self.assertEqual(record["receipt"]["hosted_skip_reason"], "explicit_override")
        self.assertEqual(record["receipt"]["request_count"], 0)


class CliReceiptHistoryTests(_IsolatedHome):
    def seed(self):
        for session, turn in (("s1", "t1"), ("s2", "t1"), ("s1", "t2")):
            self.assertTrue(
                rh.record_turn_receipt(
                    build_routing_receipt(_local_result()),
                    session_id=session,
                    turn_id=turn,
                    platform="cli",
                )
            )

    @staticmethod
    def run_command(*argv):
        parser = argparse.ArgumentParser()
        hermes_switchyard._setup_cli(parser)
        args = parser.parse_args(list(argv))
        with mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = hermes_switchyard._cli_handler(args)
        return code, stdout.getvalue()

    def test_receipt_latest_semantics_are_unchanged_without_flags(self):
        code, output = self.run_command("receipt", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), {"status": "unavailable", "reason": "no_receipt"})
        self.assertTrue(receipt_state.store_latest_receipt(build_routing_receipt(_local_result())))
        code, output = self.run_command("receipt", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["terminal_state"], "local_selection")

    def test_receipt_session_and_last_read_the_history(self):
        self.seed()
        code, output = self.run_command("receipt", "--session", "s1", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(r["session_id"], r["turn_id"]) for r in json.loads(output)],
            [("s1", "t1"), ("s1", "t2")],
        )
        code, output = self.run_command("receipt", "--last", "2", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(
            [(r["session_id"], r["turn_id"]) for r in json.loads(output)],
            [("s2", "t1"), ("s1", "t2")],
        )
        code, output = self.run_command("receipt", "--session", "s1", "--last", "1", "--json")
        self.assertEqual(code, 0)
        self.assertEqual([(r["session_id"], r["turn_id"]) for r in json.loads(output)], [("s1", "t2")])

    def test_receipt_empty_lookups_are_structured_nonzero(self):
        code, output = self.run_command("receipt", "--last", "5", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["reason"], "no_receipt_history")
        self.seed()
        code, output = self.run_command("receipt", "--session", "missing", "--json")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["reason"], "no_matching_receipts")

    def test_receipt_rejects_invalid_filters(self):
        self.assertEqual(self.run_command("receipt", "--last", "0")[0], 2)
        self.assertEqual(self.run_command("receipt", "--session", "bad id")[0], 2)

    def test_stats_summarizes_the_history_and_window(self):
        self.seed()
        code, output = self.run_command("stats", "--since", "24h", "--json")
        self.assertEqual(code, 0)
        stats = json.loads(output)
        self.assertEqual(stats["turns"], 3)
        self.assertEqual(stats["selections"], 3)
        self.assertEqual(stats["sessions"], 2)
        self.assertEqual(stats["window_seconds"], 86400)
        code, output = self.run_command("stats", "--json")
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(output)["window_seconds"])

    def test_stats_on_empty_history_reports_zero_turns(self):
        code, output = self.run_command("stats", "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["turns"], 0)

    def test_stats_rejects_an_unparseable_window(self):
        self.assertEqual(self.run_command("stats", "--since", "yesterday")[0], 2)

    def test_parser_accepts_the_history_flags(self):
        parser = argparse.ArgumentParser()
        hermes_switchyard._setup_cli(parser)
        args = parser.parse_args(["receipt", "--session", "s1", "--last", "5", "--json"])
        self.assertEqual(args.switchyard_command, "receipt")
        self.assertEqual((args.session, args.last, args.json_output), ("s1", 5, True))
        self.assertIsNone(parser.parse_args(["receipt"]).session)
        args = parser.parse_args(["stats", "--since", "24h"])
        self.assertEqual((args.switchyard_command, args.since), ("stats", "24h"))


class PluginIdentityTests(_IsolatedHome):
    def test_receipt_identity_carries_an_exact_source_sha(self):
        receipt = build_routing_receipt(_local_result())
        self.assertEqual(receipt["source_sha"], rh.process_source_sha())
        self.assertEqual(receipt["plugin_identity"]["source_sha"], receipt["source_sha"])
        self.assertTrue(
            receipt["source_sha"] == receipt_state.RECEIPT_SOURCE_SHA_UNAVAILABLE
            or receipt_state.SOURCE_SHA_RE.fullmatch(receipt["source_sha"]) is not None
        )
        self.assertTrue(receipt_state.validate_receipt(receipt))


if __name__ == "__main__":
    unittest.main()
