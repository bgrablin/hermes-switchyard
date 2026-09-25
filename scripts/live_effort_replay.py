#!/usr/bin/env python3
"""Manual exact-source installed adaptive-effort replay; model API wires are mocked.

Only Jev decisions reach the configured hosted provider. Run with an explicit
approved provider and profile home; never place credentials in the isolated home.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SHA = re.compile(r"[0-9a-f]{40}\Z")

class ReplayError(RuntimeError):
    """A replay prerequisite or acceptance check failed."""


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=True, text=True)
    return result.stdout.strip()


def _sparse_child_env(allowed: tuple[str, ...]) -> dict[str, str]:
    """Keep the isolated allowlist and the actual Windows loader root only."""
    env = {key: os.environ[key] for key in allowed if key in os.environ}
    if os.name == "nt":
        for key, value in os.environ.items():
            if key.casefold() == "systemroot" and value.strip():
                env[key] = value
                break
        else:
            raise ReplayError("Windows SystemRoot is unavailable for the isolated child")
    return env

def prepare(archive: Path, source_root: Path, source_sha: str, home: Path) -> str:
    """Reject wrong-source archives before installing only verified members."""
    from scripts.build_release import RELEASE_FILES, SOURCE_MANIFEST_NAME, verify_archive

    if not SHA.fullmatch(source_sha):
        raise ReplayError("source SHA must be an exact lowercase commit ID")
    if _git(source_root, "rev-parse", "HEAD") != source_sha:
        raise ReplayError("source checkout HEAD differs from requested SHA")
    tree = _git(source_root, "rev-parse", "HEAD^{tree}")
    verdict = verify_archive(archive, source_root=source_root, expected_source_sha=source_sha)
    if not verdict["source_verified"]:
        raise ReplayError("archive source verification failed")
    plugin = home / "plugins" / "hermes-switchyard"
    with zipfile.ZipFile(archive) as bundle:
        manifest = json.loads(bundle.read(SOURCE_MANIFEST_NAME))
        if manifest["source_sha"] != source_sha:
            raise ReplayError("manifest source SHA differs")
        for relative in RELEASE_FILES:
            destination = plugin / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(bundle.read(relative))
            if hashlib.sha256(destination.read_bytes()).hexdigest() != next(
                item["sha256"] for item in manifest["files"] if item["path"] == relative
            ):
                raise ReplayError("installed payload hash mismatch")
    return tree


def run_installed(plugin: Path, source_sha: str, tree: str, provider: str, secret_home: Path,
                  *, synthetic_client_factory=None) -> dict:
    """Run native middleware and real Jev in a subprocess with isolated plugin state."""
    import anthropic
    import httpx
    import openai
    from agent.secret_scope import build_profile_secret_scope, get_secret, reset_secret_scope, set_multiplex_active, set_secret_scope
    from agent.transports.codex import _reasoning_fields
    from hermes_cli.env_loader import hydrate_profile_secret_sources
    from hermes_cli.middleware import apply_llm_request_middleware
    from hermes_cli.plugins import get_plugin_manager

    if provider not in {"typesafe", "openrouter"}:
        raise ReplayError("provider must be explicitly typesafe or openrouter")
    hydrate_profile_secret_sources(secret_home)
    scope = build_profile_secret_scope(secret_home)
    key_name = "TYPESAFE_API_KEY" if provider == "typesafe" else "OPENROUTER_API_KEY"
    # Never inherit another provider's credential from the parent process.
    if not scope.get(key_name):
        raise ReplayError(f"approved profile scope has no {key_name}")
    set_multiplex_active(True)
    token = set_secret_scope(scope)
    try:
        if not get_secret(key_name):
            raise ReplayError("native scoped secret resolution failed")
        manager = get_plugin_manager()
        manager.discover_and_load(force=True)
        loaded = manager._plugins.get("hermes-switchyard")
        if loaded is None or not loaded.enabled or loaded.error:
            raise ReplayError("installed candidate did not load")
        callbacks = manager._middleware.get("llm_request", [])
        if len(callbacks) != 1:
            raise ReplayError("installed candidate middleware was not unique")
        controller = callbacks[0].__self__
        module = sys.modules[controller.__class__.__module__]
        if not Path(module.__file__).resolve().is_relative_to(plugin.resolve()):
            raise ReplayError("middleware module did not come from installed archive")
        if not controller.enabled or not controller.public_or_sanitized_data_ack or controller.allow_raise:
            raise ReplayError("unsafe adaptive effort settings")
        if controller.deadline_seconds > 1.5:
            raise ReplayError("candidate deadline exceeds 1.5 seconds")
        if synthetic_client_factory is not None:
            # Offline test seam only: the CLI child never supplies this argument.
            controller.client_factory = synthetic_client_factory
        if "switchyard" not in manager._plugin_commands:
            raise ReplayError("registered switchyard command missing")
        command = manager._plugin_commands["switchyard"]["handler"]
        from gateway.session_context import scoped_current_session_id

        routes = (
            ("codex", "openai-codex", "gpt-6-astra-900k", "codex_responses"),
            ("anthropic", "anthropic", "claude-opus-5-5", "anthropic_messages"),
        )
        receipts = []
        for label, route, model, api_mode in routes:
            def request_for(effort: str) -> dict:
                if label == "codex":
                    fields = _reasoning_fields(model, {}, effort=effort, enabled=True,
                        replay_encrypted_reasoning=False, is_xai_responses=False, is_github_responses=False)
                    return {"model": model, "input": "Synthetic public status.", **fields}
                return {"model": model, "messages": [{"role": "user", "content": "Synthetic public status."}],
                    "max_tokens": 32, "thinking": {"type": "adaptive"}, "output_config": {"effort": effort}}

            def wire(payload: dict) -> str:
                observed = []
                def capture(req):
                    data = json.loads(req.content)
                    observed.append(data)
                    if label == "codex":
                        return httpx.Response(200, json={"id": "resp_synthetic", "object": "response", "model": model,
                            "created_at": 0, "output": [], "status": "completed"})
                    return httpx.Response(200, json={"id": "msg_synthetic", "type": "message", "role": "assistant",
                        "model": model, "content": [], "stop_reason": "end_turn", "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1}})
                transport = httpx.Client(transport=httpx.MockTransport(capture))
                if label == "codex":
                    with openai.OpenAI(api_key="test-key", base_url="https://example.invalid/v1", http_client=transport) as sdk:
                        sdk.responses.create(**payload)
                    if observed[0]["input"] != "Synthetic public status.":
                        raise ReplayError("Codex wire input changed")
                    return observed[0]["reasoning"]["effort"]
                with anthropic.Anthropic(api_key="test-key", base_url="https://example.invalid", http_client=transport) as sdk:
                    sdk.messages.create(**payload)
                if observed[0]["thinking"] != {"type": "adaptive"}:
                    raise ReplayError("Anthropic adaptive thinking changed")
                return observed[0]["output_config"]["effort"]

            session = f"replay-{label}"
            hook = controller.build_post_tool_call_hook()
            def step(name: str, effort: str, turn: str) -> dict:
                before = controller.session_status(session).get("jev_calls", 0)
                start = time.monotonic()
                original = request_for(effort)
                result = apply_llm_request_middleware(original, provider=route, model=model,
                    api_mode=api_mode, session_id=session, turn_id=turn)
                elapsed = time.monotonic() - start
                payload = result.payload
                sent = wire(payload)
                decision = module.last_receipt()
                after = controller.session_status(session)["jev_calls"]
                ladder = ["low", "medium", "high", "xhigh", "max"] if label == "codex" else ["low", "medium", "high", "max"]
                if ladder.index(sent) > ladder.index(effort):
                    raise ReplayError(f"{label}/{name}: sent effort exceeded requested")
                if elapsed > controller.deadline_seconds + 0.5:
                    raise ReplayError(f"{label}/{name}: middleware exceeded deadline allowance")
                return {"source_sha": source_sha, "source_tree": tree, "provider": route,
                    "step": name, "requested": effort, "sent": sent,
                    "reason": decision.get("reason_code"), "jev_called": after > before,
                    "jev_status": decision.get("status"), "jev_latency_ms": decision.get("jev_latency_ms"),
                    "elapsed_ms": round(elapsed * 1000, 2)}

            # A separate high-effort probe must call real Jev: the seven-step
            # low→pin sequence correctly avoids Jev altogether.
            probe = step("hosted-probe", "high", "probe")
            receipts.append(probe)
            if not probe["jev_called"] or probe["jev_status"] != "selected":
                return {"ok": False, "source_sha": source_sha, "source_tree": tree,
                    "jev_provider": provider, "error": f"{label}: hosted Jev decision did not succeed",
                    "jev_transport": "synthetic" if synthetic_client_factory else "hosted", "receipts": receipts}
            controller.set_mode("auto", session_id=session)
            receipts.append(step("1-first", "low", "t1"))
            hook(tool_name="shell", status="error", error_message="synthetic failure", session_id=session)
            receipts.append(step("2-after-failure", "low", "t1"))
            receipts.append(step("3-user-changes-level", "medium", "t1"))
            for _ in range(3):
                hook(tool_name="shell", result='{"ok":true}', session_id=session)
            receipts.append(step("4-after-three-successes", "medium", "t1"))
            receipts.append(step("5-new-turn", "medium", "t2"))
            for _ in range(6):
                hook(tool_name="shell", result='{"ok":true}', session_id=session)
            receipts.append(step("6-after-six-successes", "medium", "t2"))
            receipts.append(step("7-another-turn", "medium", "t3"))
            sequence = receipts[-7:]
            if sequence[2]["reason"] != "pinned_by_user_change" or any(r["jev_called"] for r in sequence):
                raise ReplayError(f"{label}: pin/low sequence made an unexpected Jev call")
            if sequence[0]["reason"] != "no_room":
                raise ReplayError(f"{label}: low did not skip Jev")
            with scoped_current_session_id(session):
                if "mode: pinned" not in command("effort status"):
                    raise ReplayError(f"{label}: registered command state disagrees")
        manager.unload()
        return {"ok": True, "source_sha": source_sha, "source_tree": tree,
            "jev_provider": provider, "jev_transport": "synthetic" if synthetic_client_factory else "hosted",
            "receipts": receipts}
    finally:
        reset_secret_scope(token)
        set_multiplex_active(False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--secret-home", type=Path, required=True)
    parser.add_argument("--provider", choices=("typesafe", "openrouter"), required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--allow-hosted", action="store_true", help="explicitly authorize bounded hosted Jev calls")
    args = parser.parse_args(argv)
    report = {"ok": False, "error": "not attempted"}
    try:
        if not args.allow_hosted:
            raise ReplayError("hosted Jev not authorized; pass --allow-hosted after provider/privacy/cost review")
        with tempfile.TemporaryDirectory(prefix="effort-replay-") as scratch:
            work = Path(scratch)
            home = work / "hermes"
            tree = prepare(args.archive, args.source_root, args.source_sha, home)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [hermes-switchyard]\n  entries:\n    hermes-switchyard:\n      settings:\n"
                f"        jev_provider: {args.provider}\n"
                "        automatic_skill_recommendation: false\n"
                "        adaptive_reasoning_effort_allow_raise: false\n"
                "        adaptive_reasoning_effort_deadline_seconds: 1.5\n", encoding="utf-8")
            bundled = work / "bundled"
            bundled.mkdir()
            env = _sparse_child_env(("PATH", "LANG", "TMPDIR"))
            env.update({"HOME": str(work), "HERMES_HOME": str(home), "HERMES_BUNDLED_PLUGINS": str(bundled)})
            command = [sys.executable, str(Path(__file__).resolve()), "--child", str(home / "plugins" / "hermes-switchyard"),
                args.source_sha, tree, args.provider, str(args.secret_home.resolve())]
            child = subprocess.run(command, cwd=work, env=env, capture_output=True, text=True, timeout=60)
            try:
                child_report = json.loads(child.stdout.splitlines()[-1])
            except (IndexError, json.JSONDecodeError) as exc:
                raise ReplayError("installed replay child produced no sanitized receipt") from exc
            if child.returncode:
                error = child_report.get("error")
                if not isinstance(error, str) or error not in {
                    "ReplayError", "RuntimeError", "TypeError", "ValueError", "KeyError",
                    "approved profile scope has no TYPESAFE_API_KEY",
                    "approved profile scope has no OPENROUTER_API_KEY",
                }:
                    error = "installed replay child failed; inspect the isolated run without disclosing credentials"
                raise ReplayError(error)
            report = child_report
            if report.get("source_sha") != args.source_sha or report.get("source_tree") != tree:
                raise ReplayError("child source receipt does not match verified archive")
            if report.get("jev_transport") != "hosted":
                raise ReplayError("installed child did not use hosted Jev")
    except (ReplayError, OSError, subprocess.SubprocessError, ValueError, KeyError) as exc:
        report = {"ok": False, "error": str(exc) if isinstance(exc, ReplayError) else type(exc).__name__}
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    if len(sys.argv) == 7 and sys.argv[1] == "--child":
        try:
            with open(os.devnull, "w", encoding="utf-8") as sink, redirect_stdout(sink):
                result = run_installed(Path(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5], Path(sys.argv[6]))
            print(json.dumps(result, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 -- never print credentials or provider exception text
            print(json.dumps({"ok": False, "error": str(exc) if isinstance(exc, ReplayError) else type(exc).__name__}))
            raise SystemExit(1)
    else:
        raise SystemExit(main())
