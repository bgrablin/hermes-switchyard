#!/usr/bin/env python3
"""Independent evaluation harness for adaptive reasoning effort (issue #121).

The harness installs a plugin candidate into an isolated Hermes home, drives the
real Hermes ``pre_llm_call`` hook, ``post_tool_call`` hook, and ``llm_request``
middleware with provider kwargs built by Hermes' own request builders, and then
sends the result through the real provider SDKs to a local mock transport. The
reported effort is the effort field on that captured wire payload, not a TUI
label or a receipt claim.

Scoring uses the independent labels in the fixture book. The harness does not
reimplement the adapter policy: it only compares the wire effort with the wire
cap and with the fixture label.

Jev responder modes:
  synthetic_lowest  offline; answers every question with its lowest option.
                    Tests plumbing and code guards only. No accuracy claim.
  recorded          offline; replays responses keyed by the exact Jev request.
  live              hosted Jev calls. Requires --allow-network, a call cap,
                    and a credential in the environment. Only a live run on the
                    holdout split can support an accuracy claim.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA_VERSION = 1
REPORT_SCHEMA = "switchyard.adaptive_effort_eval.v1"
LABELS = ("lower_ok", "keep_cap", "ambiguous")
SPLITS = ("dev", "holdout")
PHASES = ("new_turn", "after_tool")
TOOL_STATUSES = ("ok", "error")
RESPONDERS = ("synthetic_lowest", "recorded", "live")
LEVEL_ORDER = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
MAX_FIXTURES = 400
MAX_LIVE_JEV_CALLS = 400
SYNTHETIC_JEV_MODEL = "typesafe/jev-1.13-20260917"
SYSTEM_SENTINEL = "SYNTHETIC_SYSTEM_PROMPT_SENTINEL"
MEMORY_SENTINEL = "SYNTHETIC_MEMORY_FACT_SENTINEL"
PLUGIN_SENTINEL = "SYNTHETIC_PLUGIN_CONTEXT_SENTINEL"
TOOL_SENTINEL = "SYNTHETIC_TOOL_BODY_SENTINEL"
ASSISTANT_SENTINEL = "SYNTHETIC_ASSISTANT_TEXT_SENTINEL"
LEAK_SENTINELS = {
    "system_prompt": SYSTEM_SENTINEL,
    "memory_context": MEMORY_SENTINEL,
    "plugin_context": PLUGIN_SENTINEL,
    "tool_body": TOOL_SENTINEL,
    "assistant_text": ASSISTANT_SENTINEL,
}
CREDENTIAL_ENV = ("TYPESAFE_API_KEY", "OPENROUTER_API_KEY")
CHILD_ENV_ALLOW = ("PATH", "PYTHONPATH", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT", "SystemRoot")
EXPECTED_WIRE_FIELD = {"anthropic_messages": ("output_config", "effort"), "codex_responses": ("reasoning", "effort")}


class FixtureError(ValueError):
    """The fixture book violates the frozen public schema."""


def repository_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _require(value: object, code: str) -> None:
    if not value:
        raise FixtureError(code)


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Fixture book
# ---------------------------------------------------------------------------

def split_digest(fixtures: Iterable[Mapping[str, Any]]) -> str:
    """Digest of every item's identity, text, split, and labels.

    Changing a label, a split assignment, or a task text after the split is
    frozen changes this digest, so tuning on the holdout split is visible.
    """
    rows = sorted(
        (item["id"], item["split"], item["user_message"], _canonical(item["labels"]))
        for item in fixtures
    )
    return _sha256(rows)


def consensus_label(labels: Mapping[str, str]) -> str:
    """Return the shared label, or ``ambiguous`` when labelers disagree."""
    values = set(labels.values())
    return values.pop() if len(values) == 1 else "ambiguous"


def validate_fixture_book(book: Any) -> dict[str, Any]:
    _require(isinstance(book, dict) and book.get("schema_version") == SCHEMA_VERSION, "fixture_schema")
    _require(book.get("public_synthetic") is True, "fixture_book_not_public_synthetic")
    routes = book.get("routes")
    _require(isinstance(routes, list) and routes, "routes")
    route_names: set[str] = set()
    for route in routes:
        _require(isinstance(route, dict), "route_object")
        _require(set(route) == {"name", "provider", "model", "api_mode"}, "route_fields")
        _require(all(isinstance(route[key], str) and route[key] for key in route), "route_values")
        _require(route["api_mode"] in EXPECTED_WIRE_FIELD, "route_api_mode_unsupported")
        _require(route["name"] not in route_names, "route_name_duplicate")
        route_names.add(route["name"])
    fixtures = book.get("fixtures")
    _require(isinstance(fixtures, list) and 1 <= len(fixtures) <= MAX_FIXTURES, "fixture_count")
    seen: set[str] = set()
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in fixtures:
        _require(isinstance(item, dict), "fixture_object")
        allowed = {"id", "slice", "split", "user_message", "requested_effort", "phase", "tool_status",
                   "labels", "contrast_group", "restricted", "public_synthetic", "note"}
        _require(set(item) <= allowed, "fixture_fields")
        fixture_id = item.get("id")
        _require(isinstance(fixture_id, str) and fixture_id and fixture_id not in seen, "fixture_id")
        seen.add(fixture_id)
        _require(item.get("public_synthetic") is True, "fixture_not_public_synthetic")
        _require(isinstance(item.get("slice"), str) and item["slice"], "fixture_slice")
        _require(item.get("split") in SPLITS, "fixture_split")
        text = item.get("user_message")
        _require(isinstance(text, str) and text.strip() and len(text) <= 4000, "fixture_user_message")
        for sentinel in LEAK_SENTINELS.values():
            _require(sentinel not in text, "fixture_contains_sentinel")
        _require(item.get("requested_effort") in LEVEL_ORDER[2:], "fixture_requested_effort")
        _require(item.get("phase") in PHASES, "fixture_phase")
        if item["phase"] == "after_tool":
            _require(item.get("tool_status") in TOOL_STATUSES, "fixture_tool_status")
        else:
            _require(item.get("tool_status") is None, "fixture_tool_status_without_tool")
        labels = item.get("labels")
        _require(isinstance(labels, dict) and labels, "fixture_labels")
        _require(all(isinstance(k, str) and k and v in LABELS for k, v in labels.items()), "fixture_label_values")
        _require(type(item.get("restricted", False)) is bool, "fixture_restricted")
        group = item.get("contrast_group")
        if group is not None:
            _require(isinstance(group, str) and group, "fixture_contrast_group")
            groups.setdefault(group, []).append(item)
    for name, members in groups.items():
        _require(len(members) >= 2, f"contrast_group_too_small:{name}")
        _require(len({len(m["user_message"]) for m in members}) == 1, f"contrast_group_length_mismatch:{name}")
        _require(len({m["split"] for m in members}) == 1, f"contrast_group_split_mismatch:{name}")
        member_labels = {consensus_label(m["labels"]) for m in members}
        _require({"lower_ok", "keep_cap"} <= member_labels, f"contrast_group_not_contrasting:{name}")
    frozen = book.get("frozen_split_sha256")
    _require(isinstance(frozen, str) and frozen == split_digest(fixtures), "frozen_split_digest_mismatch")
    return book


def load_fixture_book(path: Path) -> dict[str, Any]:
    return validate_fixture_book(json.loads(path.read_text(encoding="utf-8")))


# ---------------------------------------------------------------------------
# Metrics (pure functions; no adapter policy)
# ---------------------------------------------------------------------------

def level_index(level: Any) -> int | None:
    return LEVEL_ORDER.index(level) if level in LEVEL_ORDER else None


def wilson_interval(successes: int, total: int, z: float = 1.959964) -> tuple[float | None, float | None]:
    if total <= 0:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return round(max(0.0, centre - margin), 4), round(min(1.0, centre + margin), 4)


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def classify_outcome(sent: Any, cap: Any, reason_code: Any, jev_calls: int) -> str:
    """Outcome from wire levels and the adapter's own reason code only."""
    sent_i, cap_i = level_index(sent), level_index(cap)
    if sent_i is None or cap_i is None:
        return "unmeasured"
    if sent_i > cap_i:
        return "raised"
    if sent_i < cap_i:
        return "lowered"
    if jev_calls == 0:
        return "kept_no_jev_call"
    if isinstance(reason_code, str) and reason_code not in {"jev_selected", "cached"}:
        return "abstained"
    return "kept"


def score_record(record: dict[str, Any]) -> dict[str, Any]:
    """Compare one measured outcome with its independent label."""
    label = record["label"]
    outcome = record["outcome"]
    return {
        "false_lower": label == "keep_cap" and outcome == "lowered",
        "successful_lower": label == "lower_ok" and outcome == "lowered",
        "missed_lower": label == "lower_ok" and outcome in {"kept", "kept_no_jev_call", "abstained"},
        "abstained": outcome == "abstained",
        "raised_above_cap": outcome == "raised" and not record.get("raise_allowed", False),
    }


def summarize(records: list[dict[str, Any]], *, responder: str, split: str) -> dict[str, Any]:
    """Aggregate scored records per route; accuracy claims need a live holdout run."""
    by_route: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        by_route.setdefault(record["route"], []).append(record)
    summary: dict[str, Any] = {}
    for route, rows in sorted(by_route.items()):
        keep = [r for r in rows if r["label"] == "keep_cap" and r["outcome"] != "unmeasured"]
        lower = [r for r in rows if r["label"] == "lower_ok" and r["outcome"] != "unmeasured"]
        false_lower = sum(r["score"]["false_lower"] for r in keep)
        lowered = sum(r["score"]["successful_lower"] for r in lower)
        short_consequential = [r for r in keep if r["slice"] == "short_consequential"]
        latencies = [c["latency_ms"] for r in rows for c in r["jev"]["calls"]
                     if isinstance(c.get("latency_ms"), (int, float))]
        tokens: dict[str, float] = {}
        tokens_missing = 0
        for row in rows:
            for call in row["jev"]["calls"]:
                usage = call.get("usage") or {}
                if not usage:
                    tokens_missing += 1
                for key, value in usage.items():
                    if isinstance(value, (int, float)):
                        tokens[key] = tokens.get(key, 0.0) + float(value)
        jev_calls = sum(r["jev"]["call_count"] for r in rows)
        requests = sum(len(r["requests"]) for r in rows)
        summary[route] = {
            "items": len(rows),
            "unmeasured": sum(r["outcome"] == "unmeasured" for r in rows),
            "keep_cap": {"n": len(keep), "false_lower": false_lower,
                         "false_lower_wilson95": wilson_interval(false_lower, len(keep))},
            "short_consequential": {"n": len(short_consequential),
                                    "lowered": sum(r["outcome"] == "lowered" for r in short_consequential)},
            "lower_ok": {"n": len(lower), "lowered": lowered,
                         "lowered_wilson95": wilson_interval(lowered, len(lower))},
            "abstained": sum(r["score"]["abstained"] for r in rows),
            "raised_above_cap": sum(r["score"]["raised_above_cap"] for r in rows),
            "leak_findings": sum(len(r["leaks"]) for r in rows),
            "prompt_bytes_changed": sum(not q["prompt_bytes_identical"] for r in rows for q in r["requests"]),
            "jev_calls": jev_calls,
            "llm_requests": requests,
            "jev_calls_per_turn": round(jev_calls / len(rows), 3) if rows else None,
            "jev_latency_ms": {"p50": percentile(latencies, 0.5), "p95": percentile(latencies, 0.95),
                               "source": "provider_latency_ms_field_or_client_measured"},
            "jev_usage_totals": tokens,
            "jev_calls_without_usage": tokens_missing,
        }
    accuracy_claim = responder == "live" and split == "holdout"
    return {"routes": summary, "accuracy_claim_allowed": accuracy_claim,
            "accuracy_claim_reason": (
                "live responder on holdout split" if accuracy_claim else
                "offline or dev-split run: plumbing, egress, and distinguishability evidence only")}


def contrast_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Same-length pairs must reach Jev as different states."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        if record.get("contrast_group"):
            groups.setdefault((record["route"], record["contrast_group"]), []).append(record)
    rows = []
    for (route, group), members in sorted(groups.items()):
        digests = [m["jev"]["first_state_sha256"] for m in members]
        measurable = all(digests)
        rows.append({"route": route, "group": group, "members": [m["id"] for m in members],
                     "measurable": measurable,
                     "distinguishable": measurable and len(set(digests)) == len(digests)})
    measurable = [r for r in rows if r["measurable"]]
    return {"groups": rows, "measurable": len(measurable),
            "distinguishable": sum(r["distinguishable"] for r in measurable),
            "all_distinguishable": bool(measurable) and all(r["distinguishable"] for r in measurable)}


def acceptance(summary: dict[str, Any], contrasts: dict[str, Any], records: list[dict[str, Any]]) -> dict[str, Any]:
    """Gate results. Each gate is RED, GREEN, or NOT_MEASURED; no gate is inferred."""
    gates: dict[str, str] = {}
    gates["contrast_pairs_distinguishable"] = (
        "NOT_MEASURED" if not contrasts["measurable"] else "GREEN" if contrasts["all_distinguishable"] else "RED")
    leak_total = sum(route["leak_findings"] for route in summary["routes"].values())
    gates["no_sidecar_or_tool_leak"] = "GREEN" if leak_total == 0 else "RED"
    restricted = [r for r in records if r["restricted"]]
    gates["restricted_text_not_sent"] = (
        "NOT_MEASURED" if not restricted else
        "GREEN" if all(not r["jev"]["user_text_in_state"] for r in restricted) else "RED")
    raised = sum(route["raised_above_cap"] for route in summary["routes"].values())
    gates["never_above_cap"] = "GREEN" if raised == 0 else "RED"
    changed = sum(route["prompt_bytes_changed"] for route in summary["routes"].values())
    gates["prompt_bytes_unchanged"] = "GREEN" if changed == 0 else "RED"
    if summary["accuracy_claim_allowed"]:
        routes = summary["routes"].values()
        safe = all((route["keep_cap"]["false_lower_wilson95"][1] or 0) <= 0.05
                   and route["short_consequential"]["lowered"] == 0 for route in routes)
        benefit = all((route["lower_ok"]["n"] and route["lower_ok"]["lowered"] / route["lower_ok"]["n"] >= 0.70)
                      for route in routes)
        gates["holdout_safety"] = "GREEN" if safe else "RED"
        gates["holdout_benefit"] = "GREEN" if benefit else "RED"
    else:
        gates["holdout_safety"] = "NOT_MEASURED"
        gates["holdout_benefit"] = "NOT_MEASURED"
    overall = "RED" if "RED" in gates.values() else "GREEN" if all(v == "GREEN" for v in gates.values()) else "INCOMPLETE"
    return {"gates": gates, "overall": overall}


# ---------------------------------------------------------------------------
# Synthetic and recorded Jev responders (offline)
# ---------------------------------------------------------------------------

def synthetic_lowest_answers(questions: Mapping[str, Any]) -> dict[str, Any]:
    """Answer each typed question with its first (lowest) option.

    This is a deliberately naive, policy-free responder. It exercises the
    adapter's code guards; it is not a model of Jev accuracy.
    """
    answers: dict[str, Any] = {}
    for name, question in questions.items():
        kind = question.get("type")
        criteria = question.get("criteria")
        if kind == "choice":
            keys = list(criteria)
            answers[name] = {"choice": keys[0], "confidence": 1.0,
                             "probabilities": {key: (1.0 if key == keys[0] else 0.0) for key in keys}}
        elif kind == "score":
            legend = {str(i): text for i, text in enumerate(criteria)}
            answers[name] = {"score": 0, "legend": legend, "confidence": 1.0,
                             "probabilities": {str(i): (1.0 if i == 0 else 0.0) for i in range(len(criteria))}}
        else:
            answers[name] = {"noul": 0.0}
    return answers


def request_key(payload: Mapping[str, Any]) -> str:
    return _sha256({"model": payload.get("model"), "state": payload.get("state"),
                    "questions": payload.get("questions")})


def synthetic_wire_response(payload: Mapping[str, Any], counter: int) -> dict[str, Any]:
    prompt_tokens = len(_canonical({"state": payload.get("state"), "questions": payload.get("questions")})) // 4
    return {
        "id": f"synthetic-{counter:05d}",
        "model": SYNTHETIC_JEV_MODEL,
        "answers": synthetic_lowest_answers(payload.get("questions") or {}),
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": 3,
                  "total_tokens": prompt_tokens + 3, "cost": 0.0},
        "latency_ms": 1.0,
    }


# ---------------------------------------------------------------------------
# Child process: runs inside the isolated Hermes home
# ---------------------------------------------------------------------------

def _wire_effort(api_mode: str, wire: Mapping[str, Any]) -> Any:
    container, key = EXPECTED_WIRE_FIELD[api_mode]
    value = wire.get(container)
    return value.get(key) if isinstance(value, Mapping) else None


def _messages_for(fixture: Mapping[str, Any], *, after_tool: bool) -> list[dict[str, Any]]:
    from agent.turn_context import compose_user_api_content

    content = compose_user_api_content(fixture["user_message"], MEMORY_SENTINEL, PLUGIN_SENTINEL)
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": f"You are a synthetic assistant. {SYSTEM_SENTINEL}"},
        {"role": "user", "content": content},
    ]
    if after_tool:
        messages.append({"role": "assistant", "content": ASSISTANT_SENTINEL, "tool_calls": [{
            "id": "call_eval_1", "type": "function",
            "function": {"name": "terminal", "arguments": "{\"command\": \"synthetic\"}"}}]})
        messages.append({"role": "tool", "tool_call_id": "call_eval_1",
                         "content": f"{TOOL_SENTINEL} synthetic tool output"})
    return messages


_TOOLS = [{"type": "function", "function": {
    "name": "terminal", "description": "Run a synthetic command.",
    "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}}]


def _build_kwargs(route: Mapping[str, str], messages: list[dict[str, Any]], effort: str, session: str) -> dict:
    reasoning = {"enabled": True, "effort": effort}
    if route["api_mode"] == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_kwargs

        return build_anthropic_kwargs(route["model"], messages, _TOOLS, 64, reasoning)
    from agent.transports import get_transport

    return get_transport("codex_responses").build_kwargs(
        route["model"], messages, _TOOLS, reasoning_config=reasoning, is_codex_backend=True,
        provider=route["provider"], session_id=session)


def _send_through_sdk(route: Mapping[str, str], payload: dict[str, Any]) -> dict[str, Any]:
    import httpx

    captured: list[dict[str, Any]] = []
    if route["api_mode"] == "anthropic_messages":
        import anthropic

        def capture(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "msg_eval", "type": "message", "role": "assistant", "model": route["model"],
                "content": [], "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 1, "output_tokens": 1}})

        with anthropic.Anthropic(api_key="fixture-key", base_url="https://example.invalid",
                                 http_client=httpx.Client(transport=httpx.MockTransport(capture))) as client:
            client.messages.create(**payload)
    else:
        import openai

        def capture(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={"id": "resp_eval", "object": "response", "model": route["model"],
                                             "created_at": 0, "output": [], "status": "completed"})

        with openai.OpenAI(api_key="fixture-key", base_url="https://example.invalid/v1",
                           http_client=httpx.Client(transport=httpx.MockTransport(capture))) as client:
            client.responses.create(**payload)
    if len(captured) != 1:
        raise RuntimeError("wire_capture_count")
    return captured[0]


def _prompt_bytes(api_mode: str, payload: Mapping[str, Any]) -> str:
    keys = ("messages", "system", "tools") if api_mode == "anthropic_messages" else ("input", "instructions", "tools")
    return _canonical({key: payload.get(key) for key in keys})


class _JevRecorder:
    """Wrap the plugin's Jev client and record exact request and response fields."""

    def __init__(self, spec: Mapping[str, Any], client_module: Any, original_factory: Any):
        self.mode = spec["responder"]
        self.cap = int(spec["max_jev_calls"])
        self.recordings: dict[str, Any] = spec.get("recordings") or {}
        self.record_new: dict[str, Any] = {}
        self.client_module = client_module
        self.original_factory = original_factory
        self.calls: list[dict[str, Any]] = []
        self.total = 0
        self.cap_reached = False

    def _offline_transport(self, payload: dict) -> dict:
        key = request_key(payload)
        if self.mode == "recorded":
            entry = self.recordings.get(key)
            if entry is None:
                raise LookupError("recording_missing")
            return copy.deepcopy(entry["response"])
        return synthetic_wire_response(payload, self.total)

    def factory(self):
        if self.mode == "live":
            client = self.original_factory()
        else:
            client = self.client_module.DecisionClient(api_key="fixture-key", transport=self._offline_transport)
        original_post = client._post
        recorder = self

        def recorded_post(payload: dict) -> dict:
            if recorder.total >= recorder.cap:
                recorder.cap_reached = True
                raise RuntimeError("jev_call_cap")
            recorder.total += 1
            key = request_key(payload)
            entry: dict[str, Any] = {"request_key": key, "state": copy.deepcopy(payload.get("state")),
                                     "question_names": sorted((payload.get("questions") or {})),
                                     "question_types": {name: q.get("type") for name, q in
                                                        (payload.get("questions") or {}).items()},
                                     "provider_routing": copy.deepcopy(payload.get("provider"))}
            started = time.perf_counter()
            try:
                response = original_post(payload)
            except Exception as exc:  # noqa: BLE001 -- record the type only
                entry.update({"ok": False, "error_type": type(exc).__name__,
                              "client_latency_ms": round((time.perf_counter() - started) * 1000, 3)})
                recorder.calls.append(entry)
                raise
            entry.update({
                "ok": True,
                "client_latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "wire_latency_ms": response.get("latency_ms"),
                "latency_ms": response.get("latency_ms", round((time.perf_counter() - started) * 1000, 3)),
                "model": response.get("model"),
                "request_id": response.get("request_id"),
                "usage": {k: v for k, v in (response.get("usage") or {}).items() if isinstance(v, (int, float))},
                "answers": copy.deepcopy(response.get("answers")),
                "response_source": "live" if recorder.mode == "live" else (
                    recorder.recordings.get(key, {}).get("source", "recorded")
                    if recorder.mode == "recorded" else "synthetic_lowest"),
            })
            recorder.calls.append(entry)
            recorder.record_new[key] = {"source": entry["response_source"], "response": {
                key2: copy.deepcopy(response[key2]) for key2 in ("model", "answers", "usage", "latency_ms")
                if key2 in response} | ({"id": response["request_id"]} if response.get("request_id") else {})}
            return response

        client._post = recorded_post
        return client


def _run_child(spec_path: Path, output_path: Path) -> int:
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    from hermes_cli.lifecycle import invoke_hook
    from hermes_cli.middleware import apply_llm_request_middleware
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    loaded = manager._plugins.get("hermes-switchyard")
    if loaded is None or not loaded.enabled or loaded.error is not None:
        output_path.write_text(json.dumps({"status": "error", "code": "plugin_not_enabled"}), encoding="utf-8")
        return 2
    callbacks = manager._middleware.get("llm_request", [])
    controller = getattr(callbacks[0], "__self__", None) if len(callbacks) == 1 else None
    if controller is None:
        output_path.write_text(json.dumps({"status": "error", "code": "controller_unavailable"}), encoding="utf-8")
        return 2
    adapter = sys.modules[controller.__class__.__module__]
    package = adapter.__name__.rsplit(".", 1)[0]
    client_module = sys.modules[package + ".client"]
    recorder = _JevRecorder(spec, client_module, controller.client_factory)
    controller.client_factory = recorder.factory
    last_receipt = getattr(adapter, "last_receipt", lambda: {})

    records = []
    for route in spec["routes"]:
        for fixture in spec["fixtures"]:
            session = f"eval-{route['name']}-{fixture['id']}"
            turn = "t1"
            calls_before = len(recorder.calls)
            invoke_hook("pre_llm_call", session_id=session, task_id=session, turn_id=turn,
                        user_message=fixture["user_message"], conversation_history=[], is_first_turn=True,
                        model=route["model"], platform="cli", parent_session_id="", sender_id="")
            plan = [False] + ([True] if fixture["phase"] == "after_tool" else [])
            requests = []
            for index, after_tool in enumerate(plan):
                if after_tool:
                    status = fixture["tool_status"]
                    invoke_hook("post_tool_call", tool_name="terminal", args={"command": "synthetic"},
                                result=(f"{TOOL_SENTINEL} failed" if status == "error" else f"{TOOL_SENTINEL} ok"),
                                task_id=session, session_id=session, tool_call_id="call_eval_1", turn_id=turn,
                                duration_ms=1, status=status,
                                error_type=("tool_error" if status == "error" else None),
                                error_message=(f"{TOOL_SENTINEL} failed" if status == "error" else None),
                                middleware_trace=[])
                messages = _messages_for(fixture, after_tool=after_tool)
                kwargs = _build_kwargs(route, messages, fixture["requested_effort"], session)
                requested_wire = _wire_effort(route["api_mode"], kwargs)
                result = apply_llm_request_middleware(
                    kwargs, task_id=session, turn_id=turn, api_request_id=f"{session}-{index}",
                    session_id=session, platform="cli", model=route["model"], provider=route["provider"],
                    base_url="https://example.invalid", api_mode=route["api_mode"], api_call_count=index + 1)
                wire = _send_through_sdk(route, result.payload)
                receipt = dict(last_receipt() or {})
                requests.append({
                    "phase": "after_tool" if after_tool else "new_turn",
                    "requested_wire": requested_wire,
                    "sent_wire": _wire_effort(route["api_mode"], wire),
                    "middleware_changed": bool(result.changed),
                    "middleware_trace": [dict(t) for t in (result.trace or [])],
                    "prompt_bytes_identical": _prompt_bytes(route["api_mode"], kwargs)
                    == _prompt_bytes(route["api_mode"], result.payload),
                    "receipt": {key: receipt.get(key) for key in (
                        "status", "reason_code", "cap", "effort", "requested_effort", "mode",
                        "jev_called", "confidence", "applied", "source")},
                })
            records.append({"id": fixture["id"], "route": route["name"], "requests": requests,
                            "jev_calls": recorder.calls[calls_before:]})
    output_path.write_text(json.dumps({"status": "ok", "records": records, "jev_total": recorder.total,
                                       "jev_cap_reached": recorder.cap_reached,
                                       "new_recordings": recorder.record_new}), encoding="utf-8")
    manager.unload()
    return 0


# ---------------------------------------------------------------------------
# Parent: install the candidate, run the child, score the result
# ---------------------------------------------------------------------------

def install_candidate(plugin_parent: Path, home: Path) -> Path:
    """Copy the candidate plugin (entrypoint, manifest, package) into an isolated home."""
    target = home / "plugins" / "hermes-switchyard"
    package = plugin_parent / "hermes_switchyard"
    for required in (plugin_parent / "__init__.py", plugin_parent / "plugin.yaml", package / "__init__.py"):
        if not required.is_file():
            raise FileNotFoundError("candidate_missing:" + required.name)
    target.mkdir(parents=True)
    shutil.copyfile(plugin_parent / "__init__.py", target / "__init__.py")
    shutil.copyfile(plugin_parent / "plugin.yaml", target / "plugin.yaml")
    shutil.copytree(package, target / "hermes_switchyard",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return target


def child_environment(workspace: Path, home: Path, bundled: Path, *, live: bool) -> dict[str, str]:
    env = {key: os.environ[key] for key in CHILD_ENV_ALLOW if key in os.environ}
    env.update({"HOME": str(workspace), "HERMES_HOME": str(home), "HERMES_BUNDLED_PLUGINS": str(bundled),
                "PYTHONDONTWRITEBYTECODE": "1"})
    if live:
        for key in CREDENTIAL_ENV:
            if os.environ.get(key):
                env[key] = os.environ[key]
    return env


def _config_yaml(jev_provider: str) -> str:
    return ("plugins:\n  enabled: [hermes-switchyard]\n  entries:\n    hermes-switchyard:\n"
            "      settings:\n        automatic_skill_recommendation: false\n"
            f"        jev_provider: {jev_provider}\n")


def _source_hashes(plugin_parent: Path) -> dict[str, str]:
    root = plugin_parent / "hermes_switchyard"
    files = [plugin_parent / "__init__.py", plugin_parent / "plugin.yaml",
             *sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.parts)]
    return {p.relative_to(plugin_parent).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in files}


def _user_text_in_state(text: str, states: list[Any]) -> bool:
    normalized = " ".join(text.split())
    return any(normalized and normalized in " ".join(_canonical(state).split()) for state in states)


def build_records(book: Mapping[str, Any], selected: list[dict[str, Any]], child: Mapping[str, Any],
                  responder: str) -> list[dict[str, Any]]:
    fixtures = {item["id"]: item for item in selected}
    records = []
    for raw in child["records"]:
        fixture = fixtures[raw["id"]]
        label = consensus_label(fixture["labels"])
        measured = raw["requests"][-1]
        calls = raw["jev_calls"]
        states = [c.get("state") for c in calls]
        state_text = _canonical(states)
        leaks = [name for name, sentinel in LEAK_SENTINELS.items() if sentinel in state_text]
        flags = []
        if responder != "live":
            flags.append("responder_not_live_no_accuracy_claim")
        if len(fixture["labels"]) < 2:
            flags.append("single_labeler")
        if not calls:
            flags.append("no_jev_call")
        if any(c.get("ok") and not c.get("usage") for c in calls):
            flags.append("jev_usage_missing")
        if any(c.get("ok") and c.get("wire_latency_ms") is None for c in calls):
            flags.append("jev_wire_latency_missing")
        if any(c.get("ok") and not c.get("request_id") for c in calls):
            flags.append("jev_request_id_missing")
        if any(not c.get("ok") for c in calls):
            flags.append("jev_call_failed")
        if measured["sent_wire"] is None:
            flags.append("wire_effort_missing")
        if measured["receipt"].get("status") is None:
            flags.append("receipt_missing")
        record = {
            "id": fixture["id"], "route": raw["route"], "slice": fixture["slice"], "split": fixture["split"],
            "label": label, "labels": dict(fixture["labels"]), "restricted": fixture.get("restricted", False),
            "contrast_group": fixture.get("contrast_group"), "phase": fixture["phase"],
            "user_message_chars": len(fixture["user_message"]),
            "requests": [{key: q[key] for key in ("phase", "requested_wire", "sent_wire", "middleware_changed",
                                                  "prompt_bytes_identical", "receipt")} for q in raw["requests"]],
            "requested_wire": measured["requested_wire"], "sent_wire": measured["sent_wire"],
            "cap": measured["receipt"].get("cap") or measured["requested_wire"],
            "raise_allowed": False,
            "jev": {
                "call_count": len(calls),
                "calls": [{key: c.get(key) for key in ("ok", "error_type", "model", "request_id", "latency_ms",
                                                         "wire_latency_ms", "client_latency_ms", "usage",
                                                         "question_names", "question_types", "answers",
                                                         "response_source", "provider_routing")} for c in calls],
                "first_state_sha256": _sha256(states[0]) if states else None,
                "state_keys": sorted(states[0]) if states and isinstance(states[0], dict) else None,
                "user_text_in_state": _user_text_in_state(fixture["user_message"], states),
            },
            "leaks": leaks,
            "missing_evidence": flags,
        }
        record["outcome"] = classify_outcome(record["sent_wire"], record["cap"],
                                             measured["receipt"].get("reason_code"), len(calls))
        record["reason_code"] = measured["receipt"].get("reason_code")
        record["score"] = score_record(record)
        records.append(record)
    return records


def run_evaluation(*, plugin_parent: Path, fixtures_path: Path, split: str, responder: str,
                   recordings: dict[str, Any] | None = None, max_jev_calls: int = 64,
                   jev_provider: str = "openrouter", python: str | None = None,
                   timeout: float = 600.0) -> dict[str, Any]:
    book = load_fixture_book(fixtures_path)
    selected = [item for item in book["fixtures"] if split == "all" or item["split"] == split]
    if not selected:
        raise FixtureError("no_fixtures_for_split")
    live = responder == "live"
    with tempfile.TemporaryDirectory(prefix="switchyard-adaptive-eval-") as temporary:
        workspace = Path(temporary)
        home = workspace / "home"
        bundled = workspace / "bundled"
        bundled.mkdir()
        home.mkdir()
        install_candidate(plugin_parent, home)
        (home / "config.yaml").write_text(_config_yaml(jev_provider), encoding="utf-8")
        spec = {"routes": book["routes"], "fixtures": selected, "responder": responder,
                "max_jev_calls": max_jev_calls, "recordings": recordings or {}}
        spec_path = workspace / "spec.json"
        out_path = workspace / "child.json"
        spec_path.write_text(json.dumps(spec), encoding="utf-8")
        started = time.perf_counter()
        completed = subprocess.run(
            [python or sys.executable, str(Path(__file__).resolve()), "--child", str(spec_path), str(out_path)],
            cwd=str(workspace), env=child_environment(workspace, home, bundled, live=live),
            capture_output=True, text=True, timeout=timeout)
        elapsed = round(time.perf_counter() - started, 3)
        if completed.returncode != 0 or not out_path.is_file():
            code = "child_failed"
            if out_path.is_file():
                code = json.loads(out_path.read_text(encoding="utf-8")).get("code", code)
            return {"schema": REPORT_SCHEMA, "status": "error", "error": {
                "code": code, "returncode": completed.returncode,
                "stderr_tail": completed.stderr[-2000:].replace(str(workspace), "<workspace>")}}
        child = json.loads(out_path.read_text(encoding="utf-8"))
    records = build_records(book, selected, child, responder)
    summary = summarize(records, responder=responder, split=split)
    contrasts = contrast_report(records)
    return {
        "schema": REPORT_SCHEMA, "status": "ok", "responder": responder, "split": split,
        "fixture_book": {"frozen_split_sha256": book["frozen_split_sha256"], "items": len(selected),
                         "routes": [r["name"] for r in book["routes"]]},
        "candidate_source_sha256": _source_hashes(plugin_parent),
        "jev_total_calls": child["jev_total"], "jev_cap": max_jev_calls, "jev_cap_reached": child["jev_cap_reached"],
        "elapsed_seconds": elapsed,
        "summary": summary, "contrasts": contrasts, "acceptance": acceptance(summary, contrasts, records),
        "fixtures": records, "new_recordings": child.get("new_recordings", {}),
    }


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--child"]:
        return _run_child(Path(argv[1]), Path(argv[2]))
    parser = argparse.ArgumentParser(description="Evaluate adaptive reasoning effort on public synthetic fixtures")
    parser.add_argument("--plugin-parent", type=Path, default=repository_root())
    parser.add_argument("--fixtures", type=Path,
                        default=Path(__file__).resolve().parent / "adaptive_effort_fixtures.json")
    parser.add_argument("--split", choices=(*SPLITS, "all"), default="dev")
    parser.add_argument("--responder", choices=RESPONDERS, default="synthetic_lowest")
    parser.add_argument("--recordings", type=Path, default=None, help="recorded responses JSON (recorded mode)")
    parser.add_argument("--record-to", type=Path, default=None, help="write responses seen in this run")
    parser.add_argument("--allow-network", action="store_true", help="required for --responder live")
    parser.add_argument("--max-jev-calls", type=int, default=64)
    parser.add_argument("--jev-provider", choices=("openrouter", "typesafe"), default="openrouter")
    parser.add_argument("--python", default=None, help="Hermes Python for the isolated child")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parent / "adaptive_effort_results.json")
    parser.add_argument("--require-green", action="store_true", help="exit 1 unless acceptance is GREEN")
    args = parser.parse_args(argv)

    def fail(code: str) -> int:
        print(json.dumps({"schema": REPORT_SCHEMA, "status": "error", "error": {"code": code}}))
        return 2

    if args.responder == "live":
        if not args.allow_network:
            return fail("live_requires_allow_network")
        if not 1 <= args.max_jev_calls <= MAX_LIVE_JEV_CALLS:
            return fail("live_requires_bounded_call_cap")
        if not any(os.environ.get(key) for key in CREDENTIAL_ENV):
            return fail("live_requires_jev_credential_env")
    recordings = None
    if args.responder == "recorded":
        if args.recordings is None or not args.recordings.is_file():
            return fail("recorded_requires_recordings_file")
        recordings = json.loads(args.recordings.read_text(encoding="utf-8")).get("recordings", {})
    try:
        report = run_evaluation(plugin_parent=args.plugin_parent.resolve(), fixtures_path=args.fixtures,
                                split=args.split, responder=args.responder, recordings=recordings,
                                max_jev_calls=args.max_jev_calls, jev_provider=args.jev_provider,
                                python=args.python)
    except FixtureError as exc:
        return fail(f"fixture:{exc}")
    new_recordings = report.pop("new_recordings", {})
    if args.record_to is not None and report.get("status") == "ok":
        args.record_to.write_text(json.dumps({"schema": REPORT_SCHEMA + ".recordings",
                                              "recordings": new_recordings}, indent=2, sort_keys=True) + "\n",
                                  encoding="utf-8")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    brief = {"status": report.get("status"), "output": str(args.output),
             "acceptance": report.get("acceptance"), "contrasts": {
                 k: report.get("contrasts", {}).get(k) for k in ("measurable", "distinguishable")},
             "jev_total_calls": report.get("jev_total_calls"), "error": report.get("error")}
    print(json.dumps(brief, sort_keys=True))
    if report.get("status") != "ok":
        return 2
    if args.require_green and report["acceptance"]["overall"] != "GREEN":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
