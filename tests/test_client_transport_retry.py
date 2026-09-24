"""Bounded transport retries and closed-set failure sub-codes (issue #92).

Covers the parts of #92 beyond the stale-idle retry: HTTP 429/529 backoff
that honors Retry-After and the operation deadline, a per-operation retry
bound, closed-set diagnostic sub-codes without provider text, retry counts in
routing metadata, and a rate-limited WARNING. Every test uses in-memory fake
connections and a fake clock; no test reaches the network or sleeps.
"""
from __future__ import annotations

import http.client
import logging
import ssl
import unittest
from unittest import mock

from hermes_switchyard import client as module
from hermes_switchyard import routing
from hermes_switchyard.automatic import AutomaticSkillRecommender
from hermes_switchyard.client import (
    HOSTED_ERROR_DETAILS,
    DecisionClient,
    JevRequestError,
    hosted_error_detail,
    parse_retry_after,
    request_budget_scope,
)

_OK_BODY = b'{"model":"typesafe/jev-1.13","answers":{"answer":{"noul":0.9}},"usage":{}}'
_QUESTIONS = {"answer": {"type": "noul", "instructions": "Is it true?"}}
_PROVIDER_SECRET_TEXT = b'{"error":{"message":"provider-internal-detail sk-or-v1-abc"}}'


class _Response:
    def __init__(self, status: int = 200, body: bytes = _OK_BODY, headers: dict | None = None):
        self.status = status
        self.body = body
        self.reason = "fixture"
        self.headers = dict(headers or {})
        self.will_close = False

    def read(self, size: int | None = None) -> bytes:
        return self.body if size is None else self.body[:size]

    def close(self) -> None:
        pass


class _Connection:
    """Fake HTTPSConnection that serves a scripted list of outcomes.

    Each item is an exception to raise from ``request`` or a ``_Response`` to
    return from ``getresponse``. The list is shared across connections so a
    script describes the whole exchange regardless of reconnects.
    """

    def __init__(self, script: list):
        self.script = script
        self.requests = 0
        self.closed = False
        self.sock = None
        self._pending: _Response | None = None

    def request(self, *_args, **_kwargs):
        self.requests += 1
        item = self.script.pop(0) if self.script else _Response()
        if isinstance(item, BaseException):
            raise item
        self._pending = item

    def getresponse(self):
        response, self._pending = self._pending, None
        return response

    def close(self):
        self.closed = True


class _Clock:
    def __init__(self, start: float = 1000.0):
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class _Harness:
    def __init__(self, script: list):
        self.script = script
        self.connections: list[_Connection] = []
        self.clock = _Clock()

    def factory(self, *_args, **_kwargs):
        connection = _Connection(self.script)
        self.connections.append(connection)
        return connection

    def patches(self):
        return (
            mock.patch.object(module.http.client, "HTTPSConnection", side_effect=self.factory),
            mock.patch.object(module.time, "monotonic", side_effect=self.clock.monotonic),
            mock.patch.object(module.time, "sleep", side_effect=self.clock.sleep),
        )

    @property
    def http_requests(self) -> int:
        return sum(connection.requests for connection in self.connections)


def _run(harness: _Harness, action):
    p1, p2, p3 = harness.patches()
    with p1, p2, p3:
        return action()


def _decide(client: DecisionClient):
    return client.decide("public", _QUESTIONS, public_or_sanitized_data_ack=True)


class RateLimitRetryTests(unittest.TestCase):
    def setUp(self):
        module._reset_warning_rate_limit()

    def test_429_then_success_retries_with_backoff_and_records_retry(self):
        harness = _Harness([_Response(429), _Response()])
        client = DecisionClient(api_key="fixture")
        result = _run(harness, lambda: _decide(client))
        self.assertEqual(result["answers"]["answer"]["noul"], 0.9)
        self.assertEqual(harness.http_requests, 2)
        self.assertEqual(sum(harness.clock.sleeps), module.RATE_LIMIT_BACKOFF_BASE_SECONDS)
        self.assertEqual(result["transport_retries"], {"http_429": 1})
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(client.rate_limit_retries, 1)

    def test_529_uses_exponential_backoff(self):
        harness = _Harness([_Response(529), _Response(529), _Response()])
        client = DecisionClient(api_key="fixture")
        result = _run(harness, lambda: _decide(client))
        self.assertEqual(result["transport_retries"], {"http_529": 2})
        self.assertAlmostEqual(sum(harness.clock.sleeps), 0.5 + 1.0)

    def test_retryable_status_does_not_parse_or_read_provider_error_body(self):
        for status, body, headers in (
            (429, b"{broken", {}),
            (529, b"private-error", {"Content-Length": str(module.MAX_ERROR_BYTES + 1)}),
        ):
            with self.subTest(status=status):
                response = _Response(status, body=body, headers=headers)
                response.close = mock.Mock()
                harness = _Harness([response, _Response()])
                client = DecisionClient(api_key="fixture")
                with mock.patch.object(response, "read", side_effect=AssertionError("retryable body read")):
                    result = _run(harness, lambda: _decide(client))
                self.assertEqual(result["transport_retries"], {f"http_{status}": 1})
                self.assertEqual(harness.http_requests, 2)
                response.close.assert_called_once_with()

    def test_terminal_error_still_validates_malformed_json(self):
        harness = _Harness([_Response(500, body=b"{broken"), _Response()])
        client = DecisionClient(api_key="fixture")
        with self.assertRaises(JevRequestError) as raised:
            _run(harness, lambda: _decide(client))
        self.assertEqual(raised.exception.detail, "invalid_response")
        self.assertEqual(harness.http_requests, 1)

    def test_numeric_retry_after_is_honored(self):
        harness = _Harness([_Response(429, headers={"Retry-After": "3"}), _Response()])
        client = DecisionClient(api_key="fixture")
        _run(harness, lambda: _decide(client))
        self.assertAlmostEqual(sum(harness.clock.sleeps), 3.0)

    def test_retry_after_beyond_cap_is_not_slept(self):
        harness = _Harness([_Response(429, headers={"Retry-After": "3600"}), _Response()])
        client = DecisionClient(api_key="fixture")
        with self.assertRaises(JevRequestError) as raised:
            _run(harness, lambda: _decide(client))
        self.assertEqual(raised.exception.detail, "http_429")
        self.assertEqual(harness.clock.sleeps, [])
        self.assertEqual(harness.http_requests, 1)

    def test_retry_is_not_started_when_the_routing_deadline_cannot_cover_it(self):
        harness = _Harness([_Response(429, headers={"Retry-After": "2"}), _Response()])
        client = DecisionClient(api_key="fixture")

        def action():
            with request_budget_scope(client, 4, deadline_seconds=2.5):
                return _decide(client)

        with self.assertRaises(JevRequestError) as raised:
            _run(harness, action)
        self.assertEqual(raised.exception.detail, "http_429")
        self.assertEqual(harness.clock.sleeps, [])
        self.assertEqual(harness.http_requests, 1)

    def test_rate_limit_retries_per_request_are_bounded(self):
        harness = _Harness([_Response(429)] * 10)
        client = DecisionClient(api_key="fixture")
        with self.assertRaises(JevRequestError) as raised:
            _run(harness, lambda: _decide(client))
        self.assertEqual(raised.exception.detail, "http_429")
        self.assertEqual(harness.http_requests, module.MAX_RATE_LIMIT_RETRIES + 1)
        self.assertLessEqual(sum(harness.clock.sleeps), module.MAX_RETRY_AFTER_SECONDS * module.MAX_RATE_LIMIT_RETRIES)

    def test_retries_per_operation_are_bounded_across_requests(self):
        # Each logical request sees one 429 then success. The operation-wide
        # retry budget stops replays once it is spent.
        script: list = []
        for _ in range(module.MAX_OPERATION_RETRIES + 1):
            script += [_Response(429), _Response()]
        harness = _Harness(script)
        client = DecisionClient(api_key="fixture")
        outcomes: list[str] = []

        def action():
            with request_budget_scope(client, 16, deadline_seconds=60):
                for _ in range(module.MAX_OPERATION_RETRIES + 1):
                    try:
                        _decide(client)
                        outcomes.append("ok")
                    except JevRequestError as exc:
                        outcomes.append(exc.detail)

        _run(harness, action)
        self.assertEqual(outcomes[:-1], ["ok"] * module.MAX_OPERATION_RETRIES)
        self.assertEqual(outcomes[-1], "retry_budget_exhausted")
        self.assertEqual(client.rate_limit_retries, module.MAX_OPERATION_RETRIES)

    def test_other_http_errors_are_not_retried_and_hide_provider_text(self):
        for status, detail in ((401, "http_401"), (500, "http_5xx"), (400, "http_4xx"), (503, "http_5xx")):
            with self.subTest(status=status):
                harness = _Harness([_Response(status, body=_PROVIDER_SECRET_TEXT), _Response()])
                client = DecisionClient(api_key="fixture")
                with self.assertRaises(JevRequestError) as raised:
                    _run(harness, lambda: _decide(client))
                self.assertEqual(raised.exception.detail, detail)
                self.assertEqual(harness.http_requests, 1)
                self.assertNotIn("provider-internal", str(raised.exception))
                self.assertNotIn("sk-or", str(raised.exception))

    def test_sleep_is_interrupted_by_host_cancel(self):
        harness = _Harness([_Response(429, headers={"Retry-After": "4"}), _Response()])
        client = DecisionClient(api_key="fixture")
        calls = {"n": 0}

        def cancel_check():
            calls["n"] += 1
            return bool(harness.clock.sleeps)

        def action():
            with module.host_cancel_scope(cancel_check):
                return _decide(client)

        with self.assertRaises(module.HostCancelled):
            _run(harness, action)
        self.assertEqual(harness.http_requests, 1)
        self.assertLess(sum(harness.clock.sleeps), 4.0)


class AutomaticPreDecisionWarningTests(unittest.TestCase):
    def setUp(self):
        module._reset_warning_rate_limit()

    def _recommend(self, factory):
        recommender = AutomaticSkillRecommender(
            configured_candidates=[{"name": "docker-management", "description": "Manage containers"}],
            routing_mode="hosted_sanitized",
            hosted_mode="always",
            adoption_capable=True,
            client_factory=factory,
        )
        return recommender.recommend("manage containers")

    def test_client_construction_failure_warns_once_without_exception_text(self):
        def fail_factory():
            raise TimeoutError("SYNTHETIC_PRIVATE_MARKER")

        with self.assertLogs(module.logger, level=logging.WARNING) as captured:
            result = self._recommend(fail_factory)
        self.assertEqual(result["hosted_error_detail"], "timeout")
        warnings = [record.getMessage() for record in captured.records]
        self.assertEqual(len(warnings), 1)
        self.assertIn("(timeout)", warnings[0])
        self.assertNotIn("SYNTHETIC_PRIVATE_MARKER", warnings[0])

    def test_decision_failure_is_not_counted_twice(self):
        def fail_transport(_payload):
            raise TimeoutError("SYNTHETIC_PRIVATE_MARKER")

        with self.assertLogs(module.logger, level=logging.WARNING) as captured:
            result = self._recommend(lambda: DecisionClient(api_key="fixture", transport=fail_transport))
        self.assertEqual(result["hosted_error_detail"], "timeout")
        warnings = [record.getMessage() for record in captured.records]
        self.assertEqual(len(warnings), 1)
        self.assertIn("(timeout); 0 similar", warnings[0])

class StaleRetryAccountingTests(unittest.TestCase):
    def setUp(self):
        module._reset_warning_rate_limit()

    def test_ssl_eof_on_reuse_is_retried_once_and_recorded(self):
        harness = _Harness([_Response(), ssl.SSLEOFError("eof"), _Response()])
        client = DecisionClient(api_key="fixture")

        def action():
            _decide(client)
            return _decide(client)

        result = _run(harness, action)
        self.assertEqual(result["transport_retries"], {"stale_connection": 1})
        self.assertEqual(len(harness.connections), 2)

    def test_only_one_stale_retry_per_request_even_after_a_429(self):
        harness = _Harness([
            _Response(),
            http.client.RemoteDisconnected("closed"),
            _Response(429),
            _Response(),
        ])
        client = DecisionClient(api_key="fixture")

        def action():
            _decide(client)
            return _decide(client)

        result = _run(harness, action)
        self.assertEqual(result["transport_retries"], {"stale_connection": 1, "http_429": 1})

    def test_failed_stale_retry_reports_stale_connection_sub_code(self):
        harness = _Harness([
            _Response(),
            http.client.RemoteDisconnected("closed"),
            http.client.RemoteDisconnected("closed again"),
        ])
        client = DecisionClient(api_key="fixture")

        def action():
            _decide(client)
            return _decide(client)

        with self.assertRaises(JevRequestError) as raised:
            _run(harness, action)
        self.assertEqual(raised.exception.detail, "stale_connection")
        self.assertEqual(harness.http_requests, 3)

    def test_fresh_connection_failure_reports_connect_failed(self):
        harness = _Harness([ConnectionRefusedError(111, "refused")])
        client = DecisionClient(api_key="fixture")
        with self.assertRaises(JevRequestError) as raised:
            _run(harness, lambda: _decide(client))
        self.assertEqual(raised.exception.detail, "connect_failed")

    def test_stale_retry_is_not_used_when_operation_retry_budget_is_spent(self):
        harness = _Harness([_Response(), http.client.RemoteDisconnected("closed"), _Response()])
        client = DecisionClient(api_key="fixture")

        def action():
            _decide(client)
            with request_budget_scope(client, 4, deadline_seconds=60):
                client._retry_budget.get()[0] = 0
                return _decide(client)

        with self.assertRaises(JevRequestError) as raised:
            _run(harness, action)
        self.assertEqual(raised.exception.detail, "connect_failed")
        self.assertEqual(harness.http_requests, 2)


class RoutingMetadataTests(unittest.TestCase):
    def setUp(self):
        module._reset_warning_rate_limit()

    def test_hosted_selection_survives_a_stale_connection_with_bounded_request_count(self):
        body = (
            b'{"model":"typesafe/jev-1.13","answers":{'
            b'"skill":{"choice":"alpha","probabilities":{"alpha":0.95,"beta":0.05},"confidence":0.95},'
            b'"needs_skill":{"noul":0.95}},"usage":{"cost":0.0001}}'
        )
        harness = _Harness([
            _Response(body=body),
            http.client.RemoteDisconnected("closed"),
            _Response(body=body),
        ])
        client = DecisionClient(api_key="fixture")
        candidates = [
            {"name": "alpha", "description": "Alpha skill"},
            {"name": "beta", "description": "Beta skill"},
        ]

        def action():
            routing.select_skill(task="public task", candidates=candidates, client=client, deadline_seconds=20)
            return routing.select_skill(task="public task", candidates=candidates, client=client, deadline_seconds=20)

        result = _run(harness, action)
        self.assertEqual(result["selected"], "alpha")
        self.assertEqual(result["request_count"], 1)
        self.assertEqual(result["transport_retries"], {"stale_connection": 1})
        self.assertEqual(harness.http_requests, 3)

    def test_aggregate_metadata_merges_only_closed_set_retry_reasons(self):
        aggregate = routing._aggregate_metadata([
            {"request_count": 1, "transport_retries": {"http_429": 1}},
            {"request_count": 1, "transport_retries": {"http_429": 1, "stale_connection": 1, "provider text": 9}},
            {"request_count": 1, "transport_retries": "garbage"},
        ])
        self.assertEqual(aggregate["request_count"], 3)
        self.assertEqual(aggregate["transport_retries"], {"http_429": 2, "stale_connection": 1})


class SubCodeAndWarningTests(unittest.TestCase):
    def setUp(self):
        module._reset_warning_rate_limit()

    def test_sub_code_mapping_is_closed_set(self):
        cases = [
            JevRequestError("x", detail="http_429"),
            JevRequestError("x", detail="provider said something"),
            module.DeadlineExceeded("x"),
            module.HostCancelled("x"),
            module.LateResultDiscarded("x"),
            TimeoutError("x"),
            ValueError("Jev provider-request budget exceeded"),
            ValueError("bad"),
            TypeError("bad"),
            PermissionError("ack"),
            ConnectionRefusedError(),
            RuntimeError("anything"),
            module.PartialAccountingError("x", partial=[]),
        ]
        chained = module.PartialAccountingError("x", partial=[])
        chained.__cause__ = JevRequestError("x", detail="http_5xx")
        cases.append(chained)
        details = [hosted_error_detail(case) for case in cases]
        for detail in details:
            self.assertIn(detail, HOSTED_ERROR_DETAILS)
        self.assertEqual(details[0], "http_429")
        self.assertEqual(details[1], "unknown")
        self.assertEqual(details[-1], "http_5xx")

    def test_parse_retry_after(self):
        self.assertEqual(parse_retry_after("5"), 5.0)
        self.assertEqual(parse_retry_after(" 0 "), 0.0)
        self.assertIsNone(parse_retry_after("-1"))
        self.assertIsNone(parse_retry_after("soon"))
        self.assertIsNone(parse_retry_after(None))
        self.assertIsNone(parse_retry_after("9" * 100))
        self.assertAlmostEqual(
            parse_retry_after("Thu, 01 Jan 1970 00:00:10 GMT", now=4.0), 6.0
        )
        self.assertEqual(parse_retry_after("Thu, 01 Jan 1970 00:00:10 GMT", now=100.0), 0.0)

    def test_failure_warning_is_rate_limited_per_sub_code_and_has_no_provider_text(self):
        clock = _Clock()
        with mock.patch.object(module.time, "monotonic", side_effect=clock.monotonic), \
                self.assertLogs(module.logger, level=logging.WARNING) as logs:
            module._warn_hosted_failure("http_5xx")
            module._warn_hosted_failure("http_5xx")
            module._warn_hosted_failure("connect_failed")
            clock.now += module.HOSTED_FAILURE_WARNING_INTERVAL_SECONDS + 1
            module._warn_hosted_failure("http_5xx")
            module._warn_hosted_failure("host_cancelled")
        warnings = [record.getMessage() for record in logs.records]
        self.assertEqual(len(warnings), 3)
        self.assertIn("(http_5xx); 0 similar", warnings[0])
        self.assertIn("(connect_failed)", warnings[1])
        self.assertIn("(http_5xx); 1 similar", warnings[2])

    def test_decide_failure_logs_one_warning_with_sub_code_only(self):
        harness = _Harness([_Response(500, body=_PROVIDER_SECRET_TEXT)] * 3)
        client = DecisionClient(api_key="fixture")

        def action():
            for _ in range(3):
                with self.assertRaises(JevRequestError):
                    _decide(client)

        with self.assertLogs(module.logger, level=logging.WARNING) as logs:
            _run(harness, action)
        messages = [record.getMessage() for record in logs.records if record.levelno >= logging.WARNING]
        self.assertEqual(len(messages), 1)
        self.assertIn("http_5xx", messages[0])
        self.assertNotIn("provider-internal", messages[0])


if __name__ == "__main__":
    unittest.main()
