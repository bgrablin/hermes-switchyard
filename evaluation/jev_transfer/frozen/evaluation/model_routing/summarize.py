"""Recompute semantic correctness and complete-arm metrics from retained receipts."""
from __future__ import annotations
import argparse
import collections
import json
import math
import re
import statistics
from pathlib import Path

# This exact display-only suffix is emitted by v0.5.6. No answer text is stripped.
LEGACY_RECEIPT = re.compile(
    r"\n\nswitchyard: effort (?:none|minimal|low|medium|high|xhigh|max|ultra)"
    r"(?:→(?:none|minimal|low|medium|high|xhigh|max|ultra))?"
    r"(?: \(kept: [^\n()]+\))?"
    r"(?: · Jev \d+ ms)?\Z"
)

def summarize(root):
    freeze=json.loads((root/"freeze.json").read_text())
    rows=[json.loads(line) for line in (root/"raw.jsonl").read_text().splitlines()]
    arms=freeze["arms"]
    expected={c["id"]:c["expected"] for c in freeze["cases"]}
    required={(arm,c["id"]+"-"+str(r)) for arm in arms
              for c in freeze["cases"] for r in range(freeze["repeats"])}
    observed=[(r["arm"],r["id"]) for r in rows]
    counts=collections.Counter(observed)
    if set(observed)!=required or any(n!=1 for n in counts.values()):
        raise ValueError("incomplete, duplicate, or unexpected paired observations")
    summary={"arms":{},"normalization":"Only v0.5.6's exact terminal effort receipt is removed for semantic scoring. Raw final text and original strict-format scores remain in raw.jsonl."}
    for arm in arms:
        rs=[r for r in rows if r["arm"]==arm]
        values=sorted(r["wall_ms"] for r in rs)
        correct=0;stripped=0;usage_costs=[];unknown_cost=0
        for row in rs:
            if not isinstance(row["wall_ms"],(int,float)) or not math.isfinite(row["wall_ms"]) or row["wall_ms"]<0:
                raise ValueError("invalid wall latency")
            answer=str(row.get("final") or "").strip()
            clean=LEGACY_RECEIPT.sub("",answer) if arm=="release" else answer
            stripped+=clean!=answer
            correct+=clean.strip()==expected[row["id"].rsplit("-",1)[0]]
            for call in row["jev"]:
                cost=(call.get("usage") or {}).get("cost")
                if isinstance(cost,(int,float)) and not isinstance(cost,bool) and math.isfinite(cost):
                    usage_costs.append(cost)
                else:
                    unknown_cost+=1
        summary["arms"][arm]={"n":len(rs),"semantic_correct":correct,
            "raw_exact_correct":sum(r["correct"] for r in rs),"display_suffixes_removed":stripped,
            "median_ms":statistics.median(values),"total_ms":sum(values),
            "p95_ms":values[math.ceil(.95*len(values))-1],
            "main_dispatches":sum(len(r["wire"]) for r in rs),
            "jev_calls":sum(len(r["jev"]) for r in rs),
            "known_jev_cost":sum(usage_costs),"unknown_jev_cost_calls":unknown_cost,
            "pilot_consumed":sum(bool(r.get("route")) for r in rs),
            "model_switched":sum(any(x.get("applied") for x in r.get("route",[])) for r in rs)}
    candidate=summary["arms"]["candidate"]
    controls=[summary["arms"][a] for a in arms if a!="candidate"]
    quality=all(candidate["semantic_correct"]>=c["semantic_correct"] for c in controls)
    speed=all(candidate["median_ms"]<=.95*summary["arms"][a]["median_ms"]
              and candidate["total_ms"]<=.95*summary["arms"][a]["total_ms"]
              for a in ["off","main"])
    before=json.loads((root/"runtime-before.json").read_text())
    after=json.loads((root/"runtime-after.json").read_text())
    summary["runtime_unchanged"]=before==after
    summary["pilot_gate"]={"quality":quality,"median_and_total_5pct_better":speed,
        "pilot_used":candidate["pilot_consumed"]>0,"runtime_unchanged":before==after,
        "pass":quality and speed and candidate["pilot_consumed"]>0 and before==after}
    summary["release_gate"]="NOT ESTABLISHED: synthetic workload; no general model/cap/tool/history/pin compatibility qualification."
    return summary

if __name__=="__main__":
    p=argparse.ArgumentParser()
    p.add_argument("root",type=Path)
    a=p.parse_args()
    result=summarize(a.root)
    (a.root/"semantic-summary.json").write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))
