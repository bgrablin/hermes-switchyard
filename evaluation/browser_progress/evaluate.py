#!/usr/bin/env python3
"""DOM Progress & Recovery (F2) evaluation: arms A, B, and C on frozen traces.

Arms
  A  plugin disabled: the case's fixed action plan is replayed on the fake
     browser with no Jev call. It is a safety and comparability control, not
     the same controller as B and C.
  B  v0.5.4 behavior: ``run_browser_goal`` loaded from the pinned ``v0.5.4``
     tag source, with the same fake browser and the same fake Jev answers.
  C  candidate: ``run_browser_goal`` from this tree with
     ``progress_mode="advisory_stop"``. A fourth row, C_off, runs the
     candidate with the default ``off`` mode to show that it asks no feature
     questions.

B and C use a real ``DecisionClient``. Only its HTTP exchange
(``_post_attempt``) is replaced by a scripted fake, so request building,
batching, response validation, 429/529 retries, and accounting are the
shipped code. Each arm runs in its own interpreter so the v0.5.4 package
cannot mix with the candidate.

Latency: offline step latency is the measured local loop time plus a fixed
modeled 250 ms per physical Jev request (the F1 live p50 was 243 ms) plus
the client's own computed retry delay. It does not model token-dependent
provider time. Cost: a synthetic price of 2e-7 USD per input token, with
tokens estimated as request bytes / 4, so cost follows request size.
Offline results check wiring and policy only; they do not measure model
accuracy.

Live mode (``--live``) runs the four frozen public goals in ``live.json``
through the real registered ``jev_computer_use`` handler, a real browser,
and the plugin's configured Jev route. It refuses to start if a fixture hash
changed since the freeze, and it stops sending when the physical request cap
would be exceeded.
"""

from __future__ import annotations

import argparse
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

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
FIXTURES = HERE / "fixtures.json"
LIVE = HERE / "live.json"
LOCK = HERE / "fixtures.lock.json"
BASELINE_TAG = "v0.5.4"
KINDS = {
    "semantic_stall", "productive_multi_hop", "false_stall_trap", "unchanged_scroll", "a_b_a",
    "async_wait", "useful_revisit", "predicate_satisfied", "caller_value_masked", "rate_limited",
    "malformed_optional", "model_outage", "destination_denial",
}
LABELS = {"semantic_stall", "productive", "baseline_stop", "provider_failure", "destination_denial"}
FAULTS = {"http_429", "http_529", "malformed_optional", "outage"}
OPERATIONS = {"CLICK", "TYPE_TEXT", "SCROLL_DOWN", "SCROLL_UP", "WAIT", "DONE", "BLOCKED"}
TRAJECTORY = ("progress", "stagnant", "regression", "unclear")
MODEL = "typesafe/jev-1.13"
MODELED_REQUEST_MS = 250.0
PRICE_PER_TOKEN = 2e-7
MAX_LIVE_PHYSICAL = 24
MAX_LIVE_STEPS = 5
STALL_GATE = 4


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def require(condition: bool, code: str) -> None:
    if not condition:
        raise ValueError(code)


# --------------------------------------------------------------------------- fixtures


def load_book(path: Path = FIXTURES) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    book = json.loads(raw)
    validate_book(book)
    return book, sha256_bytes(raw)


def validate_book(book: Any) -> None:
    require(isinstance(book, dict) and book.get("schema_version") == 1, "fixture_schema")
    require(book.get("suite") == "browser_progress" and book.get("spec_version") == "dom-progress-v1", "fixture_suite")
    require(book.get("data_class") == "public_synthetic", "fixture_data_class")
    cases = book.get("cases")
    require(isinstance(cases, list), "fixture_cases")
    seen: set[str] = set()
    for case in cases:
        cid = case.get("id")
        require(isinstance(cid, str) and cid not in seen, "fixture_id")
        seen.add(cid)
        require(case.get("split") in {"dev", "heldout"}, f"{cid}:split")
        require(case.get("kind") in KINDS, f"{cid}:kind")
        require(case.get("label") in LABELS, f"{cid}:label")
        states = case.get("states")
        require(isinstance(states, dict) and case.get("start") in states, f"{cid}:states")
        for key, state in states.items():
            require(state["url"].startswith("https://example.org/"), f"{cid}:{key}:url")
            for element in state["elements"]:
                if "to" in element:
                    require(element["to"] in states, f"{cid}:{key}:to")
        script = case.get("script")
        require(isinstance(script, list) and script, f"{cid}:script")
        for entry in script:
            require(entry.get("op") in OPERATIONS, f"{cid}:op")
            require(entry.get("traj") in TRAJECTORY, f"{cid}:traj")
            require(0 <= entry.get("noul", 0.5) <= 1, f"{cid}:noul")
            require(entry.get("fault") in (None, *FAULTS), f"{cid}:fault")
        require(type(case.get("max_steps")) is int and 1 <= case["max_steps"] <= 10, f"{cid}:max_steps")
    require(sum(c["split"] == "dev" for c in cases) == 8, "dev_count")
    require(sum(c["split"] == "heldout" for c in cases) == 32, "heldout_count")
    require(
        sum(c["split"] == "heldout" and c["kind"] == "semantic_stall" for c in cases) == 8,
        "heldout_stall_count",
    )


def check_lock(fixture_sha: str, live_sha: str) -> dict[str, Any]:
    lock = json.loads(LOCK.read_text())
    require(lock.get("fixtures_sha256") == fixture_sha, "fixture_hash_changed_since_freeze")
    require(lock.get("live_sha256") == live_sha, "live_hash_changed_since_freeze")
    return lock


# --------------------------------------------------------------------------- fake browser


class FakeBrowser:
    """A deterministic public DOM session over fixed fixture states."""

    def __init__(self, case: dict[str, Any]):
        self.case = case
        self.states = case["states"]
        self.key = case["start"]
        self.violations: list[dict[str, Any]] = []
        self.used_once: set[tuple[str, str]] = set()
        self.dispatched = 0
        self.after_violation = 0
        self.denied_reached = False

    def observe(self) -> dict[str, Any]:
        state = self.states[self.key]
        elements = []
        for item in state["elements"]:
            elements.append({key: item[key] for key in ("id", "role", "label", "href", "kind") if key in item})
        return {
            "url": state["url"],
            "title": state["title"],
            "text": state["text"],
            "document_id": f"doc-{self.key}",
            "elements": elements,
        }

    def _element(self, element_id: str) -> dict[str, Any]:
        return next(item for item in self.states[self.key]["elements"] if item["id"] == element_id)

    def _note(self) -> None:
        self.dispatched += 1
        if self.violations:
            self.after_violation += 1

    def click(self, element_id: str, label: str = "", href: str = "") -> None:
        self._note()
        target = self._element(element_id)
        if label and target["label"] != label:
            raise RuntimeError("stale label")
        if target.get("denied"):
            # The fake browser guard refuses the navigation and records it.
            self.violations.append({"code": "navigation_refused", "fatal": True})
            return
        if target.get("no_effect_once") and (self.key, element_id) not in self.used_once:
            self.used_once.add((self.key, element_id))
            return
        if "to" in target:
            self.key = target["to"]

    def type_text(self, element_id: str, value: str, label: str = "") -> dict[str, bool]:
        self._note()
        target = self._element(element_id)
        if "type_to" in target:
            self.key = target["type_to"]
        return {"accepted": True, "changed": True}

    def text_retained(self, element_id: str, value: str, document_id: str) -> bool:
        return False

    def scroll(self, direction: str) -> None:
        self._note()
        nxt = self.states[self.key].get("scroll_to")
        if nxt:
            self.key = nxt

    def wait(self, seconds: float = 0.2) -> None:
        nxt = self.states[self.key].get("wait_to")
        if nxt:
            self.key = nxt

    def destination_violations(self, check_targets: bool = True) -> list[dict[str, Any]]:
        return list(self.violations)

    def close(self) -> None:
        return None

    def at_goal(self) -> bool:
        if self.case.get("goal_state"):
            return self.key == self.case["goal_state"]
        return bool(self.states[self.key].get("goal"))


# --------------------------------------------------------------------------- fake Jev exchange


def _choice_answer(choice: str, criteria: dict[str, str], winning: float, confidence: float) -> dict[str, Any]:
    others = [key for key in criteria if key != choice]
    probabilities = {choice: 1.0 if not others else winning}
    for key in others:
        probabilities[key] = round((1.0 - winning) / len(others), 6)
    if others:
        probabilities[choice] = round(1.0 - sum(probabilities[key] for key in others), 6)
    return {"choice": choice, "probabilities": probabilities, "confidence": confidence}


class FakeExchange:
    """Replace ``DecisionClient._post_attempt`` with scripted answers.

    A new logical step starts when the page and recent actions in the state
    differ from the previous request. A transport retry and the optional-answer
    retry keep the same page and actions, so they stay in the same step.
    """

    def __init__(self, case: dict[str, Any], client_module: Any):
        self.case = case
        self.script = case["script"]
        self.client_module = client_module
        self.step = -1
        self.step_key: str | None = None
        self.log: list[dict[str, Any]] = []
        self.fault_used: set[int] = set()
        self.retry_delays: list[float] = []
        self.bodies: list[str] = []

    def entry(self) -> dict[str, Any]:
        return self.script[min(self.step, len(self.script) - 1)]

    def __call__(self, path: str, body: bytes, headers: dict[str, str], *, allow_stale_retry: bool):
        text = body.decode("utf-8")
        self.bodies.append(text)
        payload = json.loads(text)
        state, questions = payload["state"], payload["questions"]
        key = json.dumps([state.get("page"), state.get("recent_actions")], sort_keys=True)
        if key != self.step_key:
            self.step += 1
            self.step_key = key
        entry = self.entry()
        fault = entry.get("fault")
        record = {
            "step": self.step,
            "bytes": len(body),
            "questions": sorted(questions),
            "at": time.perf_counter(),
            "fault": None,
            "cost": round(len(body) / 4 * PRICE_PER_TOKEN, 10),
        }
        self.log.append(record)
        first = self.step not in self.fault_used
        if fault in {"http_429", "http_529"} and first:
            self.fault_used.add(self.step)
            record["fault"] = fault
            record["cost"] = None
            return None, int(fault[-3:]), "0", False
        if fault == "outage" and first:
            self.fault_used.add(self.step)
            record["fault"] = fault
            record["cost"] = None
            raise self.client_module.JevRequestError("Jev connection failed: OSError", detail="connect_failed")
        answers = self.answers(entry, questions)
        if fault == "malformed_optional" and first and "trajectory" in questions:
            self.fault_used.add(self.step)
            record["fault"] = fault
            answers["trajectory"] = dict(answers["trajectory"], choice="stuck")
        response = {
            "model": MODEL,
            "answers": answers,
            "usage": {"cost": record["cost"]},
            "latency_ms": MODELED_REQUEST_MS,
        }
        return json.dumps(response).encode("utf-8"), None, None, False

    @staticmethod
    def answers(entry: dict[str, Any], questions: dict[str, Any]) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        operation = questions["operation"]["criteria"]
        op = entry["op"]
        if op not in operation:
            # A scripted operation that the page does not offer falls back to a
            # read-only scroll, then to BLOCKED.
            op = "SCROLL_DOWN" if "SCROLL_DOWN" in operation else "BLOCKED"
        answers["operation"] = _choice_answer(op, operation, 0.91, 0.9)
        for name in ("click_target", "type_target"):
            if name in questions:
                criteria = questions[name]["criteria"]
                target = entry.get("target") if entry.get("target") in criteria else next(iter(criteria))
                answers[name] = _choice_answer(target, criteria, 0.91, 0.9)
        if "trajectory" in questions:
            answers["trajectory"] = _choice_answer(
                entry["traj"], questions["trajectory"]["criteria"], entry.get("p", 0.9), entry.get("conf", 0.9)
            )
        if "new_goal_evidence" in questions:
            answers["new_goal_evidence"] = {"noul": entry.get("noul", 0.5)}
        if "next_observation" in questions:
            criteria = questions["next_observation"]["criteria"]
            nxt = entry.get("nxt") if entry.get("nxt") in criteria else "RETURN_INCOMPLETE"
            answers["next_observation"] = _choice_answer(nxt, criteria, 0.9, 0.88)
        return answers


# --------------------------------------------------------------------------- arms


def run_a(case: dict[str, Any]) -> dict[str, Any]:
    """Replay the fixed action plan with no Jev and no plugin."""
    browser = FakeBrowser(case)
    status = "budget_exhausted"
    started = time.perf_counter()
    condition = case.get("completion_condition")
    actions = 0
    for step in range(case["max_steps"]):
        if browser.violations:
            status = "blocked"
            break
        entry = case["script"][min(step, len(case["script"]) - 1)]
        if entry.get("fault") == "outage":
            # The plan does not depend on a provider; the outage does not apply.
            pass
        op = entry["op"]
        page = browser.observe()
        if op == "DONE":
            status = "completion_candidate"
            break
        if op == "BLOCKED":
            status = "blocked"
            break
        ids = [item["id"] for item in page["elements"]]
        if op == "CLICK" and entry.get("target") in ids:
            browser.click(entry["target"])
        elif op == "TYPE_TEXT" and entry.get("target") in ids:
            browser.type_text(entry["target"], "")
        elif op in {"SCROLL_DOWN", "SCROLL_UP"} or op in {"CLICK", "TYPE_TEXT"}:
            browser.scroll("down")
        elif op == "WAIT":
            browser.wait()
        actions += 1
        if condition and condition.get("url_contains", "\0") in browser.observe()["url"]:
            status = "completion_candidate"
            break
    return {
        "status": status,
        "failure_phase": None,
        "completed": status == "completion_candidate" and browser.at_goal(),
        "false_completion": status == "completion_candidate" and not browser.at_goal(),
        "dispatched_actions": browser.dispatched,
        "logical_requests": 0,
        "physical_requests": 0,
        "wall_ms": round((time.perf_counter() - started) * 1000, 3),
        "destination_bypass": browser.after_violation > 0,
        "semantic_stop": False,
        "verified": False,
        "steps": [],
        "known_cost": 0.0,
        "unknown_cost_count": 0,
        "caller_value_leak": False,
        "feature_questions_sent": False,
    }


def run_jev_arm(arm: str, case: dict[str, Any]) -> dict[str, Any]:
    from unittest import mock

    import hermes_switchyard.browser_use as browser_use
    import hermes_switchyard.client as client_module

    browser = FakeBrowser(case)
    exchange = FakeExchange(case, client_module)
    client = client_module.DecisionClient(api_key="fixture-key")
    delays: list[float] = []
    kwargs: dict[str, Any] = {
        "goal": case["goal"],
        "client": client,
        "session": browser,
        "max_steps": case["max_steps"],
        "min_actions_before_done": case.get("min_actions_before_done", 0),
        "public_or_sanitized_data_ack": True,
        "completion_condition": case.get("completion_condition"),
        "text_inputs": case.get("text_inputs"),
    }
    if arm == "C":
        kwargs["progress_mode"] = "advisory_stop"
    elif arm == "C_off":
        kwargs["progress_mode"] = "off"
    started = time.perf_counter()
    with mock.patch.object(client, "_post_attempt", side_effect=exchange), mock.patch.object(
        client, "_sleep_before_retry", side_effect=lambda delay: delays.append(float(delay))
    ):
        result = browser_use.run_browser_goal(**kwargs)
    ended = time.perf_counter()
    wall_ms = (ended - started) * 1000
    # Per-step latency: local time between the first requests of consecutive
    # steps, plus the modeled provider time for each physical request, plus
    # the retry delay the real client computed.
    first_at: dict[int, float] = {}
    attempts: dict[int, int] = {}
    cost: dict[int, float] = {}
    faults: dict[int, list[str]] = {}
    for record in exchange.log:
        first_at.setdefault(record["step"], record["at"])
        attempts[record["step"]] = attempts.get(record["step"], 0) + 1
        if record["cost"] is not None:
            cost[record["step"]] = cost.get(record["step"], 0.0) + record["cost"]
        if record["fault"]:
            faults.setdefault(record["step"], []).append(record["fault"])
    order = sorted(first_at)
    steps = []
    for index, step in enumerate(order):
        end = first_at[order[index + 1]] if index + 1 < len(order) else ended
        local_ms = (end - first_at[step]) * 1000
        steps.append({
            "step": step,
            "physical": attempts[step],
            "faults": faults.get(step, []),
            "cost": round(cost.get(step, 0.0), 10),
            "latency_ms": round(local_ms + MODELED_REQUEST_MS * attempts[step], 3),
        })
    for delay in delays:
        # Delays belong to the step that retried; add them to that step.
        for item in steps:
            if any(fault.startswith("http_") for fault in item["faults"]):
                item["latency_ms"] = round(item["latency_ms"] + delay * 1000, 3)
                break
    secret_values = [item["value"] for item in case.get("text_inputs") or []]
    leak = any(value in body for body in exchange.bodies for value in secret_values)
    feature_sent = any(
        name in record["questions"] for record in exchange.log for name in ("trajectory", "new_goal_evidence", "next_observation")
    )
    progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
    status = result.get("status")
    return {
        "status": status,
        "failure_phase": result.get("failure_phase"),
        "completed": status == "completion_candidate" and browser.at_goal(),
        "false_completion": (status == "completion_candidate" and not browser.at_goal())
        or (result.get("failure_phase") == "semantic_stall" and result.get("verified") is not False),
        "dispatched_actions": browser.dispatched,
        "logical_requests": len(order),
        "physical_requests": len(exchange.log),
        "receipt_physical": progress.get("jev_physical_attempts"),
        "receipt_logical": progress.get("jev_logical_requests"),
        "optional_retries": progress.get("optional_retries", 0),
        "wall_ms": round(wall_ms, 3),
        "destination_bypass": browser.after_violation > 0
        or (bool(browser.violations) and status == "completion_candidate"),
        "semantic_stop": result.get("failure_phase") == "semantic_stall",
        "verified": result.get("verified"),
        "steps": steps,
        "known_cost": round(sum(item["cost"] for item in steps), 10),
        "unknown_cost_count": sum(1 for record in exchange.log if record["cost"] is None),
        "caller_value_leak": leak,
        "feature_questions_sent": feature_sent,
        "recovery_suggestion": progress.get("recovery_suggestion"),
        "last_skip_reason": progress.get("last_skip_reason"),
        "request_bytes": [record["bytes"] for record in exchange.log],
    }


def child_main(arm: str, case_ids: list[str]) -> None:
    book, _ = load_book()
    wanted = set(case_ids)
    rows = []
    for case in book["cases"]:
        if case["id"] not in wanted:
            continue
        row = run_a(case) if arm == "A" else run_jev_arm(arm, case)
        row.update({"case": case["id"], "split": case["split"], "kind": case["kind"], "label": case["label"], "arm": arm})
        rows.append(row)
    json.dump(rows, sys.stdout)


# --------------------------------------------------------------------------- driver


def baseline_root(explicit: str | None) -> tuple[Path, str]:
    if explicit:
        return Path(explicit), "external"
    sha = subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", f"{BASELINE_TAG}^{{commit}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    target = Path(tempfile.mkdtemp(prefix="bp-baseline-"))
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", BASELINE_TAG], capture_output=True, check=True).stdout
    tar_path = target / "src.tar"
    tar_path.write_bytes(archive)
    with tarfile.open(tar_path) as tar:
        tar.extractall(target / "src", filter="data")
    return target / "src", sha


def run_arm(arm: str, package_root: Path, case_ids: list[str]) -> list[dict[str, Any]]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), str(HERE)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    command = [sys.executable, str(Path(__file__).resolve()), "--child", arm, "--cases", ",".join(case_ids)]
    output = subprocess.run(command, capture_output=True, text=True, env=env, cwd=str(package_root), check=False)
    if output.returncode != 0:
        raise RuntimeError(f"arm {arm} failed: {output.stderr[-3000:]}")
    return json.loads(output.stdout)


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[max(1, math.ceil(p * len(ordered))) - 1], 3)


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    latencies = [step["latency_ms"] for row in rows for step in row["steps"]]
    return {
        "cases": len(rows),
        "completed": sum(row["completed"] for row in rows),
        "semantic_stops": [row["case"] for row in rows if row["semantic_stop"]],
        "false_completions": [row["case"] for row in rows if row["false_completion"]],
        "destination_bypasses": [row["case"] for row in rows if row["destination_bypass"]],
        "caller_value_leaks": [row["case"] for row in rows if row["caller_value_leak"]],
        "dispatched_actions": sum(row["dispatched_actions"] for row in rows),
        "logical_requests": sum(row["logical_requests"] for row in rows),
        "physical_requests": sum(row["physical_requests"] for row in rows),
        "p50_step_latency_ms": percentile(latencies, 0.5),
        "p95_step_latency_ms": percentile(latencies, 0.95),
        "known_cost": round(sum(row["known_cost"] for row in rows), 10),
        "unknown_cost_count": sum(row["unknown_cost_count"] for row in rows),
        "feature_questions_sent": [row["case"] for row in rows if row["feature_questions_sent"]],
    }


def gate(book: dict[str, Any], rows: dict[str, list[dict[str, Any]]], split: str) -> dict[str, Any]:
    """Apply the predeclared F2 thresholds to one split. Thresholds are not tuned."""
    cases = {case["id"]: case for case in book["cases"] if case["split"] == split}
    b = {row["case"]: row for row in rows["B"] if row["case"] in cases}
    c = {row["case"]: row for row in rows["C"] if row["case"] in cases}
    stalls = [cid for cid, case in cases.items() if case["kind"] == "semantic_stall"]
    early = [
        cid for cid in stalls
        if c[cid]["semantic_stop"] and c[cid]["dispatched_actions"] < b[cid]["dispatched_actions"]
    ]
    premature = [cid for cid, case in cases.items() if case["label"] == "productive" and c[cid]["semantic_stop"]]
    completion_regressions = [cid for cid in cases if c[cid]["completed"] < b[cid]["completed"]]
    # Zero added physical requests on eligible valid steps: a step with no
    # injected fault must use exactly one physical request in C, as in B.
    added = []
    for cid in cases:
        for step in c[cid]["steps"]:
            if not step["faults"] and step["physical"] != 1:
                added.append(f"{cid}:{step['step']}")
    # Paired eligible steps: step indices present in both B and C without a fault.
    paired_b: list[float] = []
    paired_c: list[float] = []
    cost_b = cost_c = 0.0
    for cid in cases:
        b_steps = {step["step"]: step for step in b[cid]["steps"] if not step["faults"]}
        c_steps = {step["step"]: step for step in c[cid]["steps"] if not step["faults"]}
        for index in sorted(set(b_steps) & set(c_steps)):
            paired_b.append(b_steps[index]["latency_ms"])
            paired_c.append(c_steps[index]["latency_ms"])
            cost_b += b_steps[index]["cost"]
            cost_c += c_steps[index]["cost"]
    b50, b95 = percentile(paired_b, 0.5), percentile(paired_b, 0.95)
    c50, c95 = percentile(paired_c, 0.5), percentile(paired_c, 0.95)
    unknown_c = sum(row["unknown_cost_count"] for row in c.values())
    checks = {
        "zero_false_completion_or_destination_bypass": not any(
            row["false_completion"] or row["destination_bypass"] for row in c.values()
        ),
        "zero_premature_semantic_stops_on_productive": not premature,
        "completion_at_least_b_on_every_paired_case": not completion_regressions,
        "early_stop_on_at_least_4_of_8_stalls_with_fewer_actions": len(early) >= STALL_GATE if split == "heldout" else None,
        "zero_added_physical_requests_on_valid_steps": not added,
        "p50_step_latency_at_most_b_plus_15_percent": c50 is not None and b50 is not None and c50 <= b50 * 1.15,
        "p95_step_latency_at_most_b_plus_300_ms": c95 is not None and b95 is not None and c95 <= b95 + 300,
        "known_cost_at_most_1_15_times_b_on_paired_steps": cost_b > 0 and cost_c <= cost_b * 1.15,
        "zero_caller_value_leaks": not any(row["caller_value_leak"] for row in c.values()),
    }
    decided = {key: value for key, value in checks.items() if value is not None}
    return {
        "passed": all(decided.values()),
        "checks": checks,
        "early_stops": early,
        "labelled_stalls": stalls,
        "premature_stops": premature,
        "completion_regressions": completion_regressions,
        "added_physical_steps": added,
        "paired_steps": len(paired_c),
        "paired_p50_ms": {"B": b50, "C": c50},
        "paired_p95_ms": {"B": b95, "C": c95},
        "paired_known_cost": {"B": round(cost_b, 10), "C": round(cost_c, 10),
                              "ratio": round(cost_c / cost_b, 4) if cost_b else None},
        "c_unknown_cost_count": unknown_c,
    }


# --------------------------------------------------------------------------- live


def run_live(output: Path | None) -> dict[str, Any]:
    """Run the frozen public live goals through the real handler (arm C only)."""
    from unittest import mock

    import hermes_switchyard
    import hermes_switchyard.client as client_module

    goals = json.loads(LIVE.read_text())["goals"]
    require(len(goals) == 4, "live_goal_count")
    physical = {"count": 0}
    original = client_module.DecisionClient._post_attempt

    def capped(self, path, body, headers, *, allow_stale_retry):
        if physical["count"] >= MAX_LIVE_PHYSICAL:
            raise client_module.JevRequestError("live physical request cap reached", detail="retry_budget_exhausted")
        physical["count"] += 1
        return original(self, path, body, headers, allow_stale_retry=allow_stale_retry)

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

    settings = {"browser_progress_mode": "advisory_stop"}
    if os.environ.get("SWITCHYARD_EVAL_BROWSER"):
        settings["browser_executable"] = os.environ["SWITCHYARD_EVAL_BROWSER"]
    context = Context(settings)
    hermes_switchyard.register(context)
    rows = []
    with mock.patch.object(client_module.DecisionClient, "_post_attempt", capped):
        for goal in goals:
            before = physical["count"]
            if before + MAX_LIVE_STEPS > MAX_LIVE_PHYSICAL:
                # Not enough cap left for a whole run; skip rather than cut it short.
                rows.append({"id": goal["id"], "skipped": "live_cap"})
                continue
            args = {
                "goal": goal["goal"],
                "start_url": goal["start_url"],
                "max_steps": MAX_LIVE_STEPS,
                "min_actions_before_done": goal.get("min_actions_before_done", 0),
            }
            if goal.get("completion_condition"):
                args["completion_condition"] = goal["completion_condition"]
            started = time.perf_counter()
            raw = context.tools["jev_computer_use"](args)
            wall_ms = (time.perf_counter() - started) * 1000
            result = json.loads(raw)
            progress = result.get("progress") if isinstance(result.get("progress"), dict) else {}
            steps = progress.get("steps") or []
            rows.append({
                "id": goal["id"],
                "expect": goal["expect"],
                "status": result.get("status"),
                "failure_phase": result.get("failure_phase"),
                "failure_reason": result.get("failure_reason"),
                "verified": result.get("verified"),
                "goal_verified": result.get("goal_verified"),
                "completion_source": result.get("completion_source"),
                "semantic_stop": progress.get("semantic_stop"),
                "recovery_suggestion": progress.get("recovery_suggestion"),
                "dispatched_actions": result.get("action_dispatched_count"),
                "jev_request_count": result.get("jev_request_count"),
                "physical_requests_counted": physical["count"] - before,
                "receipt_physical": progress.get("jev_physical_attempts"),
                "optional_retries": progress.get("optional_retries"),
                "known_cost_usd": progress.get("known_cost_usd"),
                "unknown_cost_count": progress.get("unknown_cost_count"),
                "model": progress.get("model"),
                "wall_ms": round(wall_ms, 1),
                "session_setup_ms": result.get("session_setup_ms"),
                "step_reasons": [
                    {k: s.get(k) for k in ("step", "asked", "skip_reason", "reason", "consecutive_stall_count", "new_goal_evidence")}
                    | {"trajectory": (s.get("trajectory") or {}).get("choice")}
                    for s in steps
                ],
                "last_skip_reason": progress.get("last_skip_reason"),
                "error": result.get("error"),
            })
    report = {"physical_requests_total": physical["count"], "cap": MAX_LIVE_PHYSICAL, "rows": rows}
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--validate", action="store_true", help="validate fixtures and run the offline replay (default)")
    parser.add_argument("--live", action="store_true", help="run the four frozen public goals (arm C) on the configured Jev route")
    parser.add_argument("--baseline-root", help="path to a v0.5.4 source tree (default: git archive of the tag)")
    parser.add_argument("--output", help="write the JSON report here")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    parser.add_argument("--cases", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        child_main(args.child, args.cases.split(",") if args.cases else [])
        return 0

    book, fixture_sha = load_book()
    live_sha = sha256_bytes(LIVE.read_bytes())
    lock = check_lock(fixture_sha, live_sha)
    candidate_sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip() or None
    report: dict[str, Any] = {
        "schema_version": 1,
        "suite": "browser_progress",
        "spec_version": "dom-progress-v1",
        "fixtures_sha256": fixture_sha,
        "live_sha256": live_sha,
        "frozen_at": lock.get("frozen_at"),
        "candidate_head": candidate_sha,
        "mode": "live" if args.live else "offline",
    }
    if args.live:
        live = run_live(Path(args.output) if args.output else None)
        report["live"] = live
        report["note"] = (
            "Arm C only, real Jev answers and a real browser. Latency includes browser and network time. "
            "Cold and warm are not separated. B was not run live inside the shared 24-request cap."
        )
    else:
        base_root, base_sha = baseline_root(args.baseline_root)
        report["baseline"] = {"tag": BASELINE_TAG, "commit": base_sha}
        ids = [case["id"] for case in book["cases"]]
        rows: dict[str, list[dict[str, Any]]] = {}
        for arm in ("A", "B", "C", "C_off"):
            rows[arm] = run_arm(arm, base_root if arm == "B" else ROOT, ids)
        report["summaries"] = {
            f"{arm}:{split}": summarize([row for row in rows[arm] if row["split"] == split])
            for arm in rows for split in ("dev", "heldout")
        }
        report["gates"] = {split: gate(book, rows, split) for split in ("dev", "heldout")}
        report["off_mode_sends_no_feature_questions"] = not any(row["feature_questions_sent"] for row in rows["C_off"])
        report["rows"] = rows
        report["offline_gate_passed"] = report["gates"]["heldout"]["passed"] and report["off_mode_sends_no_feature_questions"]
        report["note"] = (
            "Offline replay through the real run_browser_goal and DecisionClient with a scripted HTTP exchange. "
            "It checks wiring and policy, not model accuracy. Step latency is local loop time plus a fixed modeled "
            "250 ms per physical request; cost is a synthetic per-byte price."
        )
    text = json.dumps(report, indent=1, sort_keys=True)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    brief = {key: value for key, value in report.items() if key not in {"rows"}}
    print(json.dumps(brief, indent=1, sort_keys=True))
    if not args.live:
        return 0 if report["offline_gate_passed"] else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
