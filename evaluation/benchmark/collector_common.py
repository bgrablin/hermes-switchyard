"""Shared helpers for the opt-in live benchmark collectors."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import benchmark


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def atomic_write(path: Path, payload: dict[str, Any]) -> None:
    """Persist the complete receipt after each case without partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def new_payload(arm: str, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "measurement_schema_version": benchmark.MEASUREMENT_SCHEMA_VERSION,
        "collection_mode": "live",
        "arm": arm,
        "dataset_hash": meta["dataset_hash"],
        "candidate_catalog_hash": meta["catalog_hash"],
        "public_synthetic_ack": True,
        "records": [],
    }


def load_payload(path: Path, arm: str, meta: dict[str, Any], *, resume: bool) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return new_payload(arm, meta)
    if not resume:
        raise RuntimeError("output_exists_use_resume_or_choose_new_output")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(payload, dict)
        or payload.get("schema_version") != 1
        or payload.get("measurement_schema_version") != benchmark.MEASUREMENT_SCHEMA_VERSION
        or payload.get("collection_mode") != "live"
        or payload.get("arm") != arm
    ):
        raise ValueError("existing_output_header_mismatch")
    if payload.get("dataset_hash") != meta["dataset_hash"] or payload.get("candidate_catalog_hash") != meta["catalog_hash"]:
        raise ValueError("existing_output_dataset_mismatch")
    if payload.get("public_synthetic_ack") is not True or not isinstance(payload.get("records"), list):
        raise ValueError("existing_output_contract_mismatch")
    return payload


def validated_records(
    payload: dict[str, Any],
    arm: str,
    cases: list[dict[str, Any]],
    meta: dict[str, Any],
    *,
    expected_source_hash: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Validate every resumed row before it can consume cap or skip a case."""
    rows = payload.get("records")
    if not isinstance(rows, list):
        raise ValueError("existing_output_records_invalid")
    expected = {case["id"]: case for case in cases}
    records: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("case_id") not in expected or row["case_id"] in records:
            raise ValueError("existing_output_case_set_invalid")
        case = expected[row["case_id"]]
        benchmark.validate_record(row, arm, case, meta, live=True)
        if expected_source_hash is not None and row.get("source_hash") != expected_source_hash:
            raise ValueError("existing_output_source_hash_mismatch")
        records[row["case_id"]] = row
    return records


def append_record(payload: dict[str, Any], row: dict[str, Any], path: Path) -> None:
    payload["records"].append(row)
    atomic_write(path, payload)


def safe_error(exc: BaseException, *, exit_code: int | None = None) -> dict[str, Any]:
    error: dict[str, Any] = {"type": type(exc).__name__}
    if exit_code is not None:
        error["exit_code"] = exit_code
    return error


def provenance(*, arm: str, collector: str, case: dict[str, Any], meta: dict[str, Any],
               provider_calls: int, successful: bool, wall_observed: bool,
               provider_time_observed: bool, usage_observed: bool,
               timing_scope: str = "case", batch_id: str | None = None,
               observed_case_ids: list[str] | None = None,
               actual_call: bool | None = None) -> dict[str, Any]:
    observed_case_ids = observed_case_ids or [case["id"]]
    if actual_call is None:
        actual_call = provider_calls > 0
    result: dict[str, Any] = {
        "schema_version": benchmark.MEASUREMENT_SCHEMA_VERSION,
        "kind": "actual_provider_observation",
        "collector": collector,
        "arm": arm,
        "case_id": case["id"],
        "dataset_hash": meta["dataset_hash"],
        "candidate_catalog_hash": meta["catalog_hash"],
        "request_hash": meta["request_hashes"][case["id"]],
        "request_identity": meta["request_identities"][case["id"]],
        "template_hash": meta["template_hash"],
        "recorded_at_utc": utc_now(),
        "measurement_scope": timing_scope,
        "provider_call_count": provider_calls,
        "actual_call": actual_call,
        "provider_response_observed": successful,
        "wall_time_observed": wall_observed,
        "provider_time_observed": provider_time_observed,
        "usage_observed": usage_observed,
        "observed_case_count": len(observed_case_ids),
        "observed_case_ids": observed_case_ids,
    }
    if timing_scope == "batch":
        result["batch_id"] = batch_id or "unrecorded-batch"
    return result


def null_usage() -> dict[str, Any]:
    return {
        "input_tokens": None,
        "output_tokens": None,
        "total_tokens": None,
        "included_codex_quota_tokens": None,
        "jev_payg_dollars": None,
        "reported_dollar_cost": None,
    }


def luna_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Copy only official Hermes usage fields; absent values remain null."""
    return {
        "input_tokens": raw.get("input_tokens"),
        "output_tokens": raw.get("output_tokens"),
        "total_tokens": raw.get("total_tokens"),
        "included_codex_quota_tokens": raw.get("included_codex_quota_tokens"),
        "jev_payg_dollars": None,
        "reported_dollar_cost": raw.get("estimated_cost_usd"),
    }
