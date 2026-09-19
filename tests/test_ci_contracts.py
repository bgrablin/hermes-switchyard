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
    def test_compatibility_workflow_avoids_duplicate_feature_push_runs(self):
        workflow = (
            Path(__file__).resolve().parent.parent
            / ".github"
            / "workflows"
            / "switchyard-compatibility.yml"
        ).read_text(encoding="utf-8")

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
        self.assertIn("    os: [ubuntu-latest, windows-latest]\n", workflow)
        self.assertIn("    python-version: ['3.11', '3.12', '3.13']\n", workflow)
        self.assertIn("  cancel-in-progress: true\n", workflow)

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
