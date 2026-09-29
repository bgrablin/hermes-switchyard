"""Offline, synthetic evaluation for the local Switchyard wow report."""
from __future__ import annotations

import argparse
import io
import json
import statistics
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hermes_switchyard
from hermes_switchyard import receipt_history, receipt_state
from hermes_switchyard import wow
from hermes_switchyard.automatic import build_routing_receipt
from hermes_switchyard.reasoning_effort_adapter import (
    ReasoningEffortController, append_effort_record, register_reasoning_effort_adapter,
)


class WowCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="wow-synthetic-")
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.data_patch = mock.patch.object(receipt_state, "_plugin_data_dir", return_value=self.data)
        self.data_patch.start()
        self.addCleanup(self.data_patch.stop)
        hermes_switchyard.reset_runtime_status()

    def command(self, *args):
        parser = argparse.ArgumentParser()
        hermes_switchyard._setup_cli(parser)
        parsed = parser.parse_args(args)
        output = io.StringIO()
        with redirect_stdout(output):
            code = hermes_switchyard._cli_handler(parsed)
        return code, output.getvalue()

    def assert_metric_pairs(self, report, expected):
        actual = {name: (metric.get("count", metric.get("value")), metric["n"])
                  for name, metric in report["metrics"].items()}
        self.assertEqual(actual, expected)

    def test_empty_valid_sources_report_observed_zero_without_population_claim(self):
        (self.data / "receipt-history.jsonl").write_text("")
        (self.data / "effort-history.jsonl").write_text("")
        code, text = self.command("wow", "--json")
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["schema_version"], 1)
        self.assertEqual(report["window"]["days"], 7)
        self.assertEqual(report["plugin_state"], "not_registered_here")
        self.assertEqual(report["sources"]["routing"]["state"], "available")
        self.assertEqual(report["metrics"]["observed_turns"], {"count": 0, "n": 0, "status": "observed"})
        self.assertEqual(report["metrics"]["jev_calls"], {"count": 0, "n": 0, "status": "observed"})
        self.assertEqual(report["metrics"]["median_latency_ms"], {"value": None, "n": 0, "status": "unknown"})
        self.assert_metric_pairs(report, {
            "observed_turns": (0, 0), "skills_selected": (0, 0), "skills_loaded": (0, 0),
            "below_cap_turns": (0, 0), "light_turn_bypasses": (0, 0),
            "jev_calls": (0, 0), "median_latency_ms": (None, 0),
            "hosted_failures": (0, 0), "hosted_abstentions": (0, 0),
        })
        self.assertIn("retained", report["coverage"])

    def test_mixed_receipts_have_exact_counts_denominators_and_one_call_median(self):
        clock = datetime.now(timezone.utc).replace(microsecond=0)
        def routing(turn, *, selected=None, source="none", attempted=False, requests=0,
                    latency=0, error=None, skipped=None, bypass=None, loaded=False,
                    when=None):
            receipt = build_routing_receipt({
                "selected": selected, "source": source, "hosted_attempted": attempted,
                "request_count": requests, "total_latency_ms": latency,
                "hosted_error": error, "hosted_skipped": skipped, "bypass_reason": bypass,
            })
            if loaded:
                receipt.update(consumer_status="loaded", loaded_skill=selected,
                               loaded_source=source, skill_load_verified=True)
            self.assertTrue(receipt_history.append_receipt_history(
                receipt, session_id="s1", turn_id=turn, now=when or clock, data_dir=self.data
            ))

        routing("old", selected="old-skill", source="local", when=clock - timedelta(days=8))
        routing("a", selected="skill-a", source="jev", attempted=True, requests=2,
                latency=90, loaded=True)
        routing("b", skipped="light_no_skill", bypass="light_no_skill")
        routing("c", attempted=True, requests=1, latency=70,
                error="transport_or_execution_failure")
        routing("d", attempted=True, requests=1, latency=30)
        routing("e", selected="skill-e", source="local", loaded=True)

        def effort(turn, sent, *, called=False, latency=None):
            self.assertTrue(append_effort_record({
                "session_id": "s1", "turn_id": turn, "mode": "auto",
                "requested_effort": "high", "cap": "high", "effort": sent,
                "jev_called": called, "jev_latency_ms": latency,
            }, now=clock, data_dir=self.data))

        effort("a", "low", called=True, latency=21)
        effort("a", "medium")
        effort("b", "low")
        effort("c", "high", called=True, latency=18)
        effort("e", "low")
        code, text = self.command("wow", "--json")
        self.assertEqual(code, 0)
        report = json.loads(text)
        expected = {
            "observed_turns": (5, 5), "skills_selected": (2, 5),
            "skills_loaded": (2, 5), "light_turn_bypasses": (1, 5),
            "hosted_failures": (1, 5), "hosted_abstentions": (1, 5),
            "below_cap_turns": (3, 4), "jev_calls": (6, 10),
        }
        for name, (count, n) in expected.items():
            with self.subTest(metric=name):
                self.assertEqual(report["metrics"][name]["count"], count)
                self.assertEqual(report["metrics"][name]["n"], n)
        self.assertEqual(report["metrics"]["median_latency_ms"],
                         {"value": 21.0, "n": 4, "status": "observed"})
        self.assertEqual(len(receipt_history.read_history(data_dir=self.data)), 6)
        self.assertEqual(set(report["sources"]["routing"]), {"state"})
        self.assertEqual(set(report["sources"]["effort"]), {"state"})
        self.assertNotIn("skill-a", text)
        self.assertNotIn("skill-e", text)
        self.assertEqual(self.command("wow", "--days", "0")[0], 2)

    def test_missing_source_is_unknown_not_a_zero_count(self):
        (self.data / "receipt-history.jsonl").write_text("")
        code, text = self.command("wow", "--json")
        self.assertEqual(code, 0)
        report = json.loads(text)
        self.assertEqual(report["sources"]["effort"]["state"], "unavailable")
        self.assertEqual(report["metrics"]["below_cap_turns"],
                         {"count": None, "n": 0, "status": "unknown"})
        self.assertEqual(report["metrics"]["jev_calls"],
                         {"count": None, "n": 0, "status": "unknown"})
        self.assertEqual(report["metrics"]["median_latency_ms"]["value"], None)
        self.assertEqual(report["metrics"]["observed_turns"]["count"], 0)

    def test_malformed_lines_and_effort_content_are_partial_without_leaking(self):
        marker = "SYNTHETIC_SECRET_DO_NOT_ECHO"
        (self.data / "receipt-history.jsonl").write_text(
            json.dumps({"content": marker, "local_path": "/synthetic/private"}) + "\n"
        )
        (self.data / "effort-history.jsonl").write_text(json.dumps({
            "schema": 1, "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": "s1", "turn_id": "t1", "cap": None, "sent": None,
            "jev_called": False, "jev_latency_ms": None,
            "content": marker, "local_path": "/synthetic/private",
        }) + "\n")
        for flags in (("--json",), ()):
            code, output = self.command("wow", *flags)
            self.assertEqual(code, 0)
            self.assertNotIn(marker, output)
            self.assertNotIn("/synthetic/private", output)
            self.assertNotIn('"content"', output)
        report = json.loads(self.command("wow", "--json")[1])
        self.assertEqual(report["sources"]["routing"]["state"], "partial")
        self.assertEqual(report["sources"]["effort"]["state"], "partial")
        self.assertEqual(report["metrics"]["observed_turns"]["count"], 0)
        self.assertEqual(report["metrics"]["observed_turns"]["status"], "partial")
        self.assertEqual(report["metrics"]["below_cap_turns"]["count"], None)
        self.assertEqual(report["metrics"]["below_cap_turns"]["status"], "unknown")
        self.assert_metric_pairs(report, {
            "observed_turns": (0, 0), "skills_selected": (0, 0), "skills_loaded": (0, 0),
            "below_cap_turns": (None, 0), "light_turn_bypasses": (0, 0),
            "jev_calls": (0, 0), "median_latency_ms": (None, 0),
            "hosted_failures": (0, 0), "hosted_abstentions": (0, 0),
        })

    def test_symlink_history_is_not_followed(self):
        source = self.data / "synthetic-target.jsonl"
        source.write_text("private-marker\n")
        (self.data / "receipt-history.jsonl").symlink_to(source)
        report = json.loads(self.command("wow", "--json")[1])
        self.assertEqual(report["sources"]["routing"]["state"], "unavailable")
        self.assertNotIn("private-marker", json.dumps(report))

    def test_default_writer_retains_no_more_than_500_turns(self):
        receipt = build_routing_receipt({"selected": "synthetic-skill", "source": "local"})
        for index in range(501):
            self.assertTrue(receipt_history.append_receipt_history(
                receipt, session_id="s1", turn_id=f"t{index}", data_dir=self.data))
        retained = receipt_history.read_history(data_dir=self.data)
        self.assertEqual(len(retained), 500)
        self.assertEqual(retained[0]["turn_id"], "t1")
        report = json.loads(self.command("wow", "--json")[1])
        self.assertEqual(report["sources"]["routing"]["state"], "partial")
        self.assertEqual(report["metrics"]["observed_turns"],
                         {"count": 500, "n": 500, "status": "partial"})
        self.assert_metric_pairs(report, {
            "observed_turns": (500, 500), "skills_selected": (500, 500),
            "skills_loaded": (0, 500), "below_cap_turns": (None, 0),
            "light_turn_bypasses": (0, 500), "jev_calls": (None, 500),
            "median_latency_ms": (None, 0), "hosted_failures": (0, 500),
            "hosted_abstentions": (0, 500),
        })
        self.assertIn("retained", report["coverage"])

    def test_slash_and_cli_share_schema_without_provider_or_session_store(self):
        (self.data / "receipt-history.jsonl").write_text("")
        (self.data / "effort-history.jsonl").write_text("")
        controller = ReasoningEffortController(
            client_factory=lambda: self.fail("client factory must not run"))
        with (mock.patch("hermes_switchyard.client.DecisionClient.decide",
                         side_effect=AssertionError("provider called")) as provider,
              mock.patch("socket.socket.connect", side_effect=AssertionError("network called")) as network,
              mock.patch("sqlite3.connect", side_effect=AssertionError("session store read")) as session_store,
              mock.patch.object(hermes_switchyard, "_secret",
                                side_effect=AssertionError("secrets accessed"))):
            slash_json = json.loads(controller.handle_command("wow --days 3 --json"))
            code, cli_text = self.command("wow", "--days", "3", "--json")
            slash_text = controller.handle_command("wow --days 3")
        provider.assert_not_called()
        network.assert_not_called()
        session_store.assert_not_called()
        self.assertEqual(code, 0)
        self.assertEqual(slash_json["metrics"], json.loads(cli_text)["metrics"])
        self.assertEqual(slash_json["plugin_state"], "registered_here")
        self.assertIn("n=0", slash_text)
        self.assertIn("3d", slash_text)
        self.assertNotIn("saved", slash_text.lower())
        self.assertIn("Usage", controller.handle_command("wow --days no"))

    def test_slash_remains_read_only_when_adaptive_effort_is_disabled(self):
        (self.data / "receipt-history.jsonl").write_text("")
        (self.data / "effort-history.jsonl").write_text("")
        commands = {}
        ctx = SimpleNamespace(register_command=lambda name, handler, **kwargs:
                              commands.setdefault(name, handler))
        receipt = register_reasoning_effort_adapter(ctx, enabled=False)
        self.assertEqual(receipt["mode"], "disabled")
        self.assertIn("switchyard", commands)
        self.assertEqual(json.loads(commands["switchyard"]("wow --json"))["plugin_state"],
                         "registered_here")
        self.assertIn("disabled", commands["switchyard"]("effort status"))

    def test_malformed_numeric_effort_does_not_become_a_call_or_median(self):
        (self.data / "receipt-history.jsonl").write_text("")
        (self.data / "effort-history.jsonl").write_text(json.dumps({
            "schema": 1, "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": "s1", "turn_id": "t1", "cap": "high", "sent": "low",
            "jev_called": "true", "jev_latency_ms": float("nan"),
        }) + "\n")
        code, text = self.command("wow", "--json")
        self.assertEqual(code, 0)
        report = json.loads(text, parse_constant=lambda value: self.fail(f"non-finite JSON: {value}"))
        self.assertEqual(report["sources"]["effort"]["state"], "partial")
        self.assertEqual(report["metrics"]["below_cap_turns"]["count"], None)
        self.assertEqual(report["metrics"]["median_latency_ms"]["value"], None)

    def test_future_record_is_outside_the_trailing_window(self):
        (self.data / "effort-history.jsonl").write_text("")
        self.assertTrue(receipt_history.append_receipt_history(
            build_routing_receipt({"selected": "future-skill", "source": "local"}),
            session_id="s1", turn_id="future", data_dir=self.data,
            now=datetime.now(timezone.utc) + timedelta(days=1)))
        report = json.loads(self.command("wow", "--json")[1])
        self.assertEqual(len(receipt_history.read_history(data_dir=self.data)), 1)
        self.assertEqual(report["metrics"]["observed_turns"]["count"], 0)

    def test_hostile_effort_field_shape_is_partial_not_a_report_crash(self):
        (self.data / "receipt-history.jsonl").write_text("")
        (self.data / "effort-history.jsonl").write_text(json.dumps({
            "schema": 1, "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "session_id": "s1", "turn_id": "t1", "cap": ["high"],
            "sent": {"low": "private-content"}, "jev_called": False,
            "jev_latency_ms": None,
        }) + "\n")
        code, text = self.command("wow", "--json")
        self.assertEqual(code, 0)
        self.assertNotIn("private-content", text)
        self.assertEqual(json.loads(text)["sources"]["effort"]["state"], "partial")

    def test_unsupported_effort_schema_or_mode_is_partial(self):
        (self.data / "receipt-history.jsonl").write_text("")
        self.assertTrue(append_effort_record({
            "session_id": "s1", "turn_id": "t1", "mode": "auto",
            "requested_effort": "high", "cap": "high", "effort": "low",
            "jev_called": True, "jev_latency_ms": 12,
        }, data_dir=self.data))
        path = self.data / "effort-history.jsonl"
        original = json.loads(path.read_text())
        for key, invalid in (("schema", True), ("mode", "private-content")):
            with self.subTest(field=key):
                path.write_text(json.dumps({**original, key: invalid}) + "\n")
                code, output = self.command("wow", "--json")
                self.assertEqual(code, 0)
                self.assertNotIn("private-content", output)
                report = json.loads(output)
                self.assertEqual(report["sources"]["effort"]["state"], "partial")
                self.assertEqual(report["metrics"]["jev_calls"]["status"], "partial")

    def test_displayed_bounds_include_second_precision_receipts(self):
        clock = datetime(2026, 9, 29, 17, 0, 0, 987654, tzinfo=timezone.utc)
        upper = clock.replace(microsecond=0)
        lower = upper - timedelta(days=7)
        for turn, stamp, result in (
            ("before", lower - timedelta(seconds=1), {
                "hosted_attempted": True, "hosted_error": "transport_or_execution_failure",
            }),
            ("at-lower", lower, {"bypass_reason": "light_no_skill"}),
            ("at-upper", upper, {"selected": "boundary-skill", "source": "local"}),
        ):
            receipt = build_routing_receipt(result)
            if turn == "at-upper":
                receipt.update(consumer_status="loaded", loaded_skill="boundary-skill",
                               loaded_source="local", skill_load_verified=True)
            self.assertTrue(receipt_history.append_receipt_history(
                receipt, session_id="s1", turn_id=turn, now=stamp, data_dir=self.data))
            self.assertTrue(append_effort_record({
                "session_id": "s1", "turn_id": turn, "mode": "auto",
                "cap": "high", "effort": "high" if turn == "before" else "low",
                "jev_called": False,
            }, now=stamp, data_dir=self.data))
        self.assertEqual(receipt_history.read_history(data_dir=self.data)[1]["recorded_at"],
                         "2026-09-22T17:00:00Z")
        self.assertEqual(receipt_history.read_history(data_dir=self.data)[2]["recorded_at"],
                         "2026-09-29T17:00:00Z")
        report = wow.build_report(data_dir=self.data, now=clock)
        self.assertEqual(report["window"], {"days": 7, "from": "2026-09-22T17:00:00Z",
                                            "to": "2026-09-29T17:00:00Z"})
        self.assertEqual(report["sources"], {"routing": {"state": "available"},
                                             "effort": {"state": "available"}})
        self.assert_metric_pairs(report, {
            "observed_turns": (2, 2), "skills_selected": (1, 2), "skills_loaded": (1, 2),
            "below_cap_turns": (2, 2), "light_turn_bypasses": (1, 2),
            "jev_calls": (0, 4), "median_latency_ms": (None, 0),
            "hosted_failures": (0, 2), "hosted_abstentions": (0, 2),
        })

    def test_rotated_history_counts_only_the_current_retained_file(self):
        clock = datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc)
        (self.data / "effort-history.jsonl").write_text("")
        receipt = build_routing_receipt({"selected": "synthetic-skill", "source": "local"})
        self.assertTrue(receipt_history.append_receipt_history(
            receipt, session_id="s1", turn_id="old", data_dir=self.data, now=clock))
        (self.data / "receipt-history.jsonl").rename(self.data / "receipt-history.jsonl.1")
        self.assertTrue(receipt_history.append_receipt_history(
            receipt, session_id="s1", turn_id="new", data_dir=self.data, now=clock))
        report = wow.build_report(data_dir=self.data, now=clock)
        self.assertEqual(report["window"], {"days": 7, "from": "2026-09-22T17:00:00Z",
                                            "to": "2026-09-29T17:00:00Z"})
        self.assertEqual(report["metrics"]["observed_turns"],
                         {"count": 1, "n": 1, "status": "observed"})
        self.assert_metric_pairs(report, {
            "observed_turns": (1, 1), "skills_selected": (1, 1), "skills_loaded": (0, 1),
            "below_cap_turns": (0, 0), "light_turn_bypasses": (0, 1),
            "jev_calls": (0, 1), "median_latency_ms": (None, 0),
            "hosted_failures": (0, 1), "hosted_abstentions": (0, 1),
        })
        self.assertIn("not all Hermes turns", report["coverage"])

    def test_small_byte_bound_keeps_only_retained_turns(self):
        clock = datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc)
        receipt = build_routing_receipt({"selected": "synthetic-skill", "source": "local"})
        self.assertTrue(receipt_history.append_receipt_history(
            receipt, session_id="s1", turn_id="t0", data_dir=self.data, now=clock))
        bound = (self.data / "receipt-history.jsonl").stat().st_size + 4
        self.assertTrue(receipt_history.append_receipt_history(
            receipt, session_id="s1", turn_id="t1", data_dir=self.data, now=clock,
            max_bytes=bound))
        retained = receipt_history.read_history(data_dir=self.data)
        self.assertEqual([r["turn_id"] for r in retained], ["t1"])
        (self.data / "effort-history.jsonl").write_text("")
        report = wow.build_report(data_dir=self.data, now=clock)
        self.assertEqual(report["metrics"]["observed_turns"]["count"], 1)
        self.assertEqual(report["metrics"]["observed_turns"]["n"], 1)

    def test_in_process_command_p50_under_200ms_on_frozen_fixture(self):
        clock = datetime(2026, 9, 29, 17, 0, tzinfo=timezone.utc)
        receipt = build_routing_receipt({"selected": "synthetic-skill", "source": "local"})
        for i in range(5):
            self.assertTrue(receipt_history.append_receipt_history(
                receipt, session_id="s1", turn_id=f"t{i}", data_dir=self.data, now=clock))
        for i in range(5):
            self.assertTrue(append_effort_record({
                "session_id": "s1", "turn_id": f"t{i}", "cap": "high", "effort": "low",
                "jev_called": False,
            }, now=clock, data_dir=self.data))
        samples_ms = []
        with mock.patch.object(wow, "datetime", wraps=datetime) as fake_clock:
            fake_clock.now.return_value = clock
            for _ in range(31):
                start = time.perf_counter_ns()
                code, output = self.command("wow", "--json")
                samples_ms.append((time.perf_counter_ns() - start) / 1_000_000)
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)["metrics"]["observed_turns"]["count"], 5)
        median_ms = statistics.median(samples_ms)
        print(json.dumps({"wow_in_process_p50_ms": median_ms, "n": len(samples_ms),
                          "fixture_clock": "2026-09-29T17:00:00Z", "routing_records": 5,
                          "effort_records": 5, "raw_ms": samples_ms}))
        self.assertLess(median_ms, 200)


if __name__ == "__main__":
    unittest.main()
