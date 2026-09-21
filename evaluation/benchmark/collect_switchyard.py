#!/usr/bin/env python3
"""Collect one real Switchyard skill-selection observation per public case.

This collector is deliberately opt-in. It never supplies a synthetic client or
response. The caller must provide the reviewed plugin source and Hermes' native
secret scope at execution time.
"""
from __future__ import annotations

import argparse
import sys
import time
import os
from pathlib import Path
from typing import Any

import benchmark
from collector_common import append_record, load_payload, null_usage, provenance, safe_error, validated_records

CASE_COUNT = 24
_SECRET_SCOPE_TOKEN: Any = None


def _hydrate_runtime_secret_scope() -> None:
    """Hydrate the active Hermes profile before building its scoped secret map."""
    global _SECRET_SCOPE_TOKEN
    if _SECRET_SCOPE_TOKEN is not None:
        return
    try:
        from agent.secret_scope import build_profile_secret_scope, set_secret_scope
        from hermes_constants import get_hermes_home
        home = Path(os.environ.get("HERMES_HOME") or get_hermes_home()).expanduser().resolve()
        try:
            from hermes_cli.env_loader import hydrate_profile_secret_sources
        except ImportError:
            # Hermes 0.19.0 ships secret_scope without this hydrate helper.
            hydrate_profile_secret_sources = None
        if hydrate_profile_secret_sources is not None:
            hydrate_profile_secret_sources(home)
        scope = build_profile_secret_scope(home)
        if not isinstance(scope, dict):
            scope = {}
        if not scope.get("OPENROUTER_API_KEY"):
            # Compatible last resort on hosts that already exported the key into the process.
            env_key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
            if env_key:
                scope = dict(scope)
                scope["OPENROUTER_API_KEY"] = env_key
        if not scope.get("OPENROUTER_API_KEY"):
            raise RuntimeError("openrouter_key_unavailable_in_supported_scope")
        _SECRET_SCOPE_TOKEN = set_secret_scope(scope)
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("hermes_secret_scope_unavailable") from None


def _reset_runtime_secret_scope() -> None:
    global _SECRET_SCOPE_TOKEN
    if _SECRET_SCOPE_TOKEN is None:
        return
    try:
        from agent.secret_scope import reset_secret_scope
        reset_secret_scope(_SECRET_SCOPE_TOKEN)
    finally:
        _SECRET_SCOPE_TOKEN = None


def _runtime_key() -> str:
    """Resolve the key through Hermes' supported secret scope, never print it."""
    _hydrate_runtime_secret_scope()
    try:
        from agent.secret_scope import get_secret
        value = get_secret("OPENROUTER_API_KEY")
    except Exception:  # noqa: BLE001 - do not expose scope details
        raise RuntimeError("hermes_secret_scope_unavailable") from None
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError("openrouter_key_unavailable_in_supported_scope")
    return value.strip()


def _collect(args: argparse.Namespace) -> int:
    if args.live is not True or args.public_synthetic_ack is not True or args.max_requests != CASE_COUNT:
        raise ValueError("requires --live --public-synthetic-ack --max-requests 24")
    book, meta = benchmark.load_book()
    cases = book["heldout_fixtures"]
    if len(cases) != CASE_COUNT:
        raise ValueError("heldout_case_count_is_not_24")
    plugin_path = Path(args.plugin_path).expanduser().resolve()
    routing, client_module, source = benchmark.import_plugin(plugin_path)
    if source != benchmark.source_hashes(plugin_path):
        raise RuntimeError("plugin_source_changed_before_collection")

    class CountingDecisionClient(client_module.DecisionClient):
        def __init__(self, **kwargs: Any):
            self.provider_calls = 0
            self.provider_responses = 0
            super().__init__(**kwargs)

        def _post(self, payload: dict) -> dict:
            self.provider_calls += 1
            response = super()._post(payload)
            self.provider_responses += 1
            return response

    client = CountingDecisionClient(api_key=_runtime_key(), model=client_module.EXPECTED_MODEL)
    output_path = Path(args.output)
    payload = load_payload(output_path, "switchyard", meta, resume=args.resume)
    records = validated_records(payload, "switchyard", cases, meta, expected_source_hash=source["plugin"])
    request_count = sum(int(row.get("provider_call_count", 0) or 0) for row in records.values())
    for case in cases:
        if case["id"] in records:
            continue
        if request_count + 1 > args.max_requests:
            raise RuntimeError("request_cap_reached_before_all_cases")
        started = time.perf_counter()
        calls_before = client.provider_calls
        responses_before = client.provider_responses
        successful = False
        provider_ms: float | None = None
        error: dict[str, Any] | None = None
        try:
            # This is the real routing path. Only the public task and catalog enter it.
            result = routing.select_skill(
                task=case["task"],
                candidates=meta["catalog"],
                client=client,
                public_or_sanitized_data_ack=args.public_synthetic_ack,
            )
            resolved_model = result.get("model")
            if resolved_model not in benchmark.JEV_MODELS:
                raise ValueError("provider_model_not_in_allowed_set")
            result = {**result, "provider": "openrouter", "reasoning": None,
                      "selected_skills": [result["selected"]] if result.get("selected") else []}
            provider_ms = result.get("latency_ms")
            successful = True
        except Exception as exc:  # noqa: BLE001 - retain a bounded failed measurement
            result = {"status": "failed", "selected": None, "selected_skills": [],
                      "abstention_reason": None, "model": client_module.EXPECTED_MODEL,
                      "provider": "openrouter", "reasoning": None, "usage": {}}
            error = safe_error(exc)
            result["model"] = client_module.EXPECTED_MODEL
        provider_calls = client.provider_calls - calls_before
        provider_response_observed = client.provider_responses > responses_before
        wall_ms = round((time.perf_counter() - started) * 1000, 3)
        usage = result.get("usage") if successful and isinstance(result.get("usage"), dict) else null_usage()
        row = benchmark.record(
            case, meta, "switchyard", {**result, "usage": usage}, source["plugin"],
            wall_ms=wall_ms, provider_ms=provider_ms, selector_calls=1, provider_calls=provider_calls,
            coordination_calls=1, simulated=False, timing_status="actual_provider_observation" if successful else "provider_error",
            actual_call=provider_calls > 0, measurement_status="ok" if successful else "failed",
            measurement_provenance=provenance(
                arm="switchyard", collector=benchmark.LIVE_COLLECTORS["switchyard"], case=case, meta=meta,
                provider_calls=provider_calls, successful=successful, wall_observed=True,
                provider_time_observed=successful and provider_ms is not None, usage_observed=successful,
                provider_response_observed=provider_response_observed,
            ), error=error,
        )
        try:
            benchmark.validate_record(row, "switchyard", case, meta, live=True)
        except Exception as exc:
            if not successful:
                raise
            # A malformed provider response is a failed measurement, not a selection.
            error = safe_error(exc)
            row = benchmark.record(
                case,
                meta,
                "switchyard",
                {"status": "failed", "selected": None, "selected_skills": [],
                 "model": client_module.EXPECTED_MODEL, "provider": "openrouter", "usage": null_usage()},
                source["plugin"],
                wall_ms=wall_ms,
                provider_ms=None,
                selector_calls=1,
                provider_calls=provider_calls,
                coordination_calls=1,
                simulated=False,
                timing_status="provider_error",
                actual_call=provider_calls > 0,
                measurement_status="failed",
                measurement_provenance=provenance(
                    arm="switchyard", collector=benchmark.LIVE_COLLECTORS["switchyard"], case=case, meta=meta,
                    provider_calls=provider_calls, successful=False, wall_observed=True,
                    provider_time_observed=False, usage_observed=False,
                    provider_response_observed=True,
                ),
                error=error,
            )
            benchmark.validate_record(row, "switchyard", case, meta, live=True)
        append_record(payload, row, output_path)
        records[case["id"]] = row
        request_count += provider_calls
    if source != benchmark.source_hashes(plugin_path):
        raise RuntimeError("plugin_source_changed_during_collection")
    return 0


def collect(args: argparse.Namespace) -> int:
    try:
        return _collect(args)
    finally:
        _reset_runtime_secret_scope()


def main() -> int:
    parser = argparse.ArgumentParser(description="Collect actual Switchyard measurements; never run by default")
    parser.add_argument("--live", action="store_true", required=True, help="required execution gate")
    parser.add_argument("--public-synthetic-ack", action="store_true", required=True)
    parser.add_argument("--max-requests", type=int, required=True, help="must be exactly 24")
    parser.add_argument("--plugin-path", required=True, help="reviewed source path containing hermes_switchyard/")
    parser.add_argument("--output", type=Path, default=Path("switchyard.records.json"))
    parser.add_argument("--resume", action="store_true", help="resume only a matching partial receipt")
    args = parser.parse_args()
    try:
        return collect(args)
    except Exception as exc:  # noqa: BLE001 - no provider/error text is printed
        print(f"switchyard collection refused: {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
