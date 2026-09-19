#!/usr/bin/env python3
"""Offline-first, three-arm Hermes Switchyard skill-selection benchmark."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import math
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
FIXTURES = ROOT / "fixtures.json"
PROMPT = ROOT / "baseline_prompt.md"
LUNA_MODEL = "openai-codex/gpt-5.6-luna-900k"
JEV_MODELS = {"typesafe/jev-1.13", "typesafe/jev-1.13-20260917"}
CATEGORIES = {"clear_positive", "no_fit", "near_miss", "multi_skill_or_ambiguous"}
MEASUREMENT_SCHEMA_VERSION = 1
LIVE_COLLECTORS = {
    "luna": "luna_hermes_oneshot",
    "switchyard": "switchyard_decision_client",
}
PROVIDER_ARMS = frozenset({"luna", "switchyard"})
COMPARATIVE_ARMS = frozenset({"lexical", "luna", "switchyard"})
COLLECTOR_SOURCE_FILES = {
    "luna": ("benchmark.py", "collector_common.py", "collect_luna.py"),
    "switchyard": ("benchmark.py", "collector_common.py", "collect_switchyard.py"),
}
MAX_ABSTENTION_REASON_CHARS = 512
STOP = frozenset("a an and are as at be before by can do for from has have in is it of on or that the then this to with without".split())
LEXICAL_MIN = 0.20
LEXICAL_MARGIN = 0.05


class BenchmarkRefusal(RuntimeError):
    """The report is unsafe to compare because an invariant failed."""


def canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def public_case(case: dict[str, Any]) -> dict[str, Any]:
    return {key: case[key] for key in ("id", "category", "task", "expected", "public_synthetic")}


def collector_source_hashes() -> dict[str, str]:
    file_hashes = {
        name: file_digest(ROOT / name)
        for names in COLLECTOR_SOURCE_FILES.values()
        for name in names
    }
    return {
        arm: digest({name: file_hashes[name] for name in names})
        for arm, names in COLLECTOR_SOURCE_FILES.items()
    }


def source_hashes(plugin_path: Path) -> dict[str, Any]:
    benchmark_files = {name: file_digest(ROOT / name) for name in ("benchmark.py", "fixtures.json", "baseline_prompt.md")}
    plugin_files = {}
    package = plugin_path / "jev_decision"
    for path in sorted(package.rglob("*")):
        if path.is_file() and path.suffix in {".py", ".yaml", ".yml", ".md"}:
            plugin_files[path.relative_to(plugin_path).as_posix()] = file_digest(path)
    manifest = plugin_path / "plugin.yaml"
    if manifest.is_file():
        plugin_files[manifest.relative_to(plugin_path).as_posix()] = file_digest(manifest)
    require(plugin_files, "plugin_source_hashes_empty")
    return {
        "benchmark": digest(benchmark_files),
        "prompt": file_digest(PROMPT),
        "plugin": digest(plugin_files),
        "plugin_files": plugin_files,
        "collector_hashes": collector_source_hashes(),
    }


def load_book(path: Path = FIXTURES) -> tuple[dict[str, Any], dict[str, Any]]:
    book = json.loads(path.read_text(encoding="utf-8"))
    require(book.get("schema_version") == 1 and book.get("public_synthetic") is True, "fixture_book_boundary")
    require(book.get("label_frozen") is True, "labels_must_be_frozen")
    catalog = book.get("candidate_catalog")
    require(isinstance(catalog, list) and len(catalog) >= 8, "candidate_catalog_count")
    names = []
    for candidate in catalog:
        require(isinstance(candidate, dict), "candidate_object")
        name, description = candidate.get("name"), candidate.get("description")
        require(isinstance(name, str) and name and name == name.strip() and name not in names, "candidate_name")
        require(isinstance(description, str) and description.strip(), "candidate_description")
        names.append(name)
    catalog_hash = digest(catalog)
    for split in ("development_fixtures", "heldout_fixtures"):
        fixtures = book.get(split)
        require(isinstance(fixtures, list) and fixtures, f"{split}_missing")
        seen = set()
        for case in fixtures:
            require(isinstance(case, dict), "case_object")
            cid = case.get("id")
            require(isinstance(cid, str) and cid and cid not in seen, "case_id")
            seen.add(cid)
            require(case.get("public_synthetic") is True and isinstance(case.get("task"), str) and case["task"].strip(), "case_boundary")
            require(case.get("category") in CATEGORIES, "case_category")
            expected = case.get("expected")
            require(isinstance(expected, dict), "expected_object")
            label_type = expected.get("label_type")
            require(label_type in {"single", "no_fit", "required_set", "ambiguous"}, "label_type")
            if label_type == "no_fit":
                require(case["category"] == "no_fit" and expected.get("status") == "abstained" and expected.get("selected") is None, "no_fit_label")
            elif label_type == "single":
                require(expected.get("status") == "selected" and expected.get("selected") in names, "single_label")
                require(expected.get("required_skills") == [expected["selected"]], "single_required_set")
            elif label_type == "required_set":
                required = expected.get("required_skills")
                require(case["category"] == "multi_skill_or_ambiguous" and isinstance(required, list) and len(required) >= 2 and set(required) <= set(names), "required_set_label")
            else:
                acceptable = expected.get("acceptable_skills")
                require(case["category"] == "multi_skill_or_ambiguous" and isinstance(acceptable, list) and len(acceptable) >= 2 and set(acceptable) <= set(names), "ambiguous_label")
            sim = case.get("offline_simulation")
            require(isinstance(sim, dict) and isinstance(sim.get("luna"), dict) and isinstance(sim.get("switchyard"), dict), "offline_simulation_missing")
            luna = sim["luna"]
            require(luna.get("model") == LUNA_MODEL and luna.get("provider") == "openai-codex" and luna.get("reasoning") == "max", "luna_simulation_route")
            require(luna.get("status") in {"selected", "abstained"}, "luna_simulation_status")
            selected_skills = luna.get("selected_skills", [])
            require(isinstance(selected_skills, list) and len(set(selected_skills)) == len(selected_skills) and set(selected_skills) <= set(names), "luna_simulation_skills")
            require((luna.get("status") == "selected") == bool(selected_skills), "luna_simulation_consistency")
            require(luna.get("selected") == (selected_skills[0] if selected_skills else None), "luna_simulation_top1")
            switch = sim["switchyard"]
            require(switch.get("choice") in names and type(switch.get("confidence")) in (int, float) and 0 <= switch["confidence"] <= 1, "switchyard_simulation_choice")
            require(type(switch.get("winning_probability")) in (int, float) and 0 <= switch["winning_probability"] <= 1, "switchyard_simulation_probability")
            require(type(switch.get("needs_skill")) in (int, float) and 0 <= switch["needs_skill"] <= 1, "switchyard_simulation_need")
    heldout = book["heldout_fixtures"]
    require(len(heldout) == 24, "heldout_fixture_count")
    require(Counter(case["category"] for case in heldout) == Counter({category: 6 for category in CATEGORIES}), "heldout_category_balance")
    payload = {"candidate_catalog": catalog, "cases": [public_case(case) for case in heldout]}
    dataset_hash = digest(payload)
    template_hash = file_digest(PROMPT)
    task_hashes = {case["id"]: digest(case["task"]) for case in heldout}
    request_hashes = {
        case["id"]: digest({"catalog": catalog, "task": case["task"], "prompt_hash": template_hash})
        for case in heldout
    }
    arm_request_hashes = {
        arm: {
            case["id"]: digest({
                "arm": arm,
                "catalog": catalog,
                "task": case["task"],
                "template_hash": template_hash if arm == "luna" else None,
            })
            for case in heldout
        }
        for arm in COMPARATIVE_ARMS
    }
    arm_request_identities = {
        arm: {
            case["id"]: {
                "arm": arm,
                "case_id": case["id"],
                "task_hash": task_hashes[case["id"]],
                "candidate_catalog_hash": catalog_hash,
                "template_hash": template_hash if arm == "luna" else None,
                "request_hash": arm_request_hashes[arm][case["id"]],
            }
            for case in heldout
        }
        for arm in COMPARATIVE_ARMS
    }
    meta = {
        "catalog": catalog,
        "names": names,
        "catalog_hash": catalog_hash,
        "dataset_hash": dataset_hash,
        "case_hashes": {case["id"]: digest({"catalog": catalog, "case": public_case(case)}) for case in heldout},
        "template_hash": template_hash,
        "task_hashes": task_hashes,
        "request_hashes": request_hashes,
        "arm_request_hashes": arm_request_hashes,
        "arm_request_identities": arm_request_identities,
        "collector_hashes": collector_source_hashes(),
        "request_identities": {
            case["id"]: {
                "case_id": case["id"],
                "task_hash": task_hashes[case["id"]],
                "candidate_catalog_hash": catalog_hash,
                "template_hash": template_hash,
                "request_hash": request_hashes[case["id"]],
            }
            for case in heldout
        },
    }
    return book, meta


def render_luna_prompt(case: dict[str, Any], meta: dict[str, Any]) -> str:
    """Render the direct-Luna request without labels, fixtures, or private context."""
    case_id = case.get("id")
    require(case_id in meta["request_identities"], "unknown_case_for_prompt")
    task = case.get("task")
    require(isinstance(task, str) and task.strip(), "prompt_task_required")
    require(digest(task) == meta["task_hashes"][case_id], "case_task_hash_mismatch")
    request = {"task": task, "candidate_catalog": copy.deepcopy(meta["catalog"])}
    require(
        digest({"catalog": request["candidate_catalog"], "task": task, "prompt_hash": meta["template_hash"]})
        == meta["request_hashes"][case_id],
        "request_identity_mismatch",
    )
    prompt = PROMPT.read_text(encoding="utf-8")
    require(prompt.count("{{TASK_JSON}}") == 1 and prompt.count("{{CANDIDATE_CATALOG_JSON}}") == 1, "prompt_template_placeholders")
    return prompt.replace("{{TASK_JSON}}", json.dumps(request["task"], ensure_ascii=True)) \
        .replace("{{CANDIDATE_CATALOG_JSON}}", json.dumps(request["candidate_catalog"], ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def import_plugin(plugin_path: Path) -> tuple[Any, Any, dict[str, str]]:
    plugin_path = plugin_path.expanduser().resolve()
    package = plugin_path / "jev_decision"
    require(package.is_dir() and (package / "__init__.py").is_file(), "plugin_path_must_contain_jev_decision")
    for name in list(sys.modules):
        if name == "jev_decision" or name.startswith("jev_decision."):
            del sys.modules[name]
    sys.path.insert(0, str(plugin_path))
    package_module = importlib.import_module("jev_decision")
    routing = importlib.import_module("jev_decision.routing")
    client_module = importlib.import_module("jev_decision.client")
    module_file = Path(getattr(package_module, "__file__", "")).resolve()
    require(module_file.is_relative_to(package), "plugin_import_outside_exact_path")
    return routing, client_module, source_hashes(plugin_path)


def words(text: str) -> set[str]:
    return {word for word in re.findall(r"[a-z0-9]+", text.lower()) if len(word) > 1 and word not in STOP}


def lexical_select(task: str, catalog: list[dict[str, str]]) -> dict[str, Any]:
    task_words = words(task)
    scored = []
    for position, candidate in enumerate(catalog):
        candidate_words = words(candidate["name"] + " " + candidate["description"])
        overlap = task_words & candidate_words
        score = len(overlap) / max(1, len(task_words))
        scored.append({"name": candidate["name"], "score": round(score, 8), "overlap": sorted(overlap), "position": position})
    ranked = sorted(scored, key=lambda item: (-item["score"], item["position"]))
    best, second = ranked[0], ranked[1] if len(ranked) > 1 else {"score": 0}
    abstain = best["score"] < LEXICAL_MIN or (second["score"] > 0 and best["score"] - second["score"] < LEXICAL_MARGIN)
    return {"status": "abstained" if abstain else "selected", "selected": None if abstain else best["name"],
            "selected_skills": [] if abstain else [best["name"]], "scores": ranked,
            "abstention_reason": "lexical_score_or_margin_gate" if abstain else None}


def run_lexical_case(case: dict[str, Any], meta: dict[str, Any], source: dict[str, str]) -> dict[str, Any]:
    """Run the deterministic arm without pretending it is a provider observation."""
    started = time.perf_counter()
    result = lexical_select(case["task"], meta["catalog"])
    return record(
        case,
        meta,
        "lexical",
        result,
        source["benchmark"],
        wall_ms=(time.perf_counter() - started) * 1000,
        provider_ms=None,
        selector_calls=0,
        provider_calls=0,
        coordination_calls=0,
        simulated=True,
        timing_status="offline_local",
    )


class SyntheticClient:
    def __init__(self, response: dict[str, Any]):
        self.response = response
        self.calls = 0
        self.provider_ms = 0.0

    def decide(self, *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        started = time.perf_counter()
        self.calls += 1
        result = copy.deepcopy(self.response)
        self.provider_ms += (time.perf_counter() - started) * 1000
        return result


def switchyard_response(case: dict[str, Any], names: list[str]) -> dict[str, Any]:
    sim = case["offline_simulation"]["switchyard"]
    winner = float(sim["winning_probability"])
    rest = (1.0 - winner) / max(1, len(names) - 1)
    probabilities = {name: rest for name in names}
    probabilities[sim["choice"]] = winner
    return {"model": sim.get("model", "typesafe/jev-1.13-20260917"), "latency_ms": None,
            "usage": sim.get("usage") or {}, "answers": {
                "skill": {"choice": sim["choice"], "probabilities": probabilities, "confidence": sim["confidence"]},
                "needs_skill": {"noul": sim["needs_skill"]},
            }}


def usage_view(raw: Any, arm: str) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    return {
        "input_tokens": raw.get("input_tokens"), "output_tokens": raw.get("output_tokens"), "total_tokens": raw.get("total_tokens"),
        "included_codex_quota_tokens": raw.get("included_codex_quota_tokens"),
        "jev_payg_dollars": raw.get("jev_payg_dollars", raw.get("cost") if arm == "switchyard" else None),
        "reported_dollar_cost": raw.get("reported_dollar_cost", raw.get("cost")),
    }


def record(case: dict[str, Any], meta: dict[str, Any], arm: str, output: dict[str, Any], source_hash: str, *, wall_ms: float | None,
           provider_ms: float | None, selector_calls: int, provider_calls: int, coordination_calls: int, simulated: bool,
           timing_status: str, actual_call: bool = False, measurement_status: str | None = None,
           collector_source_hash: str | None = None,
           measurement_provenance: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    selected_skills = output.get("selected_skills")
    if selected_skills is None:
        selected_skills = [output["selected"]] if output.get("selected") is not None else []
    if measurement_status is None:
        measurement_status = "simulated" if simulated else "ok"
    if collector_source_hash is None:
        collector_source_hash = meta.get("collector_hashes", {}).get(arm)
    request_hash = meta.get("arm_request_hashes", {}).get(arm, meta["request_hashes"])[case["id"]]
    request_identity = meta.get("arm_request_identities", {}).get(arm, meta["request_identities"])[case["id"]]
    return {"arm": arm, "case_id": case["id"], "dataset_hash": meta["dataset_hash"], "fixture_hash": meta["case_hashes"][case["id"]],
            "request_hash": request_hash, "request_identity": copy.deepcopy(request_identity),
            "candidate_catalog_hash": meta["catalog_hash"], "source_hash": source_hash,
            "collector_source_hash": collector_source_hash,
            "status": output.get("status"), "selected": output.get("selected"), "selected_skills": selected_skills,
            "abstention_reason": output.get("abstention_reason"), "model": output.get("model"), "provider": output.get("provider"),
            "reasoning": output.get("reasoning"), "usage": usage_view(output.get("usage"), arm), "wall_ms": wall_ms,
            "provider_call_ms": provider_ms, "timing_scope": output.get("timing_scope", "case"), "timing_status": timing_status,
            "selector_invocation_count": selector_calls, "provider_call_count": provider_calls, "coordination_call_count": coordination_calls,
            "public_synthetic_ack": True, "actual_call": actual_call, "simulated": simulated,
            "measurement_schema_version": MEASUREMENT_SCHEMA_VERSION, "measurement_status": measurement_status,
            "measurement_provenance": copy.deepcopy(measurement_provenance), "error": copy.deepcopy(error)}


def run_offline_case(case: dict[str, Any], meta: dict[str, Any], routing: Any, source: dict[str, str]) -> dict[str, dict[str, Any]]:
    a = run_lexical_case(case, meta, source)
    luna = case["offline_simulation"]["luna"]
    b = record(case, meta, "luna", luna, source["prompt"], wall_ms=luna.get("wall_ms"), provider_ms=luna.get("provider_call_ms"),
               selector_calls=0, provider_calls=1, coordination_calls=0, simulated=True, timing_status="offline_simulation")
    client = SyntheticClient(switchyard_response(case, meta["names"]))
    started = time.perf_counter()
    result = routing.select_skill(task=case["task"], candidates=meta["catalog"], client=client, public_or_sanitized_data_ack=True)
    c = record(case, meta, "switchyard", {**result, "selected_skills": [result["selected"]] if result.get("selected") else []}, source["plugin"],
               wall_ms=(time.perf_counter() - started) * 1000, provider_ms=client.provider_ms, selector_calls=1,
               provider_calls=client.calls, coordination_calls=1, simulated=True, timing_status="offline_synthetic")
    return {"lexical": a, "luna": b, "switchyard": c}


def validate_usage(value: Any) -> None:
    require(isinstance(value, dict), "usage_must_be_object")
    for key, item in value.items():
        require(item is None or (type(item) in (int, float) and math.isfinite(item) and item >= 0), f"invalid_usage_{key}")


def _validate_measurement_provenance(row: dict[str, Any], arm: str, case: dict[str, Any], meta: dict[str, Any]) -> None:
    provenance = row.get("measurement_provenance")
    require(isinstance(provenance, dict), "live_measurement_provenance_required")
    assert isinstance(provenance, dict)
    require(provenance.get("schema_version") == MEASUREMENT_SCHEMA_VERSION, "live_measurement_provenance_version")
    require(provenance.get("kind") == "actual_provider_observation", "live_measurement_kind")
    require(provenance.get("collector") == LIVE_COLLECTORS[arm], "live_measurement_collector")
    require(provenance.get("arm") == arm and provenance.get("case_id") == case["id"], "live_measurement_identity")
    require(provenance.get("dataset_hash") == meta["dataset_hash"], "live_measurement_dataset_hash")
    require(provenance.get("candidate_catalog_hash") == meta["catalog_hash"], "live_measurement_catalog_hash")
    require(provenance.get("collector_source_hash") == row["collector_source_hash"], "live_measurement_collector_source_hash")
    require(provenance.get("request_hash") == row["request_hash"], "live_measurement_request_hash")
    require(provenance.get("request_identity") == row["request_identity"], "live_measurement_request_identity")
    expected_template_hash = meta["template_hash"] if arm == "luna" else None
    require(provenance.get("template_hash") == expected_template_hash, "live_measurement_template_hash")
    require(isinstance(provenance.get("recorded_at_utc"), str) and provenance["recorded_at_utc"].strip(), "live_measurement_timestamp")
    require(provenance.get("measurement_scope") == row["timing_scope"], "live_measurement_scope")
    require(type(provenance.get("provider_call_count")) is int and provenance["provider_call_count"] == row["provider_call_count"], "live_measurement_call_count")
    require(type(provenance.get("actual_call")) is bool and provenance["actual_call"] == row["actual_call"], "live_measurement_actual_call")
    require(type(provenance.get("provider_response_observed")) is bool, "live_measurement_response_flag")
    require(type(provenance.get("wall_time_observed")) is bool, "live_measurement_wall_flag")
    require(type(provenance.get("provider_time_observed")) is bool, "live_measurement_provider_time_flag")
    require(type(provenance.get("usage_observed")) is bool, "live_measurement_usage_flag")
    successful = row["measurement_status"] == "ok"
    if successful:
        require(provenance["provider_response_observed"] is True, "successful_provider_response_required")
    require(provenance["wall_time_observed"] == (row["wall_ms"] is not None), "live_measurement_wall_observation")
    require(provenance["provider_time_observed"] == (row["provider_call_ms"] is not None), "live_measurement_provider_time_observation")
    if not provenance["usage_observed"]:
        require(all(value is None for value in row["usage"].values()), "unobserved_usage_must_be_null")
    if not successful:
        require(provenance["usage_observed"] is False, "failed_usage_observation")
        require(all(value is None for value in row["usage"].values()), "failed_usage_must_be_null")
    if row["timing_scope"] == "case":
        require(provenance.get("observed_case_count") == 1 and provenance.get("observed_case_ids") == [case["id"]], "live_case_observation_coverage")
    else:
        observed_ids = provenance.get("observed_case_ids")
        require(isinstance(provenance.get("batch_id"), str) and provenance["batch_id"].strip(), "live_batch_id_required")
        require(isinstance(observed_ids, list) and observed_ids and len(set(observed_ids)) == len(observed_ids), "live_batch_observation_ids")
        assert isinstance(observed_ids, list)
        require(provenance.get("observed_case_count") == len(observed_ids) and case["id"] in observed_ids, "live_batch_observation_coverage")


def validate_record(row: dict[str, Any], arm: str, case: dict[str, Any], meta: dict[str, Any], *, live: bool) -> None:
    """Validate one normalized row before it can enter a comparative summary."""
    require(isinstance(row, dict) and row.get("arm") == arm and row.get("case_id") == case["id"], "record_identity")
    expected_request_hash = meta.get("arm_request_hashes", {}).get(arm, meta["request_hashes"])[case["id"]]
    expected_request_identity = meta.get("arm_request_identities", {}).get(arm, meta["request_identities"])[case["id"]]
    for key, expected_value in (("dataset_hash", meta["dataset_hash"]), ("fixture_hash", meta["case_hashes"][case["id"]]),
                                ("request_hash", expected_request_hash), ("candidate_catalog_hash", meta["catalog_hash"])):
        require(row.get(key) == expected_value, f"mixed_or_wrong_{key}")
    require(row.get("request_identity") == expected_request_identity, "request_identity_mismatch")
    require(row.get("public_synthetic_ack") is True, "public_synthetic_ack_required")
    require(row.get("measurement_schema_version") == MEASUREMENT_SCHEMA_VERSION, "measurement_schema_version")
    require(type(row.get("actual_call")) is bool and type(row.get("simulated")) is bool, "measurement_flags")
    require(row.get("timing_scope") in {"case", "batch"}, "invalid_timing_scope")
    require(row.get("measurement_status") in ({"ok", "failed"} if live else {"simulated"}), "measurement_status")
    status = row.get("status")
    selected = row.get("selected")
    skills = row.get("selected_skills")
    abstention_reason = row.get("abstention_reason")
    require(
        abstention_reason is None
        or (type(abstention_reason) is str and len(abstention_reason) <= MAX_ABSTENTION_REASON_CHARS and "\x00" not in abstention_reason),
        "invalid_abstention_reason",
    )
    require(isinstance(skills, list) and all(type(skill) is str for skill in skills), "arm_input_selected_skills")
    assert isinstance(skills, list)
    require(len(set(skills)) == len(skills) and set(skills) <= set(meta["names"]), "arm_input_selected_skills")
    if row["measurement_status"] == "failed":
        require(status == "failed" and selected is None and skills == [], "failed_measurement_cannot_be_selection")
        require(isinstance(row.get("error"), dict) and isinstance(row["error"].get("type"), str) and row["error"]["type"], "failed_measurement_error")
    else:
        require(status in {"selected", "abstained"}, "arm_input_status")
        require((status == "selected") == bool(skills), "arm_input_selection_consistency")
        if status == "selected":
            if arm == "luna" and len(skills) > 1:
                require(selected is None or selected == skills[0], "arm_input_selection_consistency")
            else:
                require(selected == skills[0], "arm_input_selection_consistency")
        else:
            require(selected is None, "arm_input_selection_consistency")
        require(row.get("error") is None, "successful_measurement_error")
    if arm in {"lexical", "switchyard"}:
        require(len(skills) <= 1, "single_selection_arm_multiple_skills")
    if arm == "lexical":
        require(row.get("model") is None, "lexical_model_must_be_null")
    else:
        require(isinstance(row.get("model"), str) and row["model"], "arm_input_model")
    if arm == "luna":
        require(row["model"] == LUNA_MODEL and row.get("provider") == "openai-codex" and row.get("reasoning") == "max", "luna_model_route")
    if arm == "switchyard":
        require(row["model"] in JEV_MODELS, "switchyard_model_route")
        if live:
            require(row.get("provider") == "openrouter", "switchyard_provider_route")
    validate_usage(row.get("usage"))
    for key in ("wall_ms", "provider_call_ms"):
        require(row.get(key) is None or (type(row[key]) in (int, float) and math.isfinite(row[key]) and row[key] >= 0), f"invalid_{key}")
    for key in ("selector_invocation_count", "provider_call_count", "coordination_call_count"):
        require(type(row.get(key)) is int and row[key] >= 0, f"invalid_{key}")
    if arm == "luna":
        require(row["selector_invocation_count"] == 0 and row["coordination_call_count"] == 0, "luna_local_call_counts")
    if arm == "switchyard":
        require(row["selector_invocation_count"] == 1 and row["coordination_call_count"] == 1, "switchyard_call_counts")
    require(isinstance(row.get("source_hash"), str) and row["source_hash"], "missing_source_hash")
    expected_collector_hash = meta.get("collector_hashes", {}).get(arm)
    if arm in PROVIDER_ARMS:
        require(isinstance(expected_collector_hash, str) and row.get("collector_source_hash") == expected_collector_hash, "collector_source_hash_mismatch")
    else:
        require(row.get("collector_source_hash") is None, "local_arm_collector_source_hash")
    if live:
        require(row["simulated"] is False, "live_requires_non_simulated_record")
        expected_timing_status = (
            "provider_error"
            if row["measurement_status"] == "failed"
            else {"luna": "actual_end_to_end_process", "switchyard": "actual_provider_observation"}[arm]
        )
        require(row.get("timing_status") == expected_timing_status, "live_timing_status_mismatch")
        if row["measurement_status"] == "ok":
            require(row["actual_call"] is True, "live_requires_actual_provider_call")
            require(row["provider_call_count"] > 0, "live_provider_call_required")
        else:
            require(row["actual_call"] == (row["provider_call_count"] > 0), "failed_call_count_mismatch")
        _validate_measurement_provenance(row, arm, case, meta)
    else:
        require(row["simulated"] is True and row["actual_call"] is False, "offline_measurement_flags")


def ingest(path: Path, arm: str, cases: list[dict[str, Any]], meta: dict[str, Any], *, live: bool, max_requests: int | None) -> dict[str, dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(payload, dict), "arm_input_root")
    assert isinstance(payload, dict)
    require(payload.get("schema_version") == 1 and payload.get("arm") == arm, "arm_input_header")
    require(payload.get("dataset_hash") == meta["dataset_hash"], "arm_input_dataset_hash")
    require(payload.get("candidate_catalog_hash") == meta["catalog_hash"], "arm_input_catalog_hash")
    rows = payload.get("records")
    require(isinstance(rows, list), "arm_input_records")
    expected = {case["id"]: case for case in cases}
    result = {}
    for row in rows:
        require(isinstance(row, dict) and row.get("case_id") in expected and row["case_id"] not in result, "arm_input_case_set")
        case = expected[row["case_id"]]
        validate_record(row, arm, case, meta, live=live)
        result[row["case_id"]] = row
    require(set(result) == set(expected), "arm_missing_or_extra_cases")
    if live:
        require(payload.get("measurement_schema_version") == MEASUREMENT_SCHEMA_VERSION, "live_measurement_schema_version")
        require(payload.get("collection_mode") == "live", "live_input_mode")
        require(payload.get("public_synthetic_ack") is True, "live_input_ack")
        total = sum(row["provider_call_count"] for row in result.values())
        require(max_requests is not None and total <= max_requests, "live_request_cap_exceeded")
    return result


def outcome(case: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    expected = case["expected"]
    label = expected["label_type"]
    actual = set(row["selected_skills"])
    target = set(expected.get("required_skills", expected.get("acceptable_skills", [])))
    coverage = None if not target else len(actual & target) / len(target)
    positive = label != "no_fit"
    abstained = row["status"] == "abstained"
    top1_defined = label == "single"
    top1_correct = (row["status"] == "selected" and len(row["selected_skills"]) == 1 and row["selected"] == expected.get("selected")) if top1_defined else None
    no_fit_fp = (not positive and row["status"] == "selected")
    if label == "required_set":
        positive_miss = coverage < 1.0
        ambiguous_hit = None
    elif label == "ambiguous":
        positive_miss = row["status"] != "selected" or not bool(actual & target)
        ambiguous_hit = bool(actual & target) if row["status"] == "selected" else False
    elif label == "single":
        positive_miss = not top1_correct
        ambiguous_hit = None
    else:
        positive_miss = False
        ambiguous_hit = None
    return {"status": row["status"], "selected": row["selected"], "selected_skills": row["selected_skills"],
            "top1_defined": top1_defined, "top1_correct": top1_correct, "no_fit_false_positive": no_fit_fp,
            "positive_abstention": positive and abstained, "positive_miss": positive and positive_miss,
            "required_or_acceptable_skills": sorted(target), "coverage": coverage, "ambiguous_hit": ambiguous_hit,
            "fixture_hash": row["fixture_hash"], "request_hash": row["request_hash"]}


def percentile(values: list[float], p: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(1, math.ceil(p * len(ordered))) - 1
    return round(ordered[index], 3)


def timing_summary(rows: list[dict[str, Any]], mode: str) -> dict[str, Any]:
    scopes = {row["timing_scope"] for row in rows}
    individual = scopes == {"case"}
    wall = [float(row["wall_ms"]) for row in rows if individual and row["wall_ms"] is not None]
    provider = [float(row["provider_call_ms"]) for row in rows if individual and row["provider_call_ms"] is not None]
    actual = mode == "live" and all(row["measurement_status"] == "ok" and row["actual_call"] is True and row["simulated"] is False for row in rows)
    wall_complete = individual and len(wall) == len(rows) and all(
        isinstance(row.get("measurement_provenance"), dict) and row["measurement_provenance"].get("wall_time_observed") is True
        for row in rows
    )
    provider_complete = individual and len(provider) == len(rows) and all(
        isinstance(row.get("measurement_provenance"), dict) and row["measurement_provenance"].get("provider_time_observed") is True
        for row in rows
    )
    claimable = actual and wall_complete
    provider_claimable = actual and provider_complete
    if mode != "live":
        claimability_reason = "offline timings are harness/simulation timings"
    elif not actual:
        claimability_reason = "timing requires successful non-simulated provider observations"
    elif not wall_complete:
        claimability_reason = "batch scope or missing per-case wall time"
    else:
        claimability_reason = None
    return {"individual_latency": individual, "claimable": claimable, "claimability_reason": claimability_reason,
            "wall_time_scope": "case_end_to_end" if wall_complete else None,
            "total_wall_ms": round(sum(wall), 3) if wall_complete else None,
            "total_provider_call_ms": round(sum(provider), 3) if provider_claimable else None,
            "provider_timing_claimable": provider_claimable,
            "provider_timing_observation_count": len(provider),
            "provider_timing_case_count": len(rows),
            "provider_timing_coverage_complete": provider_complete,
            "provider_timing_claimability_reason": None if provider_claimable else
            ("offline timings are harness/simulation timings" if mode != "live" else
             "provider timing missing for one or more cases"),
            "p50_wall_ms": percentile(wall, 0.50), "p95_wall_ms": percentile(wall, 0.95),
            "p50_provider_call_ms": percentile(provider, 0.50) if provider_claimable else None,
            "p95_provider_call_ms": percentile(provider, 0.95) if provider_claimable else None,
            "convention": "nearest-rank: sort values, rank=ceil(p*n), clamp the rank to at least 1"}


def usage_summary(rows: list[dict[str, Any]], mode: str = "offline") -> dict[str, Any]:
    fields = ("input_tokens", "output_tokens", "total_tokens", "included_codex_quota_tokens", "jev_payg_dollars", "reported_dollar_cost")
    result = {}
    actual = mode == "live" and all(row["measurement_status"] == "ok" and row["actual_call"] is True and row["simulated"] is False for row in rows)
    for field in fields:
        values = [row["usage"].get(field) for row in rows if row["usage"].get(field) is not None] if (mode != "live" or actual) else []
        result[field] = {"known_cases": len(values), "sum": round(sum(values), 8) if values else None}
    result["unknown_is_null_not_zero"] = True
    result["actual_observation_required_for_live_totals"] = True
    result["live_totals_claimable"] = actual
    return result


def summarize(book: dict[str, Any], meta: dict[str, Any], arms: dict[str, dict[str, dict[str, Any]]], *, mode: str, source: dict[str, str]) -> dict[str, Any]:
    cases = book["heldout_fixtures"]
    expected_ids = {case["id"] for case in cases}
    expected_sources = {"lexical": source.get("benchmark"), "luna": source.get("prompt"), "switchyard": source.get("plugin")}
    if set(arms) != COMPARATIVE_ARMS:
        raise BenchmarkRefusal("comparative_summary_refused: expected lexical, luna, and switchyard arms")
    for arm, rows in arms.items():
        if arm not in expected_sources or not expected_sources[arm]:
            raise BenchmarkRefusal(f"comparative_summary_refused: no expected source hash for {arm}")
        if set(rows) != expected_ids:
            raise BenchmarkRefusal(f"comparative_summary_refused: {arm} missing or extra cases")
        for case in cases:
            row = rows[case["id"]]
            try:
                validate_record(row, arm, case, meta, live=mode == "live" and arm in PROVIDER_ARMS)
            except (KeyError, TypeError, ValueError) as exc:
                raise BenchmarkRefusal(f"comparative_summary_refused: invalid {arm} record: {exc}") from None
            if row.get("source_hash") != expected_sources[arm]:
                raise BenchmarkRefusal(f"comparative_summary_refused: {arm} source hash does not match its expected target")
            if mode == "live" and arm in PROVIDER_ARMS and row["measurement_status"] != "ok":
                raise BenchmarkRefusal(f"comparative_summary_refused: {arm} contains a failed measurement")
    case_results = []
    arm_summaries = {}
    for arm, rows in arms.items():
        scored = [outcome(case, rows[case["id"]]) for case in cases]
        top = [item for item in scored if item["top1_defined"]]
        no_fit = [item for case, item in zip(cases, scored) if case["expected"]["label_type"] == "no_fit"]
        positive = [item for case, item in zip(cases, scored) if case["expected"]["label_type"] != "no_fit"]
        required = [item for case, item in zip(cases, scored) if case["expected"]["label_type"] == "required_set"]
        ambiguous = [item for case, item in zip(cases, scored) if case["expected"]["label_type"] == "ambiguous"]
        multi_skill = required
        targets = [item for item in scored if item["required_or_acceptable_skills"]]
        covered = sum(1 for item in targets if set(item["required_or_acceptable_skills"]) <= set(meta["names"]))
        top1_correct = sum(bool(item["top1_correct"]) for item in top)
        multi_coverage = [item["coverage"] for item in multi_skill if item["coverage"] is not None]
        arm_summaries[arm] = {"case_count": len(scored), "top1": {"defined": len(top), "correct": top1_correct,
                         "accuracy": (top1_correct / len(top)) if top else None, "contract": "exactly one selected skill"},
            "no_fit": {"cases": len(no_fit), "false_positives": sum(item["no_fit_false_positive"] for item in no_fit),
                       "false_positive_rate": sum(item["no_fit_false_positive"] for item in no_fit) / len(no_fit) if no_fit else None,
                       "contract": "abstained with no selected skill is the only no-fit hit"},
            "positive_abstention": {"cases": sum(item["positive_abstention"] for item in positive), "rate": sum(item["positive_abstention"] for item in positive) / len(positive)},
            "positive_miss": {"cases": sum(item["positive_miss"] for item in positive), "rate": sum(item["positive_miss"] for item in positive) / len(positive)},
            "candidate_coverage": {"target_cases": len(targets), "covered_cases": covered, "rate": covered / len(targets) if targets else None},
            "multi_skill_capability": {"cases": len(multi_skill), "complete_cases": sum(value == 1.0 for value in multi_coverage),
                                       "mean_coverage": sum(multi_coverage) / len(multi_coverage) if multi_coverage else None,
                                       "coverage_is_not_top1": True},
            "required_set": {"cases": len(required), "complete": sum(item["coverage"] == 1.0 for item in required),
                             "mean_coverage": sum(item["coverage"] for item in required) / len(required) if required else None},
            "ambiguous": {"cases": len(ambiguous), "accepted": sum(bool(item["ambiguous_hit"]) for item in ambiguous),
                          "accepted_rate": sum(bool(item["ambiguous_hit"]) for item in ambiguous) / len(ambiguous) if ambiguous else None},
            "usage": usage_summary(list(rows.values()), mode), "timing": timing_summary(list(rows.values()), mode),
            "source_hash": expected_sources[arm],
        }
    for case in cases:
        case_results.append({"case_id": case["id"], "category": case["category"], "expected": case["expected"],
                             "arms": {arm: outcome(case, rows[case["id"]]) for arm, rows in arms.items()}})
    actual_measurements = mode == "live" and all(
        row["measurement_status"] == "ok" and row["actual_call"] is True and row["simulated"] is False
        for arm in PROVIDER_ARMS for row in arms[arm].values()
    )
    return {"status": "ok", "mode": mode, "dataset_hash": meta["dataset_hash"], "candidate_catalog_hash": meta["catalog_hash"],
            "heldout_case_count": len(cases), "label_frozen": True, "source_hashes": source,
            "timing_claims_allowed": actual_measurements,
            "comparative_summary": "complete; no advantage claim is made by this harness",
            "aggregate_score": None,
            "aggregate_score_policy": "disabled: Luna may return multiple skills while Switchyard is strict top-1",
            "baseline_limitations": [
                "Luna uses the user-selected max reasoning setting; it is not the quickest default Hermes route.",
                "This is a selector microbenchmark, not proof of whole-agent gains.",
                "Multi-skill capability coverage is reported separately from strict top-1 and no-fit contracts.",
            ],
            "arms": arm_summaries, "cases": case_results,
            "p95_and_p50_convention": "nearest-rank; batch-scoped timing is never treated as individual latency"}


def run(args: argparse.Namespace) -> dict[str, Any]:
    book, meta = load_book()
    require(args.dataset == "heldout", "only heldout is scoreable for comparative reports")
    require(getattr(args, "plugin_path", None), "reviewed_plugin_path_required")
    plugin_path = Path(args.plugin_path).expanduser().resolve()
    routing, _client_module, source = import_plugin(plugin_path)
    require(source == source_hashes(plugin_path), "source_changed_during_run")
    cases = book["heldout_fixtures"]
    if args.mode == "live":
        require(args.public_synthetic_ack and args.max_requests is not None and 1 <= args.max_requests <= 60, "live_requires_ack_and_explicit_cap_1_to_60")
        require(args.luna_input and args.switchyard_input, "live_requires_normalized_arm_inputs")
    if args.luna_input:
        luna = ingest(Path(args.luna_input), "luna", cases, meta, live=args.mode == "live", max_requests=args.max_requests)
    else:
        luna = {}
    if args.switchyard_input:
        switchyard = ingest(Path(args.switchyard_input), "switchyard", cases, meta, live=args.mode == "live", max_requests=args.max_requests)
    else:
        switchyard = {}
    if args.lexical_input:
        lexical = ingest(Path(args.lexical_input), "lexical", cases, meta, live=False, max_requests=None)
    else:
        lexical = {case["id"]: run_lexical_case(case, meta, source) for case in cases}
    if args.mode == "offline":
        offline = {case["id"]: run_offline_case(case, meta, routing, source) for case in cases}
        lexical = lexical or {cid: rows["lexical"] for cid, rows in offline.items()}
        luna = luna or {cid: rows["luna"] for cid, rows in offline.items()}
        switchyard = switchyard or {cid: rows["switchyard"] for cid, rows in offline.items()}
    total_requests = sum(row["provider_call_count"] for rows in (luna, switchyard) for row in rows.values())
    if args.mode == "live":
        require(total_requests <= args.max_requests, "combined_live_request_cap_exceeded")
    report = summarize(book, meta, {"lexical": lexical, "luna": luna, "switchyard": switchyard}, mode=args.mode, source=source)
    report["provider_call_count_total"] = total_requests
    report["actual_provider_calls"] = total_requests if args.mode == "live" else 0
    report["request_cap"] = args.max_requests
    report["plugin_path_display"] = plugin_path.name
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Offline-first Hermes Switchyard skill-selection benchmark")
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument("--dataset", choices=("heldout",), default="heldout")
    parser.add_argument("--plugin-path", required=True, help="reviewed source path containing jev_decision/; no machine-specific default")
    parser.add_argument("--lexical-input", help="normalized lexical arm JSON; normally computed locally")
    parser.add_argument("--luna-input", help="normalized direct-Luna arm JSON")
    parser.add_argument("--switchyard-input", help="normalized current-Switchyard arm JSON")
    parser.add_argument("--public-synthetic-ack", action="store_true")
    parser.add_argument("--max-requests", type=int, help="required explicit live cap, maximum 60")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = run(args)
    except (ValueError, BenchmarkRefusal, OSError, json.JSONDecodeError) as exc:
        report = {"status": "refused", "reason": type(exc).__name__, "detail": str(exc)}
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(json.dumps(report, indent=2, sort_keys=True))
        return 2
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
