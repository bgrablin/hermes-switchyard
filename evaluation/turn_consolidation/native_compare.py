"""Frozen native same-provider routing pilot; public synthetic tasks only."""

import argparse
import hashlib
import importlib.util
import io
import json
import math
import random
import shutil
import statistics
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
spec = importlib.util.spec_from_file_location(
    "worker_driver", REPO / "evaluation/decision_quality/native_compare.py"
)
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
driver.ROOT = ROOT
fixture_tasks = json.loads((REPO / "evaluation/routing_value/tasks.json").read_text())
CASES = []
for task in fixture_tasks:
    if task["skills_dir"] != "catalogs/c25":
        continue
    if task["category"] == "hidden_fact" and len(CASES) < 10:
        CASES.append(
            {
                "id": task["id"],
                "prompt": task["prompt"],
                "expected": task["checker"]["terms"][0],
            }
        )
for task in fixture_tasks:
    if (
        task["skills_dir"] == "catalogs/c25"
        and task["category"] == "no_skill_needed"
        and len(CASES) < 12
    ):
        CASES.append(
            {
                "id": task["id"],
                "prompt": task["prompt"],
                "expected": task["checker"].get(
                    "value", task["checker"].get("expected")
                ),
            }
        )
assert len(CASES) == 12 and all(isinstance(c["expected"], str) for c in CASES)


def digest(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def source_hashes():
    paths = [
        Path(__file__),
        ROOT / "pilot.py",
        ROOT / "native_worker.py",
        REPO / "evaluation/decision_quality/native_compare.py",
        REPO / "evaluation/routing_value/tasks.json",
    ]
    return {p.relative_to(REPO).as_posix(): digest(p) for p in paths}


def fixture_hashes(out):
    trees = {}
    for arm in ["off", "release", "main", "candidate"]:
        catalog = (
            out
            / ("main" if arm == "off" else arm)
            / "evaluation/routing_value/catalogs/c25/skills"
        )
        files = sorted(p for p in catalog.rglob("*") if p.is_file())
        if not files:
            raise ValueError("fixture tree missing for " + arm)
        trees[arm] = {p.relative_to(catalog).as_posix(): digest(p) for p in files}
    return trees


def runtime_hashes():
    root = Path("/home/brian/.hermes/hermes-agent")
    names = subprocess.check_output(
        ["git", "ls-files", "*.py"], cwd=root, text=True
    ).splitlines()
    return {name: digest(root / name) for name in names if (root / name).is_file()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    revisions = {}
    for arm, ref in [
        ("release", "v0.5.6"),
        ("main", "a0fd0ad670bec53a72d2fa2a6ef851382f5e46c0"),
    ]:
        revisions[arm] = subprocess.check_output(
            ["git", "rev-parse", ref], cwd=REPO, text=True
        ).strip()
        data = subprocess.check_output(["git", "archive", ref], cwd=REPO)
        dest = out / arm
        dest.mkdir()
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            tar.extractall(dest, filter="data")
    shutil.copytree(out / "main", out / "candidate")
    shutil.copytree(
        REPO / "evaluation/routing_value",
        out / "release/evaluation/routing_value",
        dirs_exist_ok=True,
    )
    shutil.copy2(
        ROOT / "pilot.py", out / "candidate/hermes_switchyard/routing_pilot.py"
    )
    (out / "candidate/__init__.py").write_text(
        "from .hermes_switchyard import register as _register\n"
        "from .hermes_switchyard.routing_pilot import ContextProxy\n"
        "def register(ctx):\n    return _register(ContextProxy(ctx))\n"
    )
    before = runtime_hashes()
    (out / "runtime-before.json").write_text(json.dumps(before, sort_keys=True))
    freeze = {
        "cases": CASES,
        "repeats": 2,
        "arms": ["off", "release", "main", "candidate"],
        "revisions": revisions,
        "source_model": "gpt-6-sol",
        "candidate_model": "gpt-6-sol",
        "provider": "openai-codex",
        "jev": "typesafe/jev-1.13-20260917",
        "jev_provider": "openrouter",
        "scope": "Synthetic hidden-fact skill tasks; read-only skills_list and skill_view; no memory or previous conversation.",
        "acceptance": "Candidate correctness no lower than every control; median and total latency at least 5% below disabled and main; no unsupported provider mutation; a shared live Jev decision must be consumed. Not sufficient for general-release qualification.",
        "routing_deadline_ms": 400,
        "concurrent_provider_calls": 1,
        "files": source_hashes(),
        "fixture_trees": fixture_hashes(out),
        "runtime_revision": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd="/home/brian/.hermes/hermes-agent",
            text=True,
        ).strip(),
    }
    (out / "freeze.json").write_text(json.dumps(freeze, indent=2))
    workers = {}
    rows = []
    try:
        for arm in freeze["arms"]:
            workers[arm] = driver.Worker(
                arm, out, out / ("main" if arm == "off" else arm)
            )
        jobs = [(r, c) for r in range(2) for c in CASES]
        rng = random.Random(873811)
        rng.shuffle(jobs)
        with (out / "raw.jsonl").open("x") as handle:
            for repeat, case in jobs:
                order = list(workers)
                rng.shuffle(order)
                for arm in order:
                    row = workers[arm].ask(
                        {"id": case["id"] + "-" + str(repeat), "prompt": case["prompt"]}
                    )
                    row["correct"] = (
                        str(row.get("final") or "").strip() == case["expected"]
                    )
                    row["expected"] = case["expected"]
                    row["repeat"] = repeat
                    rows.append(row)
                    handle.write(json.dumps(row) + "\n")
                    handle.flush()
                    print(
                        json.dumps(
                            {
                                "arm": arm,
                                "id": row["id"],
                                "correct": row["correct"],
                                "wall_ms": row["wall_ms"],
                                "wire": row["wire"],
                                "shared": bool(row.get("route")),
                            }
                        ),
                        flush=True,
                    )
    finally:
        for worker in workers.values():
            worker.close()
    after = runtime_hashes()
    (out / "runtime-after.json").write_text(json.dumps(after, sort_keys=True))
    summary = {"runtime_unchanged": before == after, "arms": {}}
    for arm in freeze["arms"]:
        rs = [r for r in rows if r["arm"] == arm]
        values = sorted(r["wall_ms"] for r in rs)
        summary["arms"][arm] = {
            "n": len(rs),
            "correct": sum(r["correct"] for r in rs),
            "median_ms": statistics.median(values),
            "total_ms": sum(values),
            "p95_ms": values[math.ceil(0.95 * len(values)) - 1],
            "main_dispatches": sum(len(r["wire"]) for r in rs),
            "jev_calls": sum(len(r["jev"]) for r in rs),
            "shared": sum(bool(r.get("route")) for r in rs),
            "wire_models": sorted({w["model"] for r in rs for w in r["wire"]}),
        }
    (out / "summary.json").write_text(json.dumps(summary, indent=2))
    print("SUMMARY " + json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
