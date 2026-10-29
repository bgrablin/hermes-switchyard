"""TypeSafe resolved-model alias allow-list."""

from __future__ import annotations

import unittest

from hermes_switchyard import client


class TypeSafeModelAliasTests(unittest.TestCase):
    def test_typesafe_jev_latest_may_resolve_to_known_concrete_releases(self):
        decider = client.DecisionClient(
            api_key="fixture-key",
            endpoint=client.TYPESAFE_ENDPOINT,
            model="jev-latest",
        )
        for resolved in ("jev-1.13.0", "jev-1.14.0", "jev-1.14"):
            with self.subTest(resolved=resolved):
                self.assertEqual(decider._validate_resolved_model(resolved), resolved)

    def test_typesafe_rejects_unknown_concrete_release(self):
        decider = client.DecisionClient(
            api_key="fixture-key",
            endpoint=client.TYPESAFE_ENDPOINT,
            model="jev-latest",
        )
        with self.assertRaisesRegex(ValueError, "outside the allowed aliases"):
            decider._validate_resolved_model("jev-9.9.9")


if __name__ == "__main__":
    unittest.main()
