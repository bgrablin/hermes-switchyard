"""Performance guard for adaptive effort: a pooled Jev client and a bounded decision budget.

The adapter keeps idle Jev clients and reuses them across decisions, so a warm decision does
not pay DNS, TCP, and TLS again. One client serves one decision at a time. The decision budget
defaults to 0.4 s. When Jev is slower, the request goes out at the cap with reason
``kept_requested_on_jev_timeout``, and the late answer is discarded.

All values are synthetic. No test reaches the network.
"""
from __future__ import annotations

from hermes_switchyard import egress_redaction

import json
import socket
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from unittest import mock

from hermes_switchyard import client as jev_client
from hermes_switchyard import reasoning_effort_adapter as adapter
from hermes_switchyard.client import DecisionClient
from hermes_switchyard.reasoning_effort_adapter import (
    ReasoningEffortController,
    append_effort_record,
    last_receipt,
)

from tests.test_reasoning_effort_user_cap import Env
from tests.test_reasoning_effort_v055 import SESSION, TextJev, begin, send
from tests.test_reasoning_effort_visible_value import finish

TIMEOUT = "kept_requested_on_jev_timeout"
FAILURE = "kept_requested_on_jev_failure"
WORK = ("refactor the parser module", "review the retry tests", "rename the helper module")


def no_network():
    def refuse(*_args, **_kwargs):
        raise AssertionError("network access attempted")

    return (
        mock.patch.object(socket, "create_connection", side_effect=refuse),
        mock.patch.object(socket.socket, "connect", side_effect=refuse),
    )


def pick(questions, level):
    levels = list(questions["reasoning_effort"]["criteria"])
    chosen = level if level in levels else levels[-1]
    return {
        "reasoning_effort": {
            "choice": chosen,
            "confidence": 0.9,
            "probabilities": {name: (1.0 if name == chosen else 0.0) for name in levels},
        },
        "stakes": {"noul": 0.0},
    }


def setUpModule():
    egress_redaction._reset_for_tests(lambda text: text, loaded=True)

def tearDownModule():
    egress_redaction._reset_for_tests()


class _Response:
    def __init__(self, body: bytes):
        self.body = body
        self.status = 200
        self.reason = "fixture"
        self.headers = {}
        self.will_close = False

    def read(self, size=None):
        return self.body if size is None else self.body[:size]

    def close(self):
        pass


class _Connection:
    """Fake keep-alive HTTPSConnection that answers every decision with ``low``."""

    def __init__(self, *_args, **_kwargs):
        self.requests = 0
        self.sock = None
        self.closed = False
        self._pending = None

    def request(self, _method, _path, body=None, headers=None):
        self.requests += 1
        payload = json.loads(body)
        answer = {"model": jev_client.EXPECTED_MODEL, "answers": pick(payload["questions"], "low"), "usage": {}}
        self._pending = json.dumps(answer).encode("utf-8")

    def getresponse(self):
        return _Response(self._pending)

    def close(self):
        self.closed = True


class Identity:
    def __init__(self):
        self.value = {"provider": "openrouter", "credential_sha256": "a" * 64}

    def __call__(self):
        return dict(self.value)


def make(factory, **kwargs):
    env = kwargs.pop("session_env", Env(HERMES_SESSION_ID=SESSION))
    return ReasoningEffortController(client_factory=factory, session_env=env, **kwargs)


def decide(controller, text, turn, *, session=SESSION):
    begin(controller, text, session=session, turn=turn)
    return send(controller, "high", session=session, turn=turn)


class PooledClientTests(unittest.TestCase):
    def test_sequential_decisions_create_one_client_and_one_connection(self):
        connections = []

        def connect(*args, **kwargs):
            connections.append(_Connection(*args, **kwargs))
            return connections[-1]

        created = []

        def factory():
            created.append(DecisionClient(api_key="fixture"))
            return created[-1]

        controller = make(factory, client_identity=Identity())
        with mock.patch.object(jev_client.http.client, "HTTPSConnection", side_effect=connect):
            for index, text in enumerate(WORK[:2]):
                self.assertEqual(decide(controller, text, f"t{index}"), "low")
                self.assertEqual(last_receipt()["reason_code"], "jev_selected")
        self.assertEqual(len(created), 1)
        self.assertEqual(len(connections), 1)
        self.assertEqual(connections[0].requests, 2)
        self.assertFalse(connections[0].closed)
        controller.close()
        self.assertTrue(connections[0].closed)

    def test_config_change_recreates_the_client_and_closes_the_old_one(self):
        created = []

        class Client(TextJev):
            def __init__(self):
                super().__init__()
                self.closed = False

            def close(self):
                self.closed = True

        def factory():
            created.append(Client())
            return created[-1]

        identity = Identity()
        controller = make(factory, client_identity=identity)
        decide(controller, WORK[0], "t1")
        decide(controller, WORK[1], "t2")
        self.assertEqual(len(created), 1)
        identity.value = {"provider": "typesafe", "credential_sha256": "b" * 64}
        decide(controller, WORK[2], "t3")
        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].closed)
        self.assertFalse(created[1].closed)
        self.assertEqual([len(item.calls) for item in created], [2, 1])

    def test_a_new_client_factory_is_a_config_change(self):
        first, second = TextJev(), TextJev()
        controller = make(lambda: first)
        decide(controller, WORK[0], "t1")
        controller.client_factory = lambda: second
        decide(controller, WORK[1], "t2")
        self.assertEqual((len(first.calls), len(second.calls)), (1, 1))

    def test_concurrent_decisions_do_not_share_one_in_flight_client(self):
        both_in_flight = threading.Barrier(2, timeout=0.2)
        created = []

        class Client:
            def __init__(self):
                self.active = 0
                self.peak = 0
                self.calls = 0
                self.lock = threading.Lock()

            def decide(self, state, questions, **_kwargs):
                with self.lock:
                    self.active += 1
                    self.peak = max(self.peak, self.active)
                    self.calls += 1
                try:
                    if len(created) <= 2 and self.calls == 1:
                        both_in_flight.wait()  # proves the two decisions overlap
                    return {"answers": pick(questions, "low")}
                finally:
                    with self.lock:
                        self.active -= 1

        def factory():
            created.append(Client())
            return created[-1]

        controller = make(factory, session_env=Env())
        results = {}

        def run(session):
            results[session] = decide(controller, WORK[0], "t1", session=session)

        threads = [threading.Thread(target=run, args=(name,)) for name in ("s-foreground", "s-delegated")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual(results, {"s-foreground": "low", "s-delegated": "low"})
        self.assertEqual(len(created), 2)
        self.assertTrue(all(item.peak == 1 for item in created))
        # A later decision reuses an idle client instead of opening a third.
        self.assertEqual(decide(controller, WORK[1], "t2", session="s-foreground"), "low")
        self.assertEqual(len(created), 2)

    def test_idle_client_expires_and_is_closed(self):
        created = []

        class Client(TextJev):
            closed = False

            def close(self):
                self.closed = True

        def factory():
            created.append(Client())
            return created[-1]

        clock = [1000.0]
        controller = make(factory)
        with mock.patch.object(adapter.time, "monotonic", side_effect=lambda: clock[0]):
            decide(controller, WORK[0], "t1")
            clock[0] += jev_client.MAX_CONNECTION_IDLE_SECONDS + 1
            decide(controller, WORK[1], "t2")
        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].closed)

    def test_registration_closes_the_pool_on_plugin_unload(self):
        class Context:
            def __init__(self):
                self.unload = []

            def register_middleware(self, *_args, **_kwargs):
                pass

            def register_hook(self, *_args, **_kwargs):
                pass

            def on_unload(self, callback):
                self.unload.append(callback)

        context = Context()
        with mock.patch.object(
            adapter, "probe_llm_request_middleware_seam", return_value={"available": True, "can_apply": True}
        ):
            receipt = adapter.register_reasoning_effort_adapter(context, client_factory=TextJev)
        controller = adapter.register_reasoning_effort_adapter.last_controller
        self.assertTrue(receipt["client_pool_close_registered"])
        self.assertEqual(context.unload, [controller.close])


class SlowJev:
    """Answers ``low`` after ``delay`` seconds; records when each call started and ended."""

    def __init__(self, delay: float):
        self.delay = delay
        self.started: list[float] = []
        self.finished: list[float] = []

    def decide(self, state, questions, **_kwargs):
        self.started.append(time.perf_counter())
        time.sleep(self.delay)
        self.finished.append(time.perf_counter())
        return {"answers": pick(questions, "low")}


class DecisionBudgetTests(unittest.TestCase):
    def test_default_budget_is_a_quarter_second(self):
        self.assertEqual(adapter.DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS, 0.4)
        self.assertEqual(ReasoningEffortController().deadline_seconds, 0.4)
        manifest = (Path(__file__).resolve().parent.parent / "plugin.yaml").read_text(encoding="utf-8")
        line = next(item for item in manifest.splitlines() if "adaptive_reasoning_effort_deadline_seconds" in item)
        self.assertIn("default: 0.4", line)

    def test_budget_setting_is_bounded(self):
        cases = {0.05: 0.1, 0.1: 0.1, 0.25: 0.25, 1.5: 1.5, 30: 1.5, 0: 0.4, -1: 0.4,
                 float("nan"): 0.4, float("inf"): 0.4, True: 0.4, "0.25": 0.4, None: 0.4}
        for value, expected in cases.items():
            with self.subTest(value=value):
                self.assertEqual(adapter.normalize_deadline_seconds(value), expected)
                self.assertEqual(ReasoningEffortController(deadline_seconds=value).deadline_seconds, expected)

    def test_slow_jev_keeps_the_cap_within_the_budget_and_the_late_answer_is_discarded(self):
        slow = SlowJev(0.65)
        records = []
        controller = make(lambda: slow, receipt_line=True, record_decision=records.append)
        begin(controller, WORK[0], turn="t1")
        started = time.perf_counter()
        sent = send(controller, "high", turn="t1")
        added = time.perf_counter() - started
        self.assertEqual(sent, "high")
        self.assertLessEqual(added, 0.45)
        receipt = last_receipt()
        self.assertEqual(receipt["reason_code"], TIMEOUT)
        self.assertIs(receipt["jev_called"], True)
        self.assertEqual(finish(controller).splitlines()[-1], "Reasoning: kept at high — cloud over 400 ms budget")

        # The late "low" arrives; it must not change this turn or the next one.
        time.sleep(0.3)
        self.assertEqual(len(slow.finished), 1)
        self.assertEqual(send(controller, "high", turn="t1"), "high")
        self.assertNotEqual(last_receipt().get("reason_code"), "jev_selected")
        fast = TextJev()
        controller.client_factory = lambda: fast
        self.assertEqual(decide(controller, "delete the prod database backups", "t2"), "high")
        self.assertEqual(len(fast.calls), 1)
        self.assertEqual(adapter.build_effort_record(records[0])["reason_code"], TIMEOUT)

    def test_client_deadline_errors_are_timeouts_and_other_errors_are_failures(self):
        cases = (
            (jev_client.DeadlineExceeded("synthetic"), TIMEOUT, "cloud over 400 ms budget"),
            (jev_client.LateResultDiscarded("synthetic"), TIMEOUT, "cloud over 400 ms budget"),
            (TimeoutError("synthetic"), TIMEOUT, "cloud over 400 ms budget"),
            (ConnectionError("synthetic"), FAILURE, "cloud unavailable"),
        )
        for error, reason, label in cases:
            with self.subTest(error=type(error).__name__):
                client = mock.Mock(spec=["decide", "close"])
                client.decide.side_effect = error
                controller = make(lambda client=client: client, receipt_line=True)
                self.assertEqual(decide(controller, WORK[0], "t1"), "high")
                self.assertEqual(last_receipt()["reason_code"], reason)
                self.assertIn(f"kept at high — {label}", finish(controller))

    def test_real_client_under_budget_discards_a_late_transport_answer(self):
        def transport(payload):
            time.sleep(0.65)
            return {"model": jev_client.EXPECTED_MODEL, "answers": pick(payload["questions"], "low"), "usage": {}}

        controller = make(lambda: DecisionClient(api_key="fixture", transport=transport))
        started = time.perf_counter()
        self.assertEqual(decide(controller, WORK[0], "t1"), "high")
        self.assertLessEqual(time.perf_counter() - started, 0.45)
        self.assertEqual(last_receipt()["reason_code"], TIMEOUT)


def _seed_history(now):
    rows = [
        # Inside the last 24 h: three Jev calls, one timeout, one local decision.
        ({"reason_code": "jev_selected", "jev_called": True, "jev_latency_ms": 200.0}, now - timedelta(hours=1)),
        ({"reason_code": "jev_selected", "jev_called": True, "jev_latency_ms": 240.0}, now - timedelta(hours=2)),
        ({"reason_code": TIMEOUT, "jev_called": True, "jev_latency_ms": 251.0}, now - timedelta(hours=3)),
        ({"reason_code": "local_trivial", "jev_called": False}, now - timedelta(hours=4)),
        # Older than 24 h: not counted.
        ({"reason_code": TIMEOUT, "jev_called": True, "jev_latency_ms": 900.0}, now - timedelta(hours=30)),
    ]
    for fields, moment in rows:
        receipt = {"session_id": "s-history", "model": "claude-opus-4-6", "mode": "auto",
                   "requested_effort": "high", "effort": "high", "cap": "high", **fields}
        assert append_effort_record(receipt, now=moment)


if __name__ == "__main__":
    unittest.main()
