"""Native Hermes worker, isolated profile, fixed provider/model, read-only file tools, no memory."""

import argparse
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).parent
MODEL = "gpt-6-sol"
JEV = "typesafe/jev-1.13-20260917"


def main(arm, name, source):
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from agent.secret_scope import build_profile_secret_scope, set_secret_scope
    from hermes_cli.runtime_provider import resolve_runtime_provider

    template = Path(os.environ.get("SWITCHYARD_EVAL_TEMPLATE", str(Path.home() / ".hermes-eval/switchyard-abc")))
    assert os.environ["HERMES_HOME"] == str(template)
    hydrate_profile_secret_sources(template)
    scope = build_profile_secret_scope(template)
    set_secret_scope(scope)
    runtime = resolve_runtime_provider(requested="openai-codex", target_model=MODEL)
    assert (
        runtime["provider"] == "openai-codex"
        and runtime["api_mode"] == "codex_responses"
        and runtime.get("api_key")
    )
    home = Path(os.environ["SWITCHYARD_EVAL_HOME"])
    if home.exists():
        raise RuntimeError("refuse reused run home")
    for part in ["workspace", "skills", "plugins", "empty-bundled"]:
        (home / part).mkdir(parents=True, exist_ok=True)
    if arm != "off":
        dest = home / "plugins/hermes-switchyard"
        src = source
        dest.mkdir()
        for file in ["__init__.py", "plugin.yaml"]:
            shutil.copy2(src / file, dest / file)
        shutil.copytree(
            src / "hermes_switchyard",
            dest / "hermes_switchyard",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
    cfg = {
        "model": {"provider": "openai-codex", "default": MODEL},
        "memory": {"memory_enabled": False, "user_profile_enabled": False},
        "terminal": {"cwd": str(home / "workspace")},
        "plugins": {
            "enabled": ["hermes-switchyard"] if arm != "off" else [],
            "entries": {
                "hermes-switchyard": {
                    "settings": {
                        "jev_provider": "openrouter",
                        "jev_model": JEV,
                        "automatic_skill_recommendation": False,
                        "adaptive_reasoning_effort": True,
                        "adaptive_reasoning_effort_receipt_mode": "off",
                        "evidence_finder_enabled": arm in {"candidate", "toggle"},
                        "evidence_finder_root": str(home / "workspace"),
                    }
                }
            },
        },
    }
    import hermes_yaml as yaml

    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_BUNDLED_PLUGINS"] = str(home / "empty-bundled")
    os.chdir(home / "workspace")
    os.environ["TERMINAL_CWD"] = str(home / "workspace")
    assert (
        subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"], capture_output=True
        ).returncode
        != 0
    )
    from hermes_constants import set_hermes_home_override

    set_hermes_home_override(str(home))
    from tools.terminal_scope import build_profile_terminal_scope, set_terminal_scope
    from agent.runtime_cwd import set_session_cwd, resolve_agent_cwd, scope_terminal_cwd

    set_terminal_scope(build_profile_terminal_scope(home))
    set_session_cwd(str(home / "workspace"))
    assert resolve_agent_cwd().resolve() == (home / "workspace").resolve()
    assert Path(scope_terminal_cwd()).resolve() == (home / "workspace").resolve()
    from hermes_cli.plugins import get_plugin_manager

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    callbacks = [c.__module__ for c in manager.iter_hook_callbacks("pre_llm_call")]
    module = next(
        (m.rsplit(".", 1)[0] for m in callbacks if "hermes_switchyard" in m), None
    )
    assert bool(module) == (arm != "off"), callbacks
    if arm == "toggle":
        importlib.import_module(module + ".source_prefetch").request_sources = (
            lambda message: None
        )
    jev = []
    wire = []
    if module:
        cls = importlib.import_module(module + ".client").DecisionClient
        original_post = cls._post

        def post(self, payload):
            assert self.model == JEV
            t = time.perf_counter()
            row = {
                "questions": len(payload["questions"]),
                "question_keys": sorted(payload["questions"]),
                "source_request": all(
                    k.startswith("p") and k[1:].isdigit() for k in payload["questions"]
                ),
            }
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
        enabled_toolsets=["file", "hermes_switchyard"] if arm != "off" else ["file"],
        reasoning_config={"effort": "high"},
        platform="cli",
        session_id="discovery-" + name,
        session_db=db,
        skip_context_files=True,
        load_soul_identity=False,
        skip_memory=True,
        skip_background_review=True,
        cwd=str(home / "workspace"),
        run_budget_seconds=90,
    )
    allowed = {"read_file", "search_files"}
    agent.tools = [tool for tool in agent.tools if tool["function"]["name"] in allowed]
    agent.valid_tool_names = {tool["function"]["name"] for tool in agent.tools}
    assert agent.valid_tool_names == allowed, agent.valid_tool_names
    assert not getattr(agent, "_fallback_chain", None)
    original_stream = agent._run_codex_stream

    def stream(payload, **kw):
        assert agent.provider == "openai-codex" and agent.model == MODEL

        def strings(value):
            if isinstance(value, str):
                yield value
            elif isinstance(value, dict):
                for item in value.values():
                    yield from strings(item)
            elif isinstance(value, list):
                for item in value:
                    yield from strings(item)

        cwd_lines = [
            line.split("Current working directory:", 1)[1].strip()
            for value in strings(payload)
            for line in value.splitlines()
            if line.startswith("Current working directory:")
        ]
        if not cwd_lines or any(
            Path(value).resolve() != (home / "workspace").resolve()
            for value in cwd_lines
        ):
            raise RuntimeError("model_workspace_context_not_isolated")
        if len(wire) >= 6:
            raise RuntimeError("physical main call cap")
        wire.append(
            {
                "model": payload.get("model"),
                "effort": (payload.get("reasoning") or {}).get("effort"),
                "workspace_context_verified": True,
            }
        )
        return original_stream(payload, **kw)

    agent._run_codex_stream = stream
    tool_rows = []
    scope_violations = []
    original_tools = agent._execute_tool_calls

    def execute(message, messages, task_id, api_call_count=0):
        for call in message.tool_calls:
            if call.function.name not in allowed:
                raise RuntimeError("unexpected tool")
            if call.function.name in {"read_file", "search_files"}:
                args = json.loads(call.function.arguments)
                value = args.get("path") or "."
                target = Path(os.path.expandvars(value)).expanduser().resolve()
                if not target.is_relative_to((home / "workspace").resolve()):
                    scope_violations.append({"tool": call.function.name, "path": value})
                    raise RuntimeError("out_of_scope_file_tool")
            if call.function.name == "tool_call":
                args = json.loads(call.function.arguments)
                if any(
                    c.get("name") != "switchyard_find" for c in args.get("calls", [])
                ):
                    raise RuntimeError("unexpected deferred tool")
        n = len(messages)
        t = time.perf_counter()
        original_tools(message, messages, task_id, api_call_count)
        tool_rows.append(
            {
                "calls": [
                    {"name": c.function.name, "arguments": c.function.arguments}
                    for c in message.tool_calls
                ],
                "results": messages[n:],
                "wall_ms": 1000 * (time.perf_counter() - t),
            }
        )

    agent._execute_tool_calls = execute
    print(
        "READY "
        + json.dumps({"arm": arm, "provider": runtime["provider"], "model": MODEL}),
        flush=True,
    )
    try:
        for line in sys.stdin:
            job = json.loads(line)
            if job.get("stop"):
                break
            jev.clear()
            wire.clear()
            tool_rows.clear()
            scope_violations.clear()
            for path in (home / "workspace").rglob("*"):
                if path.is_file():
                    path.unlink()
            for rel, content in job["files"].items():
                path = home / "workspace" / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content)
            t = time.perf_counter()
            try:
                result = agent.run_conversation(
                    job["prompt"], task_id=job["id"], conversation_history=[]
                )
            except RuntimeError as exc:
                if str(exc) != "out_of_scope_file_tool":
                    raise
                result = {"final_response": None, "completed": False}
            row = {
                "id": job["id"],
                "arm": arm,
                "wall_ms": 1000 * (time.perf_counter() - t),
                "final": result.get("final_response"),
                "completed": result.get("completed"),
                "wire": list(wire),
                "jev": list(jev),
                "tools": list(tool_rows),
                "scope_violations": list(scope_violations),
                "workspace": str(home / "workspace"),
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
            (home / (job["id"] + ".json")).write_text(json.dumps(row, indent=2))
            print("ROW " + json.dumps(row), flush=True)
    finally:
        agent.close()
        manager.unload()
        db.close()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument(
        "--arm",
        choices=["off", "release", "main", "toggle", "candidate"],
        required=True,
    )
    p.add_argument("--name", required=True)
    p.add_argument("--source", type=Path, required=True)
    a = p.parse_args()
    try:
        main(a.arm, a.name, a.source)
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
