"""Frozen component benchmark. No whole-conversation or routing-quality claim.

Pass two source roots and a new output directory. --live additionally exercises
the fixed OpenRouter route using credentials from the supplied Hermes profile.
"""
import argparse
import hashlib
import importlib
import json
import math
import random
import statistics
import sys
import time
import types
from pathlib import Path


def load(root, name):
    package = types.ModuleType(name)
    package.__path__ = [str(root / "hermes_switchyard")]
    sys.modules[name] = package
    return (importlib.import_module(name + ".routing"), importlib.import_module(name + ".client"))


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def sources(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted((root / "hermes_switchyard").rglob("*.py"))}


def cases():
    return [{"id": f"skills-{n}", "kind": kind,
             "candidates": [{"name": f"public-skill-{i}", "description": "Synthetic procedure " + "é\\\n" * 20} for i in range(n)],
             "task": "Find the public synthetic procedure."}
            for n in (25, 600) for kind in ("single", "multi")] + [
        {"id": f"questions-{n}-{size}", "kind": "decide", "state": "public synthetic state " + "x" * size,
         "questions": {f"q{i}": {"type": "noul", "instructions": f"Does the state contain the word synthetic? Question {i}."} for i in range(n)}}
        for n, size in ((1, 100), (255, 100), (600, 100), (600, 50000))]


def run_case(modules, case, *, live_key=None):
    routing, client = modules
    payloads = []

    def transport(payload):
        payloads.append(payload)
        return {"model": payload["model"], "answers": {q: {"noul": 0.5} for q in payload["questions"]}, "usage": {"input_tokens": len(payload["questions"])}}

    if case["kind"] == "single":
        result = routing._skill_chunks(case["candidates"], task=case["task"])
    elif case["kind"] == "multi":
        result = routing._multi_skill_batches(case["candidates"], task=case["task"])
    else:
        decider = client.DecisionClient(api_key=live_key or "synthetic", model="typesafe/jev-1.13-20260917", timeout=8,
                                        transport=None if live_key else transport)
        if live_key:
            original_post = decider._post

            def record_post(payload):
                payloads.append(payload)
                return original_post(payload)

            decider._post = record_post
        try:
            with decider.request_budget(64, deadline_seconds=20):
                result = decider.decide(case["state"], case["questions"])
        finally:
            decider.close()
    return result, payloads


def main(args):
    roots = {"baseline": args.baseline.resolve(), "candidate": args.candidate.resolve()}
    modules = {arm: load(root, "hotpath_" + arm) for arm, root in roots.items()}
    book = cases()
    args.output.mkdir(parents=True, exist_ok=False)
    frozen_sources = {arm: sources(root) for arm, root in roots.items()}
    jobs = [(rep, index, arm) for rep in range(3 if args.live else 21)
            for index, case in enumerate(book) if not args.live or case["kind"] == "decide"
            for arm in roots]
    random.Random(720461).shuffle(jobs)
    freeze = {"claim": "identical planning/wire payloads and reduced local serialization time; no Hermes end-to-end qualification",
              "mode": "live OpenRouter component" if args.live else "offline planning component", "sources": frozen_sources,
              "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "python": sys.version,
              "cases": book, "jobs": jobs, "model": "typesafe/jev-1.13-20260917", "deadline_seconds": 20,
              "rules": "Keep every job including failures. Offline gate: identical plans/payloads, >=50% median improvement at 600 entries; no change to feature defaults. Live timings descriptive, 3 repetitions, cold connections in both arms."}
    (args.output / "freeze.json").write_text(json.dumps(freeze, indent=2) + "\n")
    key = None
    if args.live:
        from hermes_cli.env_loader import hydrate_profile_secret_sources
        from agent.secret_scope import build_profile_secret_scope
        hydrate_profile_secret_sources(args.profile)
        key = build_profile_secret_scope(args.profile).get("OPENROUTER_API_KEY")
        if not key:
            raise RuntimeError("configured evaluation profile has no OpenRouter key")
    rows = []
    with (args.output / "observations.jsonl").open("x") as handle:
        for rep, index, arm in jobs:
            case = book[index]
            row = {"rep": rep, "case": case["id"], "kind": case["kind"], "arm": arm}
            t = time.perf_counter()
            try:
                result, payloads = run_case(modules[arm], case, live_key=key)
                elapsed = (time.perf_counter() - t) * 1000
                row.update(wall_ms=elapsed, plan_or_payload_sha256=digest(payloads if payloads else result))
                if case["kind"] == "decide":
                    row.update(request_count=result["request_count"], resolved_model=result["model"], usage=result["total_usage"], answers=result["answers"] if args.live else None)
            except Exception as exc:
                row.update(wall_ms=(time.perf_counter() - t) * 1000, error_type=type(exc).__name__)
            rows.append(row)
            handle.write(json.dumps(row) + "\n")
            handle.flush()
    if {arm: sources(root) for arm, root in roots.items()} != frozen_sources:
        raise RuntimeError("source changed during benchmark")
    summary = []
    for index, case in enumerate(book):
        arms = {}
        for arm in roots:
            selected = [r for r in rows if r["case"] == case["id"] and r["kind"] == case["kind"] and r["arm"] == arm]
            if not selected:
                continue
            times = sorted(r["wall_ms"] for r in selected)
            arms[arm] = {"n": len(selected), "failures": sum("error_type" in r for r in selected), "median_ms": statistics.median(times), "total_ms": sum(times), "p95_ms": times[math.ceil(.95 * len(times)) - 1]}
        if arms:
            relevant = [r for r in rows if r["case"] == case["id"] and r["kind"] == case["kind"]]
            summary.append({"case": case["id"], "kind": case["kind"], "arms": arms,
                            "identical_plan_or_payload": None if args.live else len({r.get("plan_or_payload_sha256") for r in relevant}) == 1,
                            "median_reduction_pct": 100 * (1 - arms["candidate"]["median_ms"] / arms["baseline"]["median_ms"])})
    (args.output / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--profile", type=Path)
    main(parser.parse_args())
