"""Offline replay for step-level adaptive effort (v0.5.5).

The replay drives the real ``ReasoningEffortController`` hooks (``pre_llm_call``,
``llm_request`` middleware, ``post_tool_call``) with frozen turn shapes. It measures:

- how many step-level Jev asks happen, and how many extra Jev calls each turn gets;
- how many requests go out below the cap at step level;
- how many of those lowered requests emitted a write tool call (the risk this change adds);
- the added latency per step ask and per turn, from the recorded Jev latency distribution.

Inputs (``adaptive_effort_step_fixtures.json``):

- ``turn_request_counts``: requests per foreground turn, from an aggregate count of a real
  effort history snapshot (77 turns, 2,000 records). No text, no session ids, no tool names.
- ``jev_latency_ms``: the 188 recorded Jev latencies from the same history.
- ``tool_mix`` and ``seed``: a SYNTHETIC tool-kind mix. The history does not record tool kinds,
  so this part is an assumption, not a measurement. The report says so.

The fake Jev is an upper bound: at step level it always picks the lowest candidate it is offered.
Every lowered step therefore counts, and the write-after-lowered rate is a worst case for this mix.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_switchyard.reasoning_effort_adapter import (  # noqa: E402
    ReasoningEffortController,
    classify_tool_kind,
)

FIXTURES = Path(__file__).resolve().parent / "adaptive_effort_step_fixtures.json"
REPORT_SCHEMA = "switchyard.adaptive_effort_step_replay.v1"
LEVEL_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
ROUTE = {"provider": "anthropic", "model": "claude-opus-5-5", "api_mode": "anthropic_messages"}
TOOL_NAMES = {"read": "read_file", "write": "write_file", "exec": "terminal", "other": "synthetic_other_tool"}
REQUEST_TEXT = {"read_only": "review the parser module", "change": "review and fix the parser module"}


class UpperBoundJev:
    """New turn: keep the cap. Step ask: pick the lowest candidate offered."""

    def __init__(self, latencies: list[int], rng: random.Random) -> None:
        self.latencies = latencies
        self.rng = rng
        self.calls: list[dict[str, Any]] = []

    def decide(self, state, questions, **kwargs):
        levels = list(questions["reasoning_effort"]["criteria"])
        step = "recent_tool_kinds" in state
        pick = levels[0] if step else levels[-1]
        self.calls.append({"step": step, "latency_ms": self.rng.choice(self.latencies)})
        return {"answers": {
            "reasoning_effort": {"choice": pick, "confidence": 0.9,
                                 "probabilities": {level: 1.0 if level == pick else 0.0 for level in levels}},
            "stakes": {"noul": 0.0},
        }}


def load_fixtures(path: Path = FIXTURES) -> dict[str, Any]:
    book = json.loads(path.read_text(encoding="utf-8"))
    frozen = book.pop("frozen_sha256")
    digest = hashlib.sha256(json.dumps(book, sort_keys=True).encode("utf-8")).hexdigest()
    if digest != frozen:
        raise ValueError(f"fixture book changed after freeze: {digest} != {frozen}")
    if book.get("data_class") != "public_synthetic_and_aggregate":
        raise ValueError("fixture book is not marked public_synthetic_and_aggregate")
    return book


def _tool_rounds(rng: random.Random, count: int, mix: dict[str, float], failure_rate: float) -> list[tuple[str, bool]]:
    kinds, weights = zip(*sorted(mix.items()))
    rounds = []
    for _ in range(count):
        kind = rng.choices(kinds, weights)[0]
        failed = kind == "exec" and rng.random() < failure_rate
        rounds.append((kind, not failed))
    return rounds


def _percentile(values: list[Any], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return float(ordered[min(len(ordered) - 1, int(fraction * (len(ordered) - 1) + 0.5))])


def replay(book: dict[str, Any], *, scenario: str, cap: str = "high", step_adaptation: bool = True) -> dict[str, Any]:
    spec = book["scenarios"][scenario]
    rng = random.Random(f"{book['seed']}:{scenario}")
    latency_rng = random.Random(f"{book['seed']}:{scenario}:latency")
    jev = UpperBoundJev(book["jev_latency_ms"], latency_rng)
    session = "synthetic-replay-session"
    controller = ReasoningEffortController(
        client_factory=lambda: jev, session_env=lambda name: session if name == "HERMES_SESSION_ID" else "",
        step_adaptation=step_adaptation,
    )
    pre = controller.build_pre_llm_call_hook()
    post = controller.build_post_tool_call_hook()
    cap_index = LEVEL_ORDER.index(cap)
    totals = {"turns": 0, "requests": 0, "lowered_steps": 0, "lowered_steps_that_wrote": 0,
              "below_cap_minus_one": 0, "above_cap": 0}
    extra_calls_per_turn: list[int] = []
    added_ms_per_turn: list[float] = []
    step_latencies: list[float] = []

    for turn_index, request_count in enumerate(book["turn_request_counts"]):
        turn = f"t{turn_index}"
        text = REQUEST_TEXT["read_only" if rng.random() < spec["read_only_share"] else "change"]
        pre(session_id=session, task_id=session, turn_id=turn, user_message=text, conversation_history=[],
            is_first_turn=turn_index == 0, model=ROUTE["model"], platform="cli", parent_session_id="")
        rounds = _tool_rounds(rng, request_count - 1, spec["tool_mix"], spec["exec_failure_rate"])
        calls_before = len(jev.calls)
        for step in range(request_count):
            request = {"model": ROUTE["model"], "messages": [{"role": "user", "content": "synthetic"}],
                       "thinking": {"type": "adaptive"}, "output_config": {"effort": cap}}
            result = controller.on_llm_request(request, session_id=session, task_id=session, turn_id=turn, **ROUTE)
            sent = (result["request"] if result else request)["output_config"]["effort"]
            sent_index = LEVEL_ORDER.index(sent)
            totals["requests"] += 1
            totals["above_cap"] += sent_index > cap_index
            totals["below_cap_minus_one"] += sent_index < cap_index - 1 and step > 0
            if step > 0 and sent_index < cap_index:
                totals["lowered_steps"] += 1
                if step < len(rounds) and rounds[step][0] == "write":
                    totals["lowered_steps_that_wrote"] += 1
            if step < len(rounds):
                kind, ok = rounds[step]
                name = TOOL_NAMES[kind]
                assert classify_tool_kind(name) == kind
                if ok:
                    post(tool_name=name, result='{"ok": true}', session_id=session, task_id=session)
                else:
                    post(tool_name=name, status="error", error_message="exit 1", session_id=session, task_id=session)
        turn_calls = jev.calls[calls_before:]
        steps = [call for call in turn_calls if call["step"]]
        extra_calls_per_turn.append(len(steps))
        step_latencies.extend(call["latency_ms"] for call in steps)
        added_ms_per_turn.append(float(sum(call["latency_ms"] for call in steps)))
        totals["turns"] += 1

    lowered = totals["lowered_steps"]
    return {
        "scenario": scenario,
        "step_adaptation": step_adaptation,
        "cap": cap,
        **totals,
        "step_asks": sum(extra_calls_per_turn),
        "write_after_lowered_rate": round(totals["lowered_steps_that_wrote"] / lowered, 4) if lowered else None,
        "lowered_request_share": round(lowered / totals["requests"], 4),
        "extra_jev_calls_per_turn": {
            "mean": round(sum(extra_calls_per_turn) / len(extra_calls_per_turn), 3),
            "max": max(extra_calls_per_turn),
            "p50": _percentile(extra_calls_per_turn, 0.50),
            "p95": _percentile(extra_calls_per_turn, 0.95),
        },
        "added_latency_ms": {
            "per_step_ask_p50": _percentile(step_latencies, 0.50),
            "per_step_ask_p95": _percentile(step_latencies, 0.95),
            "per_turn_p50": _percentile(added_ms_per_turn, 0.50),
            "per_turn_p95": _percentile(added_ms_per_turn, 0.95),
            "per_turn_max": max(added_ms_per_turn),
        },
    }


def build_report(book: dict[str, Any]) -> dict[str, Any]:
    runs = [replay(book, scenario=name) for name in sorted(book["scenarios"])]
    runs.append(replay(book, scenario=sorted(book["scenarios"])[0], step_adaptation=False))
    return {
        "schema": REPORT_SCHEMA,
        "fixture_sha256": hashlib.sha256(FIXTURES.read_bytes()).hexdigest(),
        "turn_shapes": "aggregate request counts per turn from a real effort history",
        "latency_source": "recorded Jev latencies from the same history",
        "tool_mix_source": "synthetic assumption; the history has no tool kinds",
        "responder": "upper bound: step asks always pick the lowest offered level",
        "runs": runs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline replay for step-level adaptive effort")
    parser.add_argument("--output", type=Path, help="write the JSON report here")
    args = parser.parse_args()
    report = build_report(load_fixtures())
    text = json.dumps(report, indent=2, sort_keys=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
