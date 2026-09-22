#!/usr/bin/env python3
"""Actually call every registered tool through Hermes' real dispatch path.

``check_native_hermes.py`` proves each declared tool is registered with a
closed, well-formed schema. It never calls ``entry.handler(...)``, so a PR
that breaks a handler at runtime (a bad import, a signature mismatch, a
regression in argument handling) can still pass every offline CI job. The
only place in this repository that actually invokes a handler is
``live_jev_contract.py``, which is ``workflow_dispatch``-only, requires a
paid OpenRouter credential, and never runs on a pull request.

This script closes that gap for free: it loads the plugin through Hermes'
real ``PluginManager`` and ``tools.registry`` (the same objects
``check_native_hermes.py`` uses), then calls each Jev-backed tool's real
registered handler with a synthetic HTTPS transport standing in for the Jev
provider. No network call, no credential, no cost -- but the exact code path
a live turn would use (schema -> handler closure -> ``DecisionClient`` ->
``http.client.HTTPSConnection`` -> response validation -> JSON envelope) runs
end to end.

It does not replace ``live_jev_contract.py``: a synthetic transport proves
the plugin's own code is wired correctly, not that a live Jev response would
satisfy it. It only proves "this PR did not break the wiring between a
registered tool and a working call", which is exactly the class of bug none
of the other offline jobs catch.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class NativeInvocationError(RuntimeError):
    """Raised when a registered tool cannot be invoked end to end."""


class _SyntheticResponse:
    """A minimal ``http.client.HTTPResponse``-shaped stand-in."""

    def __init__(self, body: bytes):
        self.body = body
        self.status = 200
        self.reason = "synthetic"
        self.headers = {}
        self.will_close = False

    def read(self, size: int | None = None) -> bytes:
        return self.body if size is None else self.body[:size]

    def close(self) -> None:
        pass


class _SyntheticConnection:
    """Records the outbound request and returns one synthetic Jev response."""

    def __init__(self):
        self.timeout = None
        self.requests: list[dict[str, Any]] = []

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> None:
        payload = json.loads(body) if body else {}
        questions = payload.get("questions", {})
        answers = {}
        for name, question in questions.items():
            question_type = question.get("type")
            criteria = question.get("criteria")
            if question_type == "choice":
                choice = next(iter(criteria)) if criteria else "none"
                others = [key for key in criteria if key != choice]
                # Probabilities must sum to ~1.0 (DecisionClient._validate_choice
                # enforces this); a single-candidate ballot has no "others" to
                # absorb a held-back remainder, so the choice must take all of it.
                if others:
                    probabilities = {choice: 0.9}
                    probabilities.update({key: 0.1 / len(others) for key in others})
                else:
                    probabilities = {choice: 1.0}
                answers[name] = {"choice": choice, "probabilities": probabilities, "confidence": 0.9}
            elif question_type == "score":
                length = len(criteria) if isinstance(criteria, list) else 2
                legend = {str(index): criteria[index] for index in range(length)}
                probabilities = {str(index): (1.0 if index == 0 else 0.0) for index in range(length)}
                answers[name] = {
                    "score": 0.0,
                    "legend": legend,
                    "probabilities": probabilities,
                    "confidence": 0.9,
                }
            else:
                # 0.95 clears every threshold this repo's tools apply to a noul
                # answer (needs_skill, assess fit, etc.), so a Jev-backed
                # selection tool actually selects instead of abstaining on a
                # low synthetic score -- abstention is valid plugin behavior,
                # but it must not be the reason this check stays green.
                answers[name] = {"noul": 0.95}
        response_body = json.dumps(
            {
                "model": payload.get("model", "typesafe/jev-1.13"),
                "answers": answers,
                "usage": {"cost": 0.0001},
                "latency_ms": 1.0,
            }
        ).encode("utf-8")
        self.requests.append({"path": path, "payload": payload})
        self._response = _SyntheticResponse(response_body)

    def getresponse(self) -> _SyntheticResponse:
        return self._response

    def close(self) -> None:
        pass


# One representative, publicly safe invocation per Jev-backed tool. Each case
# must exercise the tool's real handler closure exactly as a live turn would:
# same schema, same client construction, same response validation. Cases are
# deliberately minimal -- they check "did this come back as a valid typed
# result", not selection quality (that is the live contract's job).
_CASES: tuple[dict[str, Any], ...] = (
    {
        "tool": "jev_skill_select",
        "arguments": {
            "task": "Choose the offered public JSON parsing skill.",
            "candidates": [{"name": "public-json-parser", "description": "Parse a public JSON document."}],
            "public_or_sanitized_data_ack": True,
        },
    },
    {
        "tool": "jev_skill_select_many",
        "arguments": {
            "task": "Choose zero or more offered public skills.",
            "candidates": [{"name": "public-json-parser", "description": "Parse a public JSON document."}],
            "public_or_sanitized_data_ack": True,
        },
    },
    {
        "tool": "jev_model_route",
        "arguments": {
            "task": "Select a model for a public browser lookup.",
            "requirements": {"data_classes": ["public"], "tool_capabilities": ["browser"]},
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
            "public_or_sanitized_data_ack": True,
        },
    },
    {
        "tool": "jev_assess",
        "arguments": {
            "state": "A public, non-sensitive state description.",
            "questions": {
                "fit": {"type": "noul", "instructions": "Score public fit from 0 to 1."},
            },
            "public_or_sanitized_data_ack": True,
        },
    },
)


def _load_registered_tools(plugin_root: Path) -> tuple[Any, dict[str, Any]]:
    """Load the plugin with Hermes' real manager and return live registry entries."""
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    plugin_root = Path(plugin_root).resolve()
    manager = PluginManager()
    with tempfile.TemporaryDirectory(prefix="switchyard-invocation-") as scratch:
        staged_root = Path(scratch) / plugin_root.name
        shutil.copytree(
            plugin_root,
            staged_root,
            ignore=shutil.ignore_patterns(".git", "__pycache__", ".pytest_cache", "*.pyc"),
        )
        manifests = manager._scan_directory(Path(scratch), source="project")
        if len(manifests) != 1:
            raise NativeInvocationError("Hermes discovery did not find exactly one candidate plugin")
        manifest = manifests[0]
        manager._load_plugin(manifest)
        loaded = manager._plugins.get(manifest.key or manifest.name)
        if loaded is None or loaded.error or not loaded.enabled:
            raise NativeInvocationError("Hermes loaded no enabled candidate plugin")
        names = {case["tool"] for case in _CASES}
        entries = {name: registry.get_entry(name, scope=manager.scope_key) for name in names}
        missing = [name for name, entry in entries.items() if entry is None or not callable(entry.handler)]
        if missing:
            raise NativeInvocationError(f"Hermes registry did not expose: {sorted(missing)}")
        return manager, entries


def run_invocation_checks(plugin_root: Path) -> dict[str, Any]:
    """Call each registered tool's real handler with a synthetic Jev transport."""
    _manager, entries = _load_registered_tools(plugin_root)
    results: list[dict[str, Any]] = []
    # Placeholder secret is never a real credential; the transport patch below
    # means no network call is ever attempted with it.
    with mock.patch.dict(
        "os.environ",
        {"OPENROUTER_API_KEY": "offline-only-synthetic-placeholder", "TYPESAFE_API_KEY": ""},
    ), mock.patch(
        "hermes_switchyard.client.http.client.HTTPSConnection",
        return_value=_SyntheticConnection(),
    ):
        for case in _CASES:
            entry = entries[case["tool"]]
            try:
                raw = entry.handler(dict(case["arguments"]))
            except Exception as exc:  # noqa: BLE001 -- surfaced as a case failure, not a crash
                raise NativeInvocationError(
                    f"{case['tool']} handler raised {type(exc).__name__} on a valid call"
                ) from None
            if not isinstance(raw, str):
                raise NativeInvocationError(f"{case['tool']} did not return a JSON string")
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise NativeInvocationError(f"{case['tool']} returned invalid JSON") from exc
            if not isinstance(parsed, dict):
                raise NativeInvocationError(f"{case['tool']} returned a non-object result")
            if parsed.get("status") == "error":
                raise NativeInvocationError(
                    f"{case['tool']} returned a structured error against a synthetic but valid transport: "
                    f"{parsed.get('error')}"
                )
            results.append({"tool": case["tool"], "status": parsed.get("status", "ok")})
    return {"ok": True, "cases": results}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = run_invocation_checks(args.plugin_root)
    except NativeInvocationError as exc:
        report = {"ok": False, "error": str(exc)}
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
        exit_code = 0
    if args.report is not None:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
