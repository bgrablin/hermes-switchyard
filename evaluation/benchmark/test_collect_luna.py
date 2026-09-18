from __future__ import annotations

import argparse
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import collect_luna
from collect_luna import _luna_failure_error, _parse_success, _sanitize_luna_response_excerpt


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
    def test_selected_response_without_selected_skills_keeps_exact_cause_and_safe_excerpt(self):
        raw = json.dumps({
            "status": "selected",
            "selected": "git-change-preparation",
            "abstention_reason": None,
            "secret_like_field": "must-not-be-retained",
        })
        with self.assertRaises(ValueError) as raised:
            _parse_success(raw, USAGE)

        error = _luna_failure_error(raised.exception, stdout=raw, usage=USAGE, exit_code=0)
        self.assertEqual(error["type"], "ValueError")
        self.assertEqual(error["cause"], "luna_response_schema_invalid")
        self.assertEqual(
            error["response_excerpt"],
            {
                "json_valid": True,
                "fields_present": ["abstention_reason", "selected", "status"],
                "status": "selected",
                "selected": "git-change-preparation",
                "abstention_reason": None,
            },
        )
        self.assertNotIn("secret_like_field", json.dumps(error, sort_keys=True))
        self.assertEqual(error["usage_receipt"]["model"], "gpt-5.6-luna-900k")
        self.assertEqual(error["usage_receipt"]["api_calls"], 1)

    def test_invalid_json_excerpt_does_not_copy_provider_text(self):
        excerpt = _sanitize_luna_response_excerpt("not-json provider text with a token")
        self.assertEqual(excerpt, {"json_valid": False})

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
            with mock.patch.object(collect_luna, "_startup_probe", return_value=0.0):
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


if __name__ == "__main__":
    unittest.main()
