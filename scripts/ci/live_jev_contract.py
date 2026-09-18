#!/usr/bin/env python3
"""Exercise the live Jev contract through the actual candidate plugin."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.ci.check_native_hermes import EXPECTED_PLUGIN

_ALLOWED_MODELS = frozenset({"typesafe/jev-1.13", "typesafe/jev-1.13-20260917"})
_USAGE_FIELDS = frozenset({
    "cost",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
})


class LiveContractError(RuntimeError):
    """Raised when a live contract case cannot be verified safely."""


def _git_head(repository: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise LiveContractError("could not read a source commit") from exc
    return result.stdout.strip()


def _native_secret_scope(secret_home: Path) -> tuple[Any, str]:
    """Hydrate the native Hermes profile scope without writing or printing a key."""
    try:
        from agent.secret_scope import (
            build_profile_secret_scope,
            reset_secret_scope,
            set_multiplex_active,
            set_secret_scope,
        )
    except Exception as exc:
        raise LiveContractError("Hermes native secret-scope helpers are unavailable") from exc
    scope = build_profile_secret_scope(secret_home)
    environment_value = os.environ.get("OPENROUTER_API_KEY", "")
    if environment_value:
        scope["OPENROUTER_API_KEY"] = environment_value
    secret = str(scope.get("OPENROUTER_API_KEY") or "")
    if not secret:
        raise LiveContractError(
            "OPENROUTER_API_KEY is absent from the protected environment and native profile scope"
        )
    set_multiplex_active(True)
    token = set_secret_scope(scope)
    return (reset_secret_scope, set_multiplex_active, token), secret


def _load_registered_tools(plugin_root: Path) -> tuple[Any, Any, Any, dict[str, Any]]:
    """Load the plugin with Hermes' real manager and return live registry entries."""
    try:
        from hermes_cli.plugins import PluginManager
        from tools.registry import registry
    except Exception as exc:
        raise LiveContractError("Hermes native plugin loader is unavailable") from exc
    plugin_root = Path(plugin_root).resolve()
    manager = PluginManager()
    with tempfile.TemporaryDirectory(prefix="switchyard-live-plugin-") as scratch:
        staged_root = Path(scratch) / plugin_root.name
        shutil.copytree(
            plugin_root,
            staged_root,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"),
        )
        manifests = manager._scan_directory(Path(scratch), source="project")
        if len(manifests) != 1:
            raise LiveContractError("Hermes discovery did not find exactly one candidate plugin")
        manifest = manifests[0]
        manager._load_plugin(manifest)
        loaded = manager._plugins.get(manifest.key or manifest.name)
        if loaded is None or loaded.error or not loaded.enabled:
            raise LiveContractError("Hermes loaded no enabled candidate plugin")
        entries = {
            name: registry.get_entry(name, scope=manager.scope_key)
            for name in ("jev_skill_select", "jev_model_route", "jev_computer_use")
        }
        if any(entry is None or not callable(entry.handler) for entry in entries.values()):
            raise LiveContractError("Hermes registry did not expose every candidate tool")
        return manager, manifest, registry, entries


def _usage_receipt(usage: Any) -> dict[str, int | float]:
    if not isinstance(usage, dict):
        raise LiveContractError("Jev response usage is not an object")
    receipt: dict[str, int | float] = {}
    for key, value in usage.items():
        if key not in _USAGE_FIELDS:
            continue
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise LiveContractError("Jev response contains invalid numeric usage")
        receipt[key] = value
    if not receipt:
        raise LiveContractError("Jev response did not include a numeric usage receipt")
    return receipt


def _validated_case_result(case_id: str, raw: Any, *, source_sha: str) -> dict[str, Any]:
    if not isinstance(raw, str):
        raise LiveContractError(f"{case_id} returned a non-JSON tool result")
    try:
        result = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LiveContractError(f"{case_id} returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("status") == "error":
        raise LiveContractError(f"{case_id} returned a structured error")
    model = result.get("model")
    if model not in _ALLOWED_MODELS:
        raise LiveContractError(f"{case_id} did not report an allowed resolved model")
    latency = result.get("latency_ms")
    if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
        raise LiveContractError(f"{case_id} did not report valid latency")
    return {
        "case": case_id,
        "source_sha": source_sha,
        "status": result.get("status"),
        "selected": result.get("selected"),
        "abstention_reason": result.get("abstention_reason"),
        "model": model,
        "latency_ms": latency,
        "usage": _usage_receipt(result.get("usage")),
    }


def _run_case(case_id: str, entry: Any, arguments: dict[str, Any], *, source_sha: str) -> dict[str, Any]:
    try:
        raw = entry.handler(arguments)
    except Exception as exc:
        raise LiveContractError(f"{case_id} handler raised {type(exc).__name__}") from None
    return _validated_case_result(case_id, raw, source_sha=source_sha)


def run_live_contract(
    *,
    plugin_root: Path,
    upstream_root: Path,
    source_sha: str,
    upstream_sha: str,
    secret_home: Path,
) -> dict[str, Any]:
    actual_source_sha = _git_head(plugin_root)
    if actual_source_sha != source_sha:
        raise LiveContractError("candidate checkout does not match the requested source SHA")
    if _git_head(upstream_root) != upstream_sha:
        raise LiveContractError("Hermes checkout does not match the requested upstream SHA")
    if not os.environ.get("HERMES_HOME"):
        raise LiveContractError("HERMES_HOME must point at a temporary runtime home")

    cleanup, secret = _native_secret_scope(secret_home)
    reset_secret_scope, set_multiplex_active, token = cleanup
    try:
        _manager, manifest, _registry, entries = _load_registered_tools(plugin_root)
        if manifest.name != EXPECTED_PLUGIN:
            raise LiveContractError("loaded candidate manifest has the wrong plugin name")
        cases = [
            _run_case(
                "skill-selected",
                entries["jev_skill_select"],
                {
                    "task": "Choose the offered public JSON parsing skill for a public JSON document.",
                    "candidates": [
                        {
                            "name": "public-json-parser",
                            "description": "Parse a public JSON document without private data.",
                        }
                    ],
                    "choice_confidence_threshold": 0.50,
                    "needs_skill_threshold": 0.50,
                    "winning_probability_threshold": 0.50,
                    "public_or_sanitized_data_ack": True,
                },
                source_sha=source_sha,
            ),
            _run_case(
                "model-selected",
                entries["jev_model_route"],
                {
                    "task": "Select a model for a public browser lookup.",
                    "requirements": {
                        "data_classes": ["public"],
                        "tool_capabilities": ["browser"],
                        "context_limit": 4096,
                        "budget": 1.00,
                    },
                    "candidates": [
                        {
                            "id": "public-browser-model",
                            "description": "Approved public-data browser model.",
                            "approved": True,
                            "data_classes_allowed": ["public"],
                            "tool_capabilities": ["browser"],
                            "context_limit": 8192,
                            "cost": 0.10,
                        }
                    ],
                    "capability_fit_threshold": 0.50,
                    "public_or_sanitized_data_ack": True,
                },
                source_sha=source_sha,
            ),
            _run_case(
                "skill-abstained",
                entries["jev_skill_select"],
                {
                    "task": "Say hello in plain text; no specialized skill is needed.",
                    "candidates": [
                        {
                            "name": "specialized-private-workflow",
                            "description": "A specialized workflow that is not needed for a plain public greeting.",
                        }
                    ],
                    "choice_confidence_threshold": 0.99,
                    "needs_skill_threshold": 0.99,
                    "winning_probability_threshold": 0.99,
                    "public_or_sanitized_data_ack": True,
                },
                source_sha=source_sha,
            ),
        ]
        if cases[0]["status"] != "selected" or cases[0]["selected"] != "public-json-parser":
            raise LiveContractError("skill-selected case did not select its fixed public candidate")
        if cases[1]["status"] != "selected" or cases[1]["selected"] != "public-browser-model":
            raise LiveContractError("model-selected case did not select its fixed public candidate")
        if cases[2]["status"] != "abstained" or not cases[2]["abstention_reason"]:
            raise LiveContractError("skill-abstained case did not produce an explicit abstention")
        report: dict[str, Any] = {
            "ok": True,
            "plugin": manifest.name,
            "plugin_version": manifest.version,
            "source_sha": source_sha,
            "hermes_source_sha": upstream_sha,
            "hermes_version": importlib.metadata.version("hermes-agent"),
            "cases": cases,
        }
        serialized = json.dumps(report, sort_keys=True)
        if secret in serialized:
            raise LiveContractError("live receipt would contain credential material")
        return report
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--secret-home", type=Path, default=None)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    report: dict[str, Any]
    try:
        report = run_live_contract(
            plugin_root=args.plugin_root,
            upstream_root=args.upstream_root,
            source_sha=args.source_sha,
            upstream_sha=args.upstream_sha,
            secret_home=args.secret_home or Path(os.environ["HERMES_HOME"]),
        )
    except (LiveContractError, KeyError) as exc:
        report = {"ok": False, "error": str(exc)}
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
        exit_code = 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
