from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect_luna
from collect_luna import _parse_success, _sanitize_luna_response_excerpt
from collector_common import luna_usage


USAGE = {
    "completed": True,
    "partial": False,
    "failed": False,
    "model": "gpt-5.6-luna-900k",
    "provider": "openai-codex",
    "api_calls": 1,
    "turn_exit_reason": "text_response(finish_reason=stop)",
}


class LunaFailureEvidenceTests(unittest.TestCase):
    def test_startup_probe_is_bounded_and_returns_runtime_identity(self):
        completed = subprocess.CompletedProcess(
            ["fake-hermes", "--version"],
            0,
            stdout="Hermes Agent 1.2.3\n",
            stderr="",
        )
        with mock.patch.object(collect_luna.subprocess, "run", return_value=completed) as run:
            elapsed_ms, identity = collect_luna._startup_probe("fake-hermes", 2.5)
        self.assertGreaterEqual(elapsed_ms, 0.0)
        self.assertEqual(identity, "Hermes Agent 1.2.3")
        run.assert_called_once_with(
            ["fake-hermes", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2.5,
        )

    def test_startup_probe_timeout_uses_bounded_refusal(self):
        with mock.patch.object(
            collect_luna.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["fake-hermes", "--version"], 1.0),
        ):
            with self.assertRaisesRegex(RuntimeError, "hermes_version_probe_timeout"):
                collect_luna._startup_probe("fake-hermes", 1.0)

    def test_api_call_count_cannot_drop_below_main_receipt_count(self):
        usage = {
            "api_calls": 1,
            "total_including_auxiliary": {"api_calls": 0},
        }
        self.assertEqual(collect_luna._api_call_count(usage), 1)

    def test_collect_creates_nested_output_parent_before_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "new" / "nested" / "luna.json"
            args = argparse.Namespace(
                live=True,
                public_synthetic_ack=True,
                max_requests=24,
                output=output,
                hermes_command="fake-hermes",
                timeout_seconds=1.0,
                resume=False,
            )

            parent_states = []

            def failed_run(command, **_kwargs):
                usage_path = Path(command[command.index("--usage-file") + 1])
                parent_states.append(usage_path.parent.is_dir())
                raise RuntimeError("stop after parent check")

            with mock.patch.object(collect_luna, "_startup_probe", return_value=(0.0, "Hermes Agent test")):
                with mock.patch.object(collect_luna.subprocess, "run", side_effect=failed_run):
                    self.assertEqual(collect_luna.collect(args), 0)
            self.assertTrue(parent_states)
            self.assertTrue(all(parent_states))
            self.assertTrue(output.is_file())
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(payload["hermes_runtime_identity"], "Hermes Agent test")

    def test_resume_rejects_different_hermes_runtime_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "luna.json"
            _book, meta = collect_luna.benchmark.load_book()
            payload = collect_luna.load_payload(output, "luna", meta, resume=False)
            payload["hermes_runtime_identity"] = "Hermes Agent old"
            output.write_text(json.dumps(payload), encoding="utf-8")
            args = argparse.Namespace(
                live=True,
                public_synthetic_ack=True,
                max_requests=24,
                output=output,
                hermes_command="fake-hermes",
                timeout_seconds=1.0,
                resume=True,
            )
            with mock.patch.object(
                collect_luna,
                "_startup_probe",
                return_value=(0.0, "Hermes Agent current"),
            ):
                with self.assertRaisesRegex(ValueError, "existing_output_runtime_identity_mismatch"):
                    collect_luna.collect(args)

    def test_usage_includes_official_auxiliary_token_and_cost_totals(self):
        usage = luna_usage({
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "estimated_cost_usd": 0.01,
            "auxiliary": {
                "input_tokens": 8,
                "output_tokens": 2,
                "total_tokens": 10,
                "estimated_cost_usd": 0.003,
            },
            "total_including_auxiliary": {
                "api_calls": 2,
                "total_tokens": 130,
                "estimated_cost_usd": 0.013,
            },
        })
        self.assertEqual(usage["input_tokens"], 108)
        self.assertEqual(usage["output_tokens"], 22)
        self.assertEqual(usage["total_tokens"], 130)
        self.assertEqual(usage["reported_dollar_cost"], 0.013)

    def test_selected_response_without_selected_skills_normalizes_single_selection(self):
        raw = json.dumps({
            "status": "selected",
            "selected": "git-change-preparation",
            "selected_skills": [],
            "abstention_reason": None,
            "secret_like_field": "must-not-be-retained",
        })
        parsed = _parse_success(raw, USAGE)
        self.assertEqual(parsed["selected_skills"], ["git-change-preparation"])
        self.assertEqual(parsed["provider_calls"], 1)

    def test_multi_selection_with_null_top1_is_accepted(self):
        raw = json.dumps({
            "status": "selected",
            "selected": None,
            "selected_skills": ["document-intelligence", "literature-review"],
            "abstention_reason": None,
        })
        parsed = _parse_success(raw, USAGE)
        self.assertIsNone(parsed["selected"])
        self.assertEqual(parsed["selected_skills"], ["document-intelligence", "literature-review"])

    def test_contradictory_selection_forms_are_rejected(self):
        cases = (
            {"status": "selected", "selected": "git-change-preparation", "selected_skills": ["github-code-review"]},
            {"status": "abstained", "selected": None, "selected_skills": ["git-change-preparation"]},
            {"status": "selected", "selected": None, "selected_skills": []},
            {"status": "selected", "selected": "git-change-preparation", "selected_skills": ["git-change-preparation", "git-change-preparation"]},
        )
        for response in cases:
            with self.subTest(response=response):
                with self.assertRaises(ValueError):
                    _parse_success(json.dumps({**response, "abstention_reason": None}), USAGE)

    def test_invalid_json_excerpt_does_not_copy_provider_text(self):
        excerpt = _sanitize_luna_response_excerpt("not-json provider text with a token")
        self.assertEqual(excerpt, {"json_valid": False})

    def test_success_parser_rejects_calls_above_remaining_budget(self):
        usage = {**USAGE, "api_calls": 2}
        raw = json.dumps({
            "status": "selected",
            "selected": "git-change-preparation",
            "selected_skills": ["git-change-preparation"],
            "abstention_reason": None,
        })
        with self.assertRaises(ValueError):
            _parse_success(raw, usage, max_provider_calls=1)

    def test_success_parser_counts_auxiliary_calls_against_budget(self):
        usage = {
            **USAGE,
            "api_calls": 1,
            "total_including_auxiliary": {"api_calls": 2},
        }
        raw = json.dumps({
            "status": "selected",
            "selected": "git-change-preparation",
            "selected_skills": ["git-change-preparation"],
            "abstention_reason": None,
        })
        with self.assertRaisesRegex(ValueError, "official_usage_request_cap_exceeded"):
            _parse_success(raw, usage, max_provider_calls=1)

        parsed = _parse_success(raw, usage, max_provider_calls=2)
        self.assertEqual(parsed["provider_calls"], 2)

    def test_stale_usage_receipt_is_removed_before_subprocess(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "luna.json"
            book, _meta = collect_luna.benchmark.load_book()
            first_case_id = book["heldout_fixtures"][0]["id"]
            first_usage = output.parent / f".luna.{first_case_id}.usage.json"
            first_usage.write_text(json.dumps(USAGE), encoding="utf-8")

            def completed_run(command, **_kwargs):
                usage_path = Path(command[command.index("--usage-file") + 1])
                self.assertFalse(usage_path.exists())
                usage_path.write_text(json.dumps(USAGE), encoding="utf-8")
                return subprocess.CompletedProcess(
                    command,
                    0,
                    stdout=json.dumps({
                        "status": "selected",
                        "selected": "git-change-preparation",
                        "selected_skills": ["git-change-preparation"],
                        "abstention_reason": None,
                    }),
                    stderr="",
                )

            args = argparse.Namespace(
                live=True,
                public_synthetic_ack=True,
                max_requests=24,
                output=output,
                hermes_command="fake-hermes",
                timeout_seconds=1.0,
                resume=False,
            )
            with mock.patch.object(collect_luna, "_startup_probe", return_value=(0.0, "Hermes Agent test")):
                with mock.patch.object(collect_luna.subprocess, "run", side_effect=completed_run):
                    self.assertEqual(collect_luna.collect(args), 0)

    def test_timeout_retains_usage_response_and_request_cap_count(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "luna.json"

            def timeout_run(command, **_kwargs):
                usage_path = Path(command[command.index("--usage-file") + 1])
                usage_path.write_text(json.dumps({
                    "model": "gpt-5.6-luna-900k",
                    "provider": "openai-codex",
                    "api_calls": 1,
                    "completed": False,
                    "partial": True,
                    "failed": False,
                }), encoding="utf-8")
                raise subprocess.TimeoutExpired(
                    command, 1, output=b'{"status":"selected","selected":"docker-management","selected_skills":[]}'
                )

            args = argparse.Namespace(
                live=True,
                public_synthetic_ack=True,
                max_requests=24,
                output=output,
                hermes_command="fake-hermes",
                timeout_seconds=1.0,
                resume=False,
            )
            with mock.patch.object(collect_luna, "_startup_probe", return_value=(0.0, "Hermes Agent test")):
                with mock.patch.object(collect_luna.subprocess, "run", side_effect=timeout_run):
                    self.assertEqual(collect_luna.collect(args), 0)

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["records"]), 24)
            self.assertTrue(all(row["measurement_status"] == "failed" for row in payload["records"]))
            self.assertTrue(all(row["provider_call_count"] == 1 for row in payload["records"]))
            error = payload["records"][0]["error"]
            self.assertEqual(error["type"], "TimeoutExpired")
            self.assertEqual(error["response_excerpt"]["selected"], "docker-management")
            self.assertEqual(error["usage_receipt"]["api_calls"], 1)

    def test_timeout_counts_auxiliary_calls_from_usage_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "luna.json"

            def timeout_run(command, **_kwargs):
                usage_path = Path(command[command.index("--usage-file") + 1])
                usage_path.write_text(json.dumps({
                    "model": "gpt-5.6-luna-900k",
                    "provider": "openai-codex",
                    "api_calls": 1,
                    "total_including_auxiliary": {"api_calls": 2},
                    "completed": False,
                    "partial": True,
                    "failed": False,
                }), encoding="utf-8")
                raise subprocess.TimeoutExpired(command, 1)

            args = argparse.Namespace(
                live=True,
                public_synthetic_ack=True,
                max_requests=24,
                output=output,
                hermes_command="fake-hermes",
                timeout_seconds=1.0,
                resume=False,
            )
            with mock.patch.object(collect_luna, "_startup_probe", return_value=(0.0, "Hermes Agent test")):
                with mock.patch.object(collect_luna.subprocess, "run", side_effect=timeout_run):
                    with self.assertRaisesRegex(RuntimeError, "request_cap_reached_before_all_cases"):
                        collect_luna.collect(args)

            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["records"]), 12)
            self.assertTrue(all(row["provider_call_count"] == 2 for row in payload["records"]))
            self.assertEqual(
                payload["records"][0]["error"]["usage_receipt"]["total_including_auxiliary"],
                {"api_calls": 2},
            )


if __name__ == "__main__":
    unittest.main()
