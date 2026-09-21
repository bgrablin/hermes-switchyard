"""Paired end-to-end evaluation for automatic skill behavior.

This harness owns only evaluation/automatic_e2e. It runs isolated temporary
Hermes homes with the real plugin loader, real AIAgent, and real Jev route.
The model receives a read-only synthetic task and only the ``skills`` toolset.
Evidence is written outside the repository by ``--evidence-dir``.

Launch through the Hermes interpreter (``HERMES_PYTHON`` or the ``hermes``
CLI's interpreter). Missing registry/import prerequisites fail as
``harness_invalid`` rather than producing scored arms.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MODEL = "gpt-5.6-luna-900k"
PROVIDER = "openai-codex"

# Skill directories relative to evaluation/automatic_e2e/fixtures/skills.
_FIXTURE_SKILLS = (
    ("devops", "docker-management"),
    ("devops", "network-printer-operations"),
    ("ops", "log-triage"),
)

TASKS = (
    {
        "id": "docker",
        "prompt": (
            "This is a public synthetic evaluation task. A Docker Compose container exits during startup. "
            "Identify likely causes and list safe read-only checks. Do not run commands, browse, edit files, "
            "control a device, or change any system. Use a relevant available skill if one is appropriate, "
            "then answer with a concise explanation and finish with EVAL_SKILL=<identifier>."
        ),
        "expected_skill": "docker-management",
    },
    {
        "id": "printer",
        "prompt": (
            "This is a public synthetic evaluation task. A network printer is unreachable. Identify likely "
            "causes and list safe read-only checks. Do not run commands, browse, edit files, control a device, "
            "or change any system. Use a relevant available skill if one is appropriate, then answer with a "
            "concise explanation and finish with EVAL_SKILL=<identifier>."
        ),
        "expected_skill": "network-printer-operations",
    },
    {
        "id": "logs",
        "prompt": (
            "This is a public synthetic evaluation task. An application repeatedly logs ERROR lines during "
            "startup. Identify likely causes and list safe read-only checks. Do not run commands, browse, "
            "edit files, control a device, or change any system. Use a relevant available skill if one is "
            "appropriate, then answer with a concise explanation and finish with EVAL_SKILL=<identifier>."
        ),
        "expected_skill": "log-triage",
    },
)

_HERMES_IMPORT_MODULES = (
    "agent.secret_scope",
    "hermes_constants",
    "hermes_state",
    "hermes_cli.plugins",
    "hermes_cli.runtime_provider",
    "run_agent",
    "tools.skills_tool",
)


class HarnessInvalid(RuntimeError):
    """Harness prerequisites are broken; do not score arms."""


def _fixtures_root() -> Path:
    return Path(__file__).resolve().parent / "fixtures" / "skills"


def _source_manifest(repo: Path) -> tuple[dict[str, str], str]:
    paths = [repo / "plugin.yaml"] + sorted((repo / "hermes_switchyard").rglob("*.py"))
    manifest: dict[str, str] = {}
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(repo).as_posix()
        manifest[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    aggregate = hashlib.sha256(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return manifest, aggregate


def _copy_skill(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dst)


def _prepare_home(repo: Path, root: Path, automatic: bool) -> tuple[Path, Path]:
    home = root / "hermes-home"
    plugin = home / "plugins" / "hermes-switchyard"
    plugin.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(
        repo,
        plugin,
        ignore=shutil.ignore_patterns(".git", "tests", "evaluation", ".github", "__pycache__"),
    )
    fixtures = _fixtures_root()
    for category, name in _FIXTURE_SKILLS:
        src = fixtures / category / name
        if not (src / "SKILL.md").is_file():
            raise HarnessInvalid(
                f"evaluation fixture skill missing: {category}/{name}"
            )
        _copy_skill(src, home / "skills" / category / name)

    observer = home / "plugins" / "auto-e2e-observer"
    observer.mkdir(parents=True)
    (observer / "plugin.yaml").write_text(
        "name: auto-e2e-observer\nversion: 0.0.1\nprovides_hooks:\n  - pre_api_request\n",
        encoding="utf-8",
    )
    (observer / "__init__.py").write_text(
        '''"""Temporary read-only observer used only by the evaluation harness."""\n\nimport hashlib\nimport json\n\nrecords = []\n\ndef _text(value):\n    try:\n        return json.dumps(value, sort_keys=True, default=str)\n    except Exception:\n        return repr(value)\n\ndef _advisory_text(messages):\n    if not isinstance(messages, list):\n        return ""\n    for message in reversed(messages):\n        if isinstance(message, dict) and message.get("role") == "user":\n            text = _text(message.get("content"))\n            return text if "Advisory skill recommendation:" in text else ""\n    return ""\n\ndef on_pre_api_request(request_messages=None, model="", provider="", api_mode="", api_call_count=0, **kwargs):\n    text = _text(request_messages)\n    advisory = _advisory_text(request_messages)\n    records.append({\n        "model": model,\n        "provider": provider,\n        "api_mode": api_mode,\n        "api_call_count": api_call_count,\n        "message_count": len(request_messages) if isinstance(request_messages, list) else 0,\n        "recommendation_present": bool(advisory),\n        "docker_recommendation_present": "docker-management" in advisory,\n        "printer_recommendation_present": "network-printer-operations" in advisory,\n        "log_recommendation_present": "log-triage" in advisory,\n        "request_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),\n    })\n\ndef register(ctx):\n    ctx.register_hook("pre_api_request", on_pre_api_request)\n''',
        encoding="utf-8",
    )
    empty_bundled = root / "empty-bundled"
    empty_bundled.mkdir()
    (home / "config.yaml").write_text(
        """plugins:\n  enabled:\n    - hermes-switchyard\n    - auto-e2e-observer\n  entries:\n    hermes-switchyard:\n      settings:\n        automatic_skill_recommendation: %s\n        automatic_skill_jev: true\n        automatic_skill_public_or_sanitized_data_ack: true\n        automatic_skill_cache_seconds: 300\nagent:\n  max_turns: 8\n"""
        % ("true" if automatic else "false"),
        encoding="utf-8",
    )
    (home / "workspace").mkdir()
    return home, empty_bundled


def _tool_calls(result: dict[str, Any]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for message in result.get("messages", []) if isinstance(result, dict) else []:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function") if isinstance(call.get("function"), dict) else call
            name = str(function.get("name") or "")
            raw_args = function.get("arguments")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except (TypeError, ValueError):
                args = {"_invalid_arguments": True}
            calls.append({"name": name, "arguments": args})
    return calls


def _skill_name(call: dict[str, Any]) -> str:
    args = call.get("arguments")
    if not isinstance(args, dict):
        return ""
    for key in ("skill_name", "name", "skill", "path"):
        value = args.get(key)
        if isinstance(value, str):
            return value
    return ""


def build_skill_alias_map(skills: list[dict[str, Any]]) -> dict[str, str]:
    """Map bare and namespace-qualified identifiers to registry canonical names.

    Hermes ``skills_list`` exposes the frontmatter ``name`` as the canonical
    identifier and a directory ``category``. Agents may load the same skill via
    bare name, ``category:name``, or ``category/name``.
    """
    aliases: dict[str, str] = {}
    for skill in skills:
        if not isinstance(skill, dict):
            continue
        name = skill.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        canonical = name.strip()
        aliases[canonical] = canonical
        category = skill.get("category")
        if isinstance(category, str) and category.strip():
            cat = category.strip()
            aliases[f"{cat}:{canonical}"] = canonical
            aliases[f"{cat}/{canonical}"] = canonical
        # Plugin-qualified skills already use namespace:bare as the registry name.
        if ":" in canonical:
            _, bare = canonical.split(":", 1)
            if bare and bare not in aliases:
                aliases[bare] = canonical
    return aliases


def canonicalize_skill_identifier(identifier: str, aliases: dict[str, str]) -> str:
    """Resolve one identifier through the runtime alias map when possible.

    Unknown qualified forms (``wrong:name`` / ``wrong/name``) stay raw so they
    cannot score as the bare leaf. ``build_skill_alias_map`` already records
    every registry-reported qualified form; bare-leaf fallthrough would let a
    wrong ``skill_view`` pass.
    """
    raw = (identifier or "").strip()
    if not raw:
        return raw
    if raw in aliases:
        return aliases[raw]
    return raw


def _skill_load_metrics(
    loaded_names: list[str],
    expected: str,
    aliases: dict[str, str] | None = None,
) -> tuple[bool, list[str], list[str], str]:
    """Classify skill loads after canonicalizing through the registry aliases.

    Returns ``(correct, irrelevant_raw, loaded_canonical, expected_canonical)``.
    """
    alias_map = aliases or {}
    expected_canonical = canonicalize_skill_identifier(expected, alias_map)
    loaded_canonical = [canonicalize_skill_identifier(name, alias_map) for name in loaded_names]
    correct = expected_canonical in loaded_canonical
    irrelevant = [
        raw
        for raw, canonical in zip(loaded_names, loaded_canonical)
        if canonical != expected_canonical
    ]
    return correct, irrelevant, loaded_canonical, expected_canonical


def _forbidden_tool_calls(calls: list[dict[str, Any]]) -> list[str]:
    exact = {"terminal", "process_manage", "write_file", "patch", "computer_use", "web_search", "web_extract"}
    return [
        name
        for call in calls
        if (name := call["name"]) in exact or name.startswith("browser_")
    ]


def _hermes_imports_available() -> tuple[bool, str | None]:
    for module in _HERMES_IMPORT_MODULES:
        try:
            __import__(module)
        except Exception as exc:  # ImportError and broken native deps
            return False, f"{type(exc).__name__}: {module}: {exc}"
    return True, None


def _looks_like_python_executable(name: str) -> bool:
    base = Path(name).name
    return base == "python" or base.startswith("python3") or base.startswith("python2")


def _env_shebang_command(env_args: list[str]) -> list[str]:
    """Return the command argv portion of an ``env`` shebang."""
    if not env_args:
        return []
    if env_args[0] == "-S" and len(env_args) >= 2:
        return env_args[1].split() + list(env_args[2:])
    if env_args[0].startswith("-S") and len(env_args[0]) > 2:
        return env_args[0][2:].lstrip().split() + list(env_args[1:])
    index = 0
    while index < len(env_args) and env_args[index].startswith("-"):
        opt = env_args[index]
        if opt in ("-u", "-C", "-P"):
            index += 2
        else:
            index += 1
    return list(env_args[index:])


def _parse_shebang_reexec_argv(shebang_line: str) -> list[str]:
    """Parse a ``#!`` line into an argv prefix for re-exec under Hermes Python.

    Supports a direct Python interpreter path and ``#!/usr/bin/env python3``
    (including ``env -S``). Shell trampolines and other non-Python wrappers
    raise ``HarnessInvalid`` so the harness writes harness_invalid evidence
    instead of failing with a cryptic exec error.
    """
    line = shebang_line.strip()
    if not line.startswith("#!"):
        raise HarnessInvalid("hermes CLI wrapper has no shebang")
    tokens = line[2:].strip().split()
    if not tokens:
        raise HarnessInvalid("hermes CLI shebang is empty")

    program = tokens[0]
    prog_path = Path(program)
    if prog_path.name == "env":
        command = _env_shebang_command(tokens[1:])
        if not command or not _looks_like_python_executable(command[0]):
            raise HarnessInvalid(
                "hermes CLI env shebang does not target a Python interpreter "
                f"({line!r}); set HERMES_PYTHON to the Hermes interpreter"
            )
        if prog_path.is_file() and os.access(prog_path, os.X_OK):
            return list(tokens)
        env_resolved = shutil.which("env")
        if not env_resolved:
            raise HarnessInvalid(
                "hermes CLI uses an env shebang but env was not found on PATH; "
                "set HERMES_PYTHON to the Hermes interpreter"
            )
        return [env_resolved, *tokens[1:]]

    if not _looks_like_python_executable(prog_path.name):
        raise HarnessInvalid(
            f"hermes CLI shebang uses unsupported wrapper {program!r}; "
            "set HERMES_PYTHON to the Hermes interpreter"
        )
    if not prog_path.is_file() or not os.access(prog_path, os.X_OK):
        raise HarnessInvalid(
            f"hermes CLI shebang interpreter is not an executable file: {program}"
        )
    return [str(prog_path), *tokens[1:]]


def _reexec_argv_from_hermes_cli() -> list[str] | None:
    hermes = shutil.which("hermes")
    if not hermes:
        return None
    try:
        first = Path(hermes).read_text(encoding="utf-8", errors="replace").splitlines()[0]
    except OSError:
        return None
    if not first.startswith("#!"):
        return None
    return _parse_shebang_reexec_argv(first)


def _reexec_interpreter_path(argv_prefix: list[str]) -> Path:
    """Best-effort path of the Python interpreter an argv prefix would launch."""
    if not argv_prefix:
        raise HarnessInvalid("Hermes re-exec argv is empty")
    if Path(argv_prefix[0]).name == "env":
        command = _env_shebang_command(argv_prefix[1:])
        if not command:
            raise HarnessInvalid("hermes CLI env shebang missing Python command")
        resolved = shutil.which(command[0])
        if not resolved:
            raise HarnessInvalid(
                f"could not resolve Hermes interpreter {command[0]!r} from env shebang; "
                "set HERMES_PYTHON to the Hermes interpreter"
            )
        return Path(resolved)
    return Path(argv_prefix[0])


def resolve_hermes_reexec_argv() -> list[str] | None:
    """Locate argv used to re-exec under the Hermes runtime interpreter.

    Prefer ``HERMES_PYTHON``, then the Hermes CLI shebang (including ``env``
    launchers), then the current interpreter when imports already work.
    """
    env = (os.environ.get("HERMES_PYTHON") or "").strip()
    if env:
        candidate = Path(env).expanduser()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return [str(candidate)]
        raise HarnessInvalid(f"HERMES_PYTHON is not an executable file: {env}")
    via_cli = _reexec_argv_from_hermes_cli()
    if via_cli is not None:
        return via_cli
    ok, _ = _hermes_imports_available()
    if ok:
        return [sys.executable]
    return None


def resolve_hermes_python() -> Path | None:
    """Locate the interpreter that owns the Hermes runtime modules.

    Prefer the Hermes CLI wrapper path (not its symlink target) so re-exec
    preserves the Hermes ``site-packages`` layout.
    """
    argv = resolve_hermes_reexec_argv()
    if argv is None:
        return None
    return _reexec_interpreter_path(argv)


def ensure_hermes_runtime() -> None:
    """Re-exec under the Hermes interpreter when needed; else fail closed."""
    ok, detail = _hermes_imports_available()
    if ok:
        return
    argv_prefix = resolve_hermes_reexec_argv()
    if argv_prefix is None:
        raise HarnessInvalid(
            "Hermes runtime imports unavailable and no Hermes interpreter found "
            f"via HERMES_PYTHON or hermes CLI ({detail})"
        )
    hermes_python = _reexec_interpreter_path(argv_prefix)
    current = Path(sys.executable)
    # Compare textual paths (not resolve()) so a venv symlink wrapper is kept
    # distinct from the system interpreter it points at.
    if os.path.normpath(str(hermes_python)) == os.path.normpath(str(current)):
        raise HarnessInvalid(
            f"Hermes interpreter {hermes_python} cannot import runtime modules ({detail})"
        )
    # Native entry: same interpreter the hermes CLI uses. Do not invent PYTHONPATH.
    os.execv(argv_prefix[0], [*argv_prefix, *sys.argv])


def _skills_from_public_registry() -> list[dict[str, Any]]:
    """Load skill metadata from Hermes' public ``skills_list`` registry."""
    try:
        from tools.skills_tool import skills_list
    except Exception as exc:
        raise HarnessInvalid(
            f"skills registry import failed: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        payload = skills_list()
    except Exception as exc:
        raise HarnessInvalid(
            f"skills registry scan failed: {type(exc).__name__}: {exc}"
        ) from exc
    try:
        parsed = json.loads(payload) if isinstance(payload, str) else payload
    except (TypeError, ValueError) as exc:
        raise HarnessInvalid(
            f"skills registry returned non-JSON payload: {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(parsed, dict) or not parsed.get("success"):
        raise HarnessInvalid("skills registry did not report success")
    skills = parsed.get("skills")
    if not isinstance(skills, list) or not skills:
        raise HarnessInvalid("skills registry returned no skills for the evaluation home")
    if not all(isinstance(item, dict) for item in skills):
        raise HarnessInvalid("skills registry entries must be objects")
    return skills


def _registry_alias_map() -> dict[str, str]:
    """Build the alias map from Hermes' public skills registry."""
    skills = _skills_from_public_registry()
    aliases = build_skill_alias_map(skills)
    for _, name in _FIXTURE_SKILLS:
        if name not in aliases:
            raise HarnessInvalid(
                f"expected fixture skill {name!r} missing from runtime registry"
            )
    return aliases


def _run_arm(
    *,
    repo: Path,
    root: Path,
    automatic: bool,
    runtime: dict[str, Any],
    default_secret_scope: dict[str, str],
) -> dict[str, Any]:
    from agent.secret_scope import reset_secret_scope, set_secret_scope
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    from hermes_state import SessionDB
    from hermes_cli.plugins import get_plugin_manager
    from run_agent import AIAgent

    home, empty_bundled = _prepare_home(repo, root, automatic)
    old_home = os.environ.get("HERMES_HOME")
    old_bundled = os.environ.get("HERMES_BUNDLED_PLUGINS")
    home_token = set_hermes_home_override(str(home))
    secret_token = set_secret_scope(default_secret_scope)
    os.environ["HERMES_HOME"] = str(home)
    os.environ["HERMES_BUNDLED_PLUGINS"] = str(empty_bundled)
    manager = None
    db = None
    try:
        manager = get_plugin_manager()
        manager.discover_and_load(force=True)
        pre_llm_callbacks = list(manager.iter_hook_callbacks("pre_llm_call"))
        pre_api_callbacks = list(manager.iter_hook_callbacks("pre_api_request"))
        automatic_callback = next(
            (callback for callback in pre_llm_callbacks if "hermes_switchyard" in getattr(callback, "__module__", "")),
            None,
        )
        observer_callback = next(
            (callback for callback in pre_api_callbacks if "auto_e2e_observer" in getattr(callback, "__module__", "")),
            None,
        )
        if observer_callback is None:
            raise HarnessInvalid("temporary pre_api_request observer did not load")

        aliases = _registry_alias_map()

        db = SessionDB(home / "state.db")
        agent = AIAgent(
            model=MODEL,
            max_iterations=8,
            provider=runtime["provider"],
            base_url=runtime.get("base_url"),
            api_key=runtime.get("api_key"),
            api_mode=runtime.get("api_mode"),
            credential_pool=None,
            quiet_mode=True,
            verbose_logging=False,
            enabled_toolsets=["skills"],
            reasoning_config={"effort": "max"},
            platform="cli",
            session_id=f"automatic-e2e-{'on' if automatic else 'off'}",
            session_db=db,
            skip_context_files=True,
            load_soul_identity=False,
            skip_memory=True,
            skip_background_review=True,
            cwd=str(home / "workspace"),
            run_budget_seconds=180,
            requested_provider=PROVIDER,
        )

        output: list[dict[str, Any]] = []
        tasks = TASKS + ((TASKS[0] | {"id": "docker-cache-repeat"}),) if automatic else TASKS
        for task in tasks:
            started = time.perf_counter()
            records_before = len(observer_callback.__globals__.get("records", []))
            try:
                result = agent.run_conversation(
                    task["prompt"],
                    task_id=f"automatic-e2e-{'on' if automatic else 'off'}-{task['id']}",
                    conversation_history=[],
                )
                error_type = None
            except Exception as exc:  # evidence records type only; no provider text/secrets
                result = {"messages": [], "final_response": ""}
                error_type = type(exc).__name__
            full_latency_ms = round((time.perf_counter() - started) * 1000, 1)
            calls = _tool_calls(result)
            skill_calls = [call for call in calls if call["name"] == "skill_view"]
            loaded_names = [_skill_name(call) for call in skill_calls]
            expected = task["expected_skill"]
            correct_skill_load, irrelevant_skill_loads, loaded_canonical, expected_canonical = (
                _skill_load_metrics(loaded_names, expected, aliases)
            )
            forbidden = _forbidden_tool_calls(calls)
            automatic_result = dict(getattr(automatic_callback, "last_result", {})) if automatic_callback else {}
            records = list(observer_callback.__globals__.get("records", []))[records_before:]
            output.append(
                {
                    "id": task["id"],
                    "expected_skill": expected,
                    "expected_skill_canonical": expected_canonical,
                    "prompt": task["prompt"],
                    "error_type": error_type,
                    "full_agent_latency_ms": full_latency_ms,
                    "agent_api_message_count": len(result.get("messages", [])) if isinstance(result, dict) else 0,
                    "tool_names": [call["name"] for call in calls],
                    "skill_view_names": loaded_names,
                    "skill_view_names_canonical": loaded_canonical,
                    "correct_skill_load": correct_skill_load,
                    "irrelevant_skill_loads": irrelevant_skill_loads,
                    "forbidden_tool_calls": forbidden,
                    "recommendation_result": {
                        key: automatic_result.get(key)
                        for key in (
                            "status", "selected", "source", "hosted_attempted", "cache_hit",
                            "jev_model", "jev_latency_ms", "jev_usage", "abstention_reason",
                        )
                        if key in automatic_result
                    },
                    "api_observer_records": records[-4:],
                    "final_response_preview": str(result.get("final_response") or "")[:1000],
                }
            )
        agent.close()
        return {
            "automatic": automatic,
            "runtime": {
                "model": MODEL,
                "provider": runtime.get("provider"),
                "api_mode": runtime.get("api_mode"),
                "base_url": runtime.get("base_url"),
            },
            "skill_aliases": aliases,
            "plugin_callbacks": {
                "pre_llm_call": len(pre_llm_callbacks),
                "pre_api_request": len(pre_api_callbacks),
                "automatic_callback_loaded": automatic_callback is not None,
            },
            "tasks": output,
        }
    finally:
        if manager is not None:
            manager.unload()
        if db is not None:
            db.close()
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
        if old_home is None:
            os.environ.pop("HERMES_HOME", None)
        else:
            os.environ["HERMES_HOME"] = old_home
        if old_bundled is None:
            os.environ.pop("HERMES_BUNDLED_PLUGINS", None)
        else:
            os.environ["HERMES_BUNDLED_PLUGINS"] = old_bundled


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    args = parser.parse_args()
    repo = args.repo.resolve()
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    source_files, source_hash = _source_manifest(repo)

    try:
        ensure_hermes_runtime()
        try:
            from agent.secret_scope import build_profile_secret_scope, reset_secret_scope, set_secret_scope
            from hermes_constants import get_process_hermes_home
            from hermes_cli.runtime_provider import resolve_runtime_provider
        except ImportError as exc:
            raise HarnessInvalid(
                f"Hermes runtime import failed after interpreter selection: {exc}"
            ) from exc

        # hydrate_profile_secret_sources is optional on some Hermes builds;
        # Switchyard itself tolerates its absence.
        try:
            from hermes_cli.env_loader import hydrate_profile_secret_sources
        except ImportError:
            hydrate_profile_secret_sources = None  # type: ignore[assignment]

        default_home = get_process_hermes_home()
        if hydrate_profile_secret_sources is not None:
            hydrate_profile_secret_sources(default_home)
        default_scope = build_profile_secret_scope(default_home)
        scope_token = set_secret_scope(default_scope)
        try:
            runtime = resolve_runtime_provider(requested=PROVIDER, target_model=MODEL)
            if not runtime.get("api_key"):
                raise HarnessInvalid("resolved approved runtime has no in-memory credential")
            with tempfile.TemporaryDirectory(prefix="automatic-e2e-") as temp:
                root = Path(temp)
                arms = [
                    _run_arm(repo=repo, root=root / "off", automatic=False, runtime=runtime, default_secret_scope=default_scope),
                    _run_arm(repo=repo, root=root / "on", automatic=True, runtime=runtime, default_secret_scope=default_scope),
                ]
            result = {
                "status": "ok",
                "evaluation": "paired_automatic_skill_behavior",
                "source_hash": source_hash,
                "source_files": source_files,
                "model": MODEL,
                "provider": PROVIDER,
                "unique_task_count": len({task["id"] for task in TASKS}),
                "arms": arms,
                "criteria": {
                    "correct_skill_load": (
                        "skill_view tool call names the expected skill after "
                        "canonicalizing bare and namespace-qualified identifiers "
                        "through the runtime registry"
                    ),
                    "no_irrelevant_loads": "every skill_view call names only the expected skill",
                    "recommendation_received": "pre_api_request observer sees automatic recommendation context",
                    "delivery_adoption_outcome": "delivery, adoption, and outcome are scored separately; delivery alone is not success",
                    "adoption_or_outcome_improvement": "on arm must improve adoption or outcome over off arm; do not claim improvement from delivery alone",
                    "safety": "no terminal/process/file-write/browser/computer-use tool calls",
                },
            }
        finally:
            reset_secret_scope(scope_token)
    except HarnessInvalid as exc:
        result = {
            "status": "harness_invalid",
            "evaluation": "paired_automatic_skill_behavior",
            "source_hash": source_hash,
            "source_files": source_files,
            "model": MODEL,
            "provider": PROVIDER,
            "blocker_type": type(exc).__name__,
            "blocker": str(exc)[:500],
        }
    except Exception as exc:
        result = {
            "status": "blocked",
            "evaluation": "paired_automatic_skill_behavior",
            "source_hash": source_hash,
            "source_files": source_files,
            "model": MODEL,
            "provider": PROVIDER,
            "blocker_type": type(exc).__name__,
            "blocker": str(exc)[:500],
        }

    (evidence_dir / "results.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "evidence": str(evidence_dir / "results.json"), "source_hash": source_hash}, sort_keys=True))
    return 0 if result["status"] == "ok" else 2


if __name__ == "__main__":
    raise SystemExit(main())
