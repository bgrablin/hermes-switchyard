"""v0.5.5: the Hermes TUI status bar shows the reasoning level Switchyard sent.

The Hermes TUI status bar reads ``session.info.reasoning_effort`` (the user's level) and
``session.info.reasoning_effort_wire`` (the level sent) and shows ``high→low`` when they
differ. These tests use a synthetic stand-in for ``tui_gateway.server``. They prove that the
plugin re-emits ``session.info`` with the sent level for the foreground session only, never
changes ``agent.reasoning_config`` (#118), never imports the TUI gateway, and never breaks a
request. All values are synthetic; nothing opens a network connection.
"""
from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from hermes_switchyard.reasoning_effort_adapter import (
    ReasoningEffortController,
    register_reasoning_effort_adapter,
)
from hermes_switchyard.tui_status import SentEffortStatus, publish_sent_effort

from tests.test_reasoning_effort_user_cap import OPUS
from tests.test_reasoning_effort_v055 import SESSION, TextJev, begin, send

ROOT = Path(__file__).resolve().parent.parent
UI_SID = "ui-synthetic-1"
CONSEQUENTIAL = "Plan the production database migration and the rollback for the billing service."


def run_now(fn):
    fn()


class FakeTuiServer(types.ModuleType):
    """Synthetic ``tui_gateway.server``: the three names the plugin reads, and an event log."""

    def __init__(self, *, effort="high", session_id=SESSION):
        super().__init__("tui_gateway.server")
        self.agent = SimpleNamespace(session_id=session_id, model=OPUS["model"], reasoning_config={"effort": effort})
        self._sessions = {UI_SID: {"agent": self.agent, "session_key": session_id}}
        self._sessions_lock = threading.RLock()
        self.emitted = []
        self.fail = False

    def _session_info(self, agent, session=None):
        effort = agent.reasoning_config.get("effort", "")
        return {"model": agent.model, "reasoning_effort": effort, "reasoning_effort_wire": effort, "running": True}

    def _emit(self, event, sid, payload=None):
        if self.fail:
            raise RuntimeError("synthetic transport failure")
        self.emitted.append((event, sid, dict(payload or {})))
        return True

    def wires(self):
        return [payload["reasoning_effort_wire"] for event, _sid, payload in self.emitted if event == "session.info"]


def controller_with_status(**kwargs):
    jev = TextJev()
    status = kwargs.pop("status", None) or SentEffortStatus(run=run_now)
    controller = ReasoningEffortController(
        client_factory=lambda: jev, session_env=lambda _name: SESSION, status_publisher=status, **kwargs
    )
    return controller, jev


class StatusBarShowsSentLevelTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeTuiServer()
        modules = patch.dict(sys.modules, {"tui_gateway.server": self.server})
        modules.start()
        self.addCleanup(modules.stop)

    def test_trivial_turn_shows_the_lowered_level_next_to_the_users_level(self):
        controller, _ = controller_with_status()
        begin(controller, "hi")
        self.assertEqual(send(controller, "high"), "low")
        self.assertEqual(len(self.server.emitted), 1)
        event, sid, payload = self.server.emitted[0]
        self.assertEqual((event, sid), ("session.info", UI_SID))
        self.assertEqual(payload["reasoning_effort"], "high")  # the user's level is unchanged
        self.assertEqual(payload["reasoning_effort_wire"], "low")  # the TUI renders "high→low"

    def test_consequential_turn_after_a_trivial_turn_restores_the_users_level(self):
        controller, _ = controller_with_status()
        begin(controller, "hi", turn="t1")
        send(controller, "high", turn="t1")
        begin(controller, CONSEQUENTIAL, turn="t2")
        self.assertEqual(send(controller, "high", turn="t2"), "high")
        self.assertEqual(self.server.wires(), ["low", "high"])

    def test_no_event_when_the_sent_level_already_matches_the_status_bar(self):
        controller, _ = controller_with_status()
        begin(controller, CONSEQUENTIAL)
        self.assertEqual(send(controller, "high"), "high")
        self.assertEqual(self.server.emitted, [])

    def test_one_event_per_turn_for_repeated_requests_at_the_same_level(self):
        controller, _ = controller_with_status()
        begin(controller, "hi")
        send(controller, "high")
        send(controller, "high")
        self.assertEqual(self.server.wires(), ["low"])

    def test_delegated_child_never_changes_the_foreground_status_bar(self):
        controller, _ = controller_with_status()
        begin(controller, "hi")
        self.assertEqual(send(controller, "high"), "low")
        child = "subagent-0-synthetic"
        # Same session ID, different task: without the foreground filter this would publish "medium".
        begin(controller, "hi", task=child, turn="c1", parent=SESSION)
        self.assertEqual(send(controller, "medium", task=child, turn="c1"), "medium")
        self.assertEqual(self.server.wires(), ["low"])

    def test_rotated_host_session_id_reaches_the_live_agent(self):
        controller, _ = controller_with_status()
        begin(controller, CONSEQUENTIAL, turn="t1")
        send(controller, "high", turn="t1")
        rotated = "synthetic-session-rotated"
        self.server.agent.session_id = rotated  # compression rotated the host session ID
        begin(controller, "hi", session=rotated, turn="t2")
        self.assertEqual(send(controller, "high", session=rotated, turn="t2"), "low")
        self.assertEqual([sid for _e, sid, _p in self.server.emitted], [UI_SID])
        self.assertEqual(self.server.wires(), ["low"])

    def test_receipts_and_records_carry_no_host_session_field(self):
        records = []
        controller, _ = controller_with_status(record_decision=records.append)
        begin(controller, "hi")
        send(controller, "high")
        self.assertTrue(records)
        self.assertFalse(any(key.startswith("_") for record in records for key in record))

    def test_setting_off_emits_nothing(self):
        controller, _ = controller_with_status(status_bar=False)
        begin(controller, "hi")
        self.assertEqual(send(controller, "high"), "low")
        self.assertEqual(self.server.emitted, [])

    def test_never_changes_the_agents_reasoning_config(self):
        controller, _ = controller_with_status()
        begin(controller, "hi")
        send(controller, "high")
        self.assertEqual(self.server.agent.reasoning_config, {"effort": "high"})

    def test_transport_failure_never_breaks_the_request(self):
        self.server.fail = True
        controller, _ = controller_with_status()
        begin(controller, "hi")
        self.assertEqual(send(controller, "high"), "low")

    def test_other_sessions_are_not_touched(self):
        other = FakeTuiServer(session_id="another-session")
        with patch.dict(sys.modules, {"tui_gateway.server": other}):
            controller, _ = controller_with_status()
            begin(controller, "hi")
            send(controller, "high")
        self.assertEqual(other.emitted, [])

    def test_thinking_off_is_left_alone(self):
        self.server.agent.reasoning_config = {"enabled": False, "effort": ""}
        self.assertFalse(publish_sent_effort(SESSION, "low"))
        self.assertEqual(self.server.emitted, [])


class OutsideTheTuiTests(unittest.TestCase):
    def test_no_tui_gateway_loaded_is_a_no_op_and_imports_nothing(self):
        with patch.dict(sys.modules):
            for name in [n for n in sys.modules if n == "tui_gateway" or n.startswith("tui_gateway.")]:
                del sys.modules[name]
            calls = []
            status = SentEffortStatus(publish=lambda *args: calls.append(args), run=run_now)
            controller, _ = controller_with_status(status=status)
            begin(controller, "hi")
            self.assertEqual(send(controller, "high"), "low")
            self.assertNotIn("tui_gateway.server", sys.modules)
            self.assertNotIn("tui_gateway", sys.modules)
        self.assertEqual(calls, [])

    def test_default_publisher_runs_off_the_request_thread(self):
        server = FakeTuiServer()
        done = threading.Event()
        threads = []

        def publish(session_id, level):
            threads.append(threading.current_thread() is threading.main_thread())
            done.set()

        with patch.dict(sys.modules, {"tui_gateway.server": server}):
            controller, _ = controller_with_status(status=SentEffortStatus(publish=publish))
            begin(controller, "hi")
            send(controller, "high")
            self.assertTrue(done.wait(5))
        self.assertEqual(threads, [False])


class SettingAndPackagingTests(unittest.TestCase):
    def test_plugin_yaml_declares_the_setting_on_by_default(self):
        text = (ROOT / "plugin.yaml").read_text(encoding="utf-8")
        self.assertIn("adaptive_reasoning_effort_status_bar: {type: bool, default: true", text)

    def test_register_reports_the_setting(self):
        registered = []
        ctx = SimpleNamespace(
            register_middleware=lambda kind, fn: registered.append(kind),
            register_hook=lambda name, fn: None,
        )
        receipt = register_reasoning_effort_adapter(ctx, client_factory=lambda: None, status_bar=False)
        self.assertIs(receipt["settings"]["status_bar"], False)

    def test_release_manifest_ships_the_module(self):
        text = (ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
        self.assertIn('"hermes_switchyard/tui_status.py"', text)


if __name__ == "__main__":
    unittest.main()
