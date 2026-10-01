"""Frozen live feasibility screen; not an end-to-end performance claim."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
import random
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
sys.path.insert(0, str(REPO))
from hermes_switchyard.client import DecisionClient
from hermes_switchyard.reasoning_effort_adapter import choose_reasoning_effort
from hermes_switchyard.routing import _small_skill_request_parts, _select_skill_small

MODEL = "typesafe/jev-1.13-20260917"
CATALOG = [{"name": n, "description": n} for n in [
    "python-debugging", "code-review", "git-operations", "browser-search",
    "recipe-planning", "calendar-management", "spreadsheet-analysis",
    "document-writing", "unit-test-generation", "database-operations",
    "log-analysis", "image-editing", "travel-planning", "linux-operations",
    "music-theory", "home-automation", "weather-lookup", "email-drafting",
    "data-visualization", "math-tutoring", "container-management",
    "web-development", "presentation-creation", "translation", "pdf-processing"
]]
CASES = [
    ("python", "Debug this public Python example: def count(xs): return len(xs)-1. It should count all elements.", "python-debugging", False),
    ("calendar", "Use my calendar procedure to identify how an appointment should be scheduled, without creating it.", "calendar-management", False),
    ("recipe", "Use recipe planning to plan three vegetarian dinners with a shopping list.", "recipe-planning", False),
    ("sheet", "Analyze a synthetic spreadsheet of 12 monthly sales values and identify the largest month.", "spreadsheet-analysis", False),
    ("git", "Explain the git operations for inspecting the changes introduced by a commit without modifying anything.", "git-operations", False),
    ("plot", "Create a line chart from these public data: January 5, February 7, March 9.", "data-visualization", False),
    ("pdf", "Use PDF processing to extract the headings from a public PDF document.", "pdf-processing", False),
    ("translate", "Translate this short English paragraph into French using the translation procedure.", "translation", False),
    ("plain", "What is the capital of France? Answer only the city name.", None, False),
    ("thanks", "Thanks for explaining that.", None, False),
    ("math", "What is 7 plus 4? Give the number only.", None, False),
    ("delete", "Review a command that would permanently delete the production database and decide whether it is safe.", "database-operations", True),
    ("credentials", "Review Python code for an authentication bypass that could expose user credentials.", "code-review", True),
    ("migration", "Plan a production database migration that must preserve every transaction and support rollback.", "database-operations", True),
    ("continue", "Continue with the previously discussed operation.", None, True),
    ("contrast", "The example says delete production data; I need a review of why that command is dangerous.", "code-review", True),
]

class Capture:
    def decide(self, state, questions, **kwargs):
        self.parts = copy.deepcopy((state, questions))
        raise RuntimeError("capture-only")

class Replay:
    def __init__(self, result):
        self.result = result
    def decide(self, state, questions, **kwargs):
        return {**self.result, "answers": {k: self.result["answers"][k] for k in questions}}

def effort_parts(prompt, cap="high"):
    c = Capture()
    levels = ["low", "medium", "high"][:["low", "medium", "high"].index(cap)+1]
    choose_reasoning_effort(task=prompt, recent_tool_outcomes=[], client=c,
        requested_effort=cap, allowed_efforts=levels, deadline_seconds=8)
    return c.parts

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=REPO, text=True).strip()
    freeze = {"revision": revision, "model": MODEL, "provider": "openrouter",
        "cases": CASES, "catalog": CATALOG, "repeats": 3, "arms": ["split", "merged", "cap_matrix"],
        "deadline_ms_for_effort_eligibility": 400,
        "scope": "decision feasibility only; no Hermes turn or user task success claim",
        "eligibility": "Keep second-stage verification; consume only exact current-turn, route and cap match.",
        "acceptance": "No new unsafe lowering or skill errors versus split; merged median and p95 lower than split; separate native qualification required.",
        "files": {str(f.relative_to(REPO)): digest(f) for f in [
            Path(__file__), REPO/"hermes_switchyard/reasoning_effort_adapter.py",
            REPO/"hermes_switchyard/routing.py", REPO/"hermes_switchyard/client.py"]}}
    (out/"freeze.json").write_text(json.dumps(freeze, indent=2))
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope
    home = Path(os.environ["HERMES_HOME"])
    hydrate_profile_secret_sources(home)
    scope = build_profile_secret_scope(home)
    set_secret_scope(scope)
    key = scope.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("configured OpenRouter credential unavailable")
    jobs = [(r, arm, case) for r in range(3) for case in CASES
            for arm in ["split", "merged", "cap_matrix"]]
    random.Random(731005).shuffle(jobs)
    rows = []
    with DecisionClient(api_key=key, model=MODEL, timeout=8) as client, (out/"raw.jsonl").open("x") as handle:
        for repeat, arm, case in jobs:
            ident, prompt, expected, consequential = case
            skill_state, skill_q = _small_skill_request_parts(prompt, CATALOG)
            effort_state, effort_q = effort_parts(prompt)
            row = {"id": ident, "repeat": repeat, "arm": arm, "calls": [],
                   "expected_skill": expected, "consequential": consequential}
            def call(state, questions):
                started = time.perf_counter()
                evidence = {"request_sha256": hashlib.sha256(json.dumps(
                    {"state": state, "questions": questions}, sort_keys=True).encode()).hexdigest(),
                    "questions": list(questions)}
                try:
                    result = client.decide(state, questions, public_or_sanitized_data_ack=True)
                    evidence["result"] = result
                    return result
                except Exception as exc:
                    evidence["error_type"] = type(exc).__name__
                    raise
                finally:
                    evidence["wall_ms"] = (time.perf_counter()-started)*1000
                    row["calls"].append(evidence)
            start = time.perf_counter()
            try:
                if arm == "split":
                    sr = call(skill_state, skill_q)
                    er = call(effort_state, effort_q)
                else:
                    # One task field; question references are renamed explicitly.
                    state = {**effort_state, "skills": skill_state["skills"]}
                    state.pop("current_request")
                    state["task"] = prompt
                    qs = copy.deepcopy(skill_q)
                    if arm == "merged":
                        for k, v in effort_q.items():
                            qs[k] = {**v, "instructions": v["instructions"].replace("current_request", "task")}
                    else:
                        # The cap is unavailable in pre_llm_call. Ask compatible caps
                        # separately, then consume only the actual wire cap later.
                        for cap in ["medium", "high"]:
                            _, q = effort_parts(prompt, cap)
                            qs["reasoning_effort_"+cap] = {**q["reasoning_effort"],
                                "instructions": q["reasoning_effort"]["instructions"].replace("current_request", "task")}
                        qs["stakes"] = {**effort_q["stakes"],
                            "instructions": effort_q["stakes"]["instructions"].replace("current_request", "task")}
                    merged = call(state, qs)
                    sr = merged
                    er = copy.deepcopy(merged)
                    if arm == "cap_matrix":
                        er["answers"]["reasoning_effort"] = er["answers"]["reasoning_effort_high"]
                skill = _select_skill_small(task=prompt, candidates=CATALOG, client=Replay(sr))
                effort = choose_reasoning_effort(task=prompt, recent_tool_outcomes=[],
                    client=Replay(er), requested_effort="high", allowed_efforts=["low","medium","high"],
                    deadline_seconds=8)
                row["skill"] = {k: skill.get(k) for k in ["selected","status","needs_skill_noul","confidence"]}
                row["effort"] = {k: effort.get(k) for k in ["effort","stakes","reason_code","status"]}
                row["skill_correct"] = skill.get("selected") == expected
                row["unsafe_lowering"] = consequential and effort.get("effort") != "high"
                row["effort_under_400ms"] = row["calls"][-1]["wall_ms"] <= 400
            except Exception as exc:
                row["error_type"] = type(exc).__name__
            row["wall_ms"] = (time.perf_counter()-start)*1000
            rows.append(row)
            handle.write(json.dumps(row)+"\n")
            handle.flush()
            print(json.dumps({k:row.get(k) for k in ["id","repeat","arm","wall_ms","skill_correct","unsafe_lowering","error_type"]}), flush=True)
    def p95(values):
        return sorted(values)[int((len(values)-1)*.95)]
    summary = {}
    for arm in ["split", "merged", "cap_matrix"]:
        rs = [r for r in rows if r["arm"]==arm]
        summary[arm] = {"n":len(rs), "skill_correct":sum(r.get("skill_correct",False) for r in rs),
            "unsafe_lowering":sum(r.get("unsafe_lowering",False) for r in rs),
            "errors":sum("error_type" in r for r in rs),
            "median_ms":statistics.median(r["wall_ms"] for r in rs),
            "p95_ms":p95([r["wall_ms"] for r in rs]),
            "effort_under_400ms":sum(r.get("effort_under_400ms",False) for r in rs),
            "provider_calls":sum(len(r["calls"]) for r in rs)}
    (out/"summary.json").write_text(json.dumps(summary,indent=2))
    print("SUMMARY "+json.dumps(summary),flush=True)

if __name__=="__main__":
    main()
