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


def validate_job_receipt(row, job, order_index):
    """Bind a returned receipt to the exact submitted frozen job and index."""
    for key, expected in {**job, "order_index": order_index}.items():
        if (
            key not in row
            or type(row[key]) is not type(expected)
            or row[key] != expected
        ):
            raise ValueError("receipt does not match frozen job: " + key)


def validate_campaign_receipts(rows, freeze, freeze_sha256):
    """Reject missing, duplicated, reordered, or foreign campaign receipts."""
    order = freeze["order"]
    if len(rows) != len(order):
        raise ValueError("receipt count does not match frozen campaign")
    for index, (row, job) in enumerate(zip(rows, order)):
        validate_receipt(row, freeze, freeze_sha256)
        validate_job_receipt(row, job, index)
