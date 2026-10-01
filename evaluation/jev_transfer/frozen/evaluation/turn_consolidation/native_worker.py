"""Native Hermes comparison worker; credentials remain in the configured profile."""

import argparse
import importlib
import json
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

MODEL = "gpt-6-sol"
JEV = "typesafe/jev-1.13-20260917"


def main(args):
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope
    from hermes_cli.runtime_provider import resolve_runtime_provider

    template = Path(os.environ["HERMES_HOME"])
    hydrate_profile_secret_sources(template)
    set_secret_scope(build_profile_secret_scope(template))
    runtime = resolve_runtime_provider(requested="openai-codex", target_model=MODEL)
    assert (
        runtime["provider"] == "openai-codex"
        and runtime["api_mode"] == "codex_responses"
        and runtime.get("api_key")
    )
    home = args.home.resolve()
    home.mkdir(parents=True, exist_ok=False)
    for part in ["workspace", "skills", "plugins", "empty-bundled"]:
        (home / part).mkdir()
    shutil.copytree(args.source / "evaluation/routing_value/catalogs/c25/skills", home / "skills", dirs_exist_ok=True)
    enabled = args.arm != "off"
    if enabled:
        dest = home / "plugins/hermes-switchyard"
        dest.mkdir()
        for file in ["__init__.py", "plugin.yaml"]:
            shutil.copy2(args.source / file, dest / file)
        shutil.copytree(
            args.source / "hermes_switchyard",
            dest / "hermes_switchyard",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    cfg = {
        "model": {"provider": "openai-codex", "default": MODEL},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "terminal": {"cwd": str(home / "workspace")},
        "plugins": {
            "enabled": ["hermes-switchyard"] if enabled else [],
            "entries": {
                "hermes-switchyard": {
                    "settings": {
                        "jev_provider": "openrouter",
                        "jev_model": JEV,
                        "automatic_skill_recommendation": True,
                        "adaptive_reasoning_effort": True,
                        "adaptive_reasoning_effort_receipt_mode": "off",
                        "adaptive_reasoning_effort_receipt_line": False,
                    }
                }
            },
        },
    }
    import hermes_yaml as yaml

    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_BUNDLED_PLUGINS"] = str(home / "empty-bundled")
    from hermes_constants import set_hermes_home_override

    set_hermes_home_override(str(home))
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    callbacks = [c.__module__ for c in manager.iter_hook_callbacks("pre_llm_call")]
    module = next(
        (m.rsplit(".", 1)[0] for m in callbacks if "hermes_switchyard" in m), None
    )
    assert bool(module) == enabled, callbacks
    jev, wire = [], []
    pilot = importlib.import_module(module + ".routing_pilot") if args.arm == "candidate" else None
    if module:
        cls = importlib.import_module(module + ".client").DecisionClient
        original_post = cls._post

        def post(self, payload):
            assert self.model == JEV
            t = time.perf_counter()
            row = {"questions": list(payload["questions"])}
            try:
                result = original_post(self, payload)
                row.update(
                    {
                        k: result.get(k)
                        for k in [
                            "model",
                            "usage",
                            "request_id",
                            "answers",
                            "transport_retries",
                        ]
                    }
                )
                return result
            except Exception as exc:
                row["error_type"] = type(exc).__name__
                raise
            finally:
                row["wall_ms"] = 1000 * (time.perf_counter() - t)
                jev.append(row)

        cls._post = post
    from run_agent import AIAgent
    from hermes_state import SessionDB

    db = SessionDB(home / "state.db")
    agent = AIAgent(
        model=MODEL,
        provider=runtime["provider"],
        requested_provider="openai-codex",
        base_url=runtime.get("base_url"),
        api_key=runtime["api_key"],
        api_mode=runtime["api_mode"],
        credential_pool=None,
        fallback_model=None,
        max_iterations=6,
        quiet_mode=True,
        verbose_logging=False,
        enabled_toolsets=["skills"],
        reasoning_config={"effort": "high"},
        platform="cli",
        session_id="decision-quality-" + home.name,
        session_db=db,
        skip_context_files=True,
        load_soul_identity=False,
        skip_memory=True,
        skip_background_review=True,
        cwd=str(home / "workspace"),
        run_budget_seconds=90,
    )
    allowed = {"skills_list", "skill_view"}
    agent.tools = [t for t in agent.tools if t.get("function", {}).get("name") in allowed]
    agent.valid_tool_names = {t["function"]["name"] for t in agent.tools}
    assert "skill_view" in agent.valid_tool_names
    tool_calls = []
    original_execute = agent._execute_tool_calls
    def execute(calls, *args, **kwargs):
        for call in (calls.tool_calls or []):
            name = call.function.name
            if name not in allowed:
                raise RuntimeError("tool outside frozen allowlist")
            tool_calls.append({"name": name, "args": call.function.arguments})
        return original_execute(calls, *args, **kwargs)
    agent._execute_tool_calls = execute
    assert not getattr(agent, "_fallback_chain", None)
    original_stream = agent._run_codex_stream

    def stream(payload, **kw):
        assert agent.provider == "openai-codex" and agent.model == MODEL
        if len(wire) >= 6:
            raise RuntimeError("physical main call cap")
        wire.append(
            {
                "model": payload.get("model"),
                "effort": (payload.get("reasoning") or {}).get("effort"),
            }
        )
        return original_stream(payload, **kw)

    agent._run_codex_stream = stream
    print(
        "READY "
        + json.dumps(
            {"arm": args.arm, "provider": runtime["provider"], "model": MODEL}
        ),
        flush=True,
    )
    try:
        for line in sys.stdin:
            job = json.loads(line)
            if job.get("stop"):
                break
            jev.clear()
            wire.clear()
            tool_calls.clear()
            if pilot is not None:
                pilot.ACTIVE.receipts.clear()
            t = time.perf_counter()
            result = agent.run_conversation(
                job["prompt"], task_id=job["id"], conversation_history=[]
            )
            row = {
                "id": job["id"],
                "arm": args.arm,
                "route": list(pilot.ACTIVE.receipts) if pilot else [],
                "tool_calls": list(tool_calls),
                "wall_ms": 1000 * (time.perf_counter() - t),
                "final": result.get("final_response"),
                "completed": result.get("completed"),
                "wire": list(wire),
                "jev": list(jev),
                "usage": {
                    k: result.get(k)
                    for k in [
                        "api_calls",
                        "input_tokens",
                        "output_tokens",
                        "cache_read_tokens",
                        "reasoning_tokens",
                        "estimated_cost_usd",
                        "cost_status",
                        "cost_source",
                        "served_model",
                        "provider",
                    ]
                },
            }
            print("ROW " + json.dumps(row), flush=True)
    finally:
        agent.close()
        manager.unload()
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--arm", required=True)
    p.add_argument("--home", type=Path, required=True)
    p.add_argument("--source", type=Path, required=True)
    try:
        main(p.parse_args())
    except Exception as exc:
        print(
            "WORKER_ERROR "
            + json.dumps(
                {
                    "type": type(exc).__name__,
                    "frames": [
                        {
                            "file": Path(f.filename).name,
                            "line": f.lineno,
                            "function": f.name,
                        }
                        for f in traceback.extract_tb(exc.__traceback__)[-6:]
                    ],
                }
            ),
            flush=True,
        )
        sys.exit(2)
