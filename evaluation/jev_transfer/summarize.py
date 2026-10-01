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
# Offered identifiers in the retained native c25 study, from its frozen baseline.
NATIVE_SKILL_CRITERIA = frozenset({
    "block-party-seating", "board-game-pairings", "choir-practice-order",
    "day-trip-packing", "fabric-scrap-sorting", "family-ride-stops",
    "field-trip-stations", "garden-walk-access", "garden-walk-parking",
    "garden-walk-refreshments", "garden-walk-shade", "garden-walk-signage",
    "garden-walk-timing", "garden-walk-volunteers", "garden-walk-weather",
    "hermes-switchyard:hermes-switchyard-operations", "mural-workshop-layout",
    "nature-walk-notes", "paper-folding-lesson", "photo-walk-themes",
    "picnic-menu-cards", "reading-circle-seating", "seed-swap-inventory",
    "shared-chores-rota", "story-circle-prompts", "stretch-class-sequence",
})
NATIVE_EFFORT_CRITERIA = {
    "medium": frozenset({"low", "medium"}),
    "high": frozenset({"low", "medium", "high"}),
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


def routing_decision_matches(decision, calls, expected_model):
    if not isinstance(decision, dict):
        return False
    request_id = decision.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        return False
    matches = [call for call in calls if call.get("request_id") == request_id]
    if len(matches) != 1:
        return False
    call = matches[0]
    questions = call.get("questions")
    if (
        not isinstance(questions, list)
        or len(questions) != 2
        or any(not isinstance(q, str) for q in questions)
        or set(questions) != {"routine", "stakes"}
        or call.get("error_type")
        or call.get("model") != expected_model
        or decision.get("model") != expected_model
    ):
        return False
    raw_answers, answers = call.get("answers"), decision.get("answers")
    if (
        not isinstance(raw_answers, dict)
        or not isinstance(answers, dict)
        or set(raw_answers) != {"routine", "stakes"}
        or set(answers) != {"routine", "stakes"}
    ):
        return False
    for name in questions:
        raw = normalize_answer(raw_answers[name], "noul")
        normalized = answers[name]
        if (
            raw is None
            or not isinstance(normalized, dict)
            or set(normalized) != {"noul"}
            or not bounded_number(normalized["noul"], 1)
            or normalized != raw
        ):
            return False
    return True


def bounded_number(value, upper):
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 <= value <= upper
    )


def normalize_answer(answer, kind, criteria=None):
    """Replay the frozen client's raw Choice/Noul response contract."""
    if not isinstance(answer, dict):
        return None
    if "type" in answer:
        if answer["type"] != kind:
            return None
        answer = {key: value for key, value in answer.items() if key != "type"}
    if kind == "noul":
        if set(answer) != {"noul"} or not bounded_number(answer["noul"], 1):
            return None
    else:
        if set(answer) != {"choice", "probabilities", "confidence"}:
            return None
        choice, probabilities = answer["choice"], answer["probabilities"]
        if (
            not isinstance(choice, str)
            or choice not in criteria
            or not bounded_number(answer["confidence"], 1)
            or not isinstance(probabilities, dict)
            or set(probabilities) != set(criteria)
            or not all(bounded_number(v, 1) for v in probabilities.values())
            or abs(sum(probabilities.values()) - 1) >= 0.02
            or probabilities[choice] < max(probabilities.values()) - 1e-6
        ):
            return None
    return answer


def shared_decision_matches(receipt, calls, freeze):
    matches = [
        call
        for call in calls
        if call.get("request_id") == receipt["shared_request_id"]
    ]
    if len(matches) != 1:
        return False
    call = matches[0]
    required = {
        "skill", "needs_skill", "reasoning_effort_medium",
        "reasoning_effort_high", "stakes",
    }
    questions = call.get("questions")
    if (
        call.get("error_type")
        or call.get("model") != freeze["jev"]
        or not isinstance(questions, list)
        or any(not isinstance(q, str) for q in questions)
        or len(questions) != len(required)
        or set(questions) != required
        or not bounded_number(
            receipt.get("shared_latency_ms"), freeze["routing_deadline_ms"]
        )
    ):
        return False
    cap = receipt.get("cap")
    answers = call.get("answers")
    if (
        cap not in NATIVE_EFFORT_CRITERIA
        or not isinstance(answers, dict)
        or set(answers) != required
    ):
        return False
    schema = {
        "skill": ("choice", NATIVE_SKILL_CRITERIA),
        "needs_skill": ("noul", None),
        "stakes": ("noul", None),
        **{
            "reasoning_effort_" + name: ("choice", levels)
            for name, levels in NATIVE_EFFORT_CRITERIA.items()
        },
    }
    normalized = {
        name: normalize_answer(answers[name], kind, criteria)
        for name, (kind, criteria) in schema.items()
    }
    if any(answer is None for answer in normalized.values()):
        return False
    choice = normalized["reasoning_effort_" + cap]
    stakes = normalized["stakes"]
    # Frozen pilot: no prior tool failures; stakes at 0.5 veto any lowering.
    selected = choice["choice"]
    expected = cap if selected != cap and stakes["noul"] >= 0.5 else selected
    return receipt.get("effort") == expected


def native_summary(run):
    freeze, rows = run["freeze"], run["rows"]
    complete_rows(freeze, rows, True)
    expected = {case["id"]: case["expected"] for case in freeze["cases"]}
    routing = freeze["candidate_model"] != freeze["source_model"]
    shared_turns = set()
    routed_decision_turns = set()
    for row in rows:
        for call in row["jev"]:
            if call.get("error_type"):
                if any(call.get(key) is not None for key in ("model", "answers", "request_id")):
                    raise ValueError("failed Jev call includes successful response metadata")
            elif call.get("model") != freeze["jev"]:
                raise ValueError("Jev call model differs from frozen model")
        validated_models = set()
        for receipt in row.get("route", []):
            if not isinstance(receipt, dict):
                raise ValueError("invalid decision receipt")
            if routing:
                if (
                    not isinstance(receipt.get("applied"), bool)
                    or receipt.get("from") != freeze["source_model"]
                    or receipt.get("to")
                    not in {freeze["source_model"], freeze["candidate_model"]}
                ):
                    raise ValueError("invalid routing receipt")
                valid_routing_decision = routing_decision_matches(
                    receipt.get("decision"), row["jev"], freeze["jev"]
                )
                if receipt["applied"] != (receipt["to"] == freeze["candidate_model"]):
                    raise ValueError("routing receipt has inconsistent application state")
                if valid_routing_decision:
                    routed_decision_turns.add((row["arm"], row["id"]))
                    answers = receipt["decision"]["answers"]
                    if receipt["applied"] and (
                        answers["routine"]["noul"] < 0.8
                        or answers["stakes"]["noul"] >= 0.5
                        or not bounded_number(
                            receipt.get("wall_ms"), freeze["routing_deadline_ms"]
                        )
                    ):
                        raise ValueError("applied route did not qualify within its deadline")
            shared = receipt.get("shared_request_id")
            if not routing and (not isinstance(shared, str) or not shared.strip()):
                raise ValueError("unbound shared decision receipt")
            if shared is not None:
                if (
                    not shared_decision_matches(receipt, row["jev"], freeze)
                    or not row["wire"]
                    or row["wire"][0]["effort"] != receipt["effort"]
                ):
                    raise ValueError(
                        "shared decision is not bound to its call and wire effort"
                    )
                shared_turns.add((row["arm"], row["id"]))
            elif receipt.get("applied"):
                if not valid_routing_decision or not any(
                    w["model"] == receipt["to"] for w in row["wire"]
                ):
                    raise ValueError("route is not bound to its call and wire model")
                validated_models.add(receipt["to"])
        switched_models = {
            w["model"] for w in row["wire"] if w["model"] != freeze["source_model"]
        }
        if not switched_models.issubset(validated_models):
            raise ValueError("switched wire is missing a validated routing receipt")
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
                (r["arm"], r["id"])
                in (routed_decision_turns if routing else shared_turns)
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
