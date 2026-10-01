"""Live native approval probe in a disposable profile; never executes commands.

Run through Hermes' --print-runtime-command launcher. Credentials are hydrated
from the explicitly selected HERMES_HOME template; only sanitized summaries leave
this process. Output locations must be outside the source tree.
"""

from __future__ import annotations

import argparse
import importlib
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--arm",
        choices=["baseline", "candidate", "features", "recall"],
        default="candidate",
    )
    parser.add_argument(
        "--corpus",
        choices=["approval_cases", "approval_holdout"],
        default="approval_cases",
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--home", dest="run_home", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    fixture_path = args.source / "tests/fixtures" / (args.corpus + ".json")
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope

    template = Path(os.environ["HERMES_HOME"])
    hydrate_profile_secret_sources(template)
    set_secret_scope(build_profile_secret_scope(template))
    home = args.run_home
    home.mkdir(parents=True, exist_ok=False)
    dest = home / "plugins/hermes-switchyard"
    dest.mkdir(parents=True)
    for name in ("__init__.py", "plugin.yaml"):
        shutil.copy2(args.source / name, dest / name)
    shutil.copytree(
        args.source / "hermes_switchyard",
        dest / "hermes_switchyard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (home / "empty-bundled").mkdir()
    import hermes_yaml as yaml

    config = {
        "model": {"provider": "openai-codex", "default": "gpt-6-sol"},
        "auxiliary": {
            "approval": {
                "provider": "switchyard-approvals",
                "model": "typesafe/jev-1.13-20260917",
                "timeout": 2,
            }
        },
        "approvals": {"mode": "smart"},
        "terminal": {"backend": "local", "cwd": str(home / "workspace")},
        "plugins": {
            "enabled": ["hermes-switchyard"],
            "entries": {
                "hermes-switchyard": {
                    "settings": {
                        "jev_provider": "openrouter",
                        "jev_model": "typesafe/jev-1.13-20260917",
                        "smart_approval_provider": True,
                        "public_or_sanitized_data_ack": True,
                        "automatic_skill_recommendation": False,
                        "adaptive_reasoning_effort": False,
                        "consequential_tool_gate": True,
                        "repeated_output_compaction": True,
                        "cross_tool_stuck_detection": True,
                    }
                }
            },
        },
    }
    (home / "config.yaml").write_text(yaml.safe_dump(config))
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_BUNDLED_PLUGINS"] = str(home / "empty-bundled")
    from hermes_constants import set_hermes_home_override

    set_hermes_home_override(str(home))
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    callbacks = list(manager.iter_hook_callbacks("pre_tool_call"))
    module = next(c.__module__ for c in callbacks if "approval_review" in c.__module__)
    approval = importlib.import_module(module)
    assert Path(approval.__file__).is_relative_to(dest)

    def provenance():
        names = [
            "agent.auxiliary_client",
            "tools.approval_smart",
            "hermes_cli.plugins",
            "model_tools",
        ]
        return {
            "candidate_files": {
                str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(dest.rglob("*"))
                if p.is_file() and "__pycache__" not in p.parts
            },
            "native_module_hashes": {
                name: hashlib.sha256(
                    Path(importlib.import_module(name).__file__).read_bytes()
                ).hexdigest()
                for name in names
            },
            "fixture_sha256": hashlib.sha256(fixture_path.read_bytes()).hexdigest(),
        }

    source_before = provenance()
    cls = approval.DecisionClient
    original = cls._post
    calls = []

    def post(self, payload):
        assert self.model == "typesafe/jev-1.13-20260917"
        t = time.perf_counter()
        row = {"question_keys": sorted(payload["questions"])}
        try:
            response = original(self, payload)
            row.update(answers=response.get("answers"), usage=response.get("usage"))
            return response
        except Exception as exc:
            row["error_type"] = type(exc).__name__
            raise
        finally:
            row["wall_ms"] = round((time.perf_counter() - t) * 1000, 1)
            calls.append(row)

    cls._post = post
    if args.arm == "recall":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from native_recall import run

        output = run(importlib.import_module(module.rsplit(".", 1)[0]), template)
        output["provenance"] = source_before
        output["unchanged"] = source_before == provenance()
        args.output.write_text(json.dumps(output, indent=2))
        print("RECALL " + json.dumps(output["rows"]), flush=True)
        return
    if args.arm == "features":
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from native_features import run

        output = run(importlib.import_module(module.rsplit(".", 1)[0]), home)
        output["jev_calls"] = calls
        output["provenance"] = source_before
        output["unchanged"] = source_before == provenance()
        args.output.write_text(json.dumps(output, indent=2))
        print(
            "FEATURES "
            + json.dumps(
                {
                    k: v
                    for k, v in output.items()
                    if k not in {"jev_calls", "provenance"}
                }
            ),
            flush=True,
        )
        return
    native_calls = []
    if args.arm == "baseline":
        # Exercise the selected real profile's native reviewer; this is a
        # read-only text review and never executes any fixture command.
        os.environ["HERMES_HOME"] = str(template)
        set_hermes_home_override(str(template))
        from agent import auxiliary_client

        native_call = auxiliary_client.call_llm

        def observed_call(*args, **kwargs):
            route = {}
            row = {"route": route}
            native_calls.append(row)  # Count attempts, including provider failures.
            try:
                response = native_call(*args, route_info=route, **kwargs)
                row["model"] = response.model
                return response
            except Exception as exc:
                row["error_type"] = type(exc).__name__
                raise

        auxiliary_client.call_llm = observed_call
    from tools.approval_smart import _smart_approve

    cases = json.loads(fixture_path.read_text())
    rows = []
    for fixture in cases:
        name, command, expected = fixture[:3]
        from tools import approval_smart

        policy = fixture[3] if len(fixture) > 3 else ""
        approval_smart._get_smart_policy = lambda: policy
        observed_calls = native_calls if args.arm == "baseline" else calls
        before = len(observed_calls)
        t = time.perf_counter()
        verdict = _smart_approve(command, "synthetic qualification fixture")
        row = {
            "id": name,
            "expected": expected,
            "verdict": verdict,
            "requests": len(observed_calls) - before,
            "request_counter": "native_auxiliary_attempts"
            if args.arm == "baseline"
            else "jev_transport_attempts",
            "wall_ms": round((time.perf_counter() - t) * 1000, 1),
        }
        rows.append(row)
        print("ROW " + json.dumps(row), flush=True)
    output = {
        "arm": args.arm,
        "provenance": source_before,
        "unchanged": source_before == provenance(),
        "cases": rows,
        "jev_calls": calls,
        "native_calls": native_calls,
        "native_hook_modules": [
            c.__module__ for c in manager.iter_hook_callbacks("transform_tool_result")
        ],
        "approval_module_loaded_from_candidate": True,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2))
    print("DONE " + str(args.output), flush=True)


if __name__ == "__main__":
    main()
