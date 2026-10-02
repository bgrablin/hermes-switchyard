"""Exercise the installed Hermes middleware and SDK wire shape in an isolated home."""
from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts import live_effort_replay as replay
from scripts.build_release import RELEASE_FILES


def _installed_provenance(plugin_dir: Path, checkout_dir: Path, manifest_dir: str | Path | None,
                          entrypoint_file: str | Path | None, controller_file: str | Path | None) -> dict[str, str | bool]:
    """Classify strict file origins without emitting local paths or credentials."""
    def canonical(path: str | Path | None) -> Path | None:
        try:
            return Path(path).resolve(strict=True) if path is not None else None
        except (OSError, RuntimeError, TypeError, ValueError):
            return None

    installed = canonical(plugin_dir)
    checkout = canonical(checkout_dir)
    manifest = canonical(manifest_dir)
    entrypoint = canonical(entrypoint_file)
    controller = canonical(controller_file)

    def within(path: Path | None, root: Path | None) -> bool:
        return path is not None and root is not None and path.is_relative_to(root)

    def origin(path: Path | None, exact_file: Path | None) -> str:
        if within(path, checkout):
            return "checkout"
        if path is not None and exact_file is not None and path == exact_file and within(path, installed):
            return "installed"
        return "other"

    return {
        "manifest_origin": origin(manifest, installed),
        "entrypoint_origin": origin(entrypoint, installed / "__init__.py" if installed else None),
        "controller_origin": origin(controller, installed / "hermes_switchyard" / "reasoning_effort_adapter.py"
                                    if installed else None),
        "raw_root_comparison": within(controller, plugin_dir),
        "canonical_root_comparison": within(controller, installed),
        "install_outside_checkout": installed is not None and checkout is not None and not within(installed, checkout),
    }


def _closed_origin_receipt(stdout: str) -> dict[str, str | bool] | None:
    """Keep only public, closed-set fields from an untrusted child response."""
    try:
        payload = json.loads(stdout.splitlines()[-1])
    except (IndexError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("provenance"), dict):
        return None
    raw = payload["provenance"]
    receipt: dict[str, str | bool] = {}
    for field in ("manifest_origin", "entrypoint_origin", "controller_origin"):
        value = raw.get(field)
        receipt[field] = value if isinstance(value, str) and value in ("installed", "checkout", "other") else "other"
    for field in ("raw_root_comparison", "canonical_root_comparison", "install_outside_checkout"):
        value = raw.get(field)
        receipt[field] = value if type(value) is bool else False
    return receipt


def _isolated_replay(plugin_dir: Path) -> None:
    # Imports happen only after the child has an isolated HOME and HERMES_HOME.
    import anthropic
    import httpx
    import openai
    from agent.anthropic_message_convert import convert_messages_to_anthropic
    from agent.transports.codex import _reasoning_fields
    from agent.turn_context import compose_user_api_content
    from gateway.session_context import scoped_current_session_id
    from hermes_cli.commands import resolve_command
    from hermes_cli.middleware import apply_llm_request_middleware
    from hermes_cli.plugins import get_plugin_manager, invoke_hook

    manager = get_plugin_manager()
    manager.discover_and_load(force=True)
    loaded = manager._plugins.get("hermes-switchyard")
    assert loaded is not None and loaded.enabled and loaded.error is None, "installed_plugin_not_enabled"
    callbacks = manager._middleware.get("llm_request", [])
    assert len(callbacks) == 1, "installed_plugin_middleware_not_unique"
    controller = callbacks[0].__self__
    controller_module = sys.modules.get(controller.__class__.__module__)
    provenance = _installed_provenance(
        plugin_dir, Path(__file__).parent.parent, getattr(loaded.manifest, "path", None),
        getattr(loaded.module, "__file__", None), getattr(controller_module, "__file__", None),
    )
    print(json.dumps({"provenance": provenance}), flush=True)
    if not (provenance["install_outside_checkout"] and provenance["canonical_root_comparison"]
            and provenance["manifest_origin"] == "installed"
            and provenance["entrypoint_origin"] == "installed"
            and provenance["controller_origin"] == "installed"):
        raise AssertionError("installed_plugin_origin_mismatch")
    assert resolve_command("switchyard") is None
    command = manager._plugin_commands["switchyard"]["handler"]

    class SyntheticClient:
        def __init__(self):
            self.calls = []

        def decide(self, state, questions, **kwargs):
            self.calls.append((state, questions))
            levels = tuple(questions["reasoning_effort"]["criteria"])
            selected = "low" if "low" in levels else levels[0]
            probabilities = {level: 1.0 if level == selected else 0.0 for level in levels}
            return {"answers": {
                "reasoning_effort": {"choice": selected, "confidence": 1.0, "probabilities": probabilities},
                "stakes": {"noul": 0.0},
            }}

    fake = SyntheticClient()
    controller.client_factory = lambda: fake  # Never construct a credential-backed client.
    model = "gpt-6-astra-900k"
    task_text = "synthetic public routine request"

    def begin_turn(session, turn, task=None, text=task_text, parent=""):
        # The real host dispatcher, with the kwargs Hermes passes before memory prefetch.
        invoke_hook(
            "pre_llm_call", session_id=session, task_id=task or session, turn_id=turn,
            user_message=text, conversation_history=[{"role": "user", "content": "SYNTHETIC_HISTORY"}],
            is_first_turn=True, model=model, platform="cli", sender_id="", parent_session_id=parent,
        )

    def codex(effort, session="s1", turn="t1", task=None):
        fields = _reasoning_fields(
            model, {}, effort=effort, enabled=True, replay_encrypted_reasoning=False,
            is_xai_responses=False, is_github_responses=False,
        )
        request = {"model": model, "input": "synthetic", **fields}
        middleware = apply_llm_request_middleware(
            request, provider="openai-codex", model=model, api_mode="codex_responses",
            session_id=session, turn_id=turn, task_id=task or session,
        )
        return request, middleware

    # #121: without the clean pre_llm_call message there is no hosted call.
    _, uncaptured = codex("high", session="s-none")
    assert not uncaptured.changed and uncaptured.payload["reasoning"]["effort"] == "high"
    assert fake.calls == [], "jev_called_without_clean_capture"
    begin_turn("s1", "t1")
    first, lowered = codex("high")
    assert first["reasoning"]["effort"] == "high"
    assert lowered.changed and lowered.payload["reasoning"]["effort"] == "low"
    assert lowered.payload["input"] == first["input"]
    assert lowered.trace[0]["source"] == "hermes-switchyard"
    jev_state = fake.calls[0][0]
    assert set(jev_state) == {"current_request", "turn_phase", "recent_tool_statuses", "latest_tool_failed"}
    assert jev_state["current_request"] == task_text
    assert "SYNTHETIC_HISTORY" not in json.dumps(fake.calls[0])
    _, cached = codex("high")
    assert cached.payload["reasoning"]["effort"] == "low" and len(fake.calls) == 1
    # #118: a delegated task sharing the session ID must not pin or re-baseline the foreground.
    begin_turn("s1", "t-child", task="subagent-synthetic", parent="s1")
    _, delegated = codex("low", turn="t-child", task="subagent-synthetic")
    assert not delegated.changed and delegated.payload["reasoning"]["effort"] == "low"
    assert controller.session_status("s1")["mode"] == "auto", "delegated_task_changed_foreground_mode"
    _, foreground = codex("high")
    assert foreground.payload["reasoning"]["effort"] == "low" and len(fake.calls) == 1
    begin_turn("s-low", "t1")
    _, capped = codex("low", session="s-low")
    assert not capped.changed and capped.payload["reasoning"]["effort"] == "low"
    assert len(fake.calls) == 1
    # v0.5.5: a /reasoning change sets a new cap and keeps auto; only an explicit pin pins.
    _, recapped = codex("medium")
    assert recapped.changed and recapped.payload["reasoning"]["effort"] == "low"
    assert controller.session_status("s1")["mode"] == "auto", "user_change_pinned_the_session"
    assert controller.session_status("s1")["user_level"] == "medium"
    assert len(fake.calls) == 2
    with scoped_current_session_id("s1"):
        assert "pinned" in command("effort pin")
    _, pinned = codex("medium")
    assert not pinned.changed and pinned.payload["reasoning"]["effort"] == "medium"
    assert controller.session_status("s1")["mode"] == "pinned"
    assert len(fake.calls) == 2
    with scoped_current_session_id("s1"):
        assert "mode: pinned" in command("effort status")
    with scoped_current_session_id("s-low"):
        for invalid in ("effort pin garbage", "effort auto garbage", "effort status garbage", "effort unknown", "effort pinned"):
            assert command(invalid).startswith("Usage: /switchyard"), invalid
        assert controller.session_status("s-low")["mode"] == "auto"

    codex_wire = []

    def capture_codex(request):
        codex_wire.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "resp_synthetic", "object": "response", "model": model,
                                          "created_at": 0, "output": [], "status": "completed"})

    with openai.OpenAI(api_key="test-key", base_url="https://example.invalid/v1",
                       http_client=httpx.Client(transport=httpx.MockTransport(capture_codex))) as client:
        client.responses.create(**lowered.payload)
    assert codex_wire[0]["reasoning"] == {"effort": "low", "summary": "auto"}
    assert codex_wire[0]["input"] == "synthetic"
    assert "reasoning_effort" not in codex_wire[0]

    # #181: enforce the reported relay enum at the actual SDK HTTP boundary.
    # The installed plugin must make greetings and pre-existing minimal valid
    # without a Jev call. This is an offline compatibility test, not a live speed claim.
    strict_wire = []
    accepted_efforts = {"low", "medium", "high", "xhigh", "max"}

    def strict_chat(request):
        payload = json.loads(request.content)
        strict_wire.append(payload)
        if payload.get("reasoning_effort") not in accepted_efforts:
            return httpx.Response(400, json={"error": {
                "message": 'Invalid option: expected one of "low"|"medium"|"high"|"xhigh"|"max"',
                "type": "invalid_request_error", "param": "reasoning_effort",
            }})
        return httpx.Response(200, json={"id": "chat_synthetic", "object": "chat.completion",
            "created": 0, "model": payload["model"], "choices": []})

    calls_before = len(fake.calls)
    with openai.OpenAI(api_key="test-key", base_url="https://example.invalid/v1", max_retries=0,
                       http_client=httpx.Client(transport=httpx.MockTransport(strict_chat))) as client:
        for alias in ("commandcode", "command-code", "command_code"):
            for text, requested in (("hi", "high"), ("thanks", "high"), ("hi", "minimal")):
                session = f"strict-{alias}-{text}-{requested}"
                begin_turn(session, "t1", text=text)
                request = {"model": "deepseek/deepseek-v4.1-flash",
                           "messages": [{"role": "user", "content": text}], "reasoning_effort": requested}
                adapted = apply_llm_request_middleware(
                    request, provider=alias, model=request["model"], api_mode="chat_completions",
                    session_id=session, task_id=session, turn_id="t1",
                )
                assert adapted.payload["reasoning_effort"] == "low"
                assert adapted.payload["messages"] == request["messages"]
                assert request["reasoning_effort"] == requested, "original_request_mutated"
                client.chat.completions.create(**adapted.payload)
    assert len(strict_wire) == 9 and all(p["reasoning_effort"] == "low" for p in strict_wire)
    assert len(fake.calls) == calls_before, "strict_trivial_turn_called_jev"

    # The ordinary OpenRouter chat route still gets its existing minimal floor.
    begin_turn("ordinary-openrouter", "t1", text="hi")
    ordinary = apply_llm_request_middleware(
        {"model": "deepseek/deepseek-v4.1-flash", "messages": [{"role": "user", "content": "hi"}],
         "reasoning_effort": "high"}, provider="openrouter", model="deepseek/deepseek-v4.1-flash",
        api_mode="chat_completions", session_id="ordinary-openrouter", task_id="ordinary-openrouter", turn_id="t1",
    )
    assert ordinary.payload["reasoning_effort"] == "minimal"
    assert len(fake.calls) == calls_before

    # Native Anthropic request: the real host composes the memory/plugin sidecar into the user
    # content and converts an OpenAI tool message into a tool_result user block. Jev must see
    # only the clean captured message on both the first and the after-tool request.
    sidecar_content = compose_user_api_content(
        "synthetic", "SYNTHETIC_MEMORY", "SYNTHETIC_PLUGIN")
    assert sidecar_content is not None and "<memory-context>" in sidecar_content
    assert "SYNTHETIC_PLUGIN" in sidecar_content
    _, converted = convert_messages_to_anthropic([
        {"role": "user", "content": sidecar_content},
        {"role": "assistant", "content": "SYNTHETIC_ASSISTANT",
         "tool_calls": [{"id": "call_1", "type": "function",
                         "function": {"name": "shell", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "call_1", "content": "SYNTHETIC_TOOL_BODY"},
    ])
    assert converted[-1]["role"] == "user" and "SYNTHETIC_TOOL_BODY" in json.dumps(converted[-1])
    begin_turn("s-anthropic", "t1")
    anthropic_request = {
        "model": "claude-opus-5-5", "messages": converted[:1],
        "max_tokens": 32, "thinking": {"type": "adaptive"}, "output_config": {"effort": "high"},
    }
    result = apply_llm_request_middleware(
        anthropic_request, provider="anthropic", model="claude-opus-5-5",
        api_mode="anthropic_messages", session_id="s-anthropic", task_id="s-anthropic", turn_id="t1",
    )
    assert result.changed and result.payload["output_config"]["effort"] == "low"
    assert result.payload["messages"] == anthropic_request["messages"], "provider_messages_changed"
    invoke_hook("post_tool_call", tool_name="shell", result='{"ok": true}',
                session_id="s-anthropic", task_id="s-anthropic")
    after_tool = dict(anthropic_request, messages=converted)
    apply_llm_request_middleware(
        after_tool, provider="anthropic", model="claude-opus-5-5",
        api_mode="anthropic_messages", session_id="s-anthropic", task_id="s-anthropic", turn_id="t1",
    )
    anthropic_calls = [call for call in fake.calls if call[0]["current_request"] == task_text]
    outbound = json.dumps(fake.calls)
    for marker in ("SYNTHETIC_MEMORY", "SYNTHETIC_PLUGIN", "memory-context", "SYNTHETIC_TOOL_BODY",
                   "SYNTHETIC_ASSISTANT", "SYNTHETIC_HISTORY"):
        assert marker not in outbound, "jev_payload_leaked_" + marker.lower()
    assert len(anthropic_calls) == len(fake.calls)
    invoke_hook("post_llm_call", session_id="s-anthropic", task_id="s-anthropic", turn_id="t1",
                user_message=task_text, assistant_response="SYNTHETIC_ASSISTANT",
                conversation_history=[], model="claude-opus-5-5", platform="cli")
    assert controller._captured_task(session_id="s-anthropic", task_id="s-anthropic", turn_id="t1") is None
    anthropic_wire = []

    def capture_anthropic(request):
        anthropic_wire.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "msg_synthetic", "type": "message", "role": "assistant",
                                          "model": "claude-opus-5-5", "content": [], "stop_reason": "end_turn",
                                          "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    with anthropic.Anthropic(api_key="test-key", base_url="https://example.invalid",
                             http_client=httpx.Client(transport=httpx.MockTransport(capture_anthropic))) as client:
        client.messages.create(**result.payload)
    assert anthropic_wire[0]["output_config"]["effort"] == "low"
    assert anthropic_wire[0]["thinking"] == {"type": "adaptive"}
    assert "reasoning_effort" not in anthropic_wire[0]
    print(json.dumps({"provenance": provenance, "codex_wire_effort": codex_wire[0]["reasoning"]["effort"],
                      "anthropic_wire_effort": anthropic_wire[0]["output_config"]["effort"],
                      "jev_calls": len(fake.calls), "command_registered": True}))
    manager.unload()


class _ReachedPostProvenance(Exception):
    """The local origin probe stops before SDK or provider behavior."""


def _probe_origin(plugin_dir: Path, manifest_dir: Path, entrypoint: Path,
                  controller_file: Path) -> tuple[bool, str]:
    module_name = "hermes_plugins.synthetic_test.hermes_switchyard.reasoning_effort_adapter"

    class SyntheticController:
        def middleware(self, request):
            return request

    SyntheticController.__module__ = module_name
    controller = SyntheticController()
    loaded = SimpleNamespace(enabled=True, error=None,
        manifest=SimpleNamespace(path=str(manifest_dir)),
        module=SimpleNamespace(__file__=str(entrypoint)))
    manager = SimpleNamespace(discover_and_load=lambda force: None,
        _plugins={"hermes-switchyard": loaded}, _middleware={"llm_request": [controller.middleware]})
    output = io.StringIO()
    with (patch.dict(sys.modules, {module_name: SimpleNamespace(__file__=str(controller_file))}),
          patch("hermes_cli.plugins.get_plugin_manager", return_value=manager),
          patch("hermes_cli.commands.resolve_command", side_effect=_ReachedPostProvenance),
          redirect_stdout(output)):
        try:
            _isolated_replay(plugin_dir)
        except _ReachedPostProvenance:
            return True, output.getvalue()
        except AssertionError:
            return False, output.getvalue()
    raise AssertionError("origin probe reached SDK path unexpectedly")


class InstalledHermesEffortIntegrationTests(unittest.TestCase):
    def test_installed_controller_accepts_canonical_alias_root(self):
        """An alias spelling of the same install must not reject its exact module."""
        with tempfile.TemporaryDirectory(prefix="switchyard-origin-") as temporary:
            base = Path(temporary)
            (base / "alias-parent").mkdir()
            installed = base / "installed"
            adapter = installed / "hermes_switchyard" / "reasoning_effort_adapter.py"
            adapter.parent.mkdir(parents=True)
            adapter.write_text("# synthetic installed module\n", encoding="utf-8")
            (installed / "__init__.py").write_text("# synthetic entrypoint\n", encoding="utf-8")
            alias = base / "alias-parent" / ".." / "installed"
            self.assertEqual(alias.resolve(strict=True), installed.resolve(strict=True))
            self.assertFalse(adapter.resolve(strict=True).is_relative_to(alias))
            accepted, output = _probe_origin(alias, alias, alias / "__init__.py", adapter)
            self.assertTrue(accepted)
            self.assertTrue(output.strip())
            receipt = json.loads(output.splitlines()[-1])
            self.assertEqual(set(receipt), {"provenance"})
            self.assertEqual(receipt["provenance"]["controller_origin"], "installed")
            self.assertIs(receipt["provenance"]["raw_root_comparison"], False)
            self.assertIs(receipt["provenance"]["canonical_root_comparison"], True)
            self.assertNotIn(str(base), output)

    def test_installed_source_rejects_wrong_exact_files_and_checkout_with_sanitized_receipt(self):
        """Matching bytes or a directory prefix cannot establish runtime origin."""
        checkout = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="switchyard-origin-") as temporary:
            base = Path(temporary)
            installed = base / "installed"
            adapter = installed / "hermes_switchyard" / "reasoning_effort_adapter.py"
            adapter.parent.mkdir(parents=True)
            source_adapter = checkout / "hermes_switchyard" / "reasoning_effort_adapter.py"
            shutil.copyfile(source_adapter, adapter)
            same_bytes_elsewhere = adapter.with_name("other_adapter.py")
            shutil.copyfile(adapter, same_bytes_elsewhere)
            installed_entry = installed / "__init__.py"
            shutil.copyfile(checkout / "__init__.py", installed_entry)
            wrong_entry = installed / "other_entry.py"
            shutil.copyfile(installed_entry, wrong_entry)
            other_root = base / "other"
            other_root.mkdir()
            cases = (
                ("wrong_adapter", installed, installed_entry, same_bytes_elsewhere, "controller_origin", "other"),
                ("checkout_adapter", installed, installed_entry, source_adapter, "controller_origin", "checkout"),
                ("wrong_manifest", other_root, installed_entry, adapter, "manifest_origin", "other"),
                ("wrong_entrypoint", installed, wrong_entry, adapter, "entrypoint_origin", "other"),
                ("checkout_entrypoint", installed, checkout / "__init__.py", adapter, "entrypoint_origin", "checkout"),
            )
            for label, manifest, entrypoint, controller, field, expected in cases:
                with self.subTest(label=label):
                    accepted, output = _probe_origin(installed, manifest, entrypoint, controller)
                    self.assertFalse(accepted, label)
                    self.assertTrue(output.strip(), label)
                    receipt = json.loads(output.splitlines()[-1])
                    self.assertEqual(set(receipt), {"provenance"})
                    provenance = receipt["provenance"]
                    self.assertEqual(provenance[field], expected)
                    for origin in ("manifest_origin", "entrypoint_origin", "controller_origin"):
                        self.assertIn(provenance[origin], ("installed", "checkout", "other"))
                    self.assertIs(provenance["install_outside_checkout"], True)
                    self.assertIs(type(provenance["raw_root_comparison"]), bool)
                    self.assertIs(type(provenance["canonical_root_comparison"]), bool)
                    self.assertNotIn(str(base), output)
                    self.assertNotIn(str(checkout), output)

    def test_parent_rejects_missing_or_noninstalled_origin_receipt(self):
        """A synthetic success-shaped SDK receipt cannot certify a checkout import."""
        original_run = subprocess.run
        for controller_origin in (None, "checkout", "other"):
            with self.subTest(controller_origin=controller_origin):
                def intercept(command, *args, **kwargs):
                    if command[:3] == [sys.executable, "-m", "tests.test_reasoning_effort_hermes_integration"]:
                        receipt = {"codex_wire_effort": "low",
                                   "anthropic_wire_effort": "low", "jev_calls": 2, "command_registered": True}
                        if controller_origin is not None:
                            receipt["provenance"] = {"manifest_origin": "installed", "entrypoint_origin": "installed",
                                "controller_origin": controller_origin, "raw_root_comparison": False,
                                "canonical_root_comparison": False, "install_outside_checkout": True}
                        return subprocess.CompletedProcess(command, 0, json.dumps(receipt) + "\n", "")
                    return original_run(command, *args, **kwargs)

                with patch.object(subprocess, "run", side_effect=intercept):
                    with self.assertRaises(AssertionError):
                        self.test_fresh_plugin_manager_middleware_and_sdk_wire()

    def test_parent_failure_reports_only_closed_set_origin_not_child_output(self):
        """Untrusted child stderr/stdout must not publish paths or synthetic secrets."""
        original_run = subprocess.run
        private_path = str(Path(tempfile.gettempdir()) / "private-origin-marker")
        marker = "synthetic-secret-marker"
        provenance = {"manifest_origin": "installed", "entrypoint_origin": "installed",
            "controller_origin": "checkout", "raw_root_comparison": False,
            "canonical_root_comparison": False, "install_outside_checkout": True}

        def intercept(command, *args, **kwargs):
            if command[:3] == [sys.executable, "-m", "tests.test_reasoning_effort_hermes_integration"]:
                stdout = marker + " " + private_path + "\n" + json.dumps({"provenance": provenance}) + "\n"
                return subprocess.CompletedProcess(command, 1, stdout, "trace " + private_path)
            return original_run(command, *args, **kwargs)

        with patch.object(subprocess, "run", side_effect=intercept):
            with self.assertRaises(AssertionError) as raised:
                self.test_fresh_plugin_manager_middleware_and_sdk_wire()
        failure = str(raised.exception)
        self.assertIn("controller_origin", failure)
        self.assertIn("checkout", failure)
        self.assertNotIn(private_path, failure)
        self.assertNotIn(marker, failure)

    def test_parent_rejects_provenance_fields_outside_closed_set(self):
        """A success-shaped receipt must not carry an extra path-valued origin field."""
        original_run = subprocess.run
        private_path = str(Path(tempfile.gettempdir()) / "private-origin-marker")

        def intercept(command, *args, **kwargs):
            if command[:3] == [sys.executable, "-m", "tests.test_reasoning_effort_hermes_integration"]:
                provenance = {"manifest_origin": "installed", "entrypoint_origin": "installed",
                    "controller_origin": "installed", "raw_root_comparison": True,
                    "canonical_root_comparison": True, "install_outside_checkout": True,
                    "unexpected_path": private_path}
                receipt = {"provenance": provenance, "codex_wire_effort": "low",
                    "anthropic_wire_effort": "low", "jev_calls": 2, "command_registered": True}
                return subprocess.CompletedProcess(command, 0, json.dumps(receipt) + "\n", "")
            return original_run(command, *args, **kwargs)

        with patch.object(subprocess, "run", side_effect=intercept):
            with self.assertRaises(AssertionError) as raised:
                self.test_fresh_plugin_manager_middleware_and_sdk_wire()
        self.assertNotIn(private_path, str(raised.exception))

    def test_sdk_wire_child_keeps_only_actual_windows_systemroot(self):
        """Inspect the installed-child launch without running SDKs or a provider."""
        parent = {"PATH": "synthetic-path", "PYTHONPATH": "synthetic-pythonpath",
                  "TMPDIR": "synthetic-temp", "LANG": "synthetic-lang",
                  "sYsTeMrOoT": "synthetic-systemroot", "HOME": "private-parent-home",
                  "HERMES_HOME": "private-parent-hermes", "OPENROUTER_API_KEY": "synthetic-provider",
                  "TYPESAFE_API_KEY": "synthetic-other-provider", "ANTHROPIC_API_KEY": "synthetic-anthropic",
                  "HERMES_SHARED_AUTH_DIR": "private-parent-shared-auth", "GITHUB_TOKEN": "synthetic-github",
                  "GH_TOKEN": "synthetic-gh", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "synthetic-oidc"}
        windows_os = SimpleNamespace(name="nt", environ=parent)
        original_run = subprocess.run
        captured = {}

        class ChildEnvCaptured(Exception):
            pass

        def intercept(command, *args, **kwargs):
            if command[:3] == [sys.executable, "-m", "tests.test_reasoning_effort_hermes_integration"]:
                captured.update(kwargs)
                raise ChildEnvCaptured()
            return original_run(command, *args, **kwargs)

        with (patch.object(replay, "os", windows_os),
              patch.object(subprocess, "run", side_effect=intercept)):
            with self.assertRaises(ChildEnvCaptured):
                self.test_fresh_plugin_manager_middleware_and_sdk_wire()
        env = captured["env"]
        self.assertIn("sYsTeMrOoT", env)
        self.assertEqual(env["sYsTeMrOoT"], parent["sYsTeMrOoT"])
        self.assertEqual(set(env), {"PATH", "PYTHONPATH", "TMPDIR", "LANG", "sYsTeMrOoT",
            "HOME", "HERMES_HOME", "HERMES_BUNDLED_PLUGINS"})
        self.assertNotEqual(env["HOME"], parent["HOME"])
        self.assertNotEqual(env["HERMES_HOME"], parent["HERMES_HOME"])

    def test_fresh_plugin_manager_middleware_and_sdk_wire(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory(prefix="switchyard-effort-") as temporary:
            workspace = Path(temporary)
            home = workspace / "home"
            plugin = home / "plugins" / "hermes-switchyard"
            for relative in RELEASE_FILES:
                source = root / relative
                self.assertTrue(source.is_file() and not source.is_symlink(), relative)
                target = plugin / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
            (home / "config.yaml").write_text(
                "plugins:\n  enabled: [hermes-switchyard]\n  entries:\n    hermes-switchyard:\n"
                "      settings:\n        automatic_skill_recommendation: false\n"
                "        jev_provider: openrouter\n", encoding="utf-8",
            )
            bundled = workspace / "bundled"
            bundled.mkdir()
            env = replay._sparse_child_env(("PATH", "PYTHONPATH", "TMPDIR", "LANG"))
            env.update({"HOME": str(workspace), "HERMES_HOME": str(home),
                        "HERMES_BUNDLED_PLUGINS": str(bundled)})
            result = subprocess.run(
                [sys.executable, "-m", "tests.test_reasoning_effort_hermes_integration", "--child", str(plugin)],
                cwd=root, env=env, text=True, capture_output=True, timeout=90,
            )
            provenance = _closed_origin_receipt(result.stdout)
            failure = ("installed_child_failed: " + json.dumps({"provenance": provenance})
                       if provenance is not None else "installed_child_failed_without_provenance_receipt")
            self.assertEqual(result.returncode, 0, failure)
            self.assertIsNotNone(provenance, "installed_origin_receipt_missing")
            assert provenance is not None
            proof = json.loads(result.stdout.splitlines()[-1])
            for origin in ("manifest_origin", "entrypoint_origin", "controller_origin"):
                self.assertEqual(provenance[origin], "installed")
            self.assertIs(provenance["install_outside_checkout"], True)
            self.assertIs(provenance["canonical_root_comparison"], True)
            self.assertTrue(set(proof["provenance"]) == {
                "manifest_origin", "entrypoint_origin", "controller_origin", "raw_root_comparison",
                "canonical_root_comparison", "install_outside_checkout",
            }, "unexpected_provenance_fields")
            self.assertTrue(type(proof["provenance"].get("raw_root_comparison")) is bool,
                            "raw_root_comparison_not_boolean")
            self.assertTrue(isinstance(proof, dict) and set(proof) == {
                "provenance", "codex_wire_effort", "anthropic_wire_effort", "jev_calls", "command_registered",
            }, "unexpected_child_receipt_fields")
            self.assertTrue(proof.get("codex_wire_effort") == "low", "codex_wire_effort_mismatch")
            self.assertTrue(proof.get("anthropic_wire_effort") == "low", "anthropic_wire_effort_mismatch")
            # Codex first turn, the /reasoning medium re-decision, and the Anthropic turn.
            self.assertTrue(proof.get("jev_calls") == 3, "jev_calls_mismatch")
            self.assertIs(proof.get("command_registered"), True)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--child":
        _isolated_replay(Path(sys.argv[2]))
    else:
        unittest.main()
