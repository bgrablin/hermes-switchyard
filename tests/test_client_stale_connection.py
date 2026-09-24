"""Synthetic pooled-connection reuse after a server-side idle close (issue #92).

These in-memory connections simulate ``RemoteDisconnected`` before any response
byte on reuse. They test the fallback and conservative idle threshold; they do
not measure the hosted route's real idle-close timing or reach the network.
"""
from __future__ import annotations

import http.client
import unittest
from unittest import mock

from hermes_switchyard import client as module
from hermes_switchyard.client import DecisionClient

_OK_BODY = b'{"model":"typesafe/jev-1.13","answers":{"answer":{"noul":0.9}},"usage":{}}'
_QUESTIONS = {"answer": {"type": "noul", "instructions": "Is it true?"}}


class _Response:
    def __init__(self, body: bytes = _OK_BODY, status: int = 200):
        self.body = body
        self.status = status
        self.reason = "fixture"
        self.headers = {}
        self.will_close = False

    def read(self, size: int | None = None) -> bytes:
        return self.body if size is None else self.body[:size]

    def close(self) -> None:
        pass


class _Connection:
    """Fake HTTPSConnection. ``failures`` lists what each request raises."""

    def __init__(self, failures=()):
        self.failures = list(failures)
        self.requests = 0
        self.closed = False
        self.sock = None

    def request(self, *_args, **_kwargs):
        self.requests += 1
        if self.failures:
            failure = self.failures.pop(0)
            if failure is not None:
                raise failure

    def getresponse(self):
        return _Response()

    def close(self):
        self.closed = True


def _decide(client: DecisionClient):
    return client.decide("public", _QUESTIONS, public_or_sanitized_data_ack=True)


class StaleConnectionRetryTests(unittest.TestCase):
    def _run_two_calls(self, first: _Connection, second_failure: BaseException | None, clock: list[float]):
        """Make one successful call on ``first``; then fail its reuse with ``second_failure``."""
        replacement = _Connection()
        factory = mock.Mock(side_effect=[first, replacement])
        with mock.patch.object(module.http.client, "HTTPSConnection", factory), \
                mock.patch.object(module.time, "monotonic", side_effect=lambda: clock[0]):
            client = DecisionClient(api_key="fixture")
            _decide(client)
            first.failures.append(second_failure)
            clock[0] += 5.0
            result = _decide(client)
        return client, factory, replacement, result

    def test_reused_connection_dropped_by_server_is_retried_once_on_a_new_connection(self):
        first = _Connection()
        client, factory, replacement, result = self._run_two_calls(
            first, http.client.RemoteDisconnected("Remote end closed connection without response"), [1000.0]
        )
        self.assertEqual(result["answers"]["answer"]["noul"], 0.9)
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(first.closed)
        self.assertEqual(replacement.requests, 1)
        self.assertEqual(client.stale_connection_retries, 1)

    def test_reset_and_broken_pipe_on_reuse_are_retried(self):
        for failure in (ConnectionResetError(104, "reset"), BrokenPipeError(32, "broken pipe")):
            with self.subTest(failure=type(failure).__name__):
                first = _Connection()
                client, factory, replacement, _result = self._run_two_calls(first, failure, [1000.0])
                self.assertEqual(factory.call_count, 2)
                self.assertEqual(replacement.requests, 1)
                self.assertEqual(client.stale_connection_retries, 1)

    def test_failure_on_a_fresh_connection_is_not_retried(self):
        fresh = _Connection([http.client.RemoteDisconnected("closed")])
        factory = mock.Mock(side_effect=[fresh, _Connection()])
        with mock.patch.object(module.http.client, "HTTPSConnection", factory):
            client = DecisionClient(api_key="fixture")
            with self.assertRaises(RuntimeError) as raised:
                _decide(client)
        self.assertIn("RemoteDisconnected", str(raised.exception))
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(fresh.requests, 1)
        self.assertEqual(client.stale_connection_retries, 0)

    def test_second_failure_after_the_retry_is_raised(self):
        first = _Connection()
        replacement = _Connection([http.client.RemoteDisconnected("closed again")])
        factory = mock.Mock(side_effect=[first, replacement])
        with mock.patch.object(module.http.client, "HTTPSConnection", factory):
            client = DecisionClient(api_key="fixture")
            _decide(client)
            first.failures.append(http.client.RemoteDisconnected("closed"))
            with self.assertRaises(RuntimeError):
                _decide(client)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(replacement.requests, 1)

    def test_timeout_on_reuse_is_not_retried(self):
        first = _Connection()
        factory = mock.Mock(side_effect=[first, _Connection()])
        with mock.patch.object(module.http.client, "HTTPSConnection", factory):
            client = DecisionClient(api_key="fixture")
            _decide(client)
            first.failures.append(TimeoutError("timed out"))
            with self.assertRaises((RuntimeError, TimeoutError)):
                _decide(client)
        self.assertEqual(factory.call_count, 1)

    def test_connection_idle_past_the_limit_is_replaced_before_reuse(self):
        clock = [1000.0]
        first = _Connection()
        replacement = _Connection()
        factory = mock.Mock(side_effect=[first, replacement])
        with mock.patch.object(module.http.client, "HTTPSConnection", factory), \
                mock.patch.object(module.time, "monotonic", side_effect=lambda: clock[0]):
            client = DecisionClient(api_key="fixture")
            _decide(client)
            clock[0] += module.MAX_CONNECTION_IDLE_SECONDS + 1
            _decide(client)
        self.assertEqual(factory.call_count, 2)
        self.assertTrue(first.closed)
        self.assertEqual(first.requests, 1)
        self.assertEqual(replacement.requests, 1)
        self.assertEqual(client.stale_connection_retries, 0)

    def test_connection_within_the_idle_limit_is_reused(self):
        clock = [1000.0]
        first = _Connection()
        factory = mock.Mock(side_effect=[first, _Connection()])
        with mock.patch.object(module.http.client, "HTTPSConnection", factory), \
                mock.patch.object(module.time, "monotonic", side_effect=lambda: clock[0]):
            client = DecisionClient(api_key="fixture")
            _decide(client)
            clock[0] += module.MAX_CONNECTION_IDLE_SECONDS - 1
            _decide(client)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(first.requests, 2)

    def test_idle_limit_is_configurable_and_bounded(self):
        self.assertEqual(module.MAX_CONNECTION_IDLE_SECONDS, 60.0)
        clock = [1000.0]
        factory = mock.Mock(side_effect=[_Connection(), _Connection()])
        with mock.patch.object(module.http.client, "HTTPSConnection", factory), \
                mock.patch.object(module.time, "monotonic", side_effect=lambda: clock[0]):
            client = DecisionClient(api_key="fixture", max_connection_idle_seconds=5)
            _decide(client)
            clock[0] += 6
            _decide(client)
        self.assertEqual(factory.call_count, 2)
        with self.assertRaises(ValueError):
            DecisionClient(api_key="fixture", max_connection_idle_seconds=float("inf"))
        with self.assertRaises(ValueError):
            DecisionClient(api_key="fixture", max_connection_idle_seconds=0)


if __name__ == "__main__":
    unittest.main()
