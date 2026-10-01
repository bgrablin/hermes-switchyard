"""Bind each future benchmark receipt to its frozen reviewed plugin bytes."""

import hashlib
import json


def source_manifest_hash(manifest):
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def receipt_binding(arm, freeze, freeze_sha256):
    if arm not in {"off", "release", "candidate"}:
        raise ValueError("unknown benchmark arm")
    manifest = {} if arm == "off" else freeze["source_manifests"][arm]
    return {
        "source_hash": source_manifest_hash(manifest),
        "freeze_sha256": freeze_sha256,
    }


def validate_receipt(row, freeze, freeze_sha256):
    expected = receipt_binding(row["arm"], freeze, freeze_sha256)
    if any(row.get(key) != value for key, value in expected.items()):
        raise ValueError(
            "receipt source/freeze provenance missing or mismatched; do not relabel legacy rows"
        )


def validate_freeze_bytes(raw, expected_sha256):
    """Parse only the exact frozen bytes referenced by the campaign receipts."""
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError("freeze bytes do not match completion digest")
    return json.loads(raw)
