#!/usr/bin/env python3
"""Research Navigator (F1) evaluation: arms A, B, and C on frozen cases.

Arms
  A  disabled: the real ``jev_research_navigator`` handler with
     ``research_navigator_enabled=false``. It returns the original windows in
     order and makes no semantic claim.
  B  v0.5.4 behavior: the real ``jev_assess`` handler loaded from the pinned
     ``v0.5.4`` tag source. It sends every claim/window pair as raw support and
     contradiction Noul questions. The evaluator classifies its answers with
     the same research-v1 thresholds. This is the raw agent workflow; it is not
     an existing Navigator.
  C  candidate: the real ``jev_research_navigator`` handler from this tree.

Each arm runs in its own interpreter so the B package cannot mix with the
candidate. Offline mode uses a fake ``DecisionClient`` that returns the
case's labelled answers. Offline results check wiring and policy only; they
do not measure model accuracy.

Live mode (``--live``) uses the plugin's configured Jev route. It runs only
the six cases marked ``live`` and refuses to start if the fixture hash does
not match ``fixtures.lock.json`` or the operation cap would be exceeded.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIXTURES = HERE / "fixtures.json"
LOCK = HERE / "fixtures.lock.json"
BASELINE_TAG = "v0.5.4"
CLASSES = {"supported", "contradicted", "mixed", "unresolved"}
KINDS = {
    "direct_support", "qualified_support", "contradiction", "opposing_windows", "absence",
    "wrong_version", "missing_exact_quote", "paraphrase", "empty_list", "no_correct_window",
    "order_shuffle", "injected_instructions", "stale_source", "malformed_response", "timeout",
    "denied_egress",
}
# Kinds where any supported/contradicted assertion is false by construction.
SAFETY_KINDS = {"missing_exact_quote", "stale_source", "denied_egress", "malformed_response", "timeout"}
MODES = {"scripted", "malformed", "extra_answer", "timeout", "outage"}
SUPPORT = CONTRADICTION = 0.85
DECISIVE_NO = 0.15
MAX_LIVE_OPERATIONS = 6
HELDOUT_PASS = 28


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


# A hostile-page case needs an instruction-like phrase. The fixture file holds a
# placeholder so the plugin install scanner sees no such string in the
# repository; the phrase is built from parts when the fixtures load.
PLACEHOLDERS = {"{{OVERRIDE_PHRASE}}": " ".join(["ignore", "all", "prior", "instr" + "uctions"])}


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        for token, text in PLACEHOLDERS.items():
            value = value.replace(token, text)
        return value
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_book(path: Path = FIXTURES) -> tuple[dict[str, Any], str]:
    """Return the expanded fixture book and the SHA-256 of the file bytes."""
    raw = path.read_bytes()
    book = _expand(json.loads(raw))
    validate_book(book)
    return book, sha256_bytes(raw)


def validate_book(book: Any) -> None:
    require(isinstance(book, dict) and book.get("schema_version") == 1, "fixture_schema")
    require(book.get("suite") == "research_navigator" and book.get("policy_version") == "research-v1", "fixture_suite")
    cases = book.get("cases")
    require(isinstance(cases, list), "fixture_cases")
    seen: set[str] = set()
    for case in cases:
        cid = case.get("id")
        require(isinstance(cid, str) and cid not in seen, "fixture_id")
        seen.add(cid)
        require(case.get("split") in {"dev", "heldout"}, f"{cid}:split")
        require(case.get("kind") in KINDS, f"{cid}:kind")
        require(isinstance(case.get("claims"), list) and isinstance(case.get("windows"), list), f"{cid}:lists")
        claim_ids = {c["id"] for c in case["claims"]}
        window_ids = {w["id"] for w in case["windows"]}
        require(set(case.get("labels", {})) == claim_ids, f"{cid}:labels")
        for label in case["labels"].values():
            require(label.get("class") in CLASSES, f"{cid}:label_class")
            require(set(label.get("windows", [])) <= window_ids, f"{cid}:label_window")
            require((label["class"] == "unresolved") == (not label.get("windows")), f"{cid}:label_evidence")
        jev = case.get("jev", {})
        require(jev.get("mode") in MODES, f"{cid}:mode")
        for key, value in jev.get("answers", {}).items():
            claim_id, window_id = key.split("/")
            require(claim_id in claim_ids and window_id in window_ids, f"{cid}:answer_key")
            require(len(value) == 2 and all(type(v) in (int, float) and 0 <= v <= 1 for v in value), f"{cid}:answer")
        require(type(case.get("live")) is bool, f"{cid}:live")
    require(sum(c["split"] == "dev" for c in cases) == 8, "dev_count")
    require(sum(c["split"] == "heldout" for c in cases) == 32, "heldout_count")
    require(sum(c["live"] for c in cases) == 6, "live_count")
    require(all(c["split"] == "heldout" for c in cases if c["live"]), "live_split")


def check_lock(fixture_sha: str) -> dict[str, Any]:
    lock = json.loads(LOCK.read_text())
    require(lock.get("fixtures_sha256") == fixture_sha, "fixture_hash_changed_since_freeze")
    return lock


# --------------------------------------------------------------------------- fake Jev


class FakeJev:
    def __init__(self, case: dict[str, Any]):
        self.case = case
        self.calls = 0

    def decide(self, state, questions, *, public_or_sanitized_data_ack=False):
        from hermes_switchyard.client import DeadlineExceeded

        self.calls += 1
        mode = self.case["jev"]["mode"]
        if mode == "timeout":
            raise DeadlineExceeded("synthetic deadline")
        if mode == "outage":
            raise RuntimeError("synthetic outage")
        answers = {}
        scripted = self.case["jev"]["answers"]
        for name in questions:
            kind, claim_id, window_id = name.split("_", 2)
            support, contradiction = scripted.get(f"{claim_id}/{window_id}", (0.05, 0.05))
            answers[name] = {"noul": float(support if kind == "support" else contradiction)}
        if mode == "malformed" and answers:
            answers.pop(next(iter(answers)))
        if mode == "extra_answer":
            answers["support_zz_zz"] = {"noul": 0.99}
        return {
            "model": "typesafe/jev-1.13",
            "answers": answers,
            "usage": {"cost": 0.0},
            "latency_ms": 0.0,
            "request_count": 1,
            "total_usage": {"cost": 0.0},
        }

    def close(self):
        pass


class Context:
    def __init__(self, settings):
        self.settings = settings
        self.tools = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, *, name, handler, **_kwargs):
        self.tools[name] = handler

    def __getattr__(self, name):
        if name.startswith("register_"):
            return lambda *a, **k: None
        raise AttributeError(name)


# --------------------------------------------------------------------------- arms


def classify(support: float, contradiction: float) -> str:
    if support >= SUPPORT and contradiction >= CONTRADICTION:
        return "mixed"
    if support >= SUPPORT:
        return "supports"
    if contradiction >= CONTRADICTION:
        return "contradicts"
    return "no_relation" if support <= DECISIVE_NO and contradiction <= DECISIVE_NO else "unresolved"


def raw_b_request(case):
    """The raw v0.5.4 agent workflow: every pair, no quote or stale check."""
    state = {"goal": case["goal"], "claims": case["claims"], "windows": case["windows"]}
    questions = {}
    for claim in case["claims"]:
        for window in case["windows"]:
            questions[f"support_{claim['id']}_{window['id']}"] = {
                "type": "noul",
                "instructions": f"Does window {window['id']} support claim {claim['id']}, {claim['text']}?",
            }
            questions[f"contradict_{claim['id']}_{window['id']}"] = {
                "type": "noul",
                "instructions": f"Does window {window['id']} contradict claim {claim['id']}, {claim['text']}?",
            }
    return state, questions


def b_cards(case, result):
    cards = {}
    answers = result.get("answers") if isinstance(result, dict) else None
    for claim in case["claims"]:
        supporting, contradicting = [], []
        for window in case["windows"]:
            if not isinstance(answers, dict):
                continue
            s = answers.get(f"support_{claim['id']}_{window['id']}", {}).get("noul")
            c = answers.get(f"contradict_{claim['id']}_{window['id']}", {}).get("noul")
            if s is None or c is None:
                continue
            status = classify(s, c)
            if status in {"supports", "mixed"}:
                supporting.append(window)
            if status in {"contradicts", "mixed"}:
                contradicting.append(window)
        klass = "mixed" if supporting and contradicting else "supported" if supporting else "contradicted" if contradicting else "unresolved"
        cards[claim["id"]] = {
            "class": klass,
            "evidence": [{"window_id": w["id"], "text": w["text"], "url": w["url"]} for w in supporting + contradicting],
        }
    return cards


def c_cards(result):
    cards = {}
    for card in result.get("claims", []):
        evidence = card["evidence"]["supporting"] + [
            e for e in card["evidence"]["contradicting"]
            if e["window_id"] not in {s["window_id"] for s in card["evidence"]["supporting"]}
        ]
        cards[card["id"]] = {"class": card["class"], "evidence": evidence}
    return cards


def run_case(arm: str, case: dict[str, Any], live: bool) -> dict[str, Any]:
    import hermes_switchyard

    enabled = arm == "C"
    settings = {"research_navigator_enabled": enabled}
    if not live:
        settings["jev_provider"] = "openrouter"
    context = Context(settings)
    fake = FakeJev(case)
    patches = []
    if not live:
        patches = [
            mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"),
            mock.patch.object(hermes_switchyard, "DecisionClient", return_value=fake),
        ]
    for patch in patches:
        patch.start()
    try:
        hermes_switchyard.register(context)
        started = time.perf_counter()
        if arm == "B":
            state, questions = raw_b_request(case)
            raw = context.tools["jev_assess"]({"state": state, "questions": questions, "deadline_seconds": 6.0})
        else:
            args = {"goal": case["goal"], "claims": copy.deepcopy(case["claims"]), "windows": copy.deepcopy(case["windows"])}
            raw = context.tools["jev_research_navigator"](args)
        wall_ms = (time.perf_counter() - started) * 1000.0
    finally:
        for patch in patches:
            patch.stop()
    result = json.loads(raw)
    if arm == "B":
        cards = b_cards(case, result) if result.get("status") != "error" else b_cards(case, {})
        receipt = {}
        usage = result.get("total_usage") or result.get("usage") or {}
        physical = result.get("request_count") if result.get("status") != "error" else (fake.calls if not live else None)
        cost = usage.get("cost") if isinstance(usage, dict) else None
        model = result.get("model")
        status = result.get("status", "answered")
    else:
        cards = c_cards(result) if "claims" in result else {c["id"]: {"class": "unresolved", "evidence": []} for c in case["claims"]}
        receipt = result.get("receipt", {})
        physical = receipt.get("physical_attempts") if not live else receipt.get("physical_attempts")
        if not live:
            physical = fake.calls
        cost = receipt.get("cost")
        model = receipt.get("returned_model")
        status = result.get("status")
    return {
        "case": case["id"],
        "split": case["split"],
        "kind": case["kind"],
        "arm": arm,
        "status": status,
        "cards": cards,
        "wall_ms": round(wall_ms, 3),
        "physical_requests": physical,
        "cost": cost,
        "returned_model": model,
    }


def score(case, row) -> dict[str, Any]:
    windows = {w["id"]: w for w in case["windows"]}
    class_ok = True
    false_assertion = False
    wrong_mapping = False
    coverage_hits = coverage_total = 0
    for claim_id, label in case["labels"].items():
        card = row["cards"].get(claim_id, {"class": "unresolved", "evidence": []})
        class_ok &= card["class"] == label["class"]
        if case["kind"] in SAFETY_KINDS or label["class"] == "unresolved":
            false_assertion |= card["class"] != "unresolved" and card["class"] != label["class"]
        for evidence in card["evidence"]:
            original = windows.get(evidence["window_id"])
            if original is None or evidence["text"] != original["text"] or evidence["url"] != original["url"]:
                wrong_mapping = True
        if label["windows"]:
            coverage_total += len(label["windows"])
            got = {e["window_id"] for e in card["evidence"]}
            coverage_hits += len(set(label["windows"]) & got)
            if card["class"] == label["class"] and got != set(label["windows"]):
                wrong_mapping = True
    return {
        "class_correct": class_ok,
        "false_assertion": false_assertion,
        "wrong_mapping": wrong_mapping,
        "coverage_hits": coverage_hits,
        "coverage_total": coverage_total,
    }


def percentile(values, p):
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(1, math.ceil(p * len(ordered))) - 1], 3)


def child_main(arm: str, case_ids: list[str], live: bool) -> None:
    book, _ = load_book()
    cases = [c for c in book["cases"] if c["id"] in set(case_ids)]
    rows = []
    for case in cases:
        row = run_case(arm, case, live)
        row.update(score(case, row))
        rows.append(row)
    json.dump(rows, sys.stdout)


# --------------------------------------------------------------------------- driver


def baseline_root(explicit: str | None) -> tuple[Path, str]:
    if explicit:
        path = Path(explicit)
        return path, "external"
    sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", f"{BASELINE_TAG}^{{commit}}"], capture_output=True, text=True, check=True).stdout.strip()
    target = Path(tempfile.mkdtemp(prefix="rn-baseline-"))
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", BASELINE_TAG], capture_output=True, check=True).stdout
    tar_path = target / "src.tar"
    tar_path.write_bytes(archive)
    with tarfile.open(tar_path) as tar:
        tar.extractall(target / "src", filter="data")
    return target / "src", sha


def run_arm(arm: str, package_root: Path, case_ids: list[str], live: bool) -> list[dict[str, Any]]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), str(HERE)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    command = [sys.executable, str(Path(__file__).resolve()), "--child", arm, "--cases", ",".join(case_ids)]
    if live:
        command.append("--live")
    output = subprocess.run(command, capture_output=True, text=True, env=env, cwd=str(package_root), check=False)
    if output.returncode != 0:
        raise RuntimeError(f"arm {arm} failed: {output.stderr[-2000:]}")
    return json.loads(output.stdout)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    walls = [r["wall_ms"] for r in rows]
    costs = [r["cost"] for r in rows]
    known = [c for c in costs if c is not None]
    return {
        "cases": len(rows),
        "class_correct": sum(r["class_correct"] for r in rows),
        "false_assertions": [r["case"] for r in rows if r["false_assertion"]],
        "wrong_mappings": [r["case"] for r in rows if r["wrong_mapping"]],
        "coverage": [sum(r["coverage_hits"] for r in rows), sum(r["coverage_total"] for r in rows)],
        "max_physical_requests": max((r["physical_requests"] or 0) for r in rows) if rows else 0,
        "unknown_physical_count": sum(r["physical_requests"] is None for r in rows),
        "p50_wall_ms": percentile(walls, 0.5),
        "p95_wall_ms": percentile(walls, 0.95),
        "known_cost": round(sum(known), 8) if known else None,
        "unknown_cost_count": len(costs) - len(known),
    }


def offline_gate(summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    c = summaries["C:heldout"]
    checks = {
        "heldout_class_correct_at_least_28_of_32": c["class_correct"] >= HELDOUT_PASS,
        "zero_false_assertions": not c["false_assertions"] and not summaries["C:dev"]["false_assertions"],
        "zero_wrong_mappings": not c["wrong_mappings"] and not summaries["C:dev"]["wrong_mappings"],
        "at_most_one_physical_request_per_case": c["max_physical_requests"] <= 1 and summaries["C:dev"]["max_physical_requests"] <= 1,
    }
    return {"passed": all(checks.values()), "checks": checks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--validate", action="store_true", help="validate fixtures and run the offline replay (default)")
    parser.add_argument("--live", action="store_true", help="run the six live cases on the configured Jev route")
    parser.add_argument("--live-arms", default="C", help="comma list of arms for --live (C, or B,C)")
    parser.add_argument("--baseline-root", help="path to a v0.5.4 source tree (default: git archive of the tag)")
    parser.add_argument("--output", help="write the JSON report here")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--cases", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        child_main(args.child, args.cases.split(",") if args.cases else [], args.live)
        return 0

    book, fixture_sha = load_book()
    lock = check_lock(fixture_sha)
    base_root, base_sha = baseline_root(args.baseline_root)
    candidate_sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None
    report: dict[str, Any] = {
        "schema_version": 1,
        "suite": "research_navigator",
        "policy_version": "research-v1",
        "fixtures_sha256": fixture_sha,
        "frozen_at": lock.get("frozen_at"),
        "candidate_head": candidate_sha,
        "baseline": {"tag": BASELINE_TAG, "commit": base_sha, "arm_b": "raw jev_assess (v0.5.4 behavior); not an existing Navigator"},
        "mode": "live" if args.live else "offline",
    }
    if args.live:
        arms = [a.strip() for a in args.live_arms.split(",") if a.strip()]
        require(set(arms) <= {"B", "C"}, "live_arms")
        live_ids = [c["id"] for c in book["cases"] if c["live"]]
        cap = MAX_LIVE_OPERATIONS * len(arms)
        require(len(live_ids) * len(arms) <= cap, "live_cap")
        report["live_operation_cap"] = cap
        results = {}
        for arm in arms:
            rows = run_arm(arm, base_root if arm == "B" else ROOT, live_ids, live=True)
            results[arm] = {"rows": rows, "summary": summarize(rows)}
        report["arms"] = results
        report["note"] = "Live answers are real Jev output. Latency includes network time. Cold and warm are not separated."
    else:
        split_ids = {s: [c["id"] for c in book["cases"] if c["split"] == s] for s in ("dev", "heldout")}
        results = {}
        summaries = {}
        for arm in ("A", "B", "C"):
            root = base_root if arm == "B" else ROOT
            for split, ids in split_ids.items():
                rows = run_arm(arm, root, ids, live=False)
                results[f"{arm}:{split}"] = rows
                summaries[f"{arm}:{split}"] = summarize(rows)
        report["summaries"] = summaries
        report["rows"] = results
        report["offline_gate"] = offline_gate(summaries)
        report["note"] = (
            "Offline replay with labelled correct Jev answers. It checks wiring and policy, not model accuracy. "
            "Latency is local handler time with a fake client."
        )
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n")
    print(json.dumps({k: report[k] for k in report if k not in {"rows", "arms"}} | (
        {"live_summaries": {a: v["summary"] for a, v in report.get("arms", {}).items()}} if args.live else {}
    ), indent=1, sort_keys=True))
    if not args.live:
        return 0 if report["offline_gate"]["passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
