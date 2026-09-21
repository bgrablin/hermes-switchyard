#!/usr/bin/env python3
"""Build a claim-gated live Switchyard value report against its local baseline."""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
from typing import Any, cast

import benchmark


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _arm_summary(
    cases: list[dict[str, Any]],
    rows: dict[str, dict[str, Any]],
    meta: dict[str, Any],
    *,
    mode: str,
) -> dict[str, Any]:
    scored = [benchmark.outcome(case, rows[case["id"]]) for case in cases]
    strict = [item for item in scored if item["top1_defined"]]
    no_fit = [
        item for case, item in zip(cases, scored)
        if case["expected"]["label_type"] == "no_fit"
    ]
    positive = [
        item for case, item in zip(cases, scored)
        if case["expected"]["label_type"] != "no_fit"
    ]
    required = [
        item for case, item in zip(cases, scored)
        if case["expected"]["label_type"] == "required_set"
    ]
    ambiguous = [
        item for case, item in zip(cases, scored)
        if case["expected"]["label_type"] == "ambiguous"
    ]
    targets = [item for item in scored if item["required_or_acceptable_skills"]]
    top1_correct = sum(bool(item["top1_correct"]) for item in strict)
    positive_misses = sum(bool(item["positive_miss"]) for item in positive)
    positive_abstentions = sum(bool(item["positive_abstention"]) for item in positive)
    return {
        "case_count": len(scored),
        "strict_top1": {
            "cases": len(strict),
            "correct": top1_correct,
            "accuracy": top1_correct / len(strict) if strict else None,
        },
        "no_fit": {
            "cases": len(no_fit),
            "false_positives": sum(bool(item["no_fit_false_positive"]) for item in no_fit),
            "false_positive_rate": (
                sum(bool(item["no_fit_false_positive"]) for item in no_fit) / len(no_fit)
                if no_fit else None
            ),
        },
        "positive_miss": {
            "cases": positive_misses,
            "rate": positive_misses / len(positive) if positive else None,
        },
        "positive_abstention": {
            "cases": positive_abstentions,
            "rate": positive_abstentions / len(positive) if positive else None,
        },
        "required_set": {
            "cases": len(required),
            "complete": sum(bool(item["required_set_complete"]) for item in required),
            "mean_coverage": (
                sum(float(item["coverage"]) for item in required if item["coverage"] is not None) / len(required)
                if required else None
            ),
        },
        "ambiguous": {
            "cases": len(ambiguous),
            "accepted": sum(bool(item["ambiguous_hit"]) for item in ambiguous),
            "accepted_rate": (
                sum(bool(item["ambiguous_hit"]) for item in ambiguous) / len(ambiguous)
                if ambiguous else None
            ),
        },
        "candidate_coverage": {
            "target_cases": len(targets),
            "covered_cases": sum(
                set(item["required_or_acceptable_skills"]) <= set(meta["names"])
                for item in targets
            ),
            "rate": (
                sum(set(item["required_or_acceptable_skills"]) <= set(meta["names"]) for item in targets)
                / len(targets)
                if targets else None
            ),
        },
        "timing": benchmark.timing_summary(list(rows.values()), mode),
        "usage": benchmark.usage_summary(list(rows.values()), mode),
    }


def report_hash(report: dict[str, Any]) -> str:
    value = copy.deepcopy(report)
    value.pop("report_hash", None)
    return benchmark.digest(value)


def build_report(
    *,
    plugin_path: Path,
    switchyard_input: Path,
    public_synthetic_ack: bool,
    max_requests: int,
) -> dict[str, Any]:
    require(public_synthetic_ack is True, "public_synthetic_ack_required")
    require(type(max_requests) is int and 1 <= max_requests <= 60, "max_requests_must_be_1_to_60")
    plugin_path = plugin_path.expanduser().resolve()
    book, meta = benchmark.load_book()
    routing, _client, source = benchmark.import_plugin(plugin_path)
    require(source == benchmark.source_hashes(plugin_path), "source_changed_during_run")
    cases = book["heldout_fixtures"]
    switchyard = benchmark.ingest(
        switchyard_input.expanduser().resolve(),
        "switchyard",
        cases,
        meta,
        live=True,
        max_requests=max_requests,
    )
    lexical = {
        case["id"]: benchmark.run_lexical_case(case, meta, source)
        for case in cases
    }
    for case in cases:
        benchmark.validate_record(lexical[case["id"]], "lexical", case, meta, live=False)
    switch_summary = _arm_summary(cases, switchyard, meta, mode="live")
    lexical_summary = _arm_summary(cases, lexical, meta, mode="offline")
    collector_hashes = source.get("collector_hashes")
    require(isinstance(collector_hashes, dict), "collector_source_hashes_missing")
    collector_hashes = cast(dict[str, str], collector_hashes)
    require(switch_summary["timing"]["provider_timing_claimable"] is True, "provider_timing_not_claimable")
    provider_calls = sum(row["provider_call_count"] for row in switchyard.values())
    require(provider_calls <= max_requests, "provider_request_cap_exceeded")
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "ok",
        "claim_level": "live_selector_value",
        "claim_scope": (
            "Complete live Jev selector receipts compared with the deterministic local lexical baseline "
            "on the same frozen public-synthetic heldout cases."
        ),
        "dataset_hash": meta["dataset_hash"],
        "candidate_catalog_hash": meta["catalog_hash"],
        "heldout_case_count": len(cases),
        "label_frozen": True,
        "plugin_source_hash": source["plugin"],
        "collector_source_hash": collector_hashes["switchyard"],
        "report_source_hash": benchmark.file_digest(Path(__file__)),
        "provider_models": sorted({row["model"] for row in switchyard.values()}),
        "providers": sorted({row["provider"] for row in switchyard.values()}),
        "provider_call_count": provider_calls,
        "request_cap": max_requests,
        "lexical": lexical_summary,
        "switchyard": switch_summary,
        "paired_deltas": {
            "strict_top1_accuracy": (
                switch_summary["strict_top1"]["accuracy"] - lexical_summary["strict_top1"]["accuracy"]
            ),
            "positive_miss_rate": (
                switch_summary["positive_miss"]["rate"] - lexical_summary["positive_miss"]["rate"]
            ),
            "no_fit_false_positive_rate": (
                switch_summary["no_fit"]["false_positive_rate"]
                - lexical_summary["no_fit"]["false_positive_rate"]
            ),
        },
        "limitations": [
            "This is a selector benchmark, not proof of whole-agent task improvement.",
            "The deterministic lexical arm is a local baseline, not another hosted model.",
            "Strict top-1, no-fit, required-set, and ambiguity metrics remain separate; no aggregate score is emitted.",
            "Provider confidence is uncalibrated and is not reported as correctness probability.",
        ],
    }
    report["report_hash"] = report_hash(report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-path", type=Path, required=True)
    parser.add_argument("--switchyard-input", type=Path, required=True)
    parser.add_argument("--public-synthetic-ack", action="store_true")
    parser.add_argument("--max-requests", type=int, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = build_report(
            plugin_path=args.plugin_path,
            switchyard_input=args.switchyard_input,
            public_synthetic_ack=args.public_synthetic_ack,
            max_requests=args.max_requests,
        )
    except (ValueError, benchmark.BenchmarkRefusal, OSError, json.JSONDecodeError) as exc:
        report = {"status": "refused", "reason": type(exc).__name__, "detail": str(exc)}
        rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(rendered, encoding="utf-8")
        print(rendered, end="")
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
