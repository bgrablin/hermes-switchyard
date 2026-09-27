#!/usr/bin/env python3
"""F2 end-to-end harness: the A' main-model judgment arm, live B/C pairs, acceptance.

A' (judgment baseline, "plugin disabled" realistic path)
  The candidate ``run_browser_goal`` runs on the frozen held-out traces with
  ``progress_mode="advisory_stop"``, so it asks the progress questions on the
  same steps as C and stops on the same rule. The optional questions do not go
  to Jev. The main model answers them from the same state (goal, page,
  previous_page, recent actions) through one frozen prompt
  (``judge_prompt.txt``). Jev still answers the base operation questions, as
  in B. Each judgment is one ``hermes -z`` one-shot call on the configured main
  route (openai-codex / gpt-6-sol) with ``--safe-mode``, so Switchyard and all
  other plugins are not loaded. The run uses the real HERMES_HOME; the CLI
  reads its own OAuth credential. This module never reads or prints it.

Live
  B (v0.5.4 package) and C (candidate) run the four frozen public goals in
  ``live.json`` through the registered ``jev_computer_use`` handler with a
  real headless browser and the configured ``jev_provider: auto`` route. Arms
  alternate per goal. A pair runs whole or is skipped whole, inside the
  24-request physical cap.

Nothing here runs a model or a browser unless the operator passes ``--a-prime``
or ``--live``. Both refuse unless the frozen plan, prompt, and harness files
are committed and unchanged.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import evaluate  # noqa: E402

PLAN_PATH = HERE / "e2e_plan.json"
JUDGE_PROMPT = HERE / "judge_prompt.txt"
FROZEN_FILES = (
    "evaluation/browser_progress/e2e_plan.json",
    "evaluation/browser_progress/judge_prompt.txt",
    "evaluation/browser_progress/progress_e2e.py",
    "evaluation/browser_progress/evaluate.py",
    "evaluation/browser_progress/fixtures.json",
    "evaluation/browser_progress/fixtures.lock.json",
    "evaluation/browser_progress/live.json",
)

MAIN_MODEL = "gpt-6-sol"
MAIN_PROVIDER = "openai-codex"
# agent.reasoning_effort in the user's config.yaml. --safe-mode ignores the
# config, so the harness passes it explicitly.
PLAN_REASONING = "high"
MAIN_TOOLSETS = "context_engine"
JUDGE_TIMEOUT_SECONDS = 300.0
REPORTED_MAIN_MODELS = {MAIN_MODEL, f"openai/{MAIN_MODEL}"}

PRICING: dict[str, Any] = {
    "model": MAIN_MODEL,
    "source_url": "https://developers.openai.com/api/docs/models/gpt-6-sol",
    "retrieved": "2026-09-27",
    "currency": "USD",
    "per_million_tokens": {"input": 2.00, "cached_input": 0.20, "cache_write": 2.50, "output": 10.00},
    "long_prompt_threshold_tokens": 272_000,
    "per_million_tokens_above_threshold": {"input": 4.00, "cached_input": 0.40, "cache_write": 5.00, "output": 15.00},
    "rules": (
        "Standard (not Fast) API list price. Prompt tokens = input + cache_read + cache_write; above the "
        "threshold the higher rates apply to the whole request. Reasoning tokens are part of output_tokens "
        "and are not added again. Harness auxiliary calls (one-shot title generation) are recorded and not priced."
    ),
}

LIVE_MAX_STEPS = 4
LIVE_PER_RUN_CAP = 4
LIVE_TOTAL_CAP = evaluate.MAX_LIVE_PHYSICAL
LIVE_GOAL_ORDER = ("live-2", "live-3", "live-1", "live-4")
NULL_COST_FAULTS = {"http_429", "http_529", "outage"}


class LiveCapReached(RuntimeError):
    """The per-run physical request cap would be exceeded."""


# --------------------------------------------------------------------------- pricing


def _count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def list_price_usd(usage: dict[str, Any]) -> float | None:
    """List-price-equivalent USD for one main-loop call; None when a count is missing."""
    if not isinstance(usage, dict):
        return None
    inp, out = _count(usage.get("input_tokens")), _count(usage.get("output_tokens"))
    if inp is None or out is None:
        return None
    read = _count(usage.get("cache_read_tokens") if usage.get("cache_read_tokens") is not None else 0)
    write = _count(usage.get("cache_write_tokens") if usage.get("cache_write_tokens") is not None else 0)
    if read is None or write is None:
        return None
    above = inp + read + write > PRICING["long_prompt_threshold_tokens"]
    rates = PRICING["per_million_tokens_above_threshold" if above else "per_million_tokens"]
    dollars = (inp * rates["input"] + read * rates["cached_input"] + write * rates["cache_write"]
               + out * rates["output"]) / 1_000_000
    return round(dollars, 10)


def _aux_tokens(usage: dict[str, Any]) -> int:
    aux = usage.get("auxiliary") if isinstance(usage, dict) else None
    if not isinstance(aux, dict):
        return 0
    return sum(_count(aux.get(key)) or 0 for key in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))


# --------------------------------------------------------------------------- judge prompt


def load_judge_template() -> str:
    return JUDGE_PROMPT.read_text(encoding="utf-8")


def _criteria_lines(criteria: dict[str, str]) -> str:
    return "\n".join(f"- {json.dumps(key)}: {text}" for key, text in criteria.items())


def render_judge_prompt(template: str, state: dict[str, Any], optional: dict[str, Any]) -> str:
    recovery = ""
    if "next_observation" in optional:
        recovery = (
            "4. next_observation: if the run is stalled, pick exactly one key for the best read-only next step.\n"
            + _criteria_lines(optional["next_observation"]["criteria"])
            + "\n"
        )
    return (
        template.replace("<<STATE_JSON>>", json.dumps(state, ensure_ascii=False, sort_keys=True))
        .replace("<<TRAJECTORY_CRITERIA>>", _criteria_lines(optional["trajectory"]["criteria"]))
        .replace("<<RECOVERY_BLOCK>>\n", recovery)
    )


def _unit(value: Any) -> float | None:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        return None
    return float(value)


def _distribution(choice: str, keys: list[str], winning: float) -> dict[str, float]:
    others = [key for key in keys if key != choice]
    if not others:
        return {choice: 1.0}
    share = round((1.0 - winning) / len(others), 6)
    probabilities = {key: share for key in others}
    probabilities[choice] = round(1.0 - share * len(others), 6)
    return {key: probabilities[key] for key in keys}


def _choice(choice: str, criteria: dict[str, str], confidence: float) -> dict[str, Any]:
    # The winning probability equals the stated confidence, but it is never
    # below a uniform share, so the choice stays the arg-max the client checks.
    keys = list(criteria)
    winning = max(confidence, 1.0 / len(keys))
    return {"choice": choice, "probabilities": _distribution(choice, keys, winning), "confidence": confidence}


def parse_judge_reply(text: str, optional: dict[str, Any]) -> dict[str, Any] | None:
    """Map the main model's JSON reply onto Jev answer shapes, or None when invalid."""
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        reply = json.loads(match.group(0))
    except json.JSONDecodeError:
        return None
    if not isinstance(reply, dict):
        return None
    trajectory = optional["trajectory"]["criteria"]
    choice = reply.get("trajectory")
    confidence = _unit(reply.get("confidence"))
    evidence = _unit(reply.get("new_goal_evidence"))
    if choice not in trajectory or confidence is None or evidence is None:
        return None
    answers: dict[str, Any] = {
        "trajectory": _choice(choice, trajectory, confidence),
        "new_goal_evidence": {"noul": evidence},
    }
    if "next_observation" in optional:
        criteria = optional["next_observation"]["criteria"]
        suggestion = reply.get("next_observation")
        if suggestion not in criteria:
            return None
        answers["next_observation"] = _choice(suggestion, criteria, confidence)
    return answers


def abstain_answers(optional: dict[str, Any]) -> dict[str, Any]:
    """Valid answers that the tracker reads as an abstention (it cannot stop on them)."""
    answers: dict[str, Any] = {
        "trajectory": _choice("unclear", optional["trajectory"]["criteria"], 0.0),
        "new_goal_evidence": {"noul": 0.5},
    }
    if "next_observation" in optional:
        criteria = optional["next_observation"]["criteria"]
        fallback = "RETURN_INCOMPLETE" if "RETURN_INCOMPLETE" in criteria else next(iter(criteria))
        answers["next_observation"] = _choice(fallback, criteria, 0.0)
    return answers


# --------------------------------------------------------------------------- main-model judge


def check_real_hermes_home(environ: dict[str, str] | os._Environ, *, home: Path | None = None) -> None:
    """Refuse a scratch HERMES_HOME: the global CLI must use the user's real home."""
    value = environ.get("HERMES_HOME")
    if not value:
        return
    real = ((home or Path.home()) / ".hermes").resolve()
    if Path(value).expanduser().resolve() != real:
        raise RuntimeError("HERMES_HOME points away from the real Hermes home; unset it for the A' run")


def oneshot_command(hermes: str, prompt: str, usage_path: str) -> list[str]:
    return [
        hermes, "--safe-mode", "--ignore-rules", "-t", MAIN_TOOLSETS,
        "-m", MAIN_MODEL, "--provider", MAIN_PROVIDER, "--reasoning", PLAN_REASONING,
        "--usage-file", usage_path, "-z", prompt,
    ]


class HermesRunner:
    """Run one frozen judgment prompt through the global ``hermes -z`` CLI."""

    def __init__(self, hermes: str = "hermes", timeout: float = JUDGE_TIMEOUT_SECONDS):
        check_real_hermes_home(os.environ)
        self.hermes = hermes
        self.timeout = timeout

    def __call__(self, prompt: str) -> dict[str, Any]:
        with tempfile.TemporaryDirectory(prefix="bp-judge-") as tmp:
            usage_path = Path(tmp) / "usage.json"
            started = time.perf_counter()
            stdout, exit_code = "", None
            try:
                done = subprocess.run(
                    oneshot_command(self.hermes, prompt, str(usage_path)),
                    capture_output=True, text=True, check=False, timeout=self.timeout,
                )
                stdout, exit_code = done.stdout, done.returncode
            except subprocess.TimeoutExpired:
                exit_code = "timeout"
            wall_ms = round((time.perf_counter() - started) * 1000, 3)
            try:
                usage = json.loads(usage_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                usage = {}
        return {"stdout": stdout, "usage": usage if isinstance(usage, dict) else {}, "exit_code": exit_code,
                "wall_ms": wall_ms}


class MainModelJudge:
    """Answer the optional progress questions with the main model, under a hard call cap."""

    def __init__(self, runner: Callable[[str], dict[str, Any]], *, max_calls: int, template: str | None = None):
        self.runner = runner
        self.max_calls = max_calls
        self.template = template if template is not None else load_judge_template()
        self.calls: list[dict[str, Any]] = []

    def __call__(self, state: dict[str, Any], optional: dict[str, Any]) -> dict[str, Any]:
        if len(self.calls) >= self.max_calls:
            raise RuntimeError("main-model call cap reached")
        prompt = render_judge_prompt(self.template, state, optional)
        result = self.runner(prompt)
        usage = result.get("usage") or {}
        answers = None
        if result.get("exit_code") != 0:
            status = "timeout" if result.get("exit_code") == "timeout" else "hermes_failed"
        elif usage.get("model") not in REPORTED_MAIN_MODELS or usage.get("provider") != MAIN_PROVIDER:
            status = "wrong_route"
        else:
            answers = parse_judge_reply(result.get("stdout") or "", optional)
            status = "ok" if answers is not None else "invalid_reply"
        record = {
            "status": status,
            "questions": sorted(optional),
            "wall_ms": result.get("wall_ms"),
            "exit_code": result.get("exit_code"),
            "model": usage.get("model"),
            "provider": usage.get("provider"),
            "api_calls": usage.get("api_calls"),
            "input_tokens": usage.get("input_tokens"),
            "cache_read_tokens": usage.get("cache_read_tokens"),
            "cache_write_tokens": usage.get("cache_write_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
            "harness_auxiliary_tokens": _aux_tokens(usage),
            "list_price_usd": list_price_usd(usage),
            "answer": None if answers is None else {
                "trajectory": answers["trajectory"]["choice"],
                "confidence": answers["trajectory"]["confidence"],
                "new_goal_evidence": answers["new_goal_evidence"]["noul"],
                "next_observation": (answers.get("next_observation") or {}).get("choice"),
            },
        }
        self.calls.append(record)
        return answers if answers is not None else abstain_answers(optional)


class JudgeExchange:
    """Route the optional questions of each request to the judge; Jev gets the rest.

    ``inner`` is the arm's normal ``_post_attempt`` (the scripted fake offline).
    Jev receives the base state and base questions, as in B. The judge sees the
    full state C would send, including ``previous_page``.
    """

    def __init__(self, inner: Callable[..., Any], judge: MainModelJudge):
        self.inner = inner
        self.judge = judge
        self.bodies: list[bytes] = []
        self.judge_steps: list[int] = []

    def __call__(self, path: str, body: bytes, headers: dict[str, str], *, allow_stale_retry: bool):
        self.bodies.append(body)
        payload = json.loads(body.decode("utf-8"))
        questions = payload.get("questions") or {}
        optional = {name: questions[name] for name in evaluate_optional_names() if name in questions}
        if not optional:
            return self.inner(path, body, headers, allow_stale_retry=allow_stale_retry)
        state = payload.get("state") or {}
        base = dict(payload)
        base["questions"] = {name: value for name, value in questions.items() if name not in optional}
        base["state"] = {key: value for key, value in state.items() if key != "previous_page"}
        base_body = json.dumps(base, ensure_ascii=False, allow_nan=False).encode("utf-8")
        raw, status, retry_after, stale = self.inner(path, base_body, headers, allow_stale_retry=allow_stale_retry)
        if raw is None:
            return raw, status, retry_after, stale
        response = json.loads(raw)
        self.judge_steps.append(len(self.bodies) - 1)
        response.setdefault("answers", {}).update(self.judge(state, optional))
        return json.dumps(response).encode("utf-8"), status, retry_after, stale


def evaluate_optional_names() -> tuple[str, ...]:
    return ("trajectory", "new_goal_evidence", "next_observation")


# --------------------------------------------------------------------------- live


class ResponseCostRecorder:
    """Count physical Jev requests and read the provider-reported cost. Headers are never kept."""

    def __init__(self, cap: int):
        self.cap = cap
        self.records: list[dict[str, Any]] = []

    def call(self, post: Callable[..., Any], path: str, body: bytes, headers: dict[str, str], *, allow_stale_retry: bool):
        if len(self.records) >= self.cap:
            raise LiveCapReached("live physical request cap reached")
        record: dict[str, Any] = {"bytes": len(body), "status": None, "cost": None}
        self.records.append(record)
        raw, status, retry_after, stale = post(path, body, headers, allow_stale_retry=allow_stale_retry)
        record["status"] = status
        if raw is not None:
            try:
                usage = json.loads(raw).get("usage") or {}
                cost = usage.get("cost")
                if type(cost) in (int, float) and math.isfinite(cost) and cost >= 0:
                    record["cost"] = cost
            except (ValueError, AttributeError):
                pass
        return raw, status, retry_after, stale

    def wrap(self, post: Callable[..., Any]) -> Callable[..., Any]:
        def wrapped(path, body, headers, *, allow_stale_retry):
            return self.call(post, path, body, headers, allow_stale_retry=allow_stale_retry)

        return wrapped


def run_live_pairs(
    order: list[dict[str, str]],
    run_one: Callable[[dict[str, str], int], dict[str, Any]],
    *,
    total_cap: int,
    per_run_cap: int,
) -> tuple[list[dict[str, Any]], int]:
    """Run interleaved (goal, arm) items in pairs; skip a whole pair when the cap cannot cover it."""
    rows: list[dict[str, Any]] = []
    used = 0
    for index in range(0, len(order), 2):
        pair = order[index:index + 2]
        if used + per_run_cap * len(pair) > total_cap:
            rows.extend({"goal": item["goal"], "arm": item["arm"], "skipped": "live_cap"} for item in pair)
            continue
        for item in pair:
            row = run_one(item, per_run_cap)
            used += int(row.get("physical_requests") or 0)
            rows.append(row)
    return rows, used


JEV_KEY_NAMES = ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY")
DEFAULT_KEY_HELPER = Path.home() / ".hermes" / "scripts" / "hermes-keepass-export.py"


def jev_key_env(environ: Any, *, helper: Path, scope: str) -> dict[str, str]:
    """Jev key variables for the live children. Values are never printed or written."""
    if any(str(environ.get(name) or "").strip() for name in JEV_KEY_NAMES):
        return {}
    proc = subprocess.run([sys.executable, str(helper), "--scope", scope], capture_output=True, text=True,
                          timeout=60, check=False)
    if proc.returncode != 0:
        raise RuntimeError("jev_key_helper_failed")
    keys: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        name, sep, value = line.partition("=")
        if sep and name in JEV_KEY_NAMES and value.strip():
            keys[name] = value.strip()
    if not keys:
        raise RuntimeError("jev_key_missing")
    return keys


def live_order() -> list[dict[str, str]]:
    order = []
    for index, goal in enumerate(LIVE_GOAL_ORDER):
        first, second = ("B", "C") if index % 2 == 0 else ("C", "B")
        order += [{"goal": goal, "arm": first}, {"goal": goal, "arm": second}]
    return order


def live_child(arm: str, goal_id: str, cap: int) -> dict[str, Any]:
    """Run one live goal in this interpreter (the arm's package is first on sys.path)."""
    from unittest import mock

    import hermes_switchyard
    import hermes_switchyard.client as client_module

    goal = next(item for item in json.loads(evaluate.LIVE.read_text())["goals"] if item["id"] == goal_id)
    recorder = ResponseCostRecorder(cap)
    original = client_module.DecisionClient._post_attempt

    def patched(self, path, body, headers, *, allow_stale_retry):
        try:
            return recorder.call(lambda *a, **k: original(self, *a, **k), path, body, headers,
                                 allow_stale_retry=allow_stale_retry)
        except LiveCapReached as exc:
            raise client_module.JevRequestError(str(exc), detail="retry_budget_exhausted") from None

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

    settings: dict[str, Any] = {"jev_provider": "auto"}
    if arm == "C":
        settings["browser_progress_mode"] = "advisory_stop"
    if os.environ.get("SWITCHYARD_EVAL_BROWSER"):
        settings["browser_executable"] = os.environ["SWITCHYARD_EVAL_BROWSER"]
    context = Context(settings)
    hermes_switchyard.register(context)
    args = {"goal": goal["goal"], "start_url": goal["start_url"], "max_steps": LIVE_MAX_STEPS,
            "min_actions_before_done": goal.get("min_actions_before_done", 0)}
    if goal.get("completion_condition"):
        args["completion_condition"] = goal["completion_condition"]
    started = time.perf_counter()
    with mock.patch.object(client_module.DecisionClient, "_post_attempt", patched):
        raw = context.tools["jev_computer_use"](args)
    wall_ms = round((time.perf_counter() - started) * 1000, 1)
    result = json.loads(raw)
    progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
    expect = goal["expect"]
    completed = result.get("status") == "completion_candidate" and expect != "incomplete"
    return {
        "goal": goal_id, "arm": arm, "expect": expect,
        "label": "semantic_stall" if expect == "incomplete" else "productive",
        "status": result.get("status"), "failure_phase": result.get("failure_phase"),
        "failure_reason": result.get("failure_reason"), "error": result.get("error"),
        "completed": completed,
        "false_completion": result.get("status") == "completion_candidate" and expect == "incomplete",
        "goal_verified": result.get("goal_verified"),
        "semantic_stop": result.get("failure_phase") == "semantic_stall",
        "recovery_suggestion": progress.get("recovery_suggestion"),
        "dispatched_actions": result.get("action_dispatched_count"),
        "jev_request_count": result.get("jev_request_count"),
        "physical_requests": len(recorder.records),
        "request_records": recorder.records,
        "known_cost": round(sum(r["cost"] for r in recorder.records if r["cost"] is not None), 10),
        "unknown_cost_count": sum(1 for r in recorder.records if r["cost"] is None),
        "model": progress.get("model"),
        "task_latency_ms": wall_ms,
        "session_setup_ms": result.get("session_setup_ms"),
        "steps": [],
    }


# --------------------------------------------------------------------------- acceptance


def summarize_arm(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rows = [row for row in rows if not row.get("skipped")]
    latencies = [row["task_latency_ms"] for row in rows if row.get("task_latency_ms") is not None]
    stalls = [row["case"] if "case" in row else row["goal"] for row in rows if row.get("label") == "semantic_stall"]

    def key(row):
        return row["case"] if "case" in row else row["goal"]

    return {
        "cases": len(rows),
        "completed": sum(bool(row.get("completed")) for row in rows),
        "false_completions": [key(row) for row in rows if row.get("false_completion")],
        "labelled_stalls": stalls,
        "early_stops_with_suggestion": [
            key(row) for row in rows
            if row.get("label") == "semantic_stall" and row.get("semantic_stop")
            and ((row.get("recovery_suggestion") or {}).get("selected") is not None)
        ],
        "premature_stops": [key(row) for row in rows if row.get("label") == "productive" and row.get("semantic_stop")],
        "dispatched_actions": sum(int(row.get("dispatched_actions") or 0) for row in rows),
        "task_latency_p50_ms": evaluate.percentile(latencies, 0.5),
        "task_latency_total_ms": round(sum(latencies), 3),
        "known_cost_usd": round(sum(float(row.get("known_cost") or 0.0) for row in rows), 10),
        "null_cost_count": sum(int(row.get("unknown_cost_count") or 0) for row in rows),
        # Offline requests that get an injected 429/529/outage have no response
        # and so no cost. They are counted in null_cost_count; this shows how many.
        "null_cost_from_injected_faults": sum(
            1 for row in rows for step in row.get("steps") or [] for fault in step.get("faults") or []
            if fault in NULL_COST_FAULTS
        ),
    }


def acceptance(arms: dict[str, list[dict[str, Any]]], *, baselines: tuple[str, ...]) -> dict[str, Any]:
    """Apply the frozen release rules: C against every listed baseline."""
    summaries = {arm: summarize_arm(rows) for arm, rows in arms.items() if arm == "C" or arm in baselines}
    c = summaries["C"]
    rules: dict[str, dict[str, Any]] = {}
    outcome_fail = []
    for base in baselines:
        b = summaries[base]
        if c["completed"] < b["completed"]:
            outcome_fail.append(f"completion below {base}: {c['completed']} < {b['completed']}")
        if len(c["early_stops_with_suggestion"]) <= len(b["early_stops_with_suggestion"]):
            outcome_fail.append(
                f"early stops with suggestion not above {base}: "
                f"{len(c['early_stops_with_suggestion'])} <= {len(b['early_stops_with_suggestion'])}"
            )
    if c["premature_stops"]:
        outcome_fail.append(f"premature stops: {c['premature_stops']}")
    if c["false_completions"]:
        outcome_fail.append(f"false completions: {c['false_completions']}")
    rules["outcome"] = {"passed": not outcome_fail, "failures": outcome_fail}
    for name, field in (("latency_p50", "task_latency_p50_ms"), ("latency_total", "task_latency_total_ms")):
        fails = [
            f"{field} above {base}: {c[field]} > {summaries[base][field]}"
            for base in baselines
            if c[field] is None or summaries[base][field] is None or c[field] > summaries[base][field]
        ]
        rules[name] = {"passed": not fails, "failures": fails}
    cost_fail = [f"null cost in {arm}: {summaries[arm]['null_cost_count']}"
                 for arm in ("C", *baselines) if summaries[arm]["null_cost_count"]]
    cost_fail += [
        f"cost above {base}: {c['known_cost_usd']} > {summaries[base]['known_cost_usd']}"
        for base in baselines if c["known_cost_usd"] > summaries[base]["known_cost_usd"]
    ]
    rules["cost"] = {"passed": not cost_fail, "failures": cost_fail}
    return {"passed": all(rule["passed"] for rule in rules.values()), "rules": rules, "summaries": summaries}


# --------------------------------------------------------------------------- freeze


def build_plan() -> dict[str, Any]:
    book, fixture_sha = evaluate.load_book()
    return {
        "schema_version": 1,
        "suite": "browser_progress_e2e",
        "fixtures_sha256": fixture_sha,
        "live_sha256": evaluate.sha256_bytes(evaluate.LIVE.read_bytes()),
        "judge_prompt_sha256": evaluate.sha256_bytes(JUDGE_PROMPT.read_bytes()),
        "pricing": PRICING,
        "a_prime": {
            "split": "heldout",
            "cases": [case["id"] for case in book["cases"] if case["split"] == "heldout"],
            "model": MAIN_MODEL, "provider": MAIN_PROVIDER, "reasoning": PLAN_REASONING,
            "toolsets": MAIN_TOOLSETS, "flags": ["--safe-mode", "--ignore-rules"],
            "hermes_home": "real (~/.hermes); a scratch HERMES_HOME is refused",
            "judge_timeout_seconds": JUDGE_TIMEOUT_SECONDS,
            "max_main_model_calls": evaluate.a_prime_call_bound(book),
            "stop_rule": "candidate _ProgressTracker, same thresholds as C (fixtures.lock.json policy)",
            "failure_rule": "a failed, timed-out, wrong-route, or invalid judgment is an abstention; its measured tokens still count",
            "latency": "per-step local loop time (includes the real main-model wall time) plus the modeled Jev time, as for B and C",
        },
        "live": {
            "arms": ["B", "C"],
            "order": live_order(),
            "max_steps": LIVE_MAX_STEPS,
            "per_run_physical_cap": LIVE_PER_RUN_CAP,
            "max_physical_requests": LIVE_TOTAL_CAP,
            "pair_rule": "a pair runs only when the cap left covers both runs at the per-run cap; else both are skipped",
            "jev_provider": "auto",
            "fallback": "none; a route failure is recorded as a failed run",
            "completed": "status completion_candidate on a productive or done_on_first_page goal",
            "latency": "handler wall time per goal, including browser and network",
            "cost": "sum of provider-reported usage.cost per physical Jev request; a missing cost is null",
        },
        "acceptance": {
            "source": "/home/brian/.hermes/cache/scratch/v055-eval-acceptance.md",
            "baselines": {"heldout": ["A_prime", "B"], "live": ["B"]},
            "outcome": ("C completion >= every baseline; C labelled stalls stopped early with a recovery suggestion "
                        "> every baseline; 0 premature stops; 0 false completions"),
            "latency": "C task latency p50 and total <= every baseline",
            "cost": "C total known cost <= every baseline; any null cost in C or a baseline fails",
            "tuning": "none after results; a failing rule is published as is",
        },
    }


def load_plan() -> dict[str, Any]:
    return json.loads(PLAN_PATH.read_text(encoding="utf-8"))


def _git_dirty(paths: tuple[str, ...]) -> list[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--", *paths],
                         capture_output=True, text=True, check=True).stdout
    return [line[3:] for line in out.splitlines() if line.strip()]


def require_frozen(*, plan: dict[str, Any] | None = None,
                   dirty_paths: Callable[[tuple[str, ...]], list[str]] = _git_dirty) -> dict[str, Any]:
    """Refuse model or live calls unless the frozen files are committed and match the plan."""
    plan = plan if plan is not None else load_plan()
    dirty = dirty_paths(FROZEN_FILES)
    if dirty:
        raise RuntimeError(f"frozen files are not committed: {dirty}")
    if plan.get("judge_prompt_sha256") != evaluate.sha256_bytes(JUDGE_PROMPT.read_bytes()):
        raise RuntimeError("judge prompt changed since the plan was frozen")
    _book, fixture_sha = evaluate.load_book()
    if plan.get("fixtures_sha256") != fixture_sha or plan.get("live_sha256") != evaluate.sha256_bytes(evaluate.LIVE.read_bytes()):
        raise RuntimeError("fixtures or live goals changed since the plan was frozen")
    if plan.get("pricing") != PRICING:
        raise RuntimeError("pricing changed since the plan was frozen")
    return plan


# --------------------------------------------------------------------------- drivers


def _read_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_a_prime(rows_path: Path, hermes: str) -> int:
    plan = require_frozen()
    # A' runs the candidate package in this interpreter.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    book, _ = evaluate.load_book()
    by_id = {case["id"]: case for case in book["cases"]}
    done = _read_rows(rows_path)
    used = sum(int(row.get("main_model_calls") or 0) for row in done)
    finished = {row["case"] for row in done}
    judge = MainModelJudge(HermesRunner(hermes), max_calls=plan["a_prime"]["max_main_model_calls"] - used)
    for case_id in plan["a_prime"]["cases"]:
        if case_id in finished:
            continue
        case = by_id[case_id]
        row = evaluate.run_jev_arm("A_prime", case, judge=judge)
        row.update({"case": case_id, "split": case["split"], "kind": case["kind"], "label": case["label"], "arm": "A_prime"})
        with rows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        print(json.dumps({"case": case_id, "main_model_calls": row["main_model_calls"],
                          "semantic_stop": row["semantic_stop"]}), flush=True)
    return 0


def run_live(rows_path: Path, baseline: Path, *, key_helper: Path, key_scope: str) -> int:
    plan = require_frozen()
    done = _read_rows(rows_path)
    if done:
        raise RuntimeError("live rows already exist; the live run is single-shot")
    live = plan["live"]
    # Fail before any browser or Jev call when no Jev key is available.
    child_keys = jev_key_env(os.environ, helper=key_helper, scope=key_scope)

    def run_one(item: dict[str, str], cap: int) -> dict[str, Any]:
        package = baseline if item["arm"] == "B" else ROOT
        env = {**os.environ, **child_keys}
        env["PYTHONPATH"] = os.pathsep.join([str(package), str(HERE)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
        command = [sys.executable, str(Path(__file__).resolve()), "--live-child", item["arm"], "--goal", item["goal"],
                   "--cap", str(cap)]
        proc = subprocess.run(command, capture_output=True, text=True, env=env, cwd=str(package), check=False, timeout=900)
        if proc.returncode != 0:
            row = {"goal": item["goal"], "arm": item["arm"], "status": "child_error",
                   "error": (proc.stderr or proc.stdout)[-1500:], "physical_requests": cap}
        else:
            row = json.loads(proc.stdout.strip().splitlines()[-1])
        with rows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, sort_keys=True) + "\n")
        print(json.dumps({key: row.get(key) for key in ("goal", "arm", "status", "physical_requests")}), flush=True)
        return row

    rows, used = run_live_pairs(live["order"], run_one, total_cap=live["max_physical_requests"],
                                per_run_cap=live["per_run_physical_cap"])
    for row in rows:
        if row.get("skipped"):
            with rows_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, sort_keys=True) + "\n")
    print(json.dumps({"physical_requests_total": used, "cap": live["max_physical_requests"]}))
    return 0


def build_report(offline_path: Path, a_prime_path: Path, live_path: Path | None) -> dict[str, Any]:
    plan = load_plan()
    offline = json.loads(offline_path.read_text(encoding="utf-8"))
    heldout = {arm: [row for row in offline["rows"][arm] if row["split"] == "heldout"] for arm in ("B", "C")}
    for arm_rows in heldout.values():
        for row in arm_rows:
            row.setdefault("task_latency_ms", round(sum(step["latency_ms"] for step in row["steps"]), 3))
    heldout["A_prime"] = _read_rows(a_prime_path)
    report: dict[str, Any] = {
        "plan": plan,
        "offline_candidate_head": offline.get("candidate_head"),
        "heldout": acceptance(heldout, baselines=tuple(plan["acceptance"]["baselines"]["heldout"])),
    }
    if live_path is not None:
        live_rows = _read_rows(live_path)
        arms = {arm: [row for row in live_rows if row.get("arm") == arm] for arm in ("B", "C")}
        report["live"] = acceptance(arms, baselines=tuple(plan["acceptance"]["baselines"]["live"]))
        report["live"]["skipped"] = [row for row in live_rows if row.get("skipped")]
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--write-plan", action="store_true", help="write e2e_plan.json from the current frozen inputs")
    parser.add_argument("--a-prime", action="store_true", help="run the A' main-model arm on the held-out traces")
    parser.add_argument("--live", action="store_true", help="run the interleaved live B/C pairs")
    parser.add_argument("--report", action="store_true", help="build the acceptance report from row files")
    parser.add_argument("--rows", help="JSONL row file (A' or live)")
    parser.add_argument("--hermes-command", default="hermes")
    parser.add_argument("--baseline-root", help="v0.5.4 source tree (live B)")
    parser.add_argument("--jev-key-helper", default=str(DEFAULT_KEY_HELPER),
                        help="KDBX export helper; used only when no Jev key is in the environment")
    parser.add_argument("--jev-key-scope", default="default")
    parser.add_argument("--offline", help="offline evaluate.py --output JSON (report)")
    parser.add_argument("--a-prime-rows", help="A' rows JSONL (report)")
    parser.add_argument("--live-rows", help="live rows JSONL (report)")
    parser.add_argument("--output", help="report JSON path")
    parser.add_argument("--live-child", help=argparse.SUPPRESS)
    parser.add_argument("--goal", help=argparse.SUPPRESS)
    parser.add_argument("--cap", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.live_child:
        print(json.dumps(live_child(args.live_child, args.goal, args.cap), sort_keys=True))
        return 0
    if args.write_plan:
        PLAN_PATH.write_text(json.dumps(build_plan(), indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    if args.a_prime:
        if not args.rows:
            parser.error("--a-prime needs --rows")
        return run_a_prime(Path(args.rows), args.hermes_command)
    if args.live:
        if not args.rows or not args.baseline_root:
            parser.error("--live needs --rows and --baseline-root")
        return run_live(Path(args.rows), Path(args.baseline_root), key_helper=Path(args.jev_key_helper),
                        key_scope=args.jev_key_scope)
    if args.report:
        if not args.offline or not args.a_prime_rows or not args.output:
            parser.error("--report needs --offline, --a-prime-rows, and --output")
        report = build_report(Path(args.offline), Path(args.a_prime_rows), Path(args.live_rows) if args.live_rows else None)
        Path(args.output).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps({"heldout_passed": report["heldout"]["passed"],
                          "live_passed": (report.get("live") or {}).get("passed")}))
        return 0
    parser.error("choose --write-plan, --a-prime, --live, or --report")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
