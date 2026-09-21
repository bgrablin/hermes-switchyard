"""Connection pinning for the DOM browser backend.

The guard decides a request before it is sent, but Chrome resolves the host again
to connect, so a rebinding DNS server can answer differently the second time. The
validating proxy closes that: Chrome sends every connection through it, the proxy
resolves once, validates every address, and connects to the validated address
literal. Layers, cheapest first:

* the proxy, with a stub resolver and a stub connector over loopback sockets;
* the guard's proxy-facing evidence;
* a real headless browser, with a negative control that proves the gap is real
  without the proxy. The listener is test-local; nothing private is browsed.
"""

from __future__ import annotations

import socket
import threading
import time
import unittest

from hermes_switchyard import browser_use, destination_policy
from hermes_switchyard.destination_policy import DestinationGuard, ValidatingProxy

from tests.test_browser_destination import LoopbackTrap, Wire, _assert_example_origin, _online, paused

PUBLIC = "93.184.216.34"


class EchoServer:
    """A loopback echo server that stands in for the validated public address."""

    def __init__(self):
        self._sock = socket.socket()
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            threading.Thread(target=self._echo, args=(conn,), daemon=True).start()

    @staticmethod
    def _echo(conn):
        conn.settimeout(2)
        try:
            while True:
                data = conn.recv(4096)
                if not data:
                    return
                conn.sendall(data)
        except OSError:
            return
        finally:
            conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


class ProxyHarness:
    def __init__(self, resolver, **kwargs):
        self.echo = EchoServer()
        self.resolved: list[str] = []
        self.connected: list[tuple[str, int]] = []
        self.refusals: list[tuple[str, str]] = []
        self._resolver = resolver

        def resolve(host):
            self.resolved.append(host)
            return self._resolver(host)

        def connect(address, port, timeout):
            self.connected.append((address, port))
            return socket.create_connection(("127.0.0.1", self.echo.port), timeout=timeout)

        self.proxy = ValidatingProxy(
            resolver=resolve,
            connector=connect,
            on_refusal=lambda code, host: self.refusals.append((code, host)),
            **kwargs,
        )
        self.port = self.proxy.start()

    def request(self, raw: bytes, *, read_body: bool = False):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=3)
        sock.sendall(raw)
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = sock.recv(4096)
            if not chunk:
                break
            head += chunk
        return sock, head.split(b"\r\n", 1)[0].decode("latin-1")

    def connect_line(self, authority: str):
        return self.request(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode())

    def close(self):
        self.proxy.stop()
        self.echo.close()


class ValidatingProxyTests(unittest.TestCase):
    def harness(self, resolver=lambda _h: [PUBLIC], **kwargs):
        h = ProxyHarness(resolver, **kwargs)
        self.addCleanup(h.close)
        return h

    def test_public_host_is_resolved_once_and_connected_by_validated_address(self):
        h = self.harness()
        sock, status = h.connect_line("example.com:443")
        self.addCleanup(sock.close)
        self.assertIn(" 200 ", status)
        sock.sendall(b"ping")
        self.assertEqual(sock.recv(16), b"ping")
        self.assertEqual(h.resolved, ["example.com"])
        self.assertEqual(h.connected, [(PUBLIC, 443)], "the connector receives the address, never the name")
        self.assertEqual(h.refusals, [])
        self.assertEqual(h.proxy.stats()["tunnels_opened"], 1)

    def test_every_tunnel_is_pinned_to_its_own_validated_answer(self):
        answers = iter([[PUBLIC], ["10.0.0.9"], [PUBLIC]])
        h = self.harness(resolver=lambda _h: next(answers))
        first, status_a = h.connect_line("rebind.example:443")
        self.addCleanup(first.close)
        second, status_b = h.connect_line("rebind.example:443")
        self.addCleanup(second.close)
        third, status_c = h.connect_line("rebind.example:443")
        self.addCleanup(third.close)
        self.assertIn(" 200 ", status_a)
        self.assertIn(" 403 ", status_b, "a later private answer is refused, not reused from the first")
        self.assertIn(" 200 ", status_c)
        self.assertEqual(h.connected, [(PUBLIC, 443), (PUBLIC, 443)])
        self.assertEqual(h.refusals, [("resolved_non_public", "rebind.example")])

    def test_answers_that_include_a_non_public_address_are_refused(self):
        cases = {
            "loopback": ["127.0.0.1"],
            "private": ["10.1.2.3"],
            "link_local": ["169.254.169.254"],
            "ipv6_loopback": ["::1"],
            "mapped": ["::ffff:192.168.0.9"],
            "mixed": [PUBLIC, "127.0.0.1"],
        }
        for name, answer in cases.items():
            with self.subTest(name=name):
                h = self.harness(resolver=lambda _h, a=answer: a)
                sock, status = h.connect_line("rebind.example:443")
                sock.close()
                self.assertIn(" 403 ", status)
                self.assertEqual(h.connected, [])
                self.assertEqual(h.refusals, [("resolved_non_public", "rebind.example")])

    def test_resolution_failure_or_empty_answer_fails_closed_without_connecting(self):
        def boom(_host):
            raise OSError("nxdomain")

        for name, resolver in {"raises": boom, "empty": lambda _h: []}.items():
            with self.subTest(name=name):
                h = self.harness(resolver=resolver)
                sock, status = h.connect_line("nope.example:443")
                sock.close()
                self.assertRegex(status, r" (403|502) ")
                self.assertEqual(h.connected, [])
                self.assertEqual(h.refusals, [("resolution_failed", "nope.example")])

    def test_literal_and_local_destinations_are_refused_before_any_resolution(self):
        cases = {
            "10.0.0.1:443": "non_public_address",
            "127.0.0.1:443": "non_public_address",
            "[::1]:443": "non_public_address",
            "[64:ff9b::7f00:1]:443": "non_public_address",
            "localhost:443": "local_hostname",
            "metadata.google.internal:443": "local_hostname",
            "0x7f.0.0.1:443": "numeric_host_form",
            "intranet:443": "single_label_host",
        }
        for authority, code in cases.items():
            with self.subTest(authority=authority):
                h = self.harness()
                sock, status = h.connect_line(authority)
                sock.close()
                self.assertIn(" 403 ", status)
                self.assertEqual(h.resolved, [])
                self.assertEqual(h.connected, [])
                self.assertEqual([c for c, _ in h.refusals], [code])

    def test_ip_literal_public_destination_is_not_resolved(self):
        h = self.harness()
        sock, status = h.connect_line("93.184.216.34:443")
        self.addCleanup(sock.close)
        self.assertIn(" 200 ", status)
        self.assertEqual(h.resolved, [])
        self.assertEqual(h.connected, [(PUBLIC, 443)])

    def test_plain_http_and_malformed_requests_are_refused(self):
        h = self.harness()
        for raw in (
            b"GET http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",
            b"POST http://example.com/ HTTP/1.1\r\nHost: example.com\r\n\r\n",
            b"CONNECT example.com HTTP/1.1\r\n\r\n",
            b"CONNECT example.com:0 HTTP/1.1\r\n\r\n",
            b"CONNECT example.com:70000 HTTP/1.1\r\n\r\n",
            b"CONNECT example.com:abc HTTP/1.1\r\n\r\n",
            b"CONNECT user@example.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT exa mple.com:443 HTTP/1.1\r\n\r\n",
            b"CONNECT example.com/path:443 HTTP/1.1\r\n\r\n",
            b"CONNECT example.com/path:443 HTTP/garbage\r\n\r\n",
            b"CONNECT example.com:443 HTTP/garbage\r\n\r\n",
            b"CONNECT example.com:443 HTTP/2.0\r\n\r\n",
            b"\x16\x03\x01garbage\r\n\r\n",
        ):
            with self.subTest(raw=raw[:30]):
                sock, status = h.request(raw)
                sock.close()
                self.assertRegex(status, r" (400|403) ", status)
        self.assertEqual(h.connected, [])
        self.assertEqual(h.resolved, [])

    def test_oversized_request_head_is_refused(self):
        h = self.harness()
        sock, status = h.request(b"CONNECT example.com:443 HTTP/1.1\r\nX: " + b"a" * 20000 + b"\r\n\r\n")
        sock.close()
        self.assertRegex(status, r" (400|431) ")
        self.assertEqual(h.connected, [])

    def test_stalled_client_is_dropped_after_the_head_timeout(self):
        h = self.harness(head_timeout=0.3)
        sock = socket.create_connection(("127.0.0.1", h.port), timeout=3)
        self.addCleanup(sock.close)
        sock.sendall(b"CONNECT example.com:443 HT")
        data = b""
        try:
            data = sock.recv(64)
        except OSError:
            pass
        self.assertTrue(data == b"" or data.startswith(b"HTTP/1.1 4"), data)
        self.assertEqual(h.connected, [])

    def test_idle_tunnels_are_closed(self):
        h = self.harness(idle_seconds=0.3)
        sock, status = h.connect_line("example.com:443")
        self.addCleanup(sock.close)
        self.assertIn(" 200 ", status)
        sock.settimeout(2)
        self.assertEqual(sock.recv(16), b"", "the proxy closes an idle tunnel")

    def test_concurrent_tunnels_are_bounded(self):
        h = self.harness(max_tunnels=1)
        first, status_a = h.connect_line("example.com:443")
        self.addCleanup(first.close)
        second, status_b = h.connect_line("example.com:443")
        self.addCleanup(second.close)
        self.assertIn(" 200 ", status_a)
        self.assertIn(" 503 ", status_b)

    def test_stop_releases_the_listener(self):
        h = self.harness()
        port = h.port
        h.proxy.stop()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=1).close()

    def test_default_connector_dials_the_address_literal(self):
        from hermes_switchyard import destination_policy

        echo = EchoServer()
        self.addCleanup(echo.close)
        sock = destination_policy._connect_pinned("127.0.0.1", echo.port, 2.0)
        self.addCleanup(sock.close)
        sock.sendall(b"x")
        self.assertEqual(sock.recv(4), b"x")
        with self.assertRaises(ValueError):
            destination_policy._connect_pinned("example.com", 443, 1.0)


class SessionCleanupTests(unittest.TestCase):
    def test_launch_failure_stops_the_proxy_and_leaves_no_thread(self):
        from unittest import mock

        before = {t.name for t in threading.enumerate()}
        with mock.patch.object(browser_use.subprocess, "Popen", side_effect=OSError("cannot launch")):
            with mock.patch.object(destination_policy, "default_resolver", return_value=[PUBLIC]):
                with self.assertRaises(OSError):
                    browser_use.ChromiumSession("https://example.com")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and any(
            t.name == "switchyard-pin-proxy" and t.name not in before for t in threading.enumerate()
        ):
            time.sleep(0.05)
        self.assertEqual([t.name for t in threading.enumerate() if t.name == "switchyard-pin-proxy"], [])

    def test_startup_policy_error_keeps_proxy_tunnel_counts(self):
        from pathlib import Path
        from unittest import mock

        class FakeProc:
            def poll(self):
                return None

            def terminate(self):
                return None

            def wait(self, timeout=None):
                return 0

            def kill(self):
                return None

        class FakeSocket:
            def set_blocking(self):
                return None

            def close(self):
                return None

            def recv_json(self):
                raise RuntimeError("reader stopped")

        def refuse_then_fail(session):
            sock = socket.create_connection(("127.0.0.1", session._proxy.port), timeout=2)
            sock.sendall(b"CONNECT 127.0.0.1:9 HTTP/1.1\r\nHost: 127.0.0.1:9\r\n\r\n")
            sock.recv(128)
            sock.close()
            time.sleep(0.05)
            raise browser_use.DestinationPolicyError("interception_unavailable")

        with mock.patch.object(destination_policy, "default_resolver", return_value=[PUBLIC]):
            with mock.patch.object(
                browser_use, "_browser_binary_details", return_value=(Path("/usr/bin/true"), "chromium", "none")
            ):
                with mock.patch.object(browser_use.subprocess, "Popen", return_value=FakeProc()):
                    with mock.patch.object(
                        browser_use, "_wait_debugger_url", return_value="ws://127.0.0.1:9/devtools/page/T"
                    ):
                        with mock.patch.object(browser_use, "_ChromeWebSocket", return_value=FakeSocket()):
                            with mock.patch.object(
                                browser_use.ChromiumSession, "_install_interception", refuse_then_fail
                            ):
                                with self.assertRaises(browser_use.DestinationPolicyError) as caught:
                                    browser_use.ChromiumSession("https://example.com")
        report = getattr(caught.exception, "report", None)
        self.assertIsInstance(report, dict)
        self.assertGreaterEqual(report.get("tunnels_refused", 0), 1)
        self.assertIn("tunnels_opened", report)


class GuardProxyEvidenceTests(unittest.TestCase):
    def test_a_rebinding_refusal_is_fatal_evidence(self):
        guard = DestinationGuard(Wire(), pinned=True)
        guard.record_proxy_refusal("resolved_non_public", "rebind.example")
        [violation] = guard.violations()
        self.assertEqual(violation["code"], "resolved_non_public")
        self.assertEqual(violation["detected"], "proxy_connect")
        self.assertTrue(violation["fatal"])
        self.assertFalse(violation["navigation"])
        self.assertEqual(violation["host"], "rebind.example")

    def test_plain_http_at_the_proxy_is_evidence_but_not_fatal(self):
        guard = DestinationGuard(Wire(), pinned=True)
        guard.record_proxy_refusal("scheme_not_allowed", "clients2.google.com")
        [violation] = guard.violations()
        self.assertFalse(violation["fatal"])

    def test_a_resolution_failure_at_the_proxy_is_evidence_but_not_fatal(self):
        guard = DestinationGuard(Wire(), pinned=True)
        guard.record_proxy_refusal("resolution_failed", "nope.example")
        [violation] = guard.violations()
        self.assertFalse(violation["fatal"], "no connection was made")

    def test_pinned_guard_leaves_address_judgement_to_the_proxy(self):
        message = {
            "method": "Network.responseReceived",
            "params": {
                "requestId": "1",
                "type": "Document",
                "response": {"url": "https://example.com/", "remoteIPAddress": "127.0.0.1"},
            },
        }
        pinned = DestinationGuard(Wire(), pinned=True)
        pinned.handle(message)
        self.assertEqual(pinned.violations(), [], "the address seen is the local proxy's")
        unpinned = DestinationGuard(Wire())
        unpinned.handle(message)
        self.assertEqual(len(unpinned.violations()), 1)

    def test_report_states_whether_connections_are_pinned(self):
        pinned = DestinationGuard(Wire(), pinned=True).report()
        self.assertTrue(pinned["connection_pinning"])
        self.assertNotIn("dns_answer_may_change_between_check_and_connect", pinned["residual_risks"])
        self.assertFalse(pinned["post_response_address_check"])
        unpinned = DestinationGuard(Wire()).report()
        self.assertFalse(unpinned["connection_pinning"])
        self.assertIn("dns_answer_may_change_between_check_and_connect", unpinned["residual_risks"])
        self.assertTrue(unpinned["post_response_address_check"])

    def test_requests_are_still_decided_before_they_are_sent(self):
        wire = Wire()
        guard = DestinationGuard(wire, pinned=True, resolver=lambda _h: [PUBLIC])
        guard.handle(paused("1", "https://example.com/"))
        guard.handle(paused("2", "https://127.0.0.1/", resource_type="Fetch"))
        self.assertEqual(len(wire.of("Fetch.continueRequest")), 1)
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)


# --------------------------------------------------------------------------- #
# Real browser
# --------------------------------------------------------------------------- #
class UdpTrap:
    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.datagrams = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                self._sock.recvfrom(2048)
            except (socket.timeout, OSError):
                continue
            self.datagrams += 1

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


class RealBrowserPinningTests(unittest.TestCase):
    REBIND_HOST = "rebind.example.test"

    @classmethod
    def setUpClass(cls):
        import os

        # Offline discovery and hosted CI stay offline. Real Chromium + public
        # origin navigations require an explicit opt-in.
        if os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") != "1":
            raise unittest.SkipTest(
                "set SWITCHYARD_LIVE_BROWSER_TESTS=1 to run live external-browser pinning tests"
            )
        binary, _, _ = browser_use._browser_binary_details()
        if binary is None:
            raise unittest.SkipTest("no Chromium-family browser is available")
        if not _online():
            raise unittest.SkipTest("the public example.com origin does not resolve here")

    def setUp(self):
        self.trap = LoopbackTrap()
        self.addCleanup(self.trap.close)

    def _rebinding_session(self, *, pin: bool):
        """A session whose guard sees a public answer while the browser is sent to loopback.

        The guard's resolver says public. Chrome is told, out of band, that the name
        maps to loopback, which is what a rebinding server's second answer does. With
        the proxy the browser never resolves the name itself.
        """
        return browser_use.ChromiumSession(
            "https://example.com",
            _pin_connections=pin,
            _guard_resolver=lambda _h: [PUBLIC],
            _proxy_resolver=lambda host: ["127.0.0.1"] if host == self.REBIND_HOST else destination_policy.default_resolver(host),
            _extra_args=[f"--host-resolver-rules=MAP {self.REBIND_HOST} 127.0.0.1"],
        )

    def _navigate_to_rebinding_host(self, session):
        session._evaluate(
            f"setTimeout(() => {{ location.href = 'https://{self.REBIND_HOST}:{self.trap.port}/x'; }}, 0); true"
        )
        deadline = time.monotonic() + 2.5
        while time.monotonic() < deadline:
            time.sleep(0.05)
        session._guard.wait_idle(2.0)

    def test_negative_control_the_gap_is_real_without_pinning(self):
        with self._rebinding_session(pin=False) as session:
            self._navigate_to_rebinding_host(session)
            self.assertGreater(self.trap.connections, 0, "without the proxy Chrome connects to loopback")
            self.assertFalse(session.destination_report()["connection_pinning"])

    def test_pinned_session_refuses_the_rebound_answer_before_any_connection(self):
        with self._rebinding_session(pin=True) as session:
            self._navigate_to_rebinding_host(session)
            self.assertEqual(self.trap.connections, 0)
            violations = session.destination_violations()
            self.assertTrue(
                any(v["code"] == "resolved_non_public" and v["detected"] == "proxy_connect" and v["fatal"] for v in violations),
                violations,
            )
            report = session.destination_report()
            self.assertTrue(report["connection_pinning"])
            self.assertGreaterEqual(report["tunnels_refused"], 1)
            with self.assertRaises(browser_use.DestinationPolicyError):
                session.raise_if_destination_blocked()

    def test_pinned_session_loads_public_pages_through_the_proxy(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            page = session.observe()
            _assert_example_origin(self, page["url"])
            report = session.destination_report()
            self.assertTrue(report["connection_pinning"])
            self.assertGreaterEqual(report["tunnels_opened"], 1)
            self.assertEqual(report["tunnels_refused"], 0)
            self.assertEqual(session.destination_violations(), [])
            self.assertTrue(report["interception_active"])

    def test_websocket_to_a_rebound_name_is_refused_at_connect(self):
        with self._rebinding_session(pin=True) as session:
            session._evaluate(
                f"(() => {{ try {{ new WebSocket('wss://{self.REBIND_HOST}:{self.trap.port}/s'); }} catch (e) {{}} return true; }})()"
            )
            time.sleep(2.0)
            session._guard.wait_idle(2.0)
            self.assertEqual(self.trap.connections, 0)
            self.assertTrue(any(v["detected"] == "proxy_connect" for v in session.destination_violations()))

    def test_loopback_targets_are_routed_to_the_proxy_not_bypassed(self):
        # A WebSocket is not paused by request interception, so only the proxy can
        # stop it. If Chrome bypassed the proxy for loopback the trap would connect.
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(
                f"(() => {{ try {{ new WebSocket('wss://127.0.0.1:{self.trap.port}/s'); }} catch (e) {{}} return true; }})()"
            )
            time.sleep(2.0)
            session._guard.wait_idle(2.0)
            self.assertEqual(self.trap.connections, 0)
            self.assertTrue(
                any(v["detected"] == "proxy_connect" and v["code"] == "non_public_address" for v in session.destination_violations()),
                session.destination_violations(),
            )

    def _stun_datagrams(self, **session_kwargs) -> int:
        udp = UdpTrap()
        self.addCleanup(udp.close)
        with browser_use.ChromiumSession("https://example.com", **session_kwargs) as session:
            session._evaluate(
                f"""(() => {{
                  const pc = new RTCPeerConnection({{iceServers: [{{urls: 'stun:127.0.0.1:{udp.port}'}}]}});
                  pc.createDataChannel('x');
                  pc.createOffer().then(o => pc.setLocalDescription(o)).catch(() => {{}});
                  return true;
                }})()"""
            )
            time.sleep(3.0)
        return udp.datagrams

    def test_negative_control_webrtc_udp_reaches_loopback_without_the_profile_preferences(self):
        self.assertGreater(self._stun_datagrams(_profile_overrides={"webrtc": {}}), 0)

    def test_non_proxied_udp_from_webrtc_does_not_reach_loopback(self):
        self.assertEqual(self._stun_datagrams(), 0)


if __name__ == "__main__":
    unittest.main()
