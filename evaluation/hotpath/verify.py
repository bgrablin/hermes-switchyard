"""Verify retained component evidence without credentials or network calls."""
import hashlib
import json
import math
import statistics
import tarfile
from decimal import Decimal
from pathlib import Path


# Recorded provider costs, not a price estimate or a pre-call expectation.
# Bind the published accounting claims independently of recomputed timing.
MODEL = "typesafe/jev-1.13-20260917"
RECORDED_CASE_COSTS = {
    "questions-1-100": Decimal("0.000012894"),
    "questions-255-100": Decimal("0.000232386"),
    "questions-600-100": Decimal("0.000560868"),
    "questions-600-50000": Decimal("0.002132718"),
}


def planned_payloads(case):
    """Full-serialization oracle, independent of the optimized planner."""
    def payload(questions):
        return {"model": MODEL, "state": case["state"], "questions": questions,
                "provider": {"allow_fallbacks": False}}

    result, current = [], {}
    for name, question in case["questions"].items():
        trial = {**current, name: question}
        size = len(json.dumps(payload(trial), ensure_ascii=False, allow_nan=False).encode())
        if current and (len(trial) > 255 or size > 96000):
            result.append(payload(current))
            current = {}
        current[name] = question
    if current:
        result.append(payload(current))
    assert all(len(json.dumps(p, ensure_ascii=False, allow_nan=False).encode()) <= 96000 for p in result)
    return result


def verify_live(run, freeze, rows):
    assert freeze["model"] == MODEL
    cases = {case["id"]: case for case in freeze["cases"] if case["kind"] == "decide"}
    plans = {name: planned_payloads(case) for name, case in cases.items()}
    total_requests, total_cost = 0, Decimal(0)
    for row in rows:
        case = cases[row["case"]]
        plan = plans[row["case"]]
        assert type(row.get("request_count")) is int and row["request_count"] == len(plan)
        total_requests += row["request_count"]
        cost = row.get("usage", {}).get("cost")
        assert type(cost) in (int, float) and math.isfinite(cost) and cost >= 0
        assert Decimal(str(cost)) == RECORDED_CASE_COSTS[row["case"]]
        total_cost += Decimal(str(cost))
        assert set(row["answers"]) == set(case["questions"])
        for answer in row["answers"].values():
            assert set(answer) == {"noul"}
            value = answer["noul"]
            assert type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1
        if run == "live-v2":
            assert row.get("resolved_model") == MODEL
            expected_hash = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            assert row["plan_or_payload_sha256"] == expected_hash
        else:
            # v1 did not retain model or wire fields. Do not backfill them or
            # claim independent verification of those omitted observations.
            assert "resolved_model" not in row
    assert len(rows) == 24 and total_requests == 48
    assert total_cost == Decimal("0.017633196")
    return total_requests, total_cost


def verify_sources(freeze, source_root):
    """Do not apply the measured result to a different candidate or driver."""
    actual = {str(path.relative_to(source_root)): hashlib.sha256(path.read_bytes()).hexdigest()
              for path in sorted((source_root / "hermes_switchyard").rglob("*.py"))}
    assert actual == freeze["sources"]["candidate"], "candidate source drift; rerun benchmark"
    driver = source_root / "evaluation" / "hotpath" / "planning.py"
    assert hashlib.sha256(driver.read_bytes()).hexdigest() == freeze["driver_sha256"], "benchmark driver drift; rerun benchmark"


def main():
    root = Path(__file__).parent / "frozen"
    for name, expected in json.loads((root / "sha256.json").read_text()).items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
    combined_requests, combined_cost = 0, Decimal(0)
    with tarfile.open(root / "measurements.tar.gz") as archive:
        def read(name):
            return archive.extractfile(name).read().decode()

        for run in ("offline", "live", "offline-v2", "live-v2"):
            freeze = json.loads(read(run + "/freeze.json"))
            if run.endswith("v2"):
                verify_sources(freeze, Path(__file__).resolve().parents[2])
            rows = [json.loads(line) for line in read(run + "/observations.jsonl").splitlines()]
            summary = json.loads(read(run + "/summary.json"))
            expected_jobs = [(rep, freeze["cases"][idx]["id"], freeze["cases"][idx]["kind"], arm) for rep, idx, arm in freeze["jobs"]]
            actual_jobs = [(r["rep"], r["case"], r["kind"], r["arm"]) for r in rows]
            assert actual_jobs == expected_jobs, (run, "job completeness/order")
            assert not any("error_type" in r for r in rows), (run, "failed observations")
            if run.startswith("live"):
                requests, cost = verify_live(run, freeze, rows)
                combined_requests += requests
                combined_cost += cost
            for case in summary:
                subset = [r for r in rows if r["case"] == case["case"] and r["kind"] == case["kind"]]
                for arm, saved in case["arms"].items():
                    times = sorted(r["wall_ms"] for r in subset if r["arm"] == arm)
                    assert saved == {"n": len(times), "failures": 0, "median_ms": statistics.median(times), "total_ms": sum(times), "p95_ms": times[math.ceil(.95 * len(times)) - 1]}, (run, case["case"], arm)
                reduction = 100 * (1 - case["arms"]["candidate"]["median_ms"] / case["arms"]["baseline"]["median_ms"])
                assert reduction == case["median_reduction_pct"]
                if run.startswith("offline") or run == "live-v2":
                    assert len({r["plan_or_payload_sha256"] for r in subset}) == 1
                if run.startswith("offline"):
                    assert case["identical_plan_or_payload"] is True
            if run.endswith("v2"):
                assert json.loads((root / (run + "-summary.json")).read_text()) == summary
            print(f"{run}: {len(rows)} complete observations; summaries verified")
    assert combined_requests == 96 and combined_cost == Decimal("0.035266392")
    print(f"Live accounting: {combined_requests} logical requests; ${combined_cost}; no missing costs")


if __name__ == "__main__":
    main()
