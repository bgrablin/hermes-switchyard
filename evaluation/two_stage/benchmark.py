#!/usr/bin/env python3
"""Offline benchmark: names-only full fan-out vs two-stage routing (issue #94).

No network, no credentials, no private text. Every case and catalog row is
public synthetic data generated from ``fixtures.json``.

The hosted decision service is replaced by ``FieldBoundOracle``. The oracle
scores a candidate only from the fields that are actually present in the
request ``state``. A names-only request therefore gives it names only, and a
``descriptions`` request gives it the bounded descriptions of the top K. This
measures the information effect of each data boundary. It does not measure a
real model. Rates from this harness are simulation evidence, not a claim about
hosted Jev accuracy; a labelled live collection is required for that claim.

Metrics per arm:

- wrong_skill_rate_on_positives: positive cases that loaded a different skill.
- wrong_load_share_of_loads: wrong or unwanted loads / all loads.
- false_load_rate: no-fit cases that selected any skill / no-fit cases.
- hit_rate: positive cases with the exact expected skill / positive cases.
- requests: provider requests per case (mean, max).
- critical_path_requests: sequential request rounds per case (mean, max).
- description_bytes_sent: bytes of description/excerpt text that crossed the
  simulated boundary (must be 0 for names-only arms).

Run:

    python3 evaluation/two_stage/benchmark.py
    python3 evaluation/two_stage/benchmark.py --output report.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import threading
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_switchyard.routing import select_skill  # noqa: E402
from hermes_switchyard.two_stage_routing import (  # noqa: E402
    HOSTED_DETAIL_DESCRIPTIONS,
    HOSTED_DETAIL_EXCERPT,
    HOSTED_DETAIL_NAMES,
    TwoStageConfig,
    run_two_stage,
)

FIXTURES = Path(__file__).with_name("fixtures.json")
_NONE = "__jev_none_of_these__"
_TOKEN = re.compile(r"[a-z0-9]+")
_STOP = frozenset(
    "a an and are as at be by can do for from how i in is it me my of on or that the this to use "
    "with you your please help need want some".split()
)
MODEL = "typesafe/jev-1.13-20260917"


def tokens(text: str) -> set[str]:
    return {item for item in _TOKEN.findall(text.casefold()) if item not in _STOP}


def load_fixtures(path: Path = FIXTURES) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def build_catalog(fixtures: dict[str, Any]) -> list[dict[str, str]]:
    """Named skills spread across deterministic public filler up to ``catalog_size``.

    Named skills are interleaved at a fixed stride so they land in every
    partition, not only partition 0.
    """
    named = [dict(item) for item in fixtures["skills"]]
    words = fixtures["filler_words"]
    size = fixtures["catalog_size"]
    filler: list[dict[str, str]] = []
    index = 0
    while len(filler) < size - len(named):
        a = words[index % len(words)]
        b = words[(index * 7 + 3) % len(words)]
        filler.append(
            {
                "name": f"{a}-{b}-{index:03d}",
                "description": f"Generic {a} and {b} workflow number {index}.",
                "excerpt": f"Procedure for {a} {b} tasks. Step one: plan. Step two: verify.",
            }
        )
        index += 1
    stride = max(1, size // max(1, len(named)))
    catalog: list[dict[str, str]] = []
    for position in range(size):
        if position % stride == stride // 2 and named:
            catalog.append(named.pop(0))
        elif filler:
            catalog.append(filler.pop(0))
        else:
            catalog.append(named.pop(0))
    return catalog


class FieldBoundOracle:
    """Deterministic stand-in for Jev that only sees the request state fields.

    A candidate is scored from its name plus either the description and
    excerpt that the request actually carries or, when none is sent, a fixed
    ``name_prior`` (what a model may infer from a name alone, sometimes wrongly). The catalog-level early-stop
    Noul uses fixed world knowledge of every named skill, because a real model
    judges whether a task is specialized without seeing the whole catalog.
    """

    temperature = 25.0
    none_score = 0.12
    needs_center = 0.15
    needs_scale = 30.0

    def __init__(self, knowledge: dict[str, dict[str, str]]) -> None:
        self.knowledge = knowledge
        self.requests = 0
        self.detail_bytes = 0
        self.states: list[Any] = []
        self._lock = threading.Lock()

    def _score(self, task_tokens: set[str], text: str) -> float:
        candidate = tokens(text.replace("-", " "))
        if not task_tokens or not candidate:
            return 0.0
        overlap = task_tokens & candidate
        return len(overlap) / len(task_tokens) * 0.7 + len(overlap) / len(candidate) * 0.3

    def _visible_text(self, row: dict[str, str]) -> str:
        # With a real description, the reader uses it instead of a guess from
        # the name. select_skill fills a missing description with the name.
        description = row.get("description", "")
        description = "" if description == row["name"] else description
        if description or row.get("excerpt"):
            return " ".join([row["name"], description, row.get("excerpt", "")])
        prior = self.knowledge.get(row["name"], {}).get("name_prior", "")
        return " ".join([row["name"], prior])

    def _needs(self, value: float) -> float:
        return round(1 / (1 + math.exp(-(value - self.needs_center) * self.needs_scale)), 6)

    def decide(self, state: Any, questions: dict, *, public_or_sanitized_data_ack: bool = True) -> dict:
        rows = {row["name"]: row for row in state.get("skills", [])}
        detail = 0
        for row in rows.values():
            for field in ("description", "excerpt"):
                value = row.get(field)
                if isinstance(value, str) and value != row["name"]:
                    detail += len(value.encode("utf-8"))
        with self._lock:
            self.requests += 1
            self.detail_bytes += detail
            self.states.append(json.loads(json.dumps(state)))
        task_tokens = tokens(state.get("task", ""))
        best_offered = 0.0
        answers: dict[str, Any] = {}
        for name, question in questions.items():
            if question["type"] != "choice":
                continue
            scores = {}
            for key in question["criteria"]:
                if key == _NONE:
                    scores[key] = self.none_score
                else:
                    scores[key] = self._score(task_tokens, self._visible_text(rows.get(key, {"name": key})))
                    best_offered = max(best_offered, scores[key])
            peak = max(scores.values())
            weights = {key: math.exp((value - peak) * self.temperature) for key, value in scores.items()}
            total = sum(weights.values())
            probabilities = {key: value / total for key, value in weights.items()}
            choice = max(probabilities, key=lambda key: probabilities[key])
            answers[name] = {"choice": choice, "probabilities": probabilities, "confidence": probabilities[choice]}
        for name, question in questions.items():
            if question["type"] != "noul":
                continue
            if "full catalog" in question["instructions"]:
                world = max(
                    (
                        self._score(task_tokens, " ".join([key, item.get("name_prior", ""), item.get("description", "")]))
                        for key, item in self.knowledge.items()
                    ),
                    default=0.0,
                )
                answers[name] = {"noul": self._needs(world)}
            else:
                answers[name] = {"noul": self._needs(best_offered)}
        return {
            "model": MODEL,
            "answers": answers,
            "usage": {"prompt_tokens": len(json.dumps(state)) // 4, "completion_tokens": 8},
            "latency_ms": 250.0,
        }


def _hosted_rows(catalog: list[dict[str, str]]) -> list[dict[str, str]]:
    return [{"name": item["name"]} for item in catalog]


def run_arm(arm: str, catalog: list[dict[str, str]], cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    excerpts = {item["name"]: item.get("excerpt", "") for item in catalog}
    knowledge = {item["name"]: item for item in catalog if "name_prior" in item}
    local_catalog = [
        {"name": item["name"], "description": item.get("description", item["name"])} for item in catalog
    ]
    rows = []
    for case in cases:
        oracle = FieldBoundOracle(knowledge)
        if arm == "names_full_fan_out":
            result = select_skill(
                task=case["task"],
                candidates=_hosted_rows(catalog),
                client=oracle,
                deadline_seconds=20.0,
            )
            # select_skill sends every request sequentially.
            rounds = oracle.requests
        else:
            detail = {
                "two_stage_names": HOSTED_DETAIL_NAMES,
                "two_stage_descriptions": HOSTED_DETAIL_DESCRIPTIONS,
                "two_stage_excerpt": HOSTED_DETAIL_EXCERPT,
            }[arm]
            result = run_two_stage(
                task=case["task"],
                candidates=local_catalog,
                client=oracle,
                client_pool=[FieldBoundOracleProxy(oracle) for _ in range(3)],
                config=TwoStageConfig(hosted_detail=detail),
                excerpt_loader=excerpts.get,
                deadline_seconds=20.0,
            )
            rounds = result["request_rounds"]
        rows.append(
            {
                "case_id": case["id"],
                "kind": case["kind"],
                "expected": case.get("expected"),
                "selected": result.get("selected"),
                "requests": oracle.requests,
                "critical_path_requests": rounds,
                "detail_bytes": oracle.detail_bytes,
            }
        )
    return rows


class FieldBoundOracleProxy:
    """Extra pooled client that shares one oracle's accounting."""

    def __init__(self, oracle: FieldBoundOracle) -> None:
        self._oracle = oracle

    def decide(self, state: Any, questions: dict, *, public_or_sanitized_data_ack: bool = True) -> dict:
        return self._oracle.decide(state, questions, public_or_sanitized_data_ack=public_or_sanitized_data_ack)


def _ratio(numerator: int, denominator: int) -> float | None:
    return None if denominator == 0 else round(numerator / denominator, 4)


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    positives = [row for row in rows if row["kind"] == "positive"]
    no_fit = [row for row in rows if row["kind"] == "no_fit"]
    loads = [row for row in rows if row["selected"] is not None]
    wrong_loads = [
        row for row in loads
        if row["kind"] == "no_fit" or row["selected"] != row["expected"]
    ]
    return {
        "cases": len(rows),
        "positive_cases": len(positives),
        "no_fit_cases": len(no_fit),
        "hit_rate": _ratio(sum(1 for row in positives if row["selected"] == row["expected"]), len(positives)),
        "wrong_skill_rate_on_positives": _ratio(
            sum(1 for row in positives if row["selected"] not in (None, row["expected"])), len(positives)
        ),
        "false_load_rate": _ratio(sum(1 for row in no_fit if row["selected"] is not None), len(no_fit)),
        "wrong_load_share_of_loads": _ratio(len(wrong_loads), len(loads)),
        "requests_mean": round(sum(row["requests"] for row in rows) / len(rows), 3),
        "requests_max": max(row["requests"] for row in rows),
        "critical_path_requests_mean": round(sum(row["critical_path_requests"] for row in rows) / len(rows), 3),
        "critical_path_requests_max": max(row["critical_path_requests"] for row in rows),
        "description_bytes_sent": sum(row["detail_bytes"] for row in rows),
    }


ARMS = ("names_full_fan_out", "two_stage_names", "two_stage_descriptions", "two_stage_excerpt")


def run(fixtures: dict[str, Any]) -> dict[str, Any]:
    catalog = build_catalog(fixtures)
    cases = fixtures["cases"]
    names = {item["name"] for item in catalog}
    for case in cases:
        if case["kind"] == "positive" and case["expected"] not in names:
            raise ValueError(f"case {case['id']} expects a skill outside the catalog")
    arms = {arm: run_arm(arm, catalog, cases) for arm in ARMS}
    digest = hashlib.sha256(json.dumps(fixtures, sort_keys=True).encode("utf-8")).hexdigest()
    return {
        "schema": "hermes-switchyard/two-stage-benchmark/v1",
        "simulation": True,
        "claimable": False,
        "note": (
            "FieldBoundOracle simulation. It shows the information effect of each data boundary "
            "and the request shape; it is not hosted Jev accuracy."
        ),
        "fixtures_sha256": digest,
        "catalog_size": len(catalog),
        "metrics": {arm: _metrics(rows) for arm, rows in arms.items()},
        "records": arms,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline two-stage routing benchmark (simulation).")
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--records", action="store_true", help="print per-case records")
    args = parser.parse_args(argv)
    report = run(load_fixtures(args.fixtures))
    if not args.records:
        report = {key: value for key, value in report.items() if key != "records"}
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
