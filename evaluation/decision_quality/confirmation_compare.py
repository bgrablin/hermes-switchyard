"""Exercise actual triage decisions, local artifacts, and independent verifier."""

import argparse
import hashlib
import json
import os
import random
import time
from pathlib import Path

from baseline_source import activate_baseline

ROOT = Path(__file__).resolve().parent

activate_baseline()
from hermes_switchyard.client import DecisionClient  # noqa: E402 -- baseline must be selected before importing plugin
from hermes_switchyard import record_triage as triage  # noqa: E402 -- baseline must be selected before importing plugin

MODEL = "typesafe/jev-1.13-20260917"


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    book = json.loads((ROOT / "confirmation-cases.json").read_text())
    rubric = book["severity_rubric"]
    (out / "freeze.json").write_text(
        json.dumps(
            {
                "model": MODEL,
                "repeats": 2,
                "thresholds": {"disposition": 0.8, "severity": 0.8},
                "files": {
                    p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in [
                        Path(__file__),
                        ROOT / "confirmation-cases.json",
                        ROOT / "cases.json",
                        Path(triage.__file__),
                    ]
                },
                "acceptance": "More correct accepted qualified records with rated priorities, no wrong accepted priorities; unchanged thresholds, no fallback. Artifact verification checks policy, not semantic truth.",
            },
            indent=2,
        )
    )
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope

    home = Path(os.environ["HERMES_HOME"])
    hydrate_profile_secret_sources(home)
    scope = build_profile_secret_scope(home)
    set_secret_scope(scope)
    key = scope.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("configured credential unavailable")
    original = triage.build_assessment_request

    def descriptive(records):
        state, questions = original(records)
        for name, question in questions.items():
            if name.startswith("severity__"):
                question["criteria"] = rubric
        return state, questions

    jobs = [
        (repeat, arm, offset)
        for repeat in range(2)
        for arm in ["main", "candidate"]
        for offset in range(0, len(book["records"]), 4)
    ]
    random.Random(734888).shuffle(jobs)
    with (out / "raw.jsonl").open("x") as handle:
        for repeat, arm, offset in jobs:
            triage.build_assessment_request = (
                descriptive if arm == "candidate" else original
            )
            batch = book["records"][offset : offset + 4]
            ident = f"{arm}-{offset}-{repeat}"
            dest = out / "artifacts" / ident
            calls = []
            t = time.perf_counter()
            with DecisionClient(api_key=key, model=MODEL, timeout=8) as client:
                post = client._post

                def observed(payload):
                    answer = post(payload)
                    calls.append(answer)
                    return answer

                client._post = observed
                result = triage.run_record_triage(
                    batch,
                    client=client,
                    out_dir=dest,
                    deadline_seconds=8,
                    records_per_batch=4,
                )
            verify = triage.verify_artifact(dest, batch)
            row = {
                "id": ident,
                "arm": arm,
                "wall_ms": 1000 * (time.perf_counter() - t),
                "result": result,
                "verification": verify,
                "calls": calls,
                "expected": {r["id"]: book["expected"][r["id"]] for r in batch},
            }
            # Artifact paths are local implementation details; retain only the portable directory name.
            if isinstance(result.get("artifact"), dict):
                for k, v in list(result["artifact"].items()):
                    if isinstance(v, str) and str(out) in v:
                        result["artifact"][k] = v.replace(str(out), ".")
            handle.write(json.dumps(row) + "\n")
            handle.flush()
            print(
                json.dumps(
                    {
                        "id": ident,
                        "wall_ms": row["wall_ms"],
                        "decisions": [r["decision"] for r in result["records"]],
                        "verification": verify,
                    }
                ),
                flush=True,
            )
    triage.build_assessment_request = original


if __name__ == "__main__":
    main()
