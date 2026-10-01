"""Export audited synthetic measurements without operational transcripts."""

import argparse
import hashlib
import json
import math
import statistics
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def summarize(run, grades, output):
    freeze = json.loads((run / "freeze.json").read_text())
    rows = [json.loads(line) for line in (run / "raw.jsonl").read_text().splitlines()]
    expected = {(f"{case}-r{rep}", arm) for rep, case, arm in freeze["order"]}
    observed = {(row["id"], row["arm"]) for row in rows}
    assert len(rows) == len(expected) and observed == expected, "incomplete or duplicate run"
    assert set(grades) == {f"{i}:{a}" for i, a in expected}, "every answer needs review"
    after = json.loads((run / "runtime-after.json").read_text())
    assert after["unchanged"], "runtime changed during measurement"
    for name, sha in freeze["scripts"].items():
        assert digest(run / "scripts" / name) == sha, name
    for name, sha in freeze["hashes"].items():
        assert digest(run / name) == sha, name
    summaries = {}
    public_rows = []
    for row in rows:
        grade = grades[f'{row["id"]}:{row["arm"]}']
        assert isinstance(grade["correct"], bool)
        assert isinstance(grade["source_verified"], bool)
        final = row.get("final") or ""
        try:
            parsed = json.loads(final)
            strict_json = isinstance(parsed, dict) and set(parsed) == {"answer", "evidence"}
        except ValueError:
            strict_json = False
        public_rows.append({
            "id": row["id"], "case_id": row["case_id"], "arm": row["arm"],
            "repeat": row["repeat"], "wall_ms": row["wall_ms"],
            "completed": row["completed"], "main_calls": len(row["wire"]),
            "jev_calls": len(row["jev"]),
            "source_requests": sum(bool(j["source_request"]) for j in row["jev"]),
            "main_efforts": [w["effort"] for w in row["wire"]],
            "scope_violations": len(row["scope_violations"]),
            "strict_json": strict_json, "grade": grade,
            "final_sha256": hashlib.sha256(final.encode()).hexdigest(),
        })
    for arm in ["off", "release", "main", "toggle", "candidate"]:
        selected = [r for r in public_rows if r["arm"] == arm]
        times = sorted(r["wall_ms"] for r in selected)
        index = .95 * (len(times) - 1)
        lower = math.floor(index)
        linear = times[lower] + (index - lower) * (times[math.ceil(index)] - times[lower])
        summaries[arm] = {
            "n": len(selected), "completed": sum(r["completed"] for r in selected),
            "correct": sum(r["completed"] and r["grade"]["correct"] and r["grade"]["source_verified"] for r in selected),
            "strict_json": sum(r["strict_json"] for r in selected),
            "median_ms": statistics.median(times), "total_ms": sum(times),
            "p95_nearest_rank_ms": times[math.ceil(.95 * len(times)) - 1],
            "p95_linear_ms": linear,
            "main_calls": sum(r["main_calls"] for r in selected),
            "jev_calls": sum(r["jev_calls"] for r in selected),
            "scope_violations": sum(r["scope_violations"] for r in selected),
        }
    candidate = summaries["candidate"]
    comparisons = {}
    for arm in ["off", "release", "main", "toggle"]:
        base = summaries[arm]
        comparisons[arm] = {key: candidate[key] / base[key] - 1 for key in [
            "median_ms", "total_ms", "p95_nearest_rank_ms", "p95_linear_ms"
        ]}
    by_pair = {(r["id"], r["arm"]): r for r in public_rows}
    new_errors = []
    for row in public_rows:
        if row["arm"] != "candidate":
            continue
        candidate_ok = row["completed"] and row["strict_json"] and row["grade"]["correct"] and row["grade"]["source_verified"]
        if not candidate_ok:
            for arm in ["off", "main", "toggle"]:
                base = by_pair[row["id"], arm]
                if base["completed"] and base["strict_json"] and base["grade"]["correct"] and base["grade"]["source_verified"]:
                    new_errors.append({"id": row["id"], "control": arm})
    timing_gate = all(
        comparisons[arm]["median_ms"] <= -.1
        and comparisons[arm]["total_ms"] <= -.1
        and comparisons[arm]["p95_nearest_rank_ms"] <= .1
        and comparisons[arm]["p95_linear_ms"] <= .1
        for arm in ["off", "main"]
    )
    report = {
        "public_synthetic": True, "run": run.name, "planned_conversations": len(expected),
        "candidate_runtime": freeze["candidate_base"], "versions": freeze["versions"],
        "model": freeze["model"], "provider": freeze["provider"], "jev": freeze["jev"],
        "hermes_sha": freeze["hermes_sha"], "runtime_unchanged": after["unchanged"],
        "artifacts_sha256": {name: digest(run / name) for name in ["freeze.json", "cases.json", "raw.jsonl", "runtime-after.json"]},
        "measured_scripts": freeze["scripts"], "candidate_file_hashes": freeze["hashes"],
        "summaries": summaries, "relative_changes": comparisons,
        "new_errors": new_errors, "timing_gate_pass": timing_gate,
        "gate_pass": timing_gate and not new_errors and candidate["scope_violations"] == 0,
        "limitations": ["Eight synthetic cases, two repetitions; not a broad workload qualification.",
                        "Other evaluations were active on the host; interleaving does not eliminate timing noise.",
                        "Component-confirmation cases are reused for native efficiency; they are not a new semantic holdout."],
        "rows": public_rows,
    }
    output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in ["summaries", "relative_changes", "new_errors", "gate_pass"]}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--grades", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    summarize(args.run, json.loads(args.grades.read_text()), args.output)
