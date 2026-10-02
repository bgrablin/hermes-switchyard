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
import hermes_switchyard.receipt_output as receipt_output
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
        code, output = self.run_command("receipt")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["terminal_state"], "local_selection")

    def test_human_receipt_shows_selection_and_verified_load_without_task_text(self):
        hook = self.build_hook()
        hook(user_message=SMOKE_TASK, session_id="s1", turn_id="t1", platform="cli")
        code, output = self.run_command("receipt", "--human")
        self.assertEqual(code, 0)
        self.assertIn("Selected skill: docker-management", output)
        self.assertIn("Decision: Selected locally", output)
        self.assertIn("Skill load: Loaded docker-management via local (verified)", output)
        self.assertIn("Task outcome: Unverified", output)
        self.assertNotIn(SMOKE_TASK, output)

    def test_human_history_shows_record_context(self):
        self.seed()
        code, output = self.run_command("receipt", "--session", "s1", "--last", "1", "--human")
        self.assertEqual(code, 0)
        self.assertIn("Recorded:", output)
        self.assertIn("Session: s1", output)
        self.assertIn("Turn: t2", output)
        self.assertIn("Selected skill: docker-management", output)

    def test_human_empty_receipt_uses_readable_message(self):
        code, output = self.run_command("receipt", "--human")
        self.assertEqual(code, 1)
        self.assertIn("No automatic skill-routing receipt is available.", output)
        with self.assertRaises(json.JSONDecodeError):
            json.loads(output)

    def test_human_terminal_labels_cover_all_valid_receipt_states(self):
        cases = (
            ("Selected locally", {}),
            ("Selected by Jev", {
                "source": "jev", "hosted_attempted": True, "hosted_skipped": None,
            }),
            ("No skill recommended", {
                "source": "none", "selected": None, "hosted_attempted": True,
                "hosted_skipped": None, "abstention_reason": "no_confident_match",
            }),
            ("Jev decision failed", {
                "source": "none", "selected": None, "hosted_attempted": True,
                "hosted_skipped": None, "hosted_error": "deadline_exceeded",
            }),
            ("Used local fallback after Jev failed", {
                "hosted_attempted": True, "hosted_skipped": None,
                "hosted_error": "deadline_exceeded",
            }),
            ("Skipped hosted routing", {
                "source": "none", "selected": None, "hosted_skipped": "routing_mode_off",
            }),
            ("Reused cached selection", {"cache_hit": True}),
        )
        for expected, updates in cases:
            with self.subTest(expected=expected):
                result = {**_local_result(), **updates}
                receipt = build_routing_receipt(result)
                self.assertTrue(receipt_state.validate_receipt(receipt), receipt)
                self.assertIn(expected, receipt_output.format_routing_receipt(receipt))

    def test_human_load_labels_cover_consumer_and_advisory_statuses(self):
        cases = (
            (None, "Skill load: Not loaded (advisory only)", None, None, None, None),
            (
                "loaded",
                "Skill load: Loaded docker-management via local (verified)",
                "docker-management",
                "local",
                True,
                ("delivered", "adopted"),
            ),
            (
                "load_failed",
                "Skill load: Failed (not verified)",
                None,
                None,
                False,
                ("delivered", "not_adopted"),
            ),
            (
                "explicit_override",
                "Skill load: Skipped (explicit override)",
                None,
                None,
                False,
                ("skipped", "suppressed"),
            ),
            (
                "mandatory_conflict",
                "Skill load: Skipped (mandatory skill conflict)",
                None,
                None,
                False,
                ("skipped", "suppressed"),
            ),
        )
        for status, expected, skill, source, verified, contract in cases:
            with self.subTest(status=status):
                receipt = build_routing_receipt(_local_result())
                if status is None:
                    self.assertIsNone(contract)
                else:
                    if contract is None:
                        raise AssertionError("consumer status requires a delivery/adoption contract")
                    delivery, adoption = contract
                    receipt.update({
                        "consumer_status": status,
                        "loaded_skill": skill,
                        "loaded_source": source,
                        "skill_load_verified": verified,
                        "delivery_status": delivery,
                        "adoption_status": adoption,
                        "outcome_status": "unverified",
                    })
                canonical = receipt_state.canonicalize_receipt(receipt)
                self.assertIsNotNone(canonical)
                self.assertIn(expected, receipt_output.format_routing_receipt(canonical))

    def test_human_hosted_summary_includes_bounded_model_latency_and_error(self):
        result = {
            **_local_result(),
            "source": "jev",
            "hosted_attempted": True,
            "hosted_skipped": None,
            "model": "typesafe/jev-1.13",
            "request_count": 2,
            "total_latency_ms": 123.4,
        }
        receipt = build_routing_receipt(result)
        self.assertIn(
            "Jev: 2 requests in 123.4 ms (typesafe/jev-1.13)",
            receipt_output.format_routing_receipt(receipt),
        )

        failed = build_routing_receipt({
            **_local_result(),
            "source": "none",
            "selected": None,
            "hosted_attempted": True,
            "hosted_skipped": None,
            "hosted_error": "deadline_exceeded",
            "request_count": 1,
            "total_latency_ms": 87.5,
        })
        rendered = receipt_output.format_routing_receipt(failed)
        self.assertIn("Jev: 1 request in 87.5 ms; error: deadline exceeded", rendered)
        self.assertIn("Reason: deadline exceeded", rendered)

    def test_human_empty_history_and_no_match_use_plain_language(self):
        code, output = self.run_command("receipt", "--last", "3", "--human")
        self.assertEqual(code, 1)
        self.assertIn("No routing receipt history is available.", output)
        self.seed()
        code, output = self.run_command("receipt", "--session", "missing", "--human")
        self.assertEqual(code, 1)
        self.assertIn("No routing receipts match these filters.", output)

    def test_human_output_rejects_untrusted_receipt_text(self):
        canary = "SYNTHETIC_UNTRUSTED_TEXT_CANARY"
        receipt = build_routing_receipt(_local_result())
        receipt["task_text"] = canary
        self.assertIsNone(receipt_state.canonicalize_receipt(receipt))
        self.assertFalse(receipt_state.store_latest_receipt(receipt))
        unsafe_identifier = build_routing_receipt(_local_result())
        unsafe_identifier["selected"] = f"docker-management\n{canary}"
        self.assertIsNone(receipt_state.canonicalize_receipt(unsafe_identifier))
        with self.assertRaises(ValueError) as error:
            receipt_output.format_routing_receipt(receipt)
        self.assertNotIn(canary, str(error.exception))
        code, output = self.run_command("receipt", "--human")
        self.assertEqual(code, 1)
        self.assertNotIn(canary, output)

    def test_human_readback_rejects_malformed_persisted_receipts(self):
        canary = "PERSISTED_UNTRUSTED_TEXT_CANARY"
        state = receipt_state._receipt_state_file()
        self.assertIsNotNone(state)
        assert state is not None
        state.parent.mkdir(parents=True, exist_ok=True)
        malformed_receipts = (
            ("task_text", {"task_text": canary}),
            ("unsafe_identifier", {"selected": f"docker-management\n{canary}"}),
        )
        for label, malformed_fields in malformed_receipts:
            with self.subTest(label=label):
                receipt = build_routing_receipt(_local_result())
                receipt.update(malformed_fields)
                state.write_text(json.dumps(receipt), encoding="utf-8")
                self.assertIsNone(receipt_state.read_latest_receipt())
                code, output = self.run_command("receipt", "--human")
                self.assertEqual(code, 1)
                self.assertNotIn(canary, output)

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
        self.assertEqual((args.session, args.last, args.json_output, args.human_output), ("s1", 5, True, False))
        args = parser.parse_args(["receipt", "--human"])
        self.assertTrue(args.human_output)
        self.assertFalse(args.json_output)
        self.assertIsNone(parser.parse_args(["receipt"]).session)
        args = parser.parse_args(["stats", "--since", "24h"])
        self.assertEqual((args.switchyard_command, args.since), ("stats", "24h"))
        with self.assertRaises(SystemExit):
            parser.parse_args(["receipt", "--json", "--human"])


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
