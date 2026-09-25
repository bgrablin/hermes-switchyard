"""Renderer-crash and shared-memory launch-flag regressions (issue #110).

A Wikipedia renderer crashed under Snap Chromium only when
``--disable-dev-shm-usage`` was passed, and the crash surfaced as a generic
15-second readiness timeout. These tests pin both corrections offline.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest import mock

from hermes_switchyard import browser_use
from hermes_switchyard.destination_policy import DestinationGuard

GIB = 1024 * 1024 * 1024


def _statvfs(size_bytes: int):
    return SimpleNamespace(f_frsize=4096, f_blocks=size_bytes // 4096)


@unittest.skipIf(os.name == "nt", "POSIX launch flags")
class SharedMemoryFlagTests(unittest.TestCase):
    def test_a_large_writable_dev_shm_keeps_shared_memory(self):
        with mock.patch.object(browser_use.os, "statvfs", return_value=_statvfs(16 * GIB)):
            with mock.patch.object(browser_use.os, "access", return_value=True):
                flags = browser_use._posix_launch_flags()
        self.assertEqual(flags, ["--disable-gpu"])

    def test_a_small_container_dev_shm_moves_shared_memory(self):
        with mock.patch.object(browser_use.os, "statvfs", return_value=_statvfs(64 * 1024 * 1024)):
            with mock.patch.object(browser_use.os, "access", return_value=True):
                flags = browser_use._posix_launch_flags()
        self.assertEqual(flags, ["--disable-gpu", "--disable-dev-shm-usage"])

    def test_a_missing_or_unwritable_dev_shm_moves_shared_memory(self):
        with mock.patch.object(browser_use.os, "statvfs", side_effect=OSError("absent")):
            self.assertIn("--disable-dev-shm-usage", browser_use._posix_launch_flags())
        with mock.patch.object(browser_use.os, "statvfs", return_value=_statvfs(16 * GIB)):
            with mock.patch.object(browser_use.os, "access", return_value=False):
                self.assertIn("--disable-dev-shm-usage", browser_use._posix_launch_flags())

    def test_a_real_directory_is_measured(self):
        with tempfile.TemporaryDirectory() as tmp:
            flags = browser_use._posix_launch_flags(tmp)
        self.assertEqual(flags[0], "--disable-gpu")

    def test_probe_and_session_share_the_same_flags(self):
        candidate = browser_use._Candidate(browser_use.Path("/usr/bin/true"), "chromium", "none", "discovered")
        with mock.patch.object(browser_use, "_posix_launch_flags", return_value=["--disable-gpu", "--x-marker"]):
            command = browser_use._probe_command(candidate, browser_use.Path("/nonexistent"), 9222, 9)
        self.assertEqual(command[1:3], ["--disable-gpu", "--x-marker"])
        # The destination protections are untouched by the flag change.
        self.assertIn("--proxy-bypass-list=<-loopback>", command)
        self.assertIn("--disable-quic", command)
        self.assertEqual(command[-1], "about:blank")


class _Socket:
    """A protocol socket fed by the test, one frame at a time."""

    def __init__(self):
        self.frames: list[dict] = []
        self.sent: list[dict] = []
        self.ready = threading.Condition()
        self.closed = False

    def feed(self, frame: dict) -> None:
        with self.ready:
            self.frames.append(frame)
            self.ready.notify_all()

    def recv_json(self) -> dict:
        with self.ready:
            self.ready.wait_for(lambda: self.frames or self.closed, timeout=30)
            if self.closed or not self.frames:
                raise OSError("closed")
            return self.frames.pop(0)

    def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    def close(self) -> None:
        with self.ready:
            self.closed = True
            self.ready.notify_all()


class _Guard:
    def __init__(self):
        self.submitted: list[dict] = []

    def submit(self, message: dict) -> None:
        self.submitted.append(message)

    def tracks_ack(self, _message_id: int) -> bool:
        return False

    def mark_lost(self, _code: str) -> None:
        return None


def _session(sock: _Socket, guard: _Guard) -> browser_use.ChromiumSession:
    session = object.__new__(browser_use.ChromiumSession)
    session._ws = sock  # type: ignore[assignment]
    session._guard = guard  # type: ignore[assignment]
    session._closing = False
    session._send_lock = threading.Lock()
    session._responses_ready = threading.Condition()
    session._awaiting = set()
    session._responses = {}
    session._reader_stopped = False
    session._target_crashed = False
    session._next_id = 0
    session._reader = threading.Thread(target=session._read_loop, daemon=True)
    session._reader.start()
    return session


class RendererCrashTests(unittest.TestCase):
    def _setup_receipt(self, failing_method, error):
        attempted = []
        client = mock.Mock()
        client.decide.side_effect = AssertionError("no provider request before navigation")

        @contextmanager
        def opening(_url, **_kwargs):
            session = object.__new__(browser_use.ChromiumSession)
            session._guard = DestinationGuard(lambda *_args, **_kw: 1)

            def send(method, **_params):
                attempted.append(method)
                if method == failing_method:
                    raise error
                return {}

            with mock.patch.object(session, "_cdp", side_effect=send):
                session._install_interception()
                yield session

        with (
            mock.patch.object(browser_use, "open_browser_session", side_effect=opening),
            mock.patch.object(browser_use, "request_budget_scope") as scope,
        ):
            scope.return_value.__enter__.return_value = None
            scope.return_value.__exit__.return_value = None
            receipt = browser_use.run_browser_goal(
                goal="Read the public article", client=client,
                start_url="https://en.wikipedia.org/wiki/Hermes", max_steps=2,
            )
        client.decide.assert_not_called()
        return receipt, attempted

    def test_crash_during_each_interception_setup_keeps_the_startup_reason(self):
        setup_methods = ["Fetch.enable", "Network.enable", "Target.setAutoAttach"]
        for index, method in enumerate(setup_methods):
            with self.subTest(method=method):
                receipt, attempted = self._setup_receipt(method, browser_use.BrowserTargetCrashedError())
                self.assertEqual(attempted, setup_methods[: index + 1])
                self.assertEqual(receipt["status"], "blocked")
                self.assertEqual(receipt["failure_phase"], "browser_startup")
                self.assertEqual(receipt["failure_reason"], "renderer_crashed")
                self.assertEqual(receipt["browser_startup"]["reason"], "renderer_crashed")
                self.assertEqual(receipt["jev_request_count"], 0)
                self.assertEqual(receipt["attempted_action_count"], 0)
                self.assertFalse(receipt["reconcile_before_retry"])

    def test_non_crash_interception_failure_remains_a_policy_refusal(self):
        receipt, attempted = self._setup_receipt("Network.enable", RuntimeError("CDP failed"))
        self.assertEqual(attempted, ["Fetch.enable", "Network.enable"])
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["failure_phase"], "destination_policy")
        self.assertEqual(receipt["failure_reason"], "interception_unavailable")
        self.assertEqual(receipt["jev_request_count"], 0)

    def test_crash_during_initial_observation_keeps_capture_reason(self):
        session = mock.Mock()
        session.observe.side_effect = browser_use.BrowserTargetCrashedError()
        client = mock.Mock()
        client.decide.side_effect = AssertionError("no provider request after a renderer crash")
        with mock.patch.object(browser_use, "request_budget_scope") as scope:
            scope.return_value.__enter__.return_value = None
            scope.return_value.__exit__.return_value = None
            receipt = browser_use.run_browser_goal(
                goal="Read the public article", session=session, client=client, max_steps=2,
            )
        self.assertEqual(receipt["status"], "blocked")
        self.assertEqual(receipt["failure_phase"], "capture")
        self.assertEqual(receipt.get("failure_reason"), "renderer_crashed")
        self.assertEqual(receipt["jev_request_count"], 0)
        self.assertEqual(receipt["attempted_action_count"], 0)
        self.assertFalse(receipt["reconcile_before_retry"])
        session.observe.assert_called_once_with()
        client.decide.assert_not_called()

    def _run(self, frame_after_send):
        sock, guard = _Socket(), _Guard()
        session = _session(sock, guard)
        outcome: dict = {}

        def call():
            started = time.monotonic()
            try:
                outcome["value"] = session._cdp("Runtime.evaluate", expression="document.readyState")
            except BaseException as exc:  # noqa: BLE001
                outcome["error"] = exc
            outcome["seconds"] = time.monotonic() - started

        worker = threading.Thread(target=call)
        worker.start()
        deadline = time.monotonic() + 5
        while not sock.sent and time.monotonic() < deadline:
            time.sleep(0.01)
        frame_after_send(sock, sock.sent[0]["id"])
        worker.join(timeout=10)
        sock.close()
        return session, guard, outcome

    def test_a_root_renderer_crash_fails_the_pending_command_immediately(self):
        session, guard, outcome = self._run(lambda sock, _id: sock.feed({"method": "Inspector.targetCrashed", "params": {}}))
        self.assertIsInstance(outcome.get("error"), browser_use.BrowserTargetCrashedError)
        self.assertLess(outcome["seconds"], 5, "must not wait for the 15 second command timeout")
        self.assertEqual(browser_use._failure_reason(outcome["error"]), "renderer_crashed")
        self.assertEqual(browser_use._startup_failure_reason(outcome["error"]), "renderer_crashed")
        # The crash is still visible to the destination guard.
        self.assertTrue(any(m.get("method") == "Inspector.targetCrashed" for m in guard.submitted))
        # Every later command also fails closed without waiting.
        started = time.monotonic()
        with self.assertRaises(browser_use.BrowserTargetCrashedError):
            session._cdp("Runtime.evaluate", expression="location.href")
        self.assertLess(time.monotonic() - started, 1)

    def test_a_child_target_crash_does_not_fail_the_page(self):
        def feed(sock, message_id):
            sock.feed({"method": "Inspector.targetCrashed", "params": {}, "sessionId": "child-1"})
            sock.feed({"id": message_id, "result": {"result": {"value": "complete"}}})

        _session_obj, _guard, outcome = self._run(feed)
        self.assertNotIn("error", outcome)
        self.assertEqual(outcome["value"], {"result": {"value": "complete"}})

    def test_a_reply_that_arrives_first_is_still_returned(self):
        def feed(sock, message_id):
            sock.feed({"id": message_id, "result": {"result": {"value": "interactive"}}})
            sock.feed({"method": "Inspector.targetCrashed", "params": {}})

        _session_obj, _guard, outcome = self._run(feed)
        self.assertEqual(outcome["value"], {"result": {"value": "interactive"}})

    def test_a_crash_is_a_browser_startup_failure_not_a_provider_request(self):
        client = mock.Mock()
        client.decide.side_effect = AssertionError("no provider request after a renderer crash")

        def crashing_session(_url, **_kwargs):
            raise browser_use.BrowserTargetCrashedError()

        with (
            mock.patch.object(browser_use, "open_browser_session", side_effect=crashing_session),
            mock.patch.object(browser_use, "request_budget_scope") as scope,
        ):
            scope.return_value.__enter__.return_value = None
            scope.return_value.__exit__.return_value = None
            receipt = browser_use.run_browser_goal(
                goal="Open the Zeus article",
                client=client,
                start_url="https://en.wikipedia.org/wiki/Hermes",
                max_steps=2,
            )
        self.assertEqual(receipt["status"], "blocked", receipt.get("failure_reason"))
        self.assertEqual(receipt["failure_phase"], "browser_startup")
        self.assertEqual(receipt["failure_reason"], "renderer_crashed")
        self.assertEqual(receipt["browser_startup"]["reason"], "renderer_crashed")
        client.decide.assert_not_called()


if __name__ == "__main__":
    unittest.main()
