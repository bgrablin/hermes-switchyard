"""Invariant tests for the CI source gate and live usage receipts."""
from __future__ import annotations

import unittest

from scripts.ci.live_jev_contract import LiveContractError, _usage_receipt
from scripts.ci.validate_live_source import TrustedSourceError, validate_trusted_source


class CiContractTests(unittest.TestCase):
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

    def test_live_receipt_requires_numeric_provider_usage(self):
        receipt = _usage_receipt({"cost": 0.01, "total_tokens": 12, "ignored": "not emitted"})
        self.assertEqual(receipt, {"cost": 0.01, "total_tokens": 12})
        with self.assertRaises(LiveContractError):
            _usage_receipt({})
        with self.assertRaises(LiveContractError):
            _usage_receipt({"cost": -1})


if __name__ == "__main__":
    unittest.main()
