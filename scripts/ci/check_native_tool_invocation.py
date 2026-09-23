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
        state = payload.get("state")
        questions = payload.get("questions", {})
        answers = {}
        for name, question in questions.items():
            question_type = question.get("type")
            criteria = question.get("criteria")
            if question_type == "choice":
                preferred = (
                    "DONE"
                    if name == "operation"
                    and isinstance(criteria, dict)
                    and "DONE" in criteria
                    and isinstance(state, dict)
                    and state.get("action_count", 0) >= 1
                    else None
                )
                choice = preferred if preferred in criteria else (next(iter(criteria)) if criteria else "none")
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


class _SyntheticNativeDispatcher:
    """Protocol-shaped stand-in for Hermes computer_use; never touches a GUI."""

    def __init__(self) -> None:
        self.actions: list[dict[str, Any]] = []
        self.captures = 0

    def __call__(self, tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> str:
        if tool_name != "computer_use":
            raise AssertionError(f"unexpected native dispatch target: {tool_name}")
        if arguments.get("action") == "capture":
            self.captures += 1
            return json.dumps({
                "app": "Synthetic demo",
                "window_title": "Public fixture",
                "elements": [{"index": 1, "role": "Button", "label": "Continue"}],
            })
        self.actions.append(dict(arguments))
        return json.dumps({
            "ok": True,
            "action": arguments.get("action"),
            "effect": {"confirmed": True, "status": "applied"},
            "verdict": "confirmed",
            "escalation": None,
        })


# One publicly safe invocation for every registered tool. Each case must
# exercise the real registered handler; each result is checked against a
# successful, meaningful terminal contract, not just a non-error JSON envelope.
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
    {
        "tool": "jev_model_route_approved",
        "arguments": {
            "task": "Select a model for a public browser lookup.",
            "requirements": {"data_classes": ["public"], "tool_capabilities": ["browser"]},
            "public_or_sanitized_data_ack": True,
        },
    },
    {
        "tool": "jev_session_search_rerank",
        "arguments": {
            "query": "Which public session discussed JSON parsing?",
            "candidates": [
                {"session_id": "synthetic-session-a", "title": "Public JSON parsing", "snippet": "Discussed parsing a public JSON document."},
                {"session_id": "synthetic-session-b", "title": "Public routing", "snippet": "Discussed routing for a public browser request."},
            ],
            "public_or_sanitized_data_ack": True,
        },
    },
    {
        "tool": "jev_computer_use",
        "arguments": {
            "goal": "Click the harmless synthetic Continue control, then finish the fixture.",
            "app": "Synthetic demo",
            "public_or_sanitized_data_ack": True,
            "max_steps": 2,
            "min_actions_before_done": 1,
        },
    },
)


def _load_registered_tools(plugin_root: Path) -> tuple[Any, dict[str, Any], set[str]]:
    """Load the plugin with Hermes' real manager and return live registry entries."""
    from hermes_cli.plugins import PluginManager
    from tools.registry import registry

    plugin_root = Path(plugin_root).resolve()
    manager = PluginManager()
    synthetic_settings = {
        "approved_model_registry": [{
            "id": "synthetic-approved-browser-model",
            "provider": "synthetic-provider",
            "model": "synthetic-public-model",
            "account": "synthetic-test-account",
            "approved": True,
            "description": "Public-data browser model for the offline invocation fixture.",
            "data_classes_allowed": ["public"],
            "tool_capabilities": ["browser"],
            "context_limit": 8192,
            "cost": 0.0,
        }],
        "approved_model_registry_version": "offline-fixture-v1",
        "approved_model_registry_valid_until": "2099-12-31T23:59:59Z",
    }
    import hermes_cli.plugins as hermes_plugins

    def fixture_config() -> dict[str, Any]:
        return {
            "plugins": {
                "entries": {
                    "hermes-switchyard": {
                        "enabled": True,
                        "settings": synthetic_settings,
                    }
                }
            }
        }

    with mock.patch.object(hermes_plugins, "load_config_readonly", side_effect=fixture_config), tempfile.TemporaryDirectory(
        prefix="switchyard-invocation-"
    ) as scratch:
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
        manifest_tool_names = {str(name) for name in manifest.provides_tools}
        manager._load_plugin(manifest)
        loaded = manager._plugins.get(manifest.key or manifest.name)
        if loaded is None or loaded.error or not loaded.enabled:
            raise NativeInvocationError("Hermes loaded no enabled candidate plugin")
        registered_tool_names = set(manager._plugin_tool_names)
        registry_tool_names = {
            name for name in registered_tool_names
            if (entry := registry.get_entry(name, scope=manager.scope_key)) is not None
            and callable(entry.handler)
        }
        case_names_list = [case["tool"] for case in _CASES]
        case_names = set(case_names_list)
        if len(case_names) != len(case_names_list):
            raise NativeInvocationError("invocation case table contains duplicate tool cases")
        _validate_case_coverage(manifest_tool_names, registered_tool_names, registry_tool_names, case_names)
        entries = {name: registry.get_entry(name, scope=manager.scope_key) for name in registered_tool_names}
        return manager, entries, registered_tool_names


def _validate_case_coverage(
    manifest_names: set[str],
    registered_names: set[str],
    registry_names: set[str],
    case_names: set[str],
) -> None:
    if registered_names != manifest_names:
        raise NativeInvocationError(
            "Hermes loaded manifest/registration tool set differs: "
            f"manifest={sorted(manifest_names)}, registered={sorted(registered_names)}"
        )
    if registry_names != registered_names:
        raise NativeInvocationError(
            "Hermes registration/registry tool set differs: "
            f"registered={sorted(registered_names)}, registry={sorted(registry_names)}"
        )
    if case_names != registry_names:
        raise NativeInvocationError(
            "invocation cases do not cover loaded manifest/registry tools: "
            f"missing={sorted(registry_names - case_names)}, extra={sorted(case_names - registry_names)}"
        )


def _validate_registered_entries(entries: dict[str, Any], expected_names: set[str]) -> None:
    missing = sorted(name for name in expected_names if name not in entries or not callable(entries[name].handler))
    if missing:
        raise NativeInvocationError(f"Hermes registry did not expose: {missing}")


def _validate_success(tool: str, parsed: dict[str, Any]) -> str:
    if parsed.get("status") == "error":
        reason = f"{parsed.get('error')}"
        if tool == "jev_computer_use":
            raise NativeInvocationError(
                "jev_computer_use did not complete with the synthetic native-action executor "
                f"(handler result: {reason})"
            )
        raise NativeInvocationError(f"{tool} returned a structured error against synthetic input: {reason}")
    status = parsed.get("status")
    if tool in {"jev_skill_select", "jev_skill_select_many", "jev_model_route", "jev_session_search_rerank"}:
        if status != "selected":
            raise NativeInvocationError(f"{tool} did not reach selected terminal state (status={status!r})")
        selected = parsed.get("selected") or parsed.get("selected_session_id")
        if tool == "jev_skill_select_many":
            valid = isinstance(selected, list) and len(selected) > 0
        else:
            valid = isinstance(selected, str) and bool(selected)
        if not valid:
            raise NativeInvocationError(f"{tool} reported selected without a selection")
        return str(status)
    if tool == "jev_assess":
        answers = parsed.get("answers")
        score = answers["fit"].get("noul") if isinstance(answers, dict) and isinstance(answers.get("fit"), dict) else None
        if type(score) not in (int, float) or not 0 <= score <= 1:
            raise NativeInvocationError("jev_assess returned no valid typed fit score")
        return "assessed"
    if tool == "jev_model_route_approved":
        selected = parsed.get("selected")
        if status != "selected" or not isinstance(selected, str) or not selected:
            raise NativeInvocationError(
                "jev_model_route_approved cannot reach a successful state without a valid, "
                "operator-owned approved-model registry fixture"
            )
        return str(status)
    if tool == "jev_computer_use":
        actions = parsed.get("actions")
        decisions = parsed.get("decisions")
        if (
            status != "completion_candidate"
            or parsed.get("verified") is not False
            or not isinstance(actions, list)
            or not actions
            or parsed.get("completed_action_count") != len(actions)
            or any(not isinstance(action, dict) or action.get("effect_confirmed") is not True for action in actions)
            or not isinstance(decisions, list)
            or not decisions
        ):
            raise NativeInvocationError(
                "jev_computer_use requires a completion_candidate with a confirmed synthetic "
                "action and decision evidence"
            )
        return "completion_candidate_with_confirmed_action"
    raise NativeInvocationError(f"no success contract is defined for registered tool {tool!r}")


def run_invocation_checks(plugin_root: Path) -> dict[str, Any]:
    """Call each registered tool's real handler with a synthetic Jev transport."""
    from tools.registry import registry

    _manager, entries, expected_names = _load_registered_tools(plugin_root)
    _validate_registered_entries(entries, expected_names)
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    native_dispatcher = _SyntheticNativeDispatcher()
    def dispatch_synthetic_native(tool_name: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
        return native_dispatcher(tool_name, arguments, **kwargs)
    with mock.patch.dict(
        "os.environ",
        {"OPENROUTER_API_KEY": "offline-only-synthetic-placeholder", "TYPESAFE_API_KEY": ""},
    ), mock.patch(
        "hermes_switchyard.client.http.client.HTTPSConnection",
        return_value=_SyntheticConnection(),
    ), mock.patch.object(registry, "dispatch", side_effect=dispatch_synthetic_native):
        for case in _CASES:
            entry = entries[case["tool"]]
            try:
                raw = entry.handler(dict(case["arguments"]))
            except Exception as exc:  # noqa: BLE001 -- surfaced as a case failure, not a crash
                failures.append(f"{case['tool']} handler raised {type(exc).__name__} on a valid call")
                continue
            if not isinstance(raw, str):
                failures.append(f"{case['tool']} did not return a JSON string")
                continue
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                failures.append(f"{case['tool']} returned invalid JSON")
                continue
            if not isinstance(parsed, dict):
                failures.append(f"{case['tool']} returned a non-object result")
                continue
            try:
                terminal = _validate_success(case["tool"], parsed)
            except NativeInvocationError as exc:
                failures.append(str(exc))
                continue
            if case["tool"] == "jev_computer_use" and (
                native_dispatcher.actions != [{"action": "click", "element": 1}]
                or native_dispatcher.captures != 3
            ):
                failures.append(
                    "jev_computer_use synthetic dispatcher did not observe exactly one harmless click "
                    "and the required initial/fresh/post-action captures"
                )
                continue
            results.append({"tool": case["tool"], "status": terminal})
    if failures:
        passed = ", ".join(f"{item['tool']}={item['status']}" for item in results) or "none"
        raise NativeInvocationError(f"{'; '.join(failures)}; successful terminal cases: {passed}")
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
