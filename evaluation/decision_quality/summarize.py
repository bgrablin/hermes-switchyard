"""Recompute published measurements from preserved provider/native observations."""

import json
import math
import re
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LEVELS = ["cosmetic", "minor", "major", "critical"]


def rows(name):
    return [
        json.loads(line)
        for line in (ROOT / name / "raw.jsonl").read_text().splitlines()
    ]


def latency(values):
    values = sorted(values)
    return {
        "n": len(values),
        "mean_ms": round(statistics.mean(values), 3),
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(values[math.ceil(0.95 * len(values)) - 1], 3),
    }


def summarize(screen, workflow, native):
    result = {"rubrics": {}, "workflow": {}, "native": {}, "audit": {}, "batching": {}}
    for arm in ["existing", "descriptive"]:
        result["rubrics"][arm] = {}
        for split in ["development", "holdout"]:
            vals = []
            for row in screen:
                if row["family"] != "rubrics" or row["arm"] != arm:
                    continue
                answers = row.get("result", {}).get("answers", {})
                for rid, expected in row["expected"].items():
                    if expected["split"] != split:
                        continue
                    answer = answers.get("severity__" + rid, {})
                    probabilities = answer.get("probabilities", {})
                    winner = (
                        max(probabilities, key=lambda k: (probabilities[k], -int(k)))
                        if probabilities
                        else None
                    )
                    correct = (
                        winner is not None
                        and LEVELS[int(winner)] == expected["severity"]
                    )
                    accepted = winner is not None and probabilities[winner] >= 0.8
                    vals.append((correct, accepted))
            result["rubrics"][arm][split] = {
                "n": len(vals),
                "modal_correct": sum(c for c, _ in vals),
                "accepted_correct": sum(c and a for c, a in vals),
                "accepted_wrong": sum(a and not c for c, a in vals),
            }
    for arm in ["main", "candidate"]:
        selected = [r for r in workflow if r["arm"] == arm]
        entries = [
            (entry, r["expected"][entry["id"]])
            for r in selected
            for entry in r["result"]["records"]
        ]
        accepted = [
            (e, x)
            for e, x in entries
            if e["decision"]["status"] == "accepted"
            and e["decision"]["severity"] is not None
            and e["consumer"]["status"] == "acted"
        ]
        result["workflow"][arm] = {
            "records": len(entries),
            "rated_correct": sum(
                e["decision"]["severity"] == x["severity"]
                and e["decision"]["disposition"] == x["disposition"]
                for e, x in accepted
            ),
            "rated_wrong": sum(
                e["decision"]["severity"] != x["severity"]
                or e["decision"]["disposition"] != x["disposition"]
                for e, x in accepted
            ),
            "abstained": sum(
                e["decision"]["status"] == "abstained" for e, _ in entries
            ),
            "qualified_unrated": sum(
                e["decision"]["status"] == "accepted"
                and e["decision"]["severity"] is None
                for e, _ in entries
            ),
            "verified_artifacts": sum(r["verification"]["verified"] for r in selected),
            "latency": latency([r["wall_ms"] for r in selected]),
            "cost_usd": sum(r["result"]["accounting"]["total_cost"] for r in selected),
            "requests": sum(
                r["result"]["accounting"]["requests_completed"] for r in selected
            ),
        }
    for arm in ["off", "release", "main", "candidate"]:
        selected = [r for r in native if r["arm"] == arm]
        semantic = []
        for row in selected:
            text = row.get("final") or ""
            # v0.5.6 appends its known receipt even when the newer receipt_mode is off.
            text = re.sub(
                r"\n\nswitchyard: effort [^\n]+ · Jev [0-9]+ ms\s*$", "", text
            )
            semantic.append(
                text.strip().strip("`").strip().casefold() == row["expected"].casefold()
            )
        result["native"][arm] = {
            "n": len(selected),
            "strict_format_correct": sum(
                (r.get("final") or "").strip().strip("`").strip().casefold()
                == r["expected"].casefold()
                for r in selected
            ),
            "answer_correct_after_receipt_removal": sum(semantic),
            "latency": latency([r["wall_ms"] for r in selected]),
            "main_requests": sum(len(r["wire"]) for r in selected),
            "high_effort_requests": sum(
                w["effort"] == "high" for r in selected for w in r["wire"]
            ),
            "jev_attempts": sum(len(r["jev"]) for r in selected),
        }
    audits = [r for r in screen if r["family"] == "audit"]
    accepted_wrong = 0
    correct = 0
    for row in audits:
        for key, expected in row["expected"].items():
            answer = row["result"]["answers"][key]
            right = answer["choice"] == expected
            correct += right
            accepted_wrong += (
                not right
                and answer["confidence"] >= 0.8
                and answer["probabilities"][answer["choice"]] >= 0.8
            )
    result["audit"] = {
        "n": sum(len(r["expected"]) for r in audits),
        "correct": correct,
        "accepted_wrong": accepted_wrong,
    }
    serial = [
        sum(
            r["wall_ms"]
            for r in screen
            if r["family"] == "serial_triage" and r["id"].endswith("-" + str(i))
        )
        for i in range(2)
    ]
    batched = [
        r["wall_ms"]
        for r in screen
        if r["family"] == "rubrics"
        and r["arm"] == "descriptive"
        and r["id"].startswith("batch-0-")
    ]
    result["batching"] = {
        "serial_four_record_runs": latency(serial),
        "batched_four_record_runs": latency(batched),
        "scope": "Already implemented batching; conditional severity screen only. Dispositions abstained in this initial screen.",
    }
    return result


def main():
    sys.path.insert(0, str(ROOT.parents[1]))
    from validate_observations import validate

    book = validate(ROOT)
    summary = summarize(**{key: book[key] for key in ["screen", "workflow", "native"]})
    summary["review_confirmation"] = summarize(
        screen=book["screen"], workflow=book["confirmation"], native=book["native"]
    )["workflow"]
    (ROOT / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
