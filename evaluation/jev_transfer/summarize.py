"""Offline replay of retained public Jev transfer studies. No provider calls."""

from __future__ import annotations

import collections
import hashlib
import json
import math
import re
import statistics
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
RUNS = {
    "decision_screen",
    "decision_confirmation",
    "native_routing",
    "native_consolidation",
    "native_consolidation_interrupted",
}
LEGACY_RECEIPT = re.compile(
    r"\n\nswitchyard: effort (?:none|minimal|low|medium|high|xhigh|max|ultra)"
    r"(?:→(?:none|minimal|low|medium|high|xhigh|max|ultra))?"
    r"(?: \(kept: [^\n()]+\))?(?: · Jev \d+ ms)?\Z"
)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def complete_rows(freeze, rows, native, interrupted=False):
    if native:
        required = {
            (arm, case["id"] + "-" + str(repeat))
            for arm in freeze["arms"]
            for case in freeze["cases"]
            for repeat in range(freeze["repeats"])
        }
        observed = [(row["arm"], row["id"]) for row in rows]
    else:
        required = {
            (arm, case[0], repeat)
            for arm in freeze["arms"]
            for case in freeze["cases"]
            for repeat in range(freeze["repeats"])
        }
        observed = [(row["arm"], row["id"], row["repeat"]) for row in rows]
    if set(observed) - required or any(
        n != 1 for n in collections.Counter(observed).values()
    ):
        raise ValueError("duplicate or unexpected observation")
    if not interrupted and set(observed) != required:
        raise ValueError("incomplete observations")
    for row in rows:
        wall = row["wall_ms"]
        if (
            isinstance(wall, bool)
            or not isinstance(wall, (int, float))
            or not math.isfinite(wall)
            or wall < 0
        ):
            raise ValueError("invalid latency")
    return len(required - set(observed))


def latency(rows):
    values = sorted(row["wall_ms"] for row in rows)
    return {
        "n": len(rows),
        "median_ms": statistics.median(values),
        "total_ms": sum(values),
        "p95_nearest_rank_ms": values[math.ceil(0.95 * len(values)) - 1],
    }


def native_summary(run):
    freeze, rows = run["freeze"], run["rows"]
    complete_rows(freeze, rows, True)
    expected = {case["id"]: case["expected"] for case in freeze["cases"]}
    routing = freeze["candidate_model"] != freeze["source_model"]
    shared_turns = set()
    for row in rows:
        call_ids = {
            call.get("request_id") for call in row["jev"] if call.get("request_id")
        }
        for receipt in row.get("route", []):
            if not isinstance(receipt, dict):
                raise ValueError("invalid decision receipt")
            shared = receipt.get("shared_request_id")
            if not routing and (not isinstance(shared, str) or not shared):
                raise ValueError("unbound shared decision receipt")
            if shared is not None:
                if (
                    shared not in call_ids
                    or not row["wire"]
                    or row["wire"][0]["effort"] != receipt["effort"]
                ):
                    raise ValueError(
                        "shared decision is not bound to its call and wire effort"
                    )
                shared_turns.add((row["arm"], row["id"]))
            elif receipt.get("applied"):
                decision_id = (receipt.get("decision") or {}).get("request_id")
                if decision_id not in call_ids or not any(
                    w["model"] == receipt["to"] for w in row["wire"]
                ):
                    raise ValueError("route is not bound to its call and wire model")
    arms = {}
    for arm in freeze["arms"]:
        subset = [row for row in rows if row["arm"] == arm]
        correct = strict = stripped = unknown = 0
        costs = []
        for row in subset:
            answer = str(row.get("final") or "").strip()
            target = expected[row["id"].rsplit("-", 1)[0]]
            strict += answer == target
            clean = LEGACY_RECEIPT.sub("", answer) if arm == "release" else answer
            stripped += clean != answer
            correct += clean.strip() == target and row.get("completed") is True
            for call in row["jev"]:
                cost = (call.get("usage") or {}).get("cost")
                if (
                    isinstance(cost, (float, int))
                    and not isinstance(cost, bool)
                    and math.isfinite(cost)
                    and cost >= 0
                ):
                    costs.append(cost)
                else:
                    unknown += 1
        arms[arm] = {
            **latency(subset),
            "semantic_correct_completed": correct,
            "raw_exact_correct": strict,
            "legacy_receipts_removed": stripped,
            "physical_main_dispatches": sum(len(r["wire"]) for r in subset),
            "jev_calls": sum(len(r["jev"]) for r in subset),
            "known_jev_cost": sum(costs),
            "unknown_jev_cost_calls": unknown,
            "pilot_consumed": sum(
                bool(r.get("route")) if routing else (r["arm"], r["id"]) in shared_turns
                for r in subset
            ),
            "model_switched": sum(
                any(w["model"] != freeze["source_model"] for w in r["wire"])
                for r in subset
            ),
            "wire_effort_counts": dict(
                collections.Counter(w["effort"] for r in subset for w in r["wire"])
            ),
        }
    candidate = arms["candidate"]
    quality = all(
        candidate["semantic_correct_completed"] >= v["semantic_correct_completed"]
        for k, v in arms.items()
        if k != "candidate"
    )
    speed = all(
        candidate["median_ms"] <= 0.95 * arms[a]["median_ms"]
        and candidate["total_ms"] <= 0.95 * arms[a]["total_ms"]
        for a in ["off", "main"]
    )
    routing = freeze["candidate_model"] != freeze["source_model"]
    used = candidate["model_switched" if routing else "pilot_consumed"] > 0
    unchanged = run["runtime_before"] == run["runtime_after"]
    allowed_models = {freeze["source_model"], freeze["candidate_model"]}
    supported = all(
        w["model"]
        in (allowed_models if r["arm"] == "candidate" else {freeze["source_model"]})
        for r in rows
        for w in r["wire"]
    )
    return {
        "arms": arms,
        "runtime_unchanged": unchanged,
        "pilot_gate": {
            "quality": quality,
            "speed": speed,
            "used": used,
            "supported_wire_models": supported,
            "pass": quality and speed and used and unchanged and supported,
        },
        "release_qualified": False,
        "median_reduction_vs_main_pct": 100
        * (1 - candidate["median_ms"] / arms["main"]["median_ms"]),
        "total_reduction_vs_main_pct": 100
        * (1 - candidate["total_ms"] / arms["main"]["total_ms"]),
    }


def decision_summary(run):
    freeze, rows = run["freeze"], run["rows"]
    complete_rows(freeze, rows, False)
    cases = {c[0]: c for c in freeze["cases"]}
    arms = {}
    for arm in freeze["arms"]:
        subset = [r for r in rows if r["arm"] == arm]
        arms[arm] = {
            **latency(subset),
            "skill_label_matches": sum(
                "error_type" not in r and r["skill"]["selected"] == cases[r["id"]][2]
                for r in subset
            ),
            "unsafe_lowering": sum(
                "error_type" not in r
                and cases[r["id"]][3]
                and r["effort"]["effort"] != "high"
                for r in subset
            ),
            "errors": sum("error_type" in r for r in subset),
            "jev_calls": sum(len(r["calls"]) for r in subset),
        }
    return {"arms": arms, "release_qualified": False}


def summarize(root=ROOT):
    manifest = json.loads((root / "provenance.json").read_text())
    if set(manifest["files"]) != {"observations.json", "source-snapshots.json"}:
        raise ValueError("incomplete evidence manifest")
    for name, digest in manifest["files"].items():
        if sha((root / name).read_bytes()) != digest:
            raise ValueError("evidence digest mismatch: " + name)
    evidence = json.loads((root / "observations.json").read_text())
    source_index = json.loads((root / "source-snapshots.json").read_text())
    sources = {}
    for name, entry in source_index.items():
        archive = root / entry["archive_path"]
        if not archive.resolve().is_relative_to((root / "frozen").resolve()):
            raise ValueError("source archive outside frozen directory")
        data = archive.read_bytes()
        if sha(data) != entry["sha256"]:
            raise ValueError("archived source digest mismatch: " + name)
        sources[name] = data
    if set(evidence["runs"]) != RUNS:
        raise ValueError("unexpected study set")
    for digest, snapshot in evidence["runtime_snapshots"].items():
        if (
            snapshot.get("canonical_paths_and_hashes_sha256") != digest
            or not isinstance(snapshot.get("tracked_python_files"), int)
            or snapshot["tracked_python_files"] <= 0
        ):
            raise ValueError("runtime fingerprint metadata mismatch")
    for name, run in evidence["runs"].items():
        if set(run["source_bindings"]) != set(run["freeze"]["files"]):
            raise ValueError("incomplete source bindings")
        for path, digest in run["freeze"]["files"].items():
            source = run["source_bindings"][path]
            if source["kind"] == "snapshot":
                data = sources[source["path"]]
            elif source["kind"] == "redacted_snapshot":
                if source["path"] not in {
                    "evaluation/model_routing/compare.py",
                    "evaluation/turn_consolidation/native_compare.py",
                }:
                    raise ValueError("unexpected redacted historical source")
                entry = source_index[source["path"]]
                if (
                    entry.get("redaction") != "host-runtime-root"
                    or entry.get("recorded_sha256") != digest
                ):
                    raise ValueError("invalid archival redaction binding")
                continue
            elif source["kind"] == "git":
                data = subprocess.check_output(
                    ["git", "show", source["revision"] + ":" + source["path"]], cwd=REPO
                )
            else:
                raise ValueError("unknown source binding")
            if sha(data) != digest:
                raise ValueError("frozen source mismatch: " + path)
        if name.startswith("native_"):
            keys = (
                ["runtime_before"]
                if run["interrupted"]
                else ["runtime_before", "runtime_after"]
            )
            for key in keys:
                if run[key] not in evidence["runtime_snapshots"]:
                    raise ValueError("missing runtime snapshot")
    interrupted = evidence["runs"]["native_consolidation_interrupted"]
    missing = complete_rows(
        interrupted["freeze"], interrupted["rows"], True, interrupted=True
    )
    return {
        "scope": "Synthetic pilots only. No production registration or default enablement.",
        "archival_limit": "Two historical runner copies redact the host runtime path; their original-byte hashes cannot be re-derived from the public portable copies. Runtime evidence retains aggregate fingerprints and file counts.",
        "interrupted_run": {
            "rows": len(interrupted["rows"]),
            "missing": missing,
            "qualified": False,
        },
        **{
            name: (
                native_summary(run)
                if name.startswith("native_")
                else decision_summary(run)
            )
            for name, run in evidence["runs"].items()
            if not run["interrupted"]
        },
    }


if __name__ == "__main__":
    result = summarize()
    (ROOT / "summary.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
