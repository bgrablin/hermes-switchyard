#!/usr/bin/env python3
"""Run the Switchyard native compatibility contract against pinned Hermes."""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

EXPECTED_HERMES_PYTHON = ">=3.11,<3.14"
EXPECTED_PLUGIN = "hermes-switchyard"
EXPECTED_MANIFEST_VERSION = 1
EXPECTED_TOOLS = frozenset({
    "jev_assess",
    "jev_computer_use",
    "jev_model_route",
    "jev_skill_select",
    "jev_skill_select_many",
})
EXPECTED_REQUIRED_FIELDS = {
    "jev_assess": {"state", "questions"},
    "jev_computer_use": {"goal", "app"},
    "jev_model_route": {"task", "candidates"},
    "jev_skill_select": {"task", "candidates"},
    "jev_skill_select_many": {"task", "candidates"},
}


class NativeCompatibilityError(RuntimeError):
    """Raised when the pinned Hermes contract is not actually satisfied."""


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
        raise NativeCompatibilityError("could not read the pinned Hermes commit") from exc
    head = result.stdout.strip()
    if len(head) != 40 or any(character not in "0123456789abcdef" for character in head):
        raise NativeCompatibilityError("pinned Hermes checkout did not report an exact commit SHA")
    return head


def _supported_python_range(upstream_root: Path) -> str:
    pyproject = upstream_root / "pyproject.toml"
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        value = str(data["project"]["requires-python"])
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise NativeCompatibilityError("pinned Hermes pyproject could not be read") from exc
    if value != EXPECTED_HERMES_PYTHON:
        raise NativeCompatibilityError(
            f"pinned Hermes requires-python changed from {EXPECTED_HERMES_PYTHON!r}"
        )
    return value


def _schema_summary(name: str, schema: Any) -> dict[str, Any]:
    if not isinstance(schema, dict) or set(schema) != {"name", "description", "parameters"}:
        raise NativeCompatibilityError(f"registered tool {name!r} has an unexpected schema envelope")
    if schema.get("name") != name or not isinstance(schema.get("description"), str):
        raise NativeCompatibilityError(f"registered tool {name!r} has invalid name or description")
    parameters = schema.get("parameters")
    if not isinstance(parameters, dict):
        raise NativeCompatibilityError(f"registered tool {name!r} has no parameter schema")
    if parameters.get("type") != "object" or parameters.get("additionalProperties") is not False:
        raise NativeCompatibilityError(f"registered tool {name!r} is not a closed object schema")
    properties = parameters.get("properties")
    required = parameters.get("required")
    if not isinstance(properties, dict) or not isinstance(required, list):
        raise NativeCompatibilityError(f"registered tool {name!r} has malformed parameter fields")
    required_fields = EXPECTED_REQUIRED_FIELDS[name]
    required_set = set(required)
    if not required_fields.issubset(required_set):
        raise NativeCompatibilityError(f"registered tool {name!r} does not require its public contract fields")
    if not required_set.issubset(set(properties)):
        raise NativeCompatibilityError(f"registered tool {name!r} requires an undeclared field")
    acknowledgement = properties.get("public_or_sanitized_data_ack")
    if not isinstance(acknowledgement, dict) or acknowledgement.get("type") != "boolean":
        raise NativeCompatibilityError(f"registered tool {name!r} lacks the acknowledgement gate")
    if acknowledgement.get("default") is not False:
        raise NativeCompatibilityError(f"registered tool {name!r} does not default the acknowledgement closed")
    return {
        "tool": name,
        "parameters_type": parameters["type"],
        "required": sorted(str(field) for field in required),
        "property_names": sorted(str(field) for field in properties),
        "acknowledgement_default": acknowledgement["default"],
    }


def inspect_native_plugin(
    plugin_root: Path,
    *,
    upstream_root: Path,
    upstream_sha: str,
    source_sha: str | None = None,
) -> dict[str, Any]:
    """Use the pinned Hermes parser, loader, registry, and real tool entries."""
    plugin_root = Path(plugin_root).resolve()
    upstream_root = Path(upstream_root).resolve()
    actual_upstream_sha = _git_head(upstream_root)
    if actual_upstream_sha != upstream_sha:
        raise NativeCompatibilityError("Hermes checkout is not the requested exact upstream commit")
    python_range = _supported_python_range(upstream_root)
    if not (3, 11) <= sys.version_info[:2] < (3, 14):
        raise NativeCompatibilityError("native compatibility ran outside Hermes' supported Python range")

    try:
        from hermes_cli.plugin_dev import _doctor_runtime
        from hermes_cli.plugins_manifest import parse_manifest_file
        from tools.registry import registry
    except Exception as exc:
        raise NativeCompatibilityError("pinned Hermes native compatibility modules are unavailable") from exc

    manifest_file = plugin_root / "plugin.yaml"
    if not manifest_file.is_file():
        raise NativeCompatibilityError("candidate plugin has no root plugin.yaml")
    parsed_manifest = parse_manifest_file(manifest_file, plugin_root, source="project", prefix="")
    if parsed_manifest is None:
        raise NativeCompatibilityError("Hermes' pinned manifest parser rejected plugin.yaml")
    if parsed_manifest.name != EXPECTED_PLUGIN or parsed_manifest.manifest_version != EXPECTED_MANIFEST_VERSION:
        raise NativeCompatibilityError("Hermes' pinned manifest parser returned an unexpected plugin identity")

    try:
        with _doctor_runtime(plugin_root) as host:
            manifest = host.manifest
            if manifest.name != EXPECTED_PLUGIN or manifest.manifest_version != EXPECTED_MANIFEST_VERSION:
                raise NativeCompatibilityError("Hermes loader changed the candidate manifest identity")
            registered = set(host.registered_tools)
            declared = set(manifest.provides_tools)
            if registered != EXPECTED_TOOLS or declared != EXPECTED_TOOLS:
                raise NativeCompatibilityError("manifest declarations and real registrations do not match")
            schemas = []
            for name in sorted(EXPECTED_TOOLS):
                entry = registry.get_entry(name, scope=host.manager.scope_key)
                expected_toolset = "computer_use" if name == "jev_computer_use" else "hermes_switchyard"
                if entry is None or entry.toolset != expected_toolset or not callable(entry.handler):
                    raise NativeCompatibilityError(f"Hermes registry did not expose tool {name!r} correctly")
                schemas.append(_schema_summary(name, entry.schema))
    except NativeCompatibilityError:
        raise
    except Exception as exc:
        raise NativeCompatibilityError(f"Hermes native loader/registration failed: {type(exc).__name__}") from None

    result: dict[str, Any] = {
        "ok": True,
        "plugin": EXPECTED_PLUGIN,
        "manifest_version": EXPECTED_MANIFEST_VERSION,
        "manifest_tools": sorted(EXPECTED_TOOLS),
        "registered_tools": sorted(EXPECTED_TOOLS),
        "tool_schemas": schemas,
        "hermes_python": python_range,
        "hermes_version": importlib.metadata.version("hermes-agent"),
        "hermes_source_sha": actual_upstream_sha,
        "python": ".".join(str(part) for part in sys.version_info[:3]),
    }
    if source_sha is not None:
        actual_source_sha = _git_head(plugin_root)
        if actual_source_sha != source_sha:
            raise NativeCompatibilityError("candidate checkout is not the requested source SHA")
        result["source_sha"] = source_sha
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", type=Path, required=True)
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--source-sha", default=None)
    parser.add_argument("--report", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = inspect_native_plugin(
            args.plugin_root,
            upstream_root=args.upstream_root,
            upstream_sha=args.upstream_sha,
            source_sha=args.source_sha,
        )
    except NativeCompatibilityError as exc:
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
