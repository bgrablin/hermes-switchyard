import hashlib
import json
import unittest
from evaluation.c2_frozen.provenance import (
    receipt_binding,
    validate_receipt,
    validate_freeze_bytes,
)


class ReceiptProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.freeze = {
            "source_manifests": {
                "candidate": {"plugin.py": "a"},
                "release": {"plugin.py": "b"},
            }
        }
        self.digest = "frozen-digest"
        self.row = {
            "arm": "candidate",
            **receipt_binding("candidate", self.freeze, self.digest),
        }

    def test_matching_arm_and_freeze_pass(self):
        validate_receipt(self.row, self.freeze, self.digest)

    def test_missing_or_mismatched_receipts_fail(self):
        for change in (
            {"source_hash": None},
            {"source_hash": "wrong"},
            {"freeze_sha256": "wrong"},
            {"arm": "release"},
        ):
            with self.assertRaises(ValueError):
                validate_receipt({**self.row, **change}, self.freeze, self.digest)

    def test_changed_source_manifest_fails(self):
        self.freeze["source_manifests"]["candidate"]["plugin.py"] = "changed"
        with self.assertRaises(ValueError):
            validate_receipt(self.row, self.freeze, self.digest)

    def test_disabled_is_bound_to_empty_plugin_manifest_and_freeze(self):
        row = {"arm": "off", **receipt_binding("off", self.freeze, self.digest)}
        validate_receipt(row, self.freeze, self.digest)
        with self.assertRaises(ValueError):
            validate_receipt(row, self.freeze, "different-freeze")


class FreezeIntegrityTests(unittest.TestCase):
    def test_non_source_freeze_change_is_rejected(self):
        frozen = {
            "source_manifests": {},
            "model": "original",
            "gates": {"correctness": True},
        }
        raw = json.dumps(frozen).encode()
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(validate_freeze_bytes(raw, digest), frozen)
        for key, value in (("model", "changed"), ("gates", {"correctness": False})):
            changed = json.dumps({**frozen, key: value}).encode()
            with self.assertRaises(ValueError):
                validate_freeze_bytes(changed, digest)
