#!/usr/bin/env python3
"""Collect one direct Luna baseline request per public case via Hermes CLI.

The CLI one-shot route is the supported public boundary used here. It reads the
provider credential through Hermes itself, writes the official --usage-file
receipt, and never falls back to another provider. This file is preparation only;
the operator must explicitly run it.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import benchmark
from collector_common import append_record, load_payload, luna_usage, null_usage, provenance, safe_error, validated_records

CASE_COUNT = 24


def _api_call_count(usage: dict[str, Any]) -> int:
    total = usage.get("total_including_auxiliary")
    if isinstance(total, dict) and type(total.get("api_calls")) is int:
        return total["api_calls"]
    return usage["api_calls"] if type(usage.get("api_calls")) is int else 0


def _startup_probe(command: str) -> float:
    started = time.perf_counter()
    completed = subprocess.run([command, "--version"], capture_output=True, text=True, check=False)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    if completed.returncode != 0:
        raise RuntimeError("hermes_version_probe_failed")
    return elapsed


def _parse_success(
    stdout: str,
    usage: dict[str, Any],
    *,
    max_provider_calls: int | None = None,
) -> dict[str, Any]:
    if usage.get("completed") is not True or usage.get("partial") is True or usage.get("failed") is True:
        raise ValueError("official_usage_did_not_confirm_completed_turn")
    reported_model = usage.get("model")
    reported_models = {benchmark.LUNA_MODEL, benchmark.LUNA_MODEL.split("/", 1)[1]}
    if reported_model not in reported_models or usage.get("provider") != "openai-codex":
        raise ValueError("official_usage_route_mismatch")
    provider_calls = _api_call_count(usage)
    if provider_calls <= 0:
        raise ValueError("official_usage_api_call_count_missing")
    if max_provider_calls is not None and provider_calls > max_provider_calls:
        raise ValueError("official_usage_request_cap_exceeded")
    parsed = json.loads(stdout)
    if not isinstance(parsed, dict):
        raise ValueError("luna_response_not_object")
    status = parsed.get("status")
    selected = parsed.get("selected")
    skills = parsed.get("selected_skills")
    if status not in {"selected", "abstained"} or not isinstance(skills, list):
        raise ValueError("luna_response_schema_invalid")
    skills = list(skills)
    if any(type(skill) is not str for skill in skills) or len(set(skills)) != len(skills):
        raise ValueError("luna_response_selection_invalid")
    if status == "selected" and selected is not None and not skills:
        skills = [selected]
    elif status == "selected" and (selected is None and not skills or selected is not None and selected != skills[0]):
        raise ValueError("luna_response_selection_invalid")
    if status == "abstained" and (selected is not None or skills):
        raise ValueError("luna_response_status_invalid")
    return {
        "status": status,
        "selected": selected,
        "selected_skills": skills,
        "abstention_reason": parsed.get("abstention_reason"),
        "model": benchmark.LUNA_MODEL,
        "provider": usage["provider"],
        "reasoning": "max",
        "usage": luna_usage(usage),
        "provider_calls": provider_calls,
    }


_SAFE_RESPONSE_FIELDS = ("status", "selected", "selected_skills", "abstention_reason")
_SAFE_USAGE_FIELDS = (
    "model", "provider", "api_calls", "total_including_auxiliary", "completed", "partial", "failed",
    "turn_exit_reason",
)


def _sanitize_luna_response_excerpt(stdout: str) -> dict[str, Any]:
    """Keep only bounded response-contract fields from a failed public response."""
    try:
        parsed = json.loads(stdout)
    except (TypeError, ValueError):
        return {"json_valid": False}
    if not isinstance(parsed, dict):
        return {"json_valid": True, "response_type": type(parsed).__name__}
    fields_present = sorted(field for field in _SAFE_RESPONSE_FIELDS if field in parsed)
    excerpt: dict[str, Any] = {"json_valid": True, "fields_present": fields_present}
    for field in fields_present:
        value = parsed[field]
        if field == "selected_skills":
            if isinstance(value, list):
                excerpt[field] = [item[:128] for item in value[:32] if isinstance(item, str)]
            else:
                excerpt[field] = type(value).__name__
        elif value is None or isinstance(value, str):
            excerpt[field] = value[:512] if isinstance(value, str) else None
        else:
            excerpt[field] = type(value).__name__
    return excerpt


def _usage_receipt_excerpt(usage: dict[str, Any]) -> dict[str, Any] | None:
    """Retain route/completion facts from Hermes usage without session or raw fields."""
    if not isinstance(usage, dict):
        return None
    excerpt: dict[str, Any] = {}
    for field in _SAFE_USAGE_FIELDS:
        if field not in usage:
            continue
        value = usage[field]
        if field == "total_including_auxiliary" and isinstance(value, dict):
            api_calls = value.get("api_calls")
            if type(api_calls) is int:
                excerpt[field] = {"api_calls": api_calls}
        elif isinstance(value, str):
            excerpt[field] = value[:256]
        elif type(value) in (bool, int, float) or value is None:
            excerpt[field] = value
    return excerpt or None


def _read_usage_receipt(path: Path) -> dict[str, Any]:
    """Read a completed-or-partial Hermes usage receipt without exposing raw errors."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _subprocess_output_text(value: Any) -> str:
    """Normalize TimeoutExpired output without retaining stderr or arbitrary objects."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value if isinstance(value, str) else ""


def _luna_failure_error(
    exc: BaseException, *, stdout: str, usage: dict[str, Any], exit_code: int | None,
) -> dict[str, Any]:
    """Add exact collector cause and safe response/usage excerpts to a failed row."""
    error = safe_error(exc, exit_code=exit_code)
    if isinstance(exc, ValueError):
        error["cause"] = str(exc)[:200]
    error["response_excerpt"] = _sanitize_luna_response_excerpt(stdout)
    usage_excerpt = _usage_receipt_excerpt(usage)
    if usage_excerpt is not None:
        error["usage_receipt"] = usage_excerpt
    return error


def collect(args: argparse.Namespace) -> int:
    if args.live is not True or args.public_synthetic_ack is not True or args.max_requests != CASE_COUNT:
        raise ValueError("requires --live --public-synthetic-ack --max-requests 24")
    book, meta = benchmark.load_book()
    cases = book["heldout_fixtures"]
    if len(cases) != CASE_COUNT:
        raise ValueError("heldout_case_count_is_not_24")
    if benchmark.file_digest(benchmark.PROMPT) != meta["template_hash"]:
        raise RuntimeError("prompt_source_changed_before_collection")
    output_path = Path(args.output)
    payload = load_payload(output_path, "luna", meta, resume=args.resume)
    records = validated_records(payload, "luna", cases, meta, expected_source_hash=benchmark.file_digest(benchmark.PROMPT))
    startup_probe_ms = _startup_probe(args.hermes_command)
    payload["startup_overhead_probe_ms"] = startup_probe_ms
    payload["startup_overhead_note"] = "hermes --version process probe; not provider latency and not added to per-case wall time"
    request_count = sum(int(row.get("provider_call_count", 0) or 0) for row in records.values())
    for case in cases:
        if case["id"] in records:
            continue
        if request_count >= args.max_requests:
            raise RuntimeError("request_cap_reached_before_all_cases")
        prompt = benchmark.render_luna_prompt(case, meta)
        usage_path = output_path.parent / f".{output_path.stem}.{case['id']}.usage.json"
        usage_path.unlink(missing_ok=True)
        remaining_requests = args.max_requests - request_count
        started = time.perf_counter()
        successful = False
        usage: dict[str, Any] = {}
        provider_calls = 0
        error: dict[str, Any] | None = None
        result: dict[str, Any]
        stdout = ""
        exit_code: int | None = None
        try:
            command = [
                args.hermes_command, "--safe-mode", "--ignore-user-config", "--ignore-rules",
                "-t", "context_engine", "-m", benchmark.LUNA_MODEL, "--provider", "openai-codex",
                "--reasoning", "max", "--usage-file", str(usage_path), "-z", prompt,
            ]
            completed = subprocess.run(command, capture_output=True, text=True, check=False, timeout=args.timeout_seconds)
            stdout = completed.stdout
            exit_code = completed.returncode
            if usage_path.exists():
                usage = _read_usage_receipt(usage_path)
            result = _parse_success(
                completed.stdout,
                usage,
                max_provider_calls=remaining_requests,
            ) if completed.returncode == 0 else (_ for _ in ()).throw(RuntimeError("hermes_oneshot_failed"))
            provider_calls = result.pop("provider_calls")
            successful = True
        except Exception as exc:  # noqa: BLE001 - retain a bounded failed measurement
            if isinstance(exc, subprocess.TimeoutExpired):
                stdout = _subprocess_output_text(exc.stdout)
            if not usage and usage_path.exists():
                usage = _read_usage_receipt(usage_path)
            result = {
                "status": "failed", "selected": None, "selected_skills": [], "abstention_reason": None,
                "model": benchmark.LUNA_MODEL, "provider": "openai-codex", "reasoning": "max", "usage": null_usage(),
            }
            error = _luna_failure_error(exc, stdout=stdout, usage=usage, exit_code=exit_code)
            provider_calls = _api_call_count(usage)
        finally:
            usage_path.unlink(missing_ok=True)
        wall_ms = round((time.perf_counter() - started) * 1000, 3)
        row = benchmark.record(
            case, meta, "luna", result, benchmark.file_digest(benchmark.PROMPT),
            wall_ms=wall_ms, provider_ms=None, selector_calls=0, provider_calls=provider_calls,
            coordination_calls=0, simulated=False, timing_status="actual_end_to_end_process" if successful else "provider_error",
            actual_call=provider_calls > 0, measurement_status="ok" if successful else "failed",
            measurement_provenance=provenance(
                arm="luna", collector=benchmark.LIVE_COLLECTORS["luna"], case=case, meta=meta,
                provider_calls=provider_calls, successful=successful, wall_observed=True,
                provider_time_observed=False, usage_observed=successful,
                provider_response_observed=bool(stdout.strip()),
            ), error=error,
        )
        try:
            benchmark.validate_record(row, "luna", case, meta, live=True)
        except Exception as exc:
            if not successful:
                raise
            # A malformed model response is a failed measurement, not a selection.
            error = _luna_failure_error(exc, stdout=stdout, usage=usage, exit_code=exit_code)
            row = benchmark.record(
                case,
                meta,
                "luna",
                {"status": "failed", "selected": None, "selected_skills": [],
                 "model": benchmark.LUNA_MODEL, "provider": "openai-codex", "reasoning": "max", "usage": null_usage()},
                benchmark.file_digest(benchmark.PROMPT),
                wall_ms=wall_ms,
                provider_ms=None,
                selector_calls=0,
                provider_calls=provider_calls,
                coordination_calls=0,
                simulated=False,
                timing_status="provider_error",
                actual_call=provider_calls > 0,
                measurement_status="failed",
                measurement_provenance=provenance(
                    arm="luna", collector=benchmark.LIVE_COLLECTORS["luna"], case=case, meta=meta,
                    provider_calls=provider_calls, successful=False, wall_observed=True,
                    provider_time_observed=False, usage_observed=False,
                    provider_response_observed=True,
                ),
                error=error,
            )
            benchmark.validate_record(row, "luna", case, meta, live=True)
        append_record(payload, row, output_path)
        records[case["id"]] = row
        request_count += provider_calls
        if request_count > args.max_requests:
            raise RuntimeError("request_cap_exceeded_by_provider_receipt")
    if benchmark.file_digest(benchmark.PROMPT) != meta["template_hash"]:
        raise RuntimeError("prompt_source_changed_during_collection")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect actual direct Luna measurements; never run by default")
    parser.add_argument("--live", action="store_true", required=True, help="required execution gate")
    parser.add_argument("--public-synthetic-ack", action="store_true", required=True)
    parser.add_argument("--max-requests", type=int, required=True, help="must be exactly 24")
    parser.add_argument("--output", type=Path, default=Path("luna.records.json"))
    parser.add_argument("--hermes-command", default="hermes", help="supported Hermes executable, resolved by PATH")
    parser.add_argument("--timeout-seconds", type=float, default=900.0)
    parser.add_argument("--resume", action="store_true", help="resume only a matching partial receipt")
    args = parser.parse_args()
    try:
        return collect(args)
    except Exception as exc:  # noqa: BLE001 - do not print provider/auth details
        print(f"luna collection refused: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
