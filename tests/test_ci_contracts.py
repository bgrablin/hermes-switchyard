"""Invariant tests for the CI source gate and live usage receipts."""
from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from scripts.ci.live_jev_contract import LiveContractError, _usage_receipt
from scripts.ci.validate_live_source import (
    TrustedSourceError,
    main,
    validate_trusted_source,
)


class CiContractTests(unittest.TestCase):
    @staticmethod
    def _workflow_text() -> str:
        return (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "switchyard-compatibility.yml"
        ).read_text(encoding="utf-8")

    def test_compatibility_workflow_avoids_duplicate_feature_push_runs(self):
        workflow = self._workflow_text()

        push_stanza = re.search(
            r"(?m)^  push:\n(?: {4,}.*\n)*",
            workflow,
        )
        if push_stanza is None:
            self.fail("compatibility workflow is missing the push trigger")
        self.assertEqual(
            push_stanza.group(0),
            "  push:\n    branches:\n      - main\n",
        )
        self.assertIn("  pull_request:\n", workflow)
        self.assertIn("  workflow_dispatch:\n", workflow)
        schedule_stanza = re.search(r"(?m)^  schedule:\n(?: {4,}.*\n)*", workflow)
        if schedule_stanza is None:
            self.fail("compatibility workflow is missing the weekly schedule trigger")
        self.assertRegex(
            schedule_stanza.group(0),
            r"- cron: '\d{1,2} \d{1,2} \* \* [0-6]'",
        )
        self.assertIn("  cancel-in-progress: true\n", workflow)

    def test_pull_request_event_plans_exactly_one_matrix_cell(self):
        """Pin the event-dependent matrix so a future edit can't silently

        restore the six-way matrix on every PR push (the original cost
        problem this workflow fixes). Reads the exact JSON the `plan` job
        emits for each branch of its event_name guard.
        """
        workflow = self._workflow_text()
        guard = re.search(
            r'if \[ "\$\{\{ github\.event_name \}\}" = "pull_request" \]; then\n'
            r"(?P<pr_branch>(?:.*\n)*?)"
            r"          else\n"
            r"(?P<default_branch>(?:.*\n)*?)"
            r"          fi\n",
            workflow,
        )
        if guard is None:
            self.fail(
                "compatibility workflow's plan job is missing the "
                "pull_request event_name guard; the fast PR gate may have "
                "regressed to a static matrix"
            )
        pr_matrix = "".join(
            re.findall(r'\{"os":"[^"]+","python-version":"[^"]+"\}', guard.group("pr_branch"))
        )
        default_matrix = "".join(
            re.findall(r'\{"os":"[^"]+","python-version":"[^"]+"\}', guard.group("default_branch"))
        )
        pr_cells = re.findall(r'\{"os":"([^"]+)","python-version":"([^"]+)"\}', pr_matrix)
        default_cells = re.findall(r'\{"os":"([^"]+)","python-version":"([^"]+)"\}', default_matrix)

        self.assertEqual(
            pr_cells,
            [("ubuntu-latest", "3.11")],
            "pull_request events must plan exactly one fast combo",
        )
        self.assertEqual(
            sorted(default_cells),
            sorted(
                [
                    ("ubuntu-latest", "3.11"),
                    ("ubuntu-latest", "3.12"),
                    ("ubuntu-latest", "3.13"),
                    ("windows-latest", "3.11"),
                    ("windows-latest", "3.12"),
                    ("windows-latest", "3.13"),
                ]
            ),
            "push/schedule/workflow_dispatch events must plan the full "
            "2 OS x 3 Python matrix",
        )

    def test_compatibility_step_sequence_is_not_duplicated_across_lanes(self):
        """Guard against a second copy of the compatibility steps (the

        duplicate-lane drift risk Copilot flagged on PR #65): the fast PR
        gate and the full matrix must be one job whose size varies by event,
        not two jobs that can be edited independently and drift apart.
        """
        workflow = self._workflow_text()
        self.assertEqual(
            workflow.count("- name: Run offline plugin checks"),
            1,
            "the offline-checks step must appear exactly once; a second "
            "occurrence means the compatibility steps were duplicated into "
            "a separate job instead of driven by one event-dependent matrix",
        )
        jobs_section = workflow.split("\njobs:\n", 1)[1]
        job_names = re.findall(r"(?m)^  ([a-zA-Z0-9_-]+):\n", jobs_section)
        self.assertEqual(
            job_names,
            ["plan", "compatibility"],
            "expected exactly two jobs (plan, compatibility); a new job "
            "means the single-source-of-truth step sequence was split",
        )

    def test_live_source_requires_canonical_repo_allowlisted_ref_and_exact_head(self):
        source_sha = "a" * 40
        result = validate_trusted_source(
            repository="bgrablin/hermes-switchyard",
            ref="bgrablin/release-packaging",
            requested_sha=source_sha,
            checked_out_sha=source_sha,
        )
        self.assertEqual(result["source_sha"], source_sha)

        with self.assertRaises(TrustedSourceError):
            validate_trusted_source(
                repository="someone/fork",
                ref="main",
                requested_sha=source_sha,
                checked_out_sha=source_sha,
            )
        with self.assertRaises(TrustedSourceError):
            validate_trusted_source(
                repository="bgrablin/hermes-switchyard",
                ref="feature/unreviewed",
                requested_sha=source_sha,
                checked_out_sha=source_sha,
            )
        with self.assertRaises(TrustedSourceError):
            validate_trusted_source(
                repository="bgrablin/hermes-switchyard",
                ref="main",
                requested_sha=source_sha,
                checked_out_sha="b" * 40,
            )

    def test_live_source_cli_does_not_echo_source_sha(self):
        source_sha = "a" * 40
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(
                main(
                    [
                        "--repository",
                        "bgrablin/hermes-switchyard",
                        "--ref",
                        "bgrablin/release-packaging",
                        "--source-sha",
                        source_sha,
                        "--selector-only",
                    ]
                ),
                0,
            )
        self.assertEqual(output.getvalue(), "trusted live-contract source validated\n")
        self.assertNotIn(source_sha, output.getvalue())

    def test_live_receipt_requires_numeric_provider_usage(self):
        receipt = _usage_receipt({"cost": 0.01, "total_tokens": 12, "ignored": "not emitted"})
        self.assertEqual(receipt, {"cost": 0.01, "total_tokens": 12})
        with self.assertRaises(LiveContractError):
            _usage_receipt({})
        with self.assertRaises(LiveContractError):
            _usage_receipt({"cost": -1})


if __name__ == "__main__":
    unittest.main()
