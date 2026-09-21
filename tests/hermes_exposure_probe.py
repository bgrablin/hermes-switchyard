"""Probe run inside a Hermes-capable interpreter by tests/test_toolset_exposure.py.

The parent test starts this script with a disposable Hermes home. It reads one scenario file,
runs Hermes' real command-line entry point (which discovers and loads the plugin through the
real loader), then asks Hermes' own catalog builder what a session with given toolsets would
expose, and writes both answers to the result file. Network connects are refused, and nothing
is written inside the Hermes source tree.
"""
from __future__ import annotations

import contextlib
import io
import json
import socket
import sys
from pathlib import Path


def _deny_network(*_args, **_kwargs):
    raise OSError("network access is disabled in this probe")


def _catalog(model_tools, enabled_toolsets):
    """Return the tool names Hermes puts in the un-deferred session catalog."""
    definitions = model_tools.get_tool_definitions(
        enabled_toolsets=enabled_toolsets,
        disabled_toolsets=None,
        quiet_mode=True,
        skip_tool_search_assembly=True,
    )
    return sorted(item["function"]["name"] for item in definitions)


def _shadow(registry, name, toolset):
    """Register a tool that already owns a name before the plugin loads."""
    registry.register(
        name=name,
        toolset=toolset,
        schema={"name": name, "description": "stand-in", "parameters": {"type": "object", "properties": {}}},
        handler=lambda *_args, **_kwargs: "{}",
    )


def main() -> int:
    scenario = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    socket.create_connection = _deny_network
    socket.socket.connect = _deny_network
    socket.socket.connect_ex = _deny_network

    from tools.registry import registry

    # Importing model_tools discovers plugins, so any registration that must exist before the
    # plugin loads has to be made first.
    for shadow in scenario.get("shadow_registrations", []):
        _shadow(registry, shadow["name"], shadow["toolset"])

    import model_tools

    if scenario.get("credential_present"):
        import agent.secret_scope as secret_scope

        secret_scope.get_secret = lambda name, *_args, **_kwargs: (
            "offline-only-placeholder" if name == "TYPESAFE_API_KEY" else ""
        )

    from hermes_cli.main import main as hermes_main

    sys.argv = ["hermes", *scenario["argv"]]
    captured = io.StringIO()
    exit_code = 0
    with contextlib.redirect_stdout(captured):
        try:
            hermes_main()
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)

    catalogs = {}
    for label, toolsets in scenario.get("catalog_selections", {}).items():
        catalogs[label] = {"toolsets": list(toolsets), "tools": _catalog(model_tools, toolsets)}
    if scenario.get("catalog_default"):
        from hermes_cli.config import load_config
        from hermes_cli.tools_config import _get_platform_tools

        default = sorted(_get_platform_tools(load_config(), "cli"))
        catalogs["default"] = {"toolsets": default, "tools": _catalog(model_tools, default)}

    entries = {}
    for name in scenario.get("tool_names", []):
        entry = registry.get_entry(name)
        entries[name] = None if entry is None else {"toolset": entry.toolset}

    result = {
        "exit_code": exit_code,
        "stdout": captured.getvalue(),
        "catalogs": catalogs,
        "registry": entries,
    }
    Path(scenario["result_path"]).write_text(json.dumps(result), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
