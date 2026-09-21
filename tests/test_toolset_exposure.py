"""Toolset exposure contracts: a registered tool is not necessarily callable in a session.

Part one drives the exposure report against a stand-in Hermes so every classification rule is
deterministic and runs anywhere. Part two loads the plugin through Hermes' real loader and real
command-line entry point in a disposable Hermes home, then compares the plugin's answers with
Hermes' own catalog builder. Part two skips, with the reason, when no Hermes runtime can be
imported; set SWITCHYARD_REQUIRE_HERMES=1 to make a missing runtime a failure instead.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import hermes_switchyard
from hermes_switchyard import COMPUTER_USE_TOOLSET, PLUGIN_TOOLSET, TOOL_TOOLSETS

ROOT = Path(__file__).resolve().parents[1]
PROBE = Path(__file__).with_name("hermes_exposure_probe.py")
PLACEHOLDER_CREDENTIAL = "offline-only-placeholder"


def _manifest_tools() -> set[str]:
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    block = re.search(r"(?ms)^provides_tools:\s*\n((?:[ \t]+-[^\n]*\n?)+)", text)
    assert block is not None, "plugin.yaml declares no provides_tools list"
    return {line.strip()[1:].strip() for line in block.group(1).splitlines() if line.strip()}


def _manifest_version() -> str:
    text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
    match = re.search(r"(?m)^version:\s*(\S+)", text)
    assert match is not None, "plugin.yaml declares no version"
    return match.group(1)


class _StandInHermes:
    """A minimal stand-in for the Hermes registry and catalog builder.

    It models the contract the exposure report relies on: the registry holds the entry a
    registration produced, a toolset resolves to the tools registered under it, and a session
    catalog is the union of the selected toolsets minus tools whose availability check fails.
    """

    STATIC_TOOLSETS = {"terminal": ["terminal", "process_manage"], "computer_use": ["computer_use"]}

    def __init__(self):
        self.entries = {}
        self.default_selection = ("platform_default", ["terminal", COMPUTER_USE_TOOLSET, PLUGIN_TOOLSET])
        self.disabled = []
        self.hidden = set()
        self.reject_tool_search_keyword = False
        self.catalog_calls = []

    def accept(self, name, toolset, handler, check_fn):
        self.entries[name] = SimpleNamespace(name=name, toolset=toolset, handler=handler, check_fn=check_fn)

    def _toolset_tools(self, toolset):
        registered = {name for name, entry in self.entries.items() if entry.toolset == toolset}
        return sorted(set(self.STATIC_TOOLSETS.get(toolset, [])) | registered)

    def _known(self, toolset):
        return toolset in self.STATIC_TOOLSETS or any(entry.toolset == toolset for entry in self.entries.values())

    def get_tool_definitions(self, enabled_toolsets=None, disabled_toolsets=None, quiet_mode=False, **keywords):
        if keywords.get("skip_tool_search_assembly") and self.reject_tool_search_keyword:
            raise TypeError("get_tool_definitions() got an unexpected keyword argument 'skip_tool_search_assembly'")
        self.catalog_calls.append({"enabled": list(enabled_toolsets or []), "disabled": list(disabled_toolsets or [])})
        names = set()
        for toolset in enabled_toolsets or []:
            names.update(self._toolset_tools(toolset))
        for toolset in disabled_toolsets or []:
            names.difference_update(self._toolset_tools(toolset))
        definitions = []
        for name in sorted(names):
            if name in self.hidden or not self._passes_check(self.entries.get(name)):
                continue
            definitions.append({"type": "function", "function": {"name": name}})
        return definitions

    @staticmethod
    def _passes_check(entry):
        """Hermes hides a tool whose availability check is false or raises."""
        if entry is None or entry.check_fn is None:
            return True
        try:
            return bool(entry.check_fn())
        except Exception:  # noqa: BLE001 -- mirrors Hermes: a raising check means unavailable
            return False

    def seams(self):
        return SimpleNamespace(
            registry=SimpleNamespace(get_entry=lambda name: self.entries.get(name)),
            get_tool_definitions=self.get_tool_definitions,
            resolve_toolset=self._toolset_tools,
            validate_toolset=self._known,
            default_selection=lambda: self.default_selection,
            disabled_toolsets=lambda: list(self.disabled),
        )


class _PluginContext:
    """Plugin context whose registrations land in the stand-in registry."""

    def __init__(self, stand_in, settings=None):
        self.stand_in = stand_in
        self.settings = dict(settings or {})
        self.toolsets = {}

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    def register_tool(self, *, name, toolset, schema, handler, check_fn=None, **_kwargs):
        self.toolsets[name] = toolset
        self.stand_in.accept(name, toolset, handler, check_fn)

    def register_auxiliary_task(self, *_args, **_kwargs):
        pass

    def register_skill(self, *_args, **_kwargs):
        pass

    def register_hook(self, *_args, **_kwargs):
        pass


class ToolsetExposureReportTests(unittest.TestCase):
    """The report must never call a tool available that the session catalog omits."""

    def setUp(self):
        hermes_switchyard.reset_runtime_status()
        self.addCleanup(hermes_switchyard.reset_runtime_status)
        secret_stub = mock.patch.object(hermes_switchyard, "_secret", return_value="")
        secret_stub.start()
        self.addCleanup(secret_stub.stop)
        self.hermes = _StandInHermes()
        self.context = _PluginContext(self.hermes)
        hermes_switchyard.register(self.context)

    def report(self, pin=None):
        return hermes_switchyard._tool_exposure_report(pin, seams=self.hermes.seams())

    def status(self, report, *, credential=True):
        return hermes_switchyard._overall_status({"typesafe": credential, "openrouter": False}, report)

    def test_registered_tools_match_the_manifest_and_the_documented_toolsets(self):
        self.assertEqual(set(self.context.toolsets), _manifest_tools())
        self.assertEqual(self.context.toolsets, TOOL_TOOLSETS)
        self.assertEqual(TOOL_TOOLSETS["jev_computer_use"], COMPUTER_USE_TOOLSET)
        for name, toolset in TOOL_TOOLSETS.items():
            if name != "jev_computer_use":
                self.assertEqual(toolset, PLUGIN_TOOLSET, name)

    def test_selecting_both_toolsets_makes_every_tool_registered_and_callable(self):
        report = self.report(f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}")
        self.assertEqual(report["evidence"], "hermes_tool_definitions")
        self.assertIsNone(report["unavailable_reason"])
        for name, state in report["tools"].items():
            self.assertIs(state["registered"], True, name)
            self.assertIs(state["callable"], True, name)
            self.assertIsNone(state["reason"], name)
            self.assertEqual(state["registry_toolset"], TOOL_TOOLSETS[name], name)
        self.assertEqual(self.status(report), "ready")

    def test_a_tool_is_callable_exactly_when_its_toolset_is_selected(self):
        for pin in ("terminal", COMPUTER_USE_TOOLSET, PLUGIN_TOOLSET, f"terminal,{COMPUTER_USE_TOOLSET}"):
            selected = set(pin.split(","))
            with self.subTest(pin=pin):
                report = self.report(pin)
                for name, state in report["tools"].items():
                    reached = TOOL_TOOLSETS[name] in selected
                    self.assertIs(state["registered"], True, name)
                    self.assertIs(state["callable"], reached, name)
                    self.assertEqual(state["reason"], None if reached else "toolset_not_selected", name)

    def test_registered_but_not_callable_is_never_ready(self):
        report = self.report("terminal")
        self.assertTrue(all(state["registered"] is True for state in report["tools"].values()))
        self.assertTrue(all(state["callable"] is False for state in report["tools"].values()))
        self.assertEqual(self.status(report), "tools_not_callable")
        self.assertNotEqual(self.status(report, credential=False), "ready")

    def test_registration_failure_is_reported_apart_from_exposure_failure(self):
        exposure_only = self.report("terminal")
        self.assertEqual(self.status(exposure_only), "tools_not_callable")

        del self.hermes.entries["jev_computer_use"]
        missing = self.report(f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}")
        state = missing["tools"]["jev_computer_use"]
        self.assertIs(state["registered"], False)
        self.assertEqual(state["reason"], "not_registered")
        self.assertEqual(self.status(missing), "tools_not_registered")

        self.hermes.accept("jev_computer_use", "another_toolset", lambda *_a, **_k: "{}", None)
        owned = self.report(f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET},another_toolset")
        state = owned["tools"]["jev_computer_use"]
        self.assertIs(state["registered"], False)
        self.assertEqual(state["reason"], "owned_by_another_registration")
        self.assertEqual(state["registry_toolset"], "another_toolset")
        self.assertEqual(self.status(owned), "tools_not_registered")
        self.assertNotEqual(self.status(exposure_only), self.status(owned))

    def test_a_process_that_never_registered_does_not_claim_the_registry_entry(self):
        hermes_switchyard.reset_runtime_status()
        report = self.report(f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}")
        for name, state in report["tools"].items():
            self.assertIs(state["registered"], False, name)
            self.assertEqual(state["reason"], "owned_by_another_registration", name)

    def test_a_failed_availability_check_is_reported_even_when_the_toolset_is_selected(self):
        pin = f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}"
        for check in (lambda: False, mock.Mock(side_effect=RuntimeError("probe failed"))):
            with self.subTest(check=check):
                self.hermes.entries["jev_computer_use"].check_fn = check
                state = self.report(pin)["tools"]["jev_computer_use"]
                self.assertIs(state["registered"], True)
                self.assertIs(state["callable"], False)
                self.assertEqual(state["reason"], "availability_check_failed")

    def test_an_unexplained_absence_is_reported_rather_than_hidden(self):
        self.hermes.hidden.add("jev_assess")
        report = self.report(f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}")
        state = report["tools"]["jev_assess"]
        self.assertIs(state["callable"], False)
        self.assertEqual(state["reason"], "not_in_catalog")
        self.assertEqual(self.status(report), "tools_not_callable")

    def test_status_is_ready_only_when_every_tool_is_callable_and_a_credential_exists(self):
        healthy = self.report(f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}")
        self.assertEqual(self.status(healthy, credential=True), "ready")
        self.assertEqual(self.status(healthy, credential=False), "credential_required")
        # A tool problem outranks a missing credential: fixing the credential alone would not help.
        self.assertEqual(self.status(self.report("terminal"), credential=False), "tools_not_callable")

    def test_missing_evidence_is_unverified_and_never_ready(self):
        no_registry = SimpleNamespace(
            registry=None, get_tool_definitions=None, resolve_toolset=None, validate_toolset=None, default_selection=None
        )
        report = hermes_switchyard._tool_exposure_report(None, seams=no_registry)
        self.assertEqual(report["evidence"], "unavailable")
        self.assertEqual(report["unavailable_reason"], "hermes_registry_unavailable")
        for state in report["tools"].values():
            self.assertIsNone(state["registered"])
            self.assertIsNone(state["callable"])
        self.assertEqual(self.status(report, credential=True), "exposure_unverified")
        self.assertEqual(self.status(report, credential=False), "credential_required")

        seams = self.hermes.seams()
        seams.get_tool_definitions = None
        registry_only = hermes_switchyard._tool_exposure_report("terminal", seams=seams)
        self.assertEqual(registry_only["evidence"], "registry_only")
        self.assertEqual(registry_only["unavailable_reason"], "hermes_catalog_unavailable")
        self.assertTrue(all(state["registered"] is True for state in registry_only["tools"].values()))
        self.assertTrue(all(state["callable"] is None for state in registry_only["tools"].values()))
        self.assertEqual(self.status(registry_only), "exposure_unverified")

        seams = self.hermes.seams()
        seams.default_selection = mock.Mock(side_effect=RuntimeError("resolver moved"))
        self.assertEqual(
            hermes_switchyard._tool_exposure_report(None, seams=seams)["unavailable_reason"], "selection_unresolved"
        )
        seams = self.hermes.seams()
        seams.get_tool_definitions = mock.Mock(side_effect=RuntimeError("builder failed"))
        failed = hermes_switchyard._tool_exposure_report("terminal", seams=seams)
        self.assertEqual(failed["unavailable_reason"], "catalog_query_failed")
        self.assertEqual(self.status(failed), "exposure_unverified")

    def test_a_missing_hermes_runtime_degrades_to_unverified(self):
        blocked = {name: None for name in ("tools", "tools.registry", "model_tools", "toolsets")}
        with mock.patch.dict(sys.modules, blocked):
            report = hermes_switchyard._tool_exposure_report("terminal")
        self.assertEqual(report["evidence"], "unavailable")
        self.assertEqual(report["unavailable_reason"], "hermes_registry_unavailable")

    def test_a_hermes_without_the_tool_search_keyword_still_answers(self):
        self.hermes.reject_tool_search_keyword = True
        report = self.report(f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}")
        self.assertEqual(report["evidence"], "hermes_tool_definitions")
        self.assertTrue(all(state["callable"] is True for state in report["tools"].values()))

    def test_explicit_pins_are_parsed_and_typos_are_named(self):
        report = self.report(" computer_use , terminal ,, ")
        self.assertEqual(report["selection"]["source"], "explicit_toolsets")
        self.assertEqual(report["selection"]["enabled_toolsets"], ["computer_use", "terminal"])
        self.assertEqual(report["selection"]["unknown_toolsets"], [])
        typo = self.report("computer-use")
        self.assertEqual(typo["selection"]["unknown_toolsets"], ["computer-use"])
        self.assertTrue(all(state["callable"] is False for state in typo["tools"].values()))

    def test_without_a_pin_the_hermes_default_selection_is_evaluated_and_labelled(self):
        for blank in (None, "", "   "):
            report = self.report(blank)
            self.assertEqual(report["selection"]["source"], "platform_default")
            self.assertEqual(report["selection"]["enabled_toolsets"], self.hermes.default_selection[1])
        self.hermes.default_selection = ("coding_posture", ["terminal"])
        report = self.report(None)
        self.assertEqual(report["selection"]["source"], "coding_posture")
        self.assertTrue(all(state["callable"] is False for state in report["tools"].values()))


    def test_a_globally_disabled_toolset_is_not_callable_even_when_it_is_pinned(self):
        self.hermes.disabled = [COMPUTER_USE_TOOLSET]
        report = self.report(f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}")
        state = report["tools"]["jev_computer_use"]
        self.assertIs(state["registered"], True)
        self.assertIs(state["callable"], False)
        self.assertEqual(state["reason"], "toolset_disabled")
        for name, other in report["tools"].items():
            if name != "jev_computer_use":
                self.assertIs(other["callable"], True, name)
        self.assertEqual(report["selection"]["disabled_toolsets"], [COMPUTER_USE_TOOLSET])
        self.assertEqual(self.status(report), "tools_not_callable")

    def test_suppression_outranks_a_missing_selection(self):
        # Adding the toolset to the pin cannot help while the configured list names it.
        self.hermes.disabled = [PLUGIN_TOOLSET]
        report = self.report("terminal")
        self.assertEqual(report["tools"]["jev_assess"]["reason"], "toolset_disabled")
        self.assertEqual(report["tools"]["jev_computer_use"]["reason"], "toolset_not_selected")

    def test_the_default_selection_honors_the_disabled_list_too(self):
        self.hermes.disabled = [COMPUTER_USE_TOOLSET]
        state = self.report(None)["tools"]["jev_computer_use"]
        self.assertIs(state["callable"], False)
        self.assertEqual(state["reason"], "toolset_disabled")

    def test_the_catalog_builder_receives_the_configured_disabled_list(self):
        self.hermes.disabled = ["memory", COMPUTER_USE_TOOLSET]
        self.report("terminal")
        self.assertEqual(self.hermes.catalog_calls[-1]["disabled"], ["memory", COMPUTER_USE_TOOLSET])

    def test_an_unreadable_disabled_list_is_never_assumed_empty(self):
        seams = self.hermes.seams()
        seams.disabled_toolsets = mock.Mock(side_effect=RuntimeError("config unreadable"))
        report = hermes_switchyard._tool_exposure_report("terminal", seams=seams)
        self.assertEqual(report["unavailable_reason"], "selection_unresolved")
        self.assertTrue(all(state["callable"] is None for state in report["tools"].values()))
        self.assertEqual(self.status(report), "exposure_unverified")


class StatusCommandTests(unittest.TestCase):
    """The operator-facing command must carry the registered-versus-callable distinction."""

    def setUp(self):
        hermes_switchyard.reset_runtime_status()
        self.addCleanup(hermes_switchyard.reset_runtime_status)
        self.hermes = _StandInHermes()
        with mock.patch.object(hermes_switchyard, "_secret", return_value=""):
            hermes_switchyard.register(_PluginContext(self.hermes))

    def run_status(self, *, json_output, toolsets=None, credential=True):
        args = SimpleNamespace(switchyard_command="status", json_output=json_output, toolsets=toolsets)
        stored_value = PLACEHOLDER_CREDENTIAL if credential else ""
        with mock.patch.object(hermes_switchyard, "_secret", return_value=stored_value), \
             mock.patch.object(hermes_switchyard, "_load_hermes_seams", return_value=self.hermes.seams()), \
             mock.patch.object(hermes_switchyard, "DecisionClient", side_effect=AssertionError("status must stay network-free")), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            code = hermes_switchyard._cli_handler(args)
        return code, stdout.getvalue()

    def test_json_status_reports_registration_and_exposure_per_tool(self):
        code, output = self.run_status(json_output=True, toolsets="terminal")
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["status"], "tools_not_callable")
        self.assertEqual(payload["plugin_version"], _manifest_version())
        exposure = payload["tool_exposure"]
        self.assertEqual(exposure["selection"]["enabled_toolsets"], ["terminal"])
        self.assertEqual(set(exposure["tools"]), set(TOOL_TOOLSETS))
        for name, state in exposure["tools"].items():
            self.assertIs(state["registered"], True, name)
            self.assertIs(state["callable"], False, name)
            self.assertEqual(state["expected_toolset"], TOOL_TOOLSETS[name], name)

    def test_json_status_is_ready_for_the_documented_composition(self):
        code, output = self.run_status(json_output=True, toolsets=f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["status"], "ready")

    def test_text_status_names_each_failing_tool_and_its_reason(self):
        code, output = self.run_status(json_output=False, toolsets="terminal")
        self.assertEqual(code, 0)
        self.assertNotIn("Hermes Switchyard: ready", output)
        for name in TOOL_TOOLSETS:
            self.assertRegex(output, rf"{name}: registered but NOT in the session's callable catalog \(toolset_not_selected\)")
        self.assertIn(COMPUTER_USE_TOOLSET, output)
        self.assertIn(PLUGIN_TOOLSET, output)

    def test_text_status_keeps_the_credential_hint_alongside_a_tool_problem(self):
        _code, output = self.run_status(json_output=False, toolsets="terminal", credential=False)
        self.assertIn("tools_not_callable", output)
        self.assertIn("hermes switchyard setup --provider typesafe", output)

    def test_text_status_reports_registration_failures_distinctly(self):
        del self.hermes.entries["jev_assess"]
        _code, output = self.run_status(json_output=False, toolsets=PLUGIN_TOOLSET)
        self.assertIn("tools_not_registered", output)
        self.assertIn("jev_assess: NOT registered by this plugin (not_registered)", output)

    def test_a_fault_while_building_the_report_does_not_hide_the_rest_of_status(self):
        with mock.patch.object(hermes_switchyard, "_tool_exposure_report", side_effect=RuntimeError("bug")):
            code, output = self.run_status(json_output=True)
        self.assertEqual(code, 0)
        payload = json.loads(output)
        self.assertEqual(payload["tool_exposure"]["unavailable_reason"], "exposure_report_failed")
        self.assertEqual(payload["status"], "exposure_unverified")
        self.assertIs(payload["network"], False)

    def test_the_status_command_accepts_a_toolsets_pin_like_hermes_chat(self):
        parser = argparse.ArgumentParser()
        hermes_switchyard._setup_cli(parser)
        pinned = parser.parse_args(["status", "--json", "--toolsets", "computer_use,hermes_switchyard"])
        self.assertEqual(pinned.toolsets, "computer_use,hermes_switchyard")
        self.assertTrue(pinned.json_output)
        self.assertIsNone(parser.parse_args(["status"]).toolsets)


    def register_with(self, settings):
        hermes_switchyard.reset_runtime_status()
        self.hermes = _StandInHermes()
        with mock.patch.object(hermes_switchyard, "_secret", return_value=""):
            hermes_switchyard.register(_PluginContext(self.hermes, settings))

    @staticmethod
    def keys(*present):
        """Return a stand-in for the secret lookup that only knows the named providers' keys."""
        def lookup(provider="auto"):
            order = {"typesafe": ("typesafe",), "openrouter": ("openrouter",), "auto": ("typesafe", "openrouter")}[provider]
            return PLACEHOLDER_CREDENTIAL if any(name in present for name in order) else ""
        return lookup

    def status_output(self, present, *, json_output=True):
        args = SimpleNamespace(
            switchyard_command="status", json_output=json_output, toolsets=f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}"
        )
        with mock.patch.object(hermes_switchyard, "_secret", side_effect=self.keys(*present)), \
             mock.patch.object(hermes_switchyard, "_load_hermes_seams", return_value=self.hermes.seams()), \
             mock.patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self.assertEqual(hermes_switchyard._cli_handler(args), 0)
        return json.loads(stdout.getvalue()) if json_output else stdout.getvalue()

    def test_readiness_follows_the_key_of_the_provider_the_route_uses(self):
        cases = (
            ({"jev_provider": "openrouter"}, ("typesafe",), "credential_required", "openrouter"),
            ({"jev_provider": "openrouter"}, ("openrouter",), "ready", "openrouter"),
            ({"jev_provider": "typesafe"}, ("openrouter",), "credential_required", "typesafe"),
            ({"jev_provider": "typesafe"}, ("typesafe",), "ready", "typesafe"),
            ({}, ("openrouter",), "ready", "openrouter"),
            ({}, ("typesafe", "openrouter"), "ready", "typesafe"),
            ({}, (), "credential_required", "openrouter"),
        )
        for settings, present, expected_status, expected_provider in cases:
            with self.subTest(settings=settings, present=present):
                self.register_with(settings)
                payload = self.status_output(present)
                self.assertEqual(payload["status"], expected_status)
                self.assertEqual(payload["effective_provider"], expected_provider)
                self.assertEqual(
                    payload["credential_presence"], {name: name in present for name in ("typesafe", "openrouter")}
                )

    def test_the_setup_hint_names_the_provider_whose_key_is_missing(self):
        self.register_with({"jev_provider": "openrouter"})
        text = self.status_output(("typesafe",), json_output=False)
        self.assertIn("credential_required", text)
        self.assertIn("hermes switchyard setup --provider openrouter", text)
        self.assertNotIn("Hermes Switchyard: ready", text)

    def test_with_no_key_at_all_the_setup_hint_keeps_the_default_provider(self):
        self.register_with({})
        self.assertIn("hermes switchyard setup --provider typesafe", self.status_output((), json_output=False))

    def test_an_invalid_route_leaves_the_effective_provider_unknown(self):
        self.register_with({"jev_provider": "not-a-provider"})
        payload = self.status_output(("typesafe",))
        self.assertIsNone(payload["effective_provider"])
        self.assertEqual(payload["status"], "tools_not_callable")
        state = payload["tool_exposure"]["tools"]["jev_assess"]
        self.assertEqual(state["reason"], "availability_check_failed")


def _hermes_environment(home: Path) -> dict[str, str]:
    """Return an environment that points Hermes at a disposable home and nothing else."""
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("HERMES_") and key not in {"OPENROUTER_API_KEY", "TYPESAFE_API_KEY", "PYTHONHOME"}
    }
    environment.update(
        HERMES_HOME=str(home),
        HERMES_BUNDLED_PLUGINS=str(home / "bundled-plugins"),
        HERMES_ENABLE_PROJECT_PLUGINS="0",
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
    )
    return environment


def _can_import_hermes(python: str, extra_environment: dict[str, str]) -> str | None:
    """Return None when the interpreter imports Hermes, otherwise a short reason."""
    with tempfile.TemporaryDirectory(prefix="switchyard-import-", ignore_cleanup_errors=True) as scratch:
        home = Path(scratch)
        environment = _hermes_environment(home)
        environment.update(extra_environment)
        try:
            completed = subprocess.run(
                [python, "-c", "import hermes_cli.main, model_tools, toolsets, tools.registry"],
                env=environment,
                cwd=scratch,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return f"{type(exc).__name__} while starting the interpreter"
    if completed.returncode != 0:
        lines = completed.stderr.strip().splitlines()
        return f"import failed ({lines[-1] if lines else 'no error output'})"
    return None


def _hermes_source_roots() -> list[Path]:
    """Return candidate Hermes source checkouts: an explicit one, then the default install location."""
    roots = []
    configured = os.environ.get("SWITCHYARD_HERMES_ROOT")
    if configured:
        roots.append(Path(configured))
    home = os.environ.get("HERMES_HOME")
    roots.append((Path(home) if home else Path.home() / ".hermes") / "hermes-agent")
    return roots


def _hermes_runner() -> tuple[str, dict[str, str]] | str:
    """Return (python, extra environment) that can import Hermes, or the reason none can."""
    reasons = []
    problem = _can_import_hermes(sys.executable, {})
    if problem is None:
        return sys.executable, {}
    reasons.append(f"the current interpreter cannot import it: {problem}")
    for root in _hermes_source_roots():
        if not root.is_dir():
            reasons.append("a candidate Hermes source checkout does not exist")
            continue
        for folder in (".venv", "venv"):
            for relative in ("bin/python", "Scripts/python.exe"):
                candidate = root / folder / relative
                if not candidate.is_file():
                    continue
                problem = _can_import_hermes(str(candidate), {"PYTHONPATH": str(root)})
                if problem is None:
                    return str(candidate), {"PYTHONPATH": str(root)}
                reasons.append(f"a Hermes checkout interpreter cannot import it: {problem}")
    reasons.append("set SWITCHYARD_HERMES_ROOT to a Hermes source checkout with its own virtual environment")
    return "; ".join(reasons)


class HermesRunnerDiscoveryTests(unittest.TestCase):
    def test_a_missing_runtime_yields_an_explicit_reason_not_a_silent_pass(self):
        with mock.patch(f"{__name__}._can_import_hermes", return_value="import failed (nothing installed)"), \
             mock.patch(f"{__name__}._hermes_source_roots", return_value=[]):
            outcome = _hermes_runner()
        self.assertIsInstance(outcome, str)
        self.assertIn("current interpreter", outcome)
        self.assertIn("SWITCHYARD_HERMES_ROOT", outcome)


class RealHermesExposureTests(unittest.TestCase):
    """Load the plugin through Hermes' real loader and CLI, then compare with Hermes' own catalog."""

    runner: tuple[str, dict[str, str]]

    @classmethod
    def setUpClass(cls):
        outcome = _hermes_runner()
        if isinstance(outcome, str):
            message = f"Hermes runtime is not importable, so the real-loader exposure checks did not run: {outcome}"
            if os.environ.get("SWITCHYARD_REQUIRE_HERMES") == "1":
                raise AssertionError(message)
            raise unittest.SkipTest(message)
        cls.runner = outcome

    def run_hermes(self, *, pin=None, catalog_pins=(), catalog_default=False, credential=False,
                   settings=None, extra_config="", shadows=()):
        """Run `hermes switchyard status --json` for real and return the plugin's and Hermes' answers."""
        python, extra_environment = self.runner
        with tempfile.TemporaryDirectory(prefix="switchyard-exposure-", ignore_cleanup_errors=True) as scratch:
            base = Path(scratch)
            home = base / "home"
            plugin = home / "plugins" / "hermes-switchyard"
            plugin.mkdir(parents=True)
            (home / "bundled-plugins").mkdir()
            for name in ("plugin.yaml", "__init__.py", "after-install.md"):
                shutil.copy2(ROOT / name, plugin / name)
            shutil.copytree(
                ROOT / "hermes_switchyard", plugin / "hermes_switchyard",
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
            config = "plugins:\n  enabled:\n    - hermes-switchyard\n"
            if settings:
                config += "  entries:\n    hermes-switchyard:\n      settings:\n"
                config += "".join(f"        {key}: {value}\n" for key, value in settings.items())
            (home / "config.yaml").write_text(config + extra_config, encoding="utf-8")
            argv = ["switchyard", "status", "--json"] + (["--toolsets", pin] if pin else [])
            scenario = {
                "argv": argv,
                "credential_present": credential,
                "shadow_registrations": [{"name": name, "toolset": toolset} for name, toolset in shadows],
                "catalog_selections": {pin_name: pin_name.split(",") for pin_name in catalog_pins},
                "catalog_default": catalog_default,
                "tool_names": sorted(TOOL_TOOLSETS),
                "result_path": str(base / "result.json"),
            }
            scenario_path = base / "scenario.json"
            scenario_path.write_text(json.dumps(scenario), encoding="utf-8")
            environment = _hermes_environment(home)
            environment.update(extra_environment)
            completed = subprocess.run(
                [python, str(PROBE), str(scenario_path)],
                env=environment, cwd=scratch, capture_output=True, encoding="utf-8", errors="replace",
                timeout=300,
            )
            if completed.returncode != 0:
                self.fail(f"probe failed with {completed.returncode}: {completed.stderr.strip()[-1500:]}")
            result = json.loads((base / "result.json").read_text(encoding="utf-8"))
        self.assertEqual(result["exit_code"], 0, result["stdout"])
        lines = result["stdout"].strip().splitlines()
        self.assertTrue(lines, "hermes switchyard status printed nothing")
        return SimpleNamespace(
            status=json.loads(lines[-1]),
            catalogs=result["catalogs"],
            registry=result["registry"],
            disabled=result["disabled_toolsets"],
        )

    def assert_status_agrees_with_hermes(self, status, catalog_tools):
        exposure = status["tool_exposure"]
        self.assertEqual(exposure["evidence"], "hermes_tool_definitions", exposure)
        self.assertTrue(catalog_tools, "Hermes built an empty catalog, so the comparison would prove nothing")
        for name, state in exposure["tools"].items():
            self.assertIs(state["callable"], name in catalog_tools, f"{name}: status disagrees with Hermes' catalog")

    def test_the_real_loader_registers_every_declared_tool_under_its_documented_toolset(self):
        outcome = self.run_hermes()
        self.assertEqual(outcome.status["plugin_version"], _manifest_version())
        self.assertIs(outcome.status["plugin_loaded"], True)
        for name, toolset in TOOL_TOOLSETS.items():
            self.assertEqual(outcome.registry[name], {"toolset": toolset}, name)
            state = outcome.status["tool_exposure"]["tools"][name]
            self.assertIs(state["registered"], True, name)
            self.assertEqual(state["registry_toolset"], toolset, name)
        self.assertEqual(set(outcome.registry), _manifest_tools())

    def test_explicit_toolset_pins_match_hermes_own_catalog(self):
        pins = (
            "terminal",
            COMPUTER_USE_TOOLSET,
            PLUGIN_TOOLSET,
            f"{COMPUTER_USE_TOOLSET},terminal",
            f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}",
        )
        for pin in pins:
            with self.subTest(pin=pin):
                outcome = self.run_hermes(pin=pin, catalog_pins=(pin,))
                catalog = set(outcome.catalogs[pin]["tools"])
                self.assert_status_agrees_with_hermes(outcome.status, catalog)
                selected = set(pin.split(","))
                for name, state in outcome.status["tool_exposure"]["tools"].items():
                    self.assertIs(state["registered"], True, name)
                    self.assertIs(state["callable"], TOOL_TOOLSETS[name] in selected, name)
                    if not state["callable"]:
                        self.assertEqual(state["reason"], "toolset_not_selected", name)

    def test_registered_tools_that_a_pin_leaves_out_are_not_reported_ready(self):
        outcome = self.run_hermes(pin="terminal", catalog_pins=("terminal",), credential=True)
        catalog = set(outcome.catalogs["terminal"]["tools"])
        self.assertFalse(catalog & set(TOOL_TOOLSETS), "the pin was expected to leave every plugin tool out")
        self.assertEqual(set(outcome.registry), set(TOOL_TOOLSETS))
        self.assertTrue(all(entry is not None for entry in outcome.registry.values()))
        self.assertEqual(outcome.status["status"], "tools_not_callable")

    def test_the_documented_composition_is_ready_with_a_credential_and_needs_one(self):
        pin = f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}"
        ready = self.run_hermes(pin=pin, catalog_pins=(pin,), credential=True)
        self.assertLessEqual(set(TOOL_TOOLSETS), set(ready.catalogs[pin]["tools"]))
        self.assertEqual(ready.status["status"], "ready")
        keyless = self.run_hermes(pin=pin, catalog_pins=(pin,), credential=False)
        self.assertEqual(keyless.status["status"], "credential_required")
        self.assert_status_agrees_with_hermes(keyless.status, set(keyless.catalogs[pin]["tools"]))

    def test_the_default_selection_matches_hermes_own_catalog(self):
        outcome = self.run_hermes(catalog_default=True, credential=True)
        selection = outcome.status["tool_exposure"]["selection"]
        self.assertEqual(selection["source"], "platform_default")
        self.assertEqual(selection["enabled_toolsets"], outcome.catalogs["default"]["toolsets"])
        self.assert_status_agrees_with_hermes(outcome.status, set(outcome.catalogs["default"]["tools"]))

    def test_a_saved_toolset_list_that_declined_computer_use_is_reported(self):
        offered = "browser, clarify, code_execution, computer_use, connections, cronjob, delegation, file, " \
                  "image_gen, memory, session_search, skills, terminal, todo, tts, vision, web"
        saved = offered.replace("computer_use, ", "").replace(", image_gen", ", hermes_switchyard, image_gen")
        extra = (
            f"platform_toolsets:\n  cli: [{saved}]\n"
            f"known_builtin_toolsets:\n  cli: [{offered}]\n"
            "known_plugin_toolsets:\n  cli: [computer_use, hermes_switchyard]\n"
        )
        outcome = self.run_hermes(catalog_default=True, credential=True, extra_config=extra)
        catalog = set(outcome.catalogs["default"]["tools"])
        self.assert_status_agrees_with_hermes(outcome.status, catalog)
        if "jev_computer_use" in catalog:
            self.skipTest("this Hermes build keeps computer_use enabled for a saved list that declined it")
        tools = outcome.status["tool_exposure"]["tools"]
        self.assertIs(tools["jev_computer_use"]["registered"], True)
        self.assertIs(tools["jev_computer_use"]["callable"], False)
        self.assertEqual(tools["jev_computer_use"]["reason"], "toolset_not_selected")
        self.assertEqual(outcome.status["status"], "tools_not_callable")

    def test_a_registration_another_owner_already_holds_is_a_registration_failure(self):
        outcome = self.run_hermes(credential=True, catalog_default=True, shadows=[("jev_computer_use", "stand_in_toolset")])
        owner_toolset = (outcome.registry["jev_computer_use"] or {}).get("toolset")
        state = outcome.status["tool_exposure"]["tools"]["jev_computer_use"]
        self.assertIs(state["registered"], owner_toolset == TOOL_TOOLSETS["jev_computer_use"])
        self.assert_status_agrees_with_hermes(outcome.status, set(outcome.catalogs["default"]["tools"]))
        if owner_toolset != "stand_in_toolset":
            self.skipTest("this Hermes build lets the plugin replace a tool another registration owns")
        self.assertEqual(state["reason"], "owned_by_another_registration")
        self.assertEqual(state["registry_toolset"], "stand_in_toolset")
        self.assertEqual(outcome.status["status"], "tools_not_registered")

    def test_a_failed_availability_check_is_reported_with_its_own_reason(self):
        pin = f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}"
        outcome = self.run_hermes(pin=pin, catalog_pins=(pin,), credential=True, settings={"jev_provider": "not-a-provider"})
        catalog = set(outcome.catalogs[pin]["tools"])
        self.assert_status_agrees_with_hermes(outcome.status, catalog)
        decision_tools = [name for name, toolset in TOOL_TOOLSETS.items() if toolset == PLUGIN_TOOLSET]
        if catalog & set(decision_tools):
            self.skipTest("the invalid provider setting did not hide the decision tools in this Hermes build")
        tools = outcome.status["tool_exposure"]["tools"]
        for name in decision_tools:
            self.assertIs(tools[name]["registered"], True, name)
            self.assertEqual(tools[name]["reason"], "availability_check_failed", name)
        self.assertIs(tools["jev_computer_use"]["callable"], True)
        self.assertEqual(outcome.status["status"], "tools_not_callable")

    def test_a_misspelled_toolset_in_a_pin_is_named(self):
        outcome = self.run_hermes(pin="computer-use", catalog_pins=("computer-use",))
        selection = outcome.status["tool_exposure"]["selection"]
        self.assertEqual(selection["unknown_toolsets"], ["computer-use"])
        self.assertTrue(all(not state["callable"] for state in outcome.status["tool_exposure"]["tools"].values()))
        self.assertFalse(set(outcome.catalogs["computer-use"]["tools"]) & set(TOOL_TOOLSETS))


    def test_a_globally_disabled_toolset_is_not_reported_callable_even_when_pinned(self):
        pin = f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}"
        extra = f"agent:\n  disabled_toolsets: [{COMPUTER_USE_TOOLSET}]\n"
        outcome = self.run_hermes(pin=pin, catalog_pins=(pin,), credential=True, extra_config=extra)
        self.assertEqual(outcome.disabled, [COMPUTER_USE_TOOLSET], "Hermes did not read the configured suppression list")
        catalog = set(outcome.catalogs[pin]["tools"])
        self.assert_status_agrees_with_hermes(outcome.status, catalog)
        if "jev_computer_use" in catalog:
            self.skipTest("this Hermes build does not subtract agent.disabled_toolsets from a pinned selection")
        exposure = outcome.status["tool_exposure"]
        self.assertEqual(exposure["selection"]["disabled_toolsets"], [COMPUTER_USE_TOOLSET])
        state = exposure["tools"]["jev_computer_use"]
        self.assertIs(state["registered"], True)
        self.assertIs(state["callable"], False)
        self.assertEqual(state["reason"], "toolset_disabled")
        self.assertEqual(outcome.status["status"], "tools_not_callable")
        for name, toolset in TOOL_TOOLSETS.items():
            if toolset == PLUGIN_TOOLSET:
                self.assertIs(exposure["tools"][name]["callable"], True, name)

    def test_readiness_follows_the_key_of_the_provider_the_configured_route_uses(self):
        pin = f"{COMPUTER_USE_TOOLSET},{PLUGIN_TOOLSET}"
        # The probe supplies a TypeSafe key only, so an OpenRouter route has no key to use.
        mismatched = self.run_hermes(pin=pin, catalog_pins=(pin,), credential=True, settings={"jev_provider": "openrouter"})
        self.assert_status_agrees_with_hermes(mismatched.status, set(mismatched.catalogs[pin]["tools"]))
        self.assertEqual(mismatched.status["effective_provider"], "openrouter")
        self.assertEqual(mismatched.status["credential_presence"], {"typesafe": True, "openrouter": False})
        self.assertEqual(mismatched.status["status"], "credential_required")
        matched = self.run_hermes(pin=pin, catalog_pins=(pin,), credential=True, settings={"jev_provider": "typesafe"})
        self.assertEqual(matched.status["effective_provider"], "typesafe")
        self.assertEqual(matched.status["status"], "ready")


if __name__ == "__main__":
    unittest.main()
