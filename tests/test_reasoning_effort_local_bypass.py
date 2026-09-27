"""Local-first bypass for trivial turns in adaptive effort (reason_code ``local_trivial``).

A foreground greeting, thanks, or acknowledgement, by the closed list shared with automatic
skill routing, goes out at the lowest allowed level with no Jev call and no network. The cap
and pin rules are unchanged. All values are synthetic.
"""
from __future__ import annotations

import json
import socket
import unittest
from unittest.mock import patch

from hermes_switchyard import automatic, egress_redaction
from hermes_switchyard.reasoning_effort_adapter import build_effort_record, last_receipt

from tests.test_reasoning_effort_user_cap import OPUS, opus_request, sent_effort
from tests.test_reasoning_effort_v055 import SESSION, begin, make, send, tool
from tests.test_reasoning_effort_visible_value import finish

TRIVIAL = ("hi", "thanks", "ok thanks!", "👍", "Thank you so much!", "gm")
NEGATIVE = (
    "hi, delete the prod backups",
    "thanks, now deploy",
    "ok\n```\nls -la\n```",
    # These pass the closed word list alone; the code fence, URL, and path guards stop them.
    "ok\n```\ndone\n```",
    "thanks https://example.com",
    "thanks www.example.com",
    "ok ~/done/",
    "ok ./go/ahead",
    # Built at runtime so the source has no drive-letter path for the hygiene check.
    "thanks " + "C" + ":\\done",
    "ok done.py",
)
LOCAL = "local_trivial"


def no_network():
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    return patch.object(socket, "create_connection", side_effect=refuse), patch.object(
        socket.socket, "connect", side_effect=refuse
    )


class SharedDetectorTests(unittest.TestCase):
    def test_detector_is_shared_not_copied(self):
        from hermes_switchyard import reasoning_effort_adapter, trivial_turn

        self.assertIs(automatic._trivial_turn, trivial_turn.is_trivial_turn)
        self.assertIs(reasoning_effort_adapter.is_trivial_turn, trivial_turn.is_trivial_turn)
        self.assertIs(automatic._TRIVIAL_ACK_WORDS, trivial_turn.TRIVIAL_ACK_WORDS)
        self.assertEqual(automatic.TRIVIAL_ACK_MAX_WORDS, trivial_turn.TRIVIAL_ACK_MAX_WORDS)


class LocalTrivialBypassTests(unittest.TestCase):
    def test_trivial_turns_go_lowest_with_no_jev_call_and_no_network(self):
        for text in TRIVIAL:
            with self.subTest(text=text):
                controller, jev, _ = make()
                begin(controller, text)
                first, second = no_network()
                with first, second:
                    self.assertEqual(send(controller, "high"), "low")
                self.assertEqual(jev.calls, [])
                receipt = last_receipt()
                self.assertEqual(receipt["reason_code"], LOCAL)
                self.assertIs(receipt["jev_called"], False)
                self.assertNotIn("jev_latency_ms", {k for k, v in receipt.items() if v is not None})

    def test_bypass_needs_no_jev_client_at_all(self):
        from hermes_switchyard.reasoning_effort_adapter import ReasoningEffortController

        from tests.test_reasoning_effort_user_cap import Env

        def factory():
            raise AssertionError("Jev client built for a trivial turn")

        controller = ReasoningEffortController(client_factory=factory, session_env=Env(HERMES_SESSION_ID=SESSION))
        begin(controller, "thanks")
        self.assertEqual(send(controller, "high"), "low")

    def test_negative_cases_still_call_jev(self):
        for text in NEGATIVE:
            with self.subTest(text=text):
                controller, jev, _ = make()
                begin(controller, text)
                send(controller, "high")
                self.assertEqual(len(jev.calls), 1, text)
                self.assertNotEqual(last_receipt()["reason_code"], LOCAL)

    def test_lowest_level_follows_the_allowed_ladder_and_never_exceeds_the_cap(self):
        controller, jev, _ = make(allowed_efforts=("medium", "high", "xhigh", "max"))
        begin(controller, "hi")
        self.assertEqual(send(controller, "high"), "medium")
        self.assertEqual(jev.calls, [])
        for cap in ("medium", "high", "max"):
            with self.subTest(cap=cap):
                controller, jev, _ = make()
                begin(controller, "ok")
                self.assertEqual(send(controller, cap), "low")
                self.assertEqual(jev.calls, [])
        # A cap already at the lowest level has no room: the request is unchanged.
        controller, jev, _ = make()
        begin(controller, "ok")
        self.assertEqual(send(controller, "low"), "low")
        self.assertEqual(jev.calls, [])
        self.assertNotEqual(last_receipt()["reason_code"], LOCAL)

    def test_pinned_session_stays_pinned(self):
        controller, jev, _ = make()
        controller.handle_command("effort pin")
        begin(controller, "hi")
        self.assertEqual(send(controller, "high"), "high")
        self.assertEqual(jev.calls, [])
        self.assertEqual(last_receipt()["reason_code"], "pinned")

    def test_delegated_child_does_not_use_the_bypass(self):
        controller, jev, _ = make()
        child = "subagent-0-synthetic"
        begin(controller, "thanks", session=child, parent=SESSION)
        request = opus_request("high")
        result = controller.on_llm_request(request, session_id=child, task_id=child, turn_id="t1", **OPUS)
        self.assertEqual(sent_effort(request, result), "high")
        self.assertEqual(jev.calls, [])
        self.assertNotEqual(last_receipt()["reason_code"], LOCAL)

    def test_failed_tool_in_a_trivial_turn_asks_jev(self):
        controller, jev, _ = make()
        begin(controller, "ok")
        self.assertEqual(send(controller, "high"), "low")
        tool(controller, "terminal", ok=False)
        send(controller, "high")
        self.assertEqual(len(jev.calls), 1)

    def test_bypass_works_without_a_hermes_redactor(self):
        egress_redaction._reset_for_tests(None, loaded=True)
        try:
            controller, jev, _ = make()
            begin(controller, "thanks")
            self.assertEqual(send(controller, "high"), "low")
            self.assertEqual(jev.calls, [])
            self.assertEqual(last_receipt()["reason_code"], LOCAL)
        finally:
            egress_redaction._reset_for_tests()

    def test_next_turn_decides_again(self):
        controller, jev, _ = make()
        begin(controller, "hi", turn="t1")
        self.assertEqual(send(controller, "high", turn="t1"), "low")
        begin(controller, "delete the prod database backups", turn="t2")
        self.assertEqual(send(controller, "high", turn="t2"), "high")
        self.assertEqual(len(jev.calls), 1)


class LocalTrivialVisibilityTests(unittest.TestCase):
    def test_receipt_line_says_local(self):
        controller, _, _ = make(receipt_line=True)
        begin(controller, "thanks")
        send(controller, "high")
        self.assertEqual(finish(controller).splitlines()[-1], "switchyard: effort high→low · local (no Jev call)")

    def test_summary_counts_local_decisions_separately(self):
        controller, jev, _ = make()
        begin(controller, "hi", turn="t1")
        send(controller, "high", turn="t1")
        begin(controller, "refactor the parser module", turn="t2")
        send(controller, "high", turn="t2")
        summary = controller.session_status()["summary"]
        self.assertEqual((summary["local_decisions"], summary["jev_calls"]), (1, 1))
        text = controller.handle_command("effort summary")
        self.assertIn("local decisions (no Jev call): 1", text)
        self.assertIn("Jev calls: 1,", text)

    def test_history_record_is_local_and_text_free(self):
        records = []
        controller, _, _ = make(record_decision=records.append)
        begin(controller, "ok thanks!")
        send(controller, "high")
        (record,) = [build_effort_record(item) for item in records]
        self.assertEqual((record["jev_called"], record["reason_code"]), (False, LOCAL))
        self.assertEqual((record["requested"], record["sent"]), ("high", "low"))
        self.assertIsNone(record["jev_latency_ms"])
        self.assertNotIn("thanks", json.dumps(records))


if __name__ == "__main__":
    unittest.main()
