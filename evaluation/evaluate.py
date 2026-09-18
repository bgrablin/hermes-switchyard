#!/usr/bin/env python3
"""Small offline/live evaluator for an isolated jev_decision candidate."""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

MAX_FIXTURES = 10
MAX_REQUESTS = 12
EXPECTED_MODEL = "typesafe/jev-1.13-20260917"
PACKAGE = "jev_decision"
SOURCE_SUFFIXES = {".py", ".yaml", ".yml", ".md"}


def repository_root() -> Path:
    """Return the repository root without depending on the caller's working directory."""
    return Path(__file__).resolve().parent.parent


def _is_within(path: Path, parent: Path) -> bool:
    """Use Python 3.11-compatible Path operations for containment checks."""
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _display_repository_path(path: Path, root: Path) -> str:
    """Keep shareable reports free of host-specific absolute paths."""
    try:
        relative = path.resolve().relative_to(root.resolve())
    except ValueError:
        return "<external>"
    return relative.as_posix() or "."


def require(value: bool, code: str) -> None:
    if not value:
        raise ValueError(code)


def number(value: Any) -> bool:
    return type(value) in (int, float) and value >= 0


def response_shape(response: Any, answer_names: set[str], offered: set[str] | None = None) -> None:
    require(isinstance(response, dict) and response.get("model") == EXPECTED_MODEL, "synthetic_model_mismatch")
    answers = response.get("answers")
    require(isinstance(answers, dict), "synthetic_answers_not_object")
    if offered is not None:
        require(set(answers) == answer_names, "synthetic_answer_names")
        choice = answers.get("skill")
        require(isinstance(choice, dict) and choice.get("choice") in offered, "synthetic_choice")
        probabilities = choice.get("probabilities")
        require(isinstance(probabilities, dict) and set(probabilities) == offered, "synthetic_probability_keys")
        require(all(type(v) in (int, float) and 0 <= v <= 1 for v in probabilities.values()),
                "synthetic_probability_values")
        require(type(choice.get("confidence")) in (int, float) and 0 <= choice["confidence"] <= 1,
                "synthetic_confidence")
        answers = {"needs_skill": answers["needs_skill"]}
    else:
        require(answers and all(name.startswith("fit_") for name in answers), "synthetic_fit_answers")
    for answer in answers.values():
        require(isinstance(answer, dict) and type(answer.get("noul")) in (int, float)
                and 0 <= answer["noul"] <= 1, "synthetic_noul")
    usage = response.get("usage", {})
    require(isinstance(usage, dict), "synthetic_usage")
    if "cost" in usage:
        require(number(usage["cost"]), "synthetic_cost")
    if "latency_ms" in response:
        require(number(response["latency_ms"]), "synthetic_latency")


def validate_fixture_book(book: Any) -> list[dict[str, Any]]:
    require(isinstance(book, dict) and book.get("schema_version") == 1, "fixture_schema")
    require(book.get("public_synthetic") is True, "fixture_book_ack_boundary")
    fixtures = book.get("fixtures")
    require(isinstance(fixtures, list) and 1 <= len(fixtures) <= MAX_FIXTURES, "fixture_count")
    seen: set[str] = set()
    for fixture in fixtures:
        require(isinstance(fixture, dict), "fixture_object")
        fixture_id = fixture.get("id")
        require(isinstance(fixture_id, str) and fixture_id and fixture_id not in seen, "fixture_id")
        seen.add(fixture_id)
        require(fixture.get("public_synthetic") is True, "fixture_ack_boundary")
        require(fixture.get("kind") in {"skill", "model"}, "fixture_kind")
        require(isinstance(fixture.get("task"), str) and fixture["task"].strip(), "fixture_task")
        expected = fixture.get("expected")
        require(isinstance(expected, dict) and expected.get("status") in {"selected", "abstained"}, "expected_result")
        require(expected.get("semantic_class") in {"strict", "ambiguous"}, "semantic_class")
        if expected["status"] == "selected":
            require(isinstance(expected.get("selected"), str), "selected_expected_name")
        else:
            require(expected.get("selected") is None, "abstention_expected_null")
        if fixture["kind"] == "skill":
            candidates = fixture.get("candidates")
            require(isinstance(candidates, list) and candidates, "skill_candidates")
            names: set[str] = set()
            for candidate in candidates:
                require(isinstance(candidate, dict), "skill_candidate_object")
                name = candidate.get("name")
                require(isinstance(name, str) and name and name == name.strip() and name not in names,
                        "skill_candidate_name")
                require(isinstance(candidate.get("description"), str) and candidate["description"].strip(),
                        "skill_candidate_description")
                names.add(name)
            require(expected.get("selected") is None or expected["selected"] in names, "expected_skill_not_offered")
            response_shape(fixture.get("synthetic_response"), {"skill", "needs_skill"}, names)
        else:
            candidates = fixture.get("candidates")
            require(isinstance(candidates, list) and candidates, "model_candidates")
            ids: set[str] = set()
            for candidate in candidates:
                require(isinstance(candidate, dict), "model_candidate_object")
                cid = candidate.get("id")
                require(isinstance(cid, str) and cid and cid == cid.strip() and cid not in ids, "model_candidate_id")
                require(isinstance(candidate.get("description"), str), "model_candidate_description")
                require(type(candidate.get("approved")) is bool and number(candidate.get("cost")), "model_policy_metadata")
                for key in ("data_classes_allowed", "tool_capabilities"):
                    if key in candidate:
                        require(isinstance(candidate[key], list) and all(isinstance(v, str) and v for v in candidate[key]),
                                "model_string_metadata")
                if "context_limit" in candidate:
                    require(type(candidate["context_limit"]) is int and candidate["context_limit"] > 0,
                            "model_context_metadata")
                ids.add(cid)
            requirements = fixture.get("requirements")
            require(isinstance(requirements, dict), "model_requirements")
            require(set(requirements) <= {"data_classes", "tool_capabilities", "context_limit", "budget"},
                    "model_requirement_fields")
            response = fixture.get("synthetic_response")
            if expected.get("network") is True:
                response_shape(response, set())
            else:
                require(response is None and expected.get("semantic_class") == "strict", "no_network_fixture")
    return fixtures


def load_plugin(parent: Path) -> tuple[Any, dict[str, str]]:
    parent = parent.resolve()
    package_dir = parent / PACKAGE
    require(package_dir.is_dir() and (package_dir / "__init__.py").is_file(), "plugin_package_missing")
    for name in list(sys.modules):
        if name == PACKAGE or name.startswith(PACKAGE + "."):
            del sys.modules[name]
    sys.path.insert(0, str(parent))
    module = importlib.import_module(PACKAGE)
    module_file = getattr(module, "__file__", None)
    require(isinstance(module_file, str) and _is_within(Path(module_file), package_dir), "plugin_import_not_from_parent")
    hashes = {
        str(path.relative_to(parent)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(package_dir.rglob("*"))
        if path.is_file() and path.suffix in SOURCE_SUFFIXES
    }
    require(bool(hashes), "plugin_source_hashes_empty")
    return module, hashes


def make_client(client_class: Any, *, live: bool, response: dict[str, Any] | None, limit: int):
    calls = {"count": 0, "payloads": []}

    def transport(_payload: dict) -> dict[str, Any]:
        if response is None:
            raise RuntimeError("unexpected_transport_call")
        return copy.deepcopy(response)

    client = client_class(api_key=os.getenv("OPENROUTER_API_KEY") or "offline-only-placeholder",
                          transport=None if live and response is not None else transport)
    original_post = client._post

    def guarded_post(payload: dict) -> dict:
        if calls["count"] >= limit:
            raise RuntimeError("request_cap")
        calls["count"] += 1
        calls["payloads"].append({"model": payload.get("model"), "provider": payload.get("provider")})
        return original_post(payload)

    client._post = guarded_post
    return client, calls


def safe_error(exc: Exception, phase: str) -> dict[str, str]:
    code = {PermissionError: "ack_required", ValueError: "validation_failure", TypeError: "typed_response_failure",
            RuntimeError: "transport_or_execution_failure", OSError: "transport_or_execution_failure"}.get(
                type(exc), "evaluation_failure")
    return {"phase": phase, "code": code, "exception_type": type(exc).__name__}


def result_view(result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {"status": None, "selected": None, "model": None, "latency_ms": None, "cost": None}
    usage = result.get("usage") or {}
    return {"status": result.get("status"), "selected": result.get("selected"),
            "abstention_reason": result.get("abstention_reason"), "model": result.get("model"),
            "latency_ms": result.get("latency_ms"), "cost": usage.get("cost"),
            "eligible_candidates": result.get("eligible_candidates"),
            "excluded_candidates": result.get("excluded_candidates"),
            "qualified_candidates": result.get("qualified_candidates")}


def run_fixture(fixture: dict[str, Any], module: Any, *, live: bool, remaining: int) -> tuple[dict[str, Any], int]:
    started = time.perf_counter()
    expected = fixture["expected"]
    response = fixture.get("synthetic_response")
    if fixture["kind"] == "model" and expected.get("network") is not True:
        response = None
    client, calls = make_client(module.client.DecisionClient, live=live, response=response, limit=remaining)
    result: dict[str, Any] | None = None
    error: dict[str, str] | None = None
    try:
        if fixture["kind"] == "skill":
            result = module.routing.select_skill(task=fixture["task"], candidates=fixture["candidates"], client=client,
                                                 public_or_sanitized_data_ack=True)
        else:
            result = module.routing.route_model(task=fixture["task"], candidates=fixture["candidates"],
                                                requirements=fixture["requirements"], client=client,
                                                public_or_sanitized_data_ack=True)
    except Exception as exc:  # only stable type/code is emitted
        error = safe_error(exc, "fixture")
    actual = result_view(result)
    if expected["semantic_class"] == "ambiguous":
        semantic_pass = None
    else:
        semantic_pass = result is not None and actual["status"] == expected["status"] and actual["selected"] == expected.get("selected")
    network_expected = fixture["kind"] == "skill" or expected.get("network") is True
    policy_pass = error is None and all(item["provider"] == {"allow_fallbacks": False}
                                       and item["model"] == "typesafe/jev-1.13" for item in calls["payloads"])
    if network_expected:
        policy_pass = policy_pass and actual["model"] == EXPECTED_MODEL
    if expected.get("network") is False:
        policy_pass = policy_pass and calls["count"] == 0
    passed = policy_pass and semantic_pass is not False
    synthetic = fixture.get("synthetic_response") or {}
    usage = synthetic.get("usage") or {}
    record = {"id": fixture["id"], "kind": fixture["kind"], "label": fixture.get("label"),
              "semantic_class": expected["semantic_class"],
              "expected": {"status": expected["status"], "selected": expected.get("selected"),
                           "model": synthetic.get("model") if network_expected else None,
                           "latency_ms": synthetic.get("latency_ms") if network_expected else None,
                           "cost": usage.get("cost")},
              "actual": actual, "pass": passed, "policy_pass": policy_pass,
              "semantic_pass": semantic_pass, "request_count": calls["count"],
              "latency_ms": round((time.perf_counter() - started) * 1000, 3), "error": error}
    return record, calls["count"]


def validation_probes(module: Any, public_fixture: dict[str, Any]) -> list[dict[str, Any]]:
    malformed = {"schema_version": 1, "public_synthetic": True, "fixtures": []}
    try:
        validate_fixture_book(malformed)
    except ValueError:
        malformed_pass = True
    else:
        malformed_pass = False

    def broken(_payload: dict) -> dict:
        raise RuntimeError("fixture-transport-text")

    client = module.client.DecisionClient(api_key="test-key", transport=broken)
    try:
        client.decide(public_fixture["task"], {"answer": {"type": "noul"}}, public_or_sanitized_data_ack=True)
    except RuntimeError as exc:
        transport_pass = "fixture-transport-text" not in str(exc)
    except Exception:
        transport_pass = False
    else:
        transport_pass = False
    return [{"name": "malformed_fixture_rejected", "pass": malformed_pass},
            {"name": "transport_failure_redacted", "pass": transport_pass}]


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate an isolated jev_decision plugin candidate")
    parser.add_argument("--validate", action="store_true", help="run offline synthetic validation (default)")
    parser.add_argument("--live", action="store_true", help="make bounded OpenRouter Decisions API calls")
    parser.add_argument(
        "--plugin-parent",
        type=Path,
        default=None,
        help="parent containing jev_decision/ (default: this repository)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="report path; relative paths use the current working directory (default: evaluation/results.json)",
    )
    args = parser.parse_args()
    if args.validate and args.live:
        print(json.dumps({"status": "error", "error": {"code": "mutually_exclusive_mode"}}))
        return 2
    live = bool(args.live)
    evaluation_dir = Path(__file__).resolve().parent
    repo_root = repository_root()
    plugin_parent = (args.plugin_parent or repo_root).expanduser()
    if not plugin_parent.is_absolute():
        plugin_parent = Path.cwd() / plugin_parent
    plugin_parent = plugin_parent.resolve()
    output = (args.output or (evaluation_dir / "results.json")).expanduser()
    if not output.is_absolute():
        output = Path.cwd() / output
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if live and not os.getenv("OPENROUTER_API_KEY"):
        print(json.dumps({"status": "error", "error": {"code": "missing_openrouter_api_key"}}))
        return 2
    try:
        fixtures = validate_fixture_book(json.loads((evaluation_dir / "fixtures.json").read_text(encoding="utf-8")))
        module, hashes = load_plugin(plugin_parent)
        probes = validation_probes(module, fixtures[0])
        records: list[dict[str, Any]] = []
        requests = 0
        for fixture in fixtures:
            if requests >= MAX_REQUESTS:
                raise ValueError("request_cap_exhausted")
            record, count = run_fixture(fixture, module, live=live, remaining=MAX_REQUESTS - requests)
            requests += count
            records.append(record)
        policy_pass = all(item["policy_pass"] for item in records) and all(item["pass"] for item in probes)
        scored = [item for item in records if item["semantic_pass"] is not None]
        semantic = {"scored": len(scored), "passed": sum(item["semantic_pass"] for item in scored),
                    "pass": all(item["semantic_pass"] for item in scored),
                    "ambiguous_unscored": len(records) - len(scored), "quality_claim": False}
        report = {"status": "ok", "mode": "live" if live else "validate",
                  "plugin_parent": _display_repository_path(plugin_parent, repo_root),
                  "expected_resolved_model": EXPECTED_MODEL,
                  "max_requests": MAX_REQUESTS, "requests": requests, "policy_pass": policy_pass,
                  "semantic_accuracy": semantic, "fixtures": records, "validation_probes": probes,
                  "source_hashes": hashes}
    except Exception as exc:
        report = {"status": "error", "error": safe_error(exc, "setup_or_validation")}
        output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, sort_keys=True))
        return 1
    output.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if policy_pass and semantic["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
