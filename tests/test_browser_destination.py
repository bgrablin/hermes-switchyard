"""Destination-boundary checks for the DOM browser backend.

Layers, from cheapest to most native:

* the code-owned policy (pure, lexical) and its resolution check (stub resolver);
* the CDP interception guard, driven with synthetic protocol events;
* the browser loop, driven with scripted providers and fake sessions;
* a real headless browser, driven against a loopback listener that must record
  zero connections. The listener is test-local; nothing private is browsed.
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from unittest import mock
from urllib.parse import urlsplit

from hermes_switchyard import browser_use, destination_policy
from hermes_switchyard.destination_policy import (
    DestinationGuard,
    DestinationPolicyError,
    check_address,
    check_destination,
    redact_url,
)
from hermes_switchyard.browser_use import run_browser_goal


def _assert_example_origin(case: unittest.TestCase, url: str) -> None:
    """Assert *url* is exactly the example.com https origin.

    Substring checks (``startswith``) would also accept ``example.com.evil``
    or a credentialed redirect form, so the parsed components are compared.
    """
    parts = urlsplit(url)
    case.assertEqual(parts.scheme, "https", url)
    case.assertEqual(parts.hostname, "example.com", url)


def _public_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


# --------------------------------------------------------------------------- #
# Policy
# --------------------------------------------------------------------------- #
# Fullwidth digits that a browser's IDNA mapping turns into 127, built from code
# points so the fixture is unambiguous in any editor.
FULLWIDTH_127_URL = "https://" + "".join(map(chr, (0xFF11, 0xFF12, 0xFF17))) + ".0.0.1/"


class DestinationPolicyTests(unittest.TestCase):
    DENIED = {
        # schemes that are not an approved public https destination
        "file:///etc/hosts": "scheme_not_allowed",
        "data:text/html,hello": "scheme_not_allowed",
        "javascript:alert(1)": "scheme_not_allowed",
        "about:blank": "scheme_not_allowed",
        "blob:https://example.com/abc": "scheme_not_allowed",
        "ftp://example.com/": "scheme_not_allowed",
        "chrome://settings": "scheme_not_allowed",
        "http://example.com/": "scheme_not_allowed",
        "ws://example.com/": "scheme_not_allowed",
        # credentials
        "https://user:pw@example.com/": "credentialed_url",
        "https://user@example.com/": "credentialed_url",
        "https://:pw@example.com/": "credentialed_url",
        "https://@example.com/": "credentialed_url",
        # loopback / private / link-local literals
        "https://127.0.0.1/": "non_public_address",
        "https://[::1]/": "non_public_address",
        "https://10.0.0.1/": "non_public_address",
        "https://192.168.1.1/": "non_public_address",
        "https://172.16.5.5/": "non_public_address",
        "https://169.254.169.254/latest/meta-data": "non_public_address",
        "https://100.64.0.1/": "non_public_address",
        "https://0.0.0.0/": "non_public_address",
        "https://[::ffff:7f00:1]/": "non_public_address",
        "https://[::ffff:10.0.0.1]/": "non_public_address",
        "https://[64:ff9b::7f00:1]/": "non_public_address",
        "https://[2002:7f00:1::]/": "non_public_address",
        "https://[::7f00:1]/": "non_public_address",
        "https://[fe80::1]/": "non_public_address",
        "https://[fd00::1]/": "non_public_address",
        # numeric host spellings a browser resolves to an address
        "https://0x7f.0.0.1/": "numeric_host_form",
        "https://0177.0.0.1/": "numeric_host_form",
        "https://2130706433/": "numeric_host_form",
        "https://127.1/": "numeric_host_form",
        "https://0x7f000001/": "numeric_host_form",
        # local names
        "https://localhost/": "local_hostname",
        "https://localhost./": "local_hostname",
        "https://app.localhost/": "local_hostname",
        "https://printer.local/": "local_hostname",
        "https://metadata.google.internal/": "local_hostname",
        "https://files.lan/": "local_hostname",
        "https://router.home.arpa/": "local_hostname",
        "https://intranet/": "single_label_host",
        # encodings a browser normalizes into something else
        "https://%31%32%37.0.0.1/": "invalid_host_characters",
        FULLWIDTH_127_URL: "non_public_address",
        "https://example.com\\@127.0.0.1/": "invalid_url",
        "https://example.com/\n": "invalid_url",
        "https://example.com/ x": "invalid_url",
        "https://exa mple.com/": "invalid_url",
        "": "invalid_url",
        "https:///path": "missing_host",
    }

    ALLOWED = (
        "https://example.com/",
        "https://en.wikipedia.org/wiki/Cat",
        "https://www.example.org/a/b?q=1#frag",
        "https://93.184.216.34/",
        "https://[2606:4700:4700::1111]/",
        "https://xn--bcher-kva.de/",
        "https://bücher.de/",
        "https://EXAMPLE.com./",
    )

    def test_denied_destinations_carry_a_bounded_reason_code(self):
        for url, code in self.DENIED.items():
            with self.subTest(url=url):
                decision = check_destination(url)
                self.assertFalse(decision.allowed, url)
                self.assertEqual(decision.code, code, url)

    def test_public_https_destinations_are_allowed(self):
        for url in self.ALLOWED:
            with self.subTest(url=url):
                decision = check_destination(url)
                self.assertTrue(decision.allowed, (url, decision))
                self.assertEqual(decision.code, "allowed")

    def test_legacy_lexical_check_is_the_same_policy(self):
        for url in self.DENIED:
            with self.subTest(url=url):
                self.assertFalse(browser_use._public_http_url(url), url)
        for url in self.ALLOWED:
            with self.subTest(url=url):
                self.assertTrue(browser_use._public_http_url(url), url)

    def test_subresource_may_be_data_or_blob_but_never_a_document(self):
        self.assertTrue(check_destination("data:image/png;base64,AAAA", resource_type="Image").allowed)
        self.assertTrue(check_destination("blob:https://example.com/x", resource_type="Media").allowed)
        self.assertFalse(check_destination("data:text/html,x", resource_type="Document").allowed)
        self.assertFalse(check_destination("blob:https://example.com/x", resource_type="Document").allowed)
        self.assertFalse(check_destination("file:///etc/hosts", resource_type="Image").allowed)
        self.assertFalse(check_destination("javascript:1", resource_type="Script").allowed)

    def test_websocket_requires_a_secure_public_destination(self):
        self.assertTrue(check_destination("wss://example.com/s", resource_type="WebSocket").allowed)
        self.assertFalse(check_destination("ws://example.com/s", resource_type="WebSocket").allowed)
        self.assertFalse(check_destination("wss://127.0.0.1/s", resource_type="WebSocket").allowed)

    def test_resolution_to_a_non_public_range_is_refused(self):
        cases = {
            "loopback": ["127.0.0.1"],
            "private": ["10.1.2.3"],
            "link_local": ["169.254.169.254"],
            "ipv6_loopback": ["::1"],
            "mapped": ["::ffff:192.168.0.9"],
            "nat64": ["64:ff9b::a00:1"],
            "mixed_public_and_private": ["93.184.216.34", "127.0.0.1"],
        }
        for name, addresses in cases.items():
            with self.subTest(name=name):
                decision = check_destination(
                    "https://rebind.example/", resolve=True, resolver=lambda _h, a=addresses: a
                )
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, "resolved_non_public")

    def test_resolution_failure_or_empty_answer_fails_closed(self):
        def boom(_host):
            raise OSError("no resolver")

        for name, resolver in {"raises": boom, "empty": lambda _h: []}.items():
            with self.subTest(name=name):
                decision = check_destination("https://example.com/", resolve=True, resolver=resolver)
                self.assertFalse(decision.allowed)
                self.assertEqual(decision.code, "resolution_failed")

    def test_public_resolution_is_allowed_and_ip_literals_are_not_resolved(self):
        calls: list[str] = []

        def resolver(host):
            calls.append(host)
            return ["93.184.216.34"]

        self.assertTrue(check_destination("https://example.com/", resolve=True, resolver=resolver).allowed)
        self.assertTrue(check_destination("https://93.184.216.34/", resolve=True, resolver=resolver).allowed)
        self.assertEqual(calls, ["example.com"])

    def test_address_check_covers_post_response_evidence(self):
        self.assertTrue(check_address("93.184.216.34").allowed)
        self.assertTrue(check_address("[2606:4700:4700::1111]").allowed)
        for value in ("127.0.0.1", "[::1]", "10.0.0.5", "not-an-ip", ""):
            with self.subTest(value=value):
                self.assertFalse(check_address(value).allowed)

    def test_evidence_never_carries_credentials_path_or_query(self):
        decision = check_destination("https://user:hunter2@127.0.0.1:8443/secret?token=abc")
        evidence = decision.evidence()
        self.assertEqual(set(evidence), {"code", "scheme", "host"})
        text = json.dumps(evidence)
        for leaked in ("hunter2", "user", "secret", "token", "abc", "8443"):
            self.assertNotIn(leaked, text)
        self.assertEqual(redact_url("https://user:hunter2@127.0.0.1:8443/secret?token=abc"), "https://127.0.0.1/")
        self.assertEqual(redact_url("file:///forbidden/local-secret"), "file:")
        self.assertEqual(redact_url("javascript:fetch('//x')"), "javascript:")
        self.assertEqual(redact_url("https://example.com/a?b=c"), "https://example.com/a?b=c")
        self.assertEqual(redact_url("https://example.com/\n"), "")


# --------------------------------------------------------------------------- #
# Guard (CDP interception), driven with synthetic events
# --------------------------------------------------------------------------- #
class Wire:
    """Records every command the guard sends."""

    def __init__(self):
        self.sent: list[dict] = []
        self._next = 100

    def __call__(self, method, params=None, session_id=None):
        self._next += 1
        self.sent.append({"id": self._next, "method": method, "params": params or {}, "sessionId": session_id})
        return self._next

    def of(self, method):
        return [item for item in self.sent if item["method"] == method]


def paused(request_id, url, *, resource_type="Document", redirected_from=None, session_id=None, frame="F1"):
    params = {
        "requestId": request_id,
        "request": {"url": url, "method": "GET"},
        "frameId": frame,
        "resourceType": resource_type,
    }
    if redirected_from is not None:
        params["redirectedRequestId"] = redirected_from
    message = {"method": "Fetch.requestPaused", "params": params}
    if session_id:
        message["sessionId"] = session_id
    return message


class DestinationGuardTests(unittest.TestCase):
    def guard(self, resolver=_public_resolver, **kwargs):
        wire = Wire()
        return DestinationGuard(wire, resolver=resolver, **kwargs), wire

    def test_allowed_same_origin_and_public_requests_continue(self):
        guard, wire = self.guard()
        guard.handle(paused("1", "https://example.com/"))
        guard.handle(paused("2", "https://example.com/app.js", resource_type="Script"))
        guard.handle(paused("3", "https://cdn.example.net/x.png", resource_type="Image"))
        guard.handle(paused("4", "data:image/png;base64,AAAA", resource_type="Image"))
        self.assertEqual([c["params"]["requestId"] for c in wire.of("Fetch.continueRequest")], ["1", "2", "3", "4"])
        self.assertEqual(wire.of("Fetch.failRequest"), [])
        report = guard.report()
        self.assertEqual(report["requests_checked"], 4)
        self.assertEqual(report["requests_blocked"], 0)
        self.assertEqual(guard.violations(), [])

    def test_public_to_private_redirect_is_blocked_at_the_hop(self):
        guard, wire = self.guard(resolver=lambda host: ["10.0.0.7"] if host == "internal.example" else ["93.184.216.34"])
        guard.handle(paused("1", "https://example.com/start"))
        guard.handle(paused("2", "https://internal.example/admin", redirected_from="1"))
        self.assertEqual(len(wire.of("Fetch.continueRequest")), 1)
        failed = wire.of("Fetch.failRequest")
        self.assertEqual(failed[0]["params"], {"requestId": "2", "errorReason": "BlockedByClient"})
        [violation] = guard.violations()
        self.assertEqual(violation["code"], "resolved_non_public")
        self.assertTrue(violation["navigation"])
        self.assertTrue(violation["redirected"])
        self.assertTrue(violation["fatal"])

    def test_same_host_http_redirect_is_upgraded_before_network_dispatch(self):
        guard, wire = self.guard()
        guard.handle(paused("1", "https://www.iana.org/domains/example"))
        guard.handle(paused("2", "http://www.iana.org/help/example-domains?q=1", redirected_from="1"))
        self.assertEqual(wire.of("Fetch.fulfillRequest")[-1]["params"], {
            "requestId": "2", "responseCode": 307,
            "responseHeaders": [{"name": "Location", "value": "https://www.iana.org/help/example-domains?q=1"}],
        })
        self.assertEqual(len(wire.of("Fetch.continueRequest")), 1)
        self.assertEqual(wire.of("Fetch.failRequest"), [])
        self.assertEqual(guard.report()["redirect_hops"], 1)

    def test_synthetic_https_follow_up_uses_one_server_redirect_hop(self):
        guard, wire = self.guard(max_redirects=1)
        guard.handle(paused("1", "https://www.iana.org/domains/example"))
        guard.handle(paused("2", "http://www.iana.org/help/example-domains", redirected_from="1"))
        self.assertEqual(len(wire.of("Fetch.fulfillRequest")), 1)
        guard.handle(paused("3", "https://www.iana.org/help/example-domains", redirected_from="2"))
        self.assertEqual([c["params"]["requestId"] for c in wire.of("Fetch.continueRequest")], ["1", "3"])
        self.assertEqual(wire.of("Fetch.failRequest"), [])
        self.assertEqual(guard.report()["redirect_hops"], 1)

        guard.handle(paused("4", "https://www.iana.org/another", redirected_from="3"))
        self.assertEqual([c["params"]["requestId"] for c in wire.of("Fetch.failRequest")], ["4"])
        self.assertEqual(guard.violations()[0]["code"], "redirect_limit")

    def test_only_exact_synthetic_https_follow_up_reuses_redirect_budget(self):
        for url in (
            "https://www.iana.org/other",
            "https://other.example/help/example-domains",
            "https://192.168.1.1/help/example-domains",
            "http://www.iana.org/help/example-domains",
        ):
            with self.subTest(url=url):
                guard, wire = self.guard(max_redirects=1)
                guard.handle(paused("1", "https://www.iana.org/domains/example"))
                guard.handle(paused("2", "http://www.iana.org/help/example-domains", redirected_from="1"))
                guard.handle(paused("3", url, redirected_from="2"))
                self.assertEqual([c["params"]["requestId"] for c in wire.of("Fetch.failRequest")], ["3"])
                self.assertEqual([c["params"]["requestId"] for c in wire.of("Fetch.continueRequest")], ["1"])

    def test_http_downgrade_to_another_host_or_private_address_stays_blocked(self):
        for url in ("http://other.example/help", "http://127.0.0.1/help", "http://user@www.iana.org/help"):
            with self.subTest(url=url):
                guard, wire = self.guard()
                guard.handle(paused("1", "https://www.iana.org/domains/example"))
                guard.handle(paused("2", url, redirected_from="1"))
                self.assertEqual(len(wire.of("Fetch.continueRequest")), 1)
                self.assertEqual(len(wire.of("Fetch.failRequest")), 1)

    def test_http_upgrade_requires_an_approved_prior_request(self):
        guard, wire = self.guard(resolver=lambda _host: ["10.0.0.1"])
        guard.handle(paused("1", "https://www.iana.org/start"))
        guard.handle(paused("2", "http://www.iana.org/next", redirected_from="1"))
        self.assertEqual(wire.of("Fetch.continueRequest"), [])
        self.assertEqual(wire.of("Fetch.fulfillRequest"), [])
        self.assertEqual(len(wire.of("Fetch.failRequest")), 2)

    def test_http_upgrade_cannot_exceed_redirect_budget(self):
        guard, wire = self.guard(max_redirects=0)
        guard.handle(paused("1", "https://www.iana.org/start"))
        guard.handle(paused("2", "http://www.iana.org/next", redirected_from="1"))
        self.assertEqual(len(wire.of("Fetch.continueRequest")), 1)
        self.assertEqual(wire.of("Fetch.fulfillRequest"), [])
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)

    def test_redirect_to_a_literal_private_address_is_blocked(self):
        guard, wire = self.guard()
        guard.handle(paused("1", "https://example.com/"))
        guard.handle(paused("2", "https://127.0.0.1/", redirected_from="1"))
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)
        self.assertEqual(guard.violations()[0]["code"], "non_public_address")

    def test_redirect_chain_drift_is_blocked_at_the_hop_that_drifts(self):
        guard, wire = self.guard()
        guard.handle(paused("1", "https://example.com/a"))
        guard.handle(paused("2", "https://example.org/b", redirected_from="1"))
        guard.handle(paused("3", "https://example.net/c", redirected_from="2"))
        guard.handle(paused("4", "https://192.168.0.10/d", redirected_from="3"))
        self.assertEqual([c["params"]["requestId"] for c in wire.of("Fetch.continueRequest")], ["1", "2", "3"])
        self.assertEqual([c["params"]["requestId"] for c in wire.of("Fetch.failRequest")], ["4"])
        report = guard.report()
        self.assertEqual(report["redirect_hops"], 3)
        self.assertEqual(report["cross_origin_redirects"], 3)
        self.assertEqual(report["navigation_blocks"], 1)

    def test_redirect_chain_length_is_bounded(self):
        guard, wire = self.guard(max_redirects=3)
        previous = "1"
        guard.handle(paused(previous, "https://example.com/0"))
        for n in range(2, 7):
            guard.handle(paused(str(n), f"https://example.com/{n}", redirected_from=previous))
            previous = str(n)
        self.assertEqual(len(wire.of("Fetch.continueRequest")), 4)
        self.assertEqual(guard.violations()[0]["code"], "redirect_limit")
        self.assertGreaterEqual(len(wire.of("Fetch.failRequest")), 1)

    def test_resolution_to_loopback_or_private_ranges_is_blocked(self):
        for address in ("127.0.0.1", "::1", "10.9.9.9", "169.254.169.254", "::ffff:127.0.0.1"):
            with self.subTest(address=address):
                guard, wire = self.guard(resolver=lambda _h, a=address: [a])
                guard.handle(paused("1", "https://rebind.example/"))
                self.assertEqual(len(wire.of("Fetch.failRequest")), 1)
                self.assertEqual(guard.violations()[0]["code"], "resolved_non_public")

    def test_resolution_failure_fails_closed(self):
        def boom(_host):
            raise OSError("resolver unavailable")

        guard, wire = self.guard(resolver=boom)
        guard.handle(paused("1", "https://example.com/"))
        self.assertEqual(wire.of("Fetch.continueRequest"), [])
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)
        self.assertEqual(guard.violations()[0]["code"], "resolution_failed")

    def test_credentialed_and_non_https_document_navigations_are_blocked(self):
        for url, code in {
            "https://user:pw@example.com/": "credentialed_url",
            "file:///etc/hosts": "scheme_not_allowed",
            "data:text/html,<script>1</script>": "scheme_not_allowed",
            "javascript:alert(1)": "scheme_not_allowed",
            "http://example.com/": "scheme_not_allowed",
            "about:blank": "scheme_not_allowed",
        }.items():
            with self.subTest(url=url):
                guard, wire = self.guard()
                guard.handle(paused("1", url))
                self.assertEqual(wire.of("Fetch.continueRequest"), [])
                self.assertEqual(len(wire.of("Fetch.failRequest")), 1)
                self.assertEqual(guard.violations()[0]["code"], code)
                self.assertTrue(guard.violations()[0]["navigation"])

    def test_subresource_requests_to_private_destinations_are_blocked_without_stopping_the_run(self):
        guard, wire = self.guard()
        for n, (kind, url) in enumerate(
            (
                ("Image", "https://10.0.0.5/pixel.gif"),
                ("XHR", "https://127.0.0.1:8443/api"),
                ("Fetch", "https://localhost/api"),
                ("Script", "https://169.254.169.254/latest/meta-data"),
                ("Stylesheet", "file:///forbidden/local-secret"),
                ("Media", "https://192.168.0.2/v.mp4"),
            ),
            start=1,
        ):
            guard.handle(paused(str(n), url, resource_type=kind))
        self.assertEqual(len(wire.of("Fetch.failRequest")), 6)
        self.assertEqual(wire.of("Fetch.continueRequest"), [])
        for violation in guard.violations():
            self.assertFalse(violation["navigation"])
            self.assertFalse(violation["fatal"])
        report = guard.report()
        self.assertEqual(report["subresource_blocks"], 6)
        self.assertEqual(report["navigation_blocks"], 0)

    def test_subresource_redirect_to_a_private_destination_is_blocked(self):
        guard, wire = self.guard()
        guard.handle(paused("1", "https://cdn.example.com/x.png", resource_type="Image"))
        guard.handle(paused("2", "https://10.0.0.1/x.png", resource_type="Image", redirected_from="1"))
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)
        self.assertTrue(guard.violations()[0]["redirected"])
        self.assertFalse(guard.violations()[0]["fatal"])

    def test_policy_exception_fails_the_request_closed(self):
        guard, wire = self.guard()
        with mock.patch.object(destination_policy, "check_destination", side_effect=RuntimeError("boom")):
            guard.handle(paused("1", "https://example.com/"))
        self.assertEqual(wire.of("Fetch.continueRequest"), [])
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)
        self.assertEqual(guard.violations()[0]["code"], "policy_error")
        self.assertTrue(guard.violations()[0]["fatal"])

    def test_root_setup_installs_request_stage_interception(self):
        guard, _wire = self.guard()
        setup = dict((method, params) for method, params in guard.root_setup())
        self.assertIn("Fetch.enable", setup)
        self.assertEqual(setup["Fetch.enable"]["patterns"], [{"urlPattern": "*", "requestStage": "Request"}])
        self.assertFalse(setup["Fetch.enable"].get("handleAuthRequests", False))
        self.assertIn("Network.enable", setup)
        auto = setup["Target.setAutoAttach"]
        self.assertTrue(auto["autoAttach"])
        self.assertTrue(auto["waitForDebuggerOnStart"])
        self.assertTrue(auto["flatten"])

    def test_child_targets_get_interception_before_they_run(self):
        guard, wire = self.guard()
        guard.handle(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": "S1",
                    "targetInfo": {"targetId": "T1", "type": "iframe", "url": "https://frame.example/"},
                    "waitingForDebugger": True,
                },
            }
        )
        methods = [(c["method"], c["sessionId"]) for c in wire.sent]
        self.assertIn(("Fetch.enable", "S1"), methods)
        self.assertIn(("Network.enable", "S1"), methods)
        self.assertIn(("Target.setAutoAttach", "S1"), methods)
        self.assertNotIn("Runtime.runIfWaitingForDebugger", [c["method"] for c in wire.sent])
        for command in list(wire.sent):
            if command["sessionId"] == "S1" and command["method"] != "Runtime.runIfWaitingForDebugger":
                guard.handle({"id": command["id"], "sessionId": "S1", "result": {}})
        self.assertEqual(wire.sent[-1]["method"], "Runtime.runIfWaitingForDebugger")
        self.assertEqual(wire.sent[-1]["sessionId"], "S1")
        guard.handle(paused("9", "https://127.0.0.1/", resource_type="Fetch", session_id="S1"))
        failed = wire.of("Fetch.failRequest")
        self.assertEqual(failed[0]["sessionId"], "S1")

    def test_dedicated_workers_are_covered_by_the_parent_session(self):
        guard, wire = self.guard()
        guard.handle(
            {
                "method": "Target.attachedToTarget",
                "params": {"sessionId": "W1", "targetInfo": {"type": "worker"}, "waitingForDebugger": True},
            }
        )
        self.assertEqual([c["sessionId"] for c in wire.of("Fetch.enable")], [], "a worker rejects Fetch.enable")
        self.assertEqual([c["sessionId"] for c in wire.of("Network.enable")], ["W1"])
        self.assertNotIn("Runtime.runIfWaitingForDebugger", [c["method"] for c in wire.sent])
        for command in list(wire.sent):
            if command["sessionId"] == "W1":
                guard.handle({"id": command["id"], "sessionId": "W1", "result": {}})
        self.assertEqual(wire.sent[-1]["method"], "Runtime.runIfWaitingForDebugger")
        # The worker's own request is paused on the parent page session and is decided there.
        guard.handle(paused("5", "https://127.0.0.1/w", resource_type="Fetch"))
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)
        self.assertTrue(guard.report()["interception_active"])

    def test_failed_child_interception_setup_is_an_integrity_failure(self):
        guard, wire = self.guard()
        guard.handle(
            {
                "method": "Target.attachedToTarget",
                "params": {"sessionId": "S1", "targetInfo": {"type": "iframe"}, "waitingForDebugger": True},
            }
        )
        fetch_enable = wire.of("Fetch.enable")[0]
        guard.handle({"id": fetch_enable["id"], "sessionId": "S1", "error": {"message": "not supported"}})
        self.assertNotIn("Runtime.runIfWaitingForDebugger", [c["method"] for c in wire.sent])
        [violation] = guard.violations()
        self.assertEqual(violation["code"], "interception_unavailable")
        self.assertTrue(violation["fatal"])
        self.assertFalse(guard.report()["interception_active"])

    def test_lost_connection_is_an_integrity_failure(self):
        guard, _wire = self.guard()
        guard.mark_lost("reader_stopped")
        [violation] = guard.violations()
        self.assertEqual(violation["code"], "interception_unavailable")
        self.assertTrue(violation["fatal"])
        self.assertFalse(guard.report()["interception_active"])

    def test_send_failure_while_answering_is_an_integrity_failure(self):
        calls = {"n": 0}

        def flaky(method, params=None, session_id=None):
            calls["n"] += 1
            raise OSError("socket closed")

        guard = DestinationGuard(flaky, resolver=_public_resolver)
        guard.handle(paused("1", "https://example.com/"))
        [violation] = guard.violations()
        self.assertEqual(violation["code"], "interception_unavailable")
        self.assertTrue(violation["fatal"])

    def test_response_from_a_non_public_address_is_detected_after_the_fact(self):
        guard, _wire = self.guard()
        guard.handle(
            {
                "method": "Network.responseReceived",
                "params": {
                    "requestId": "1",
                    "type": "Document",
                    "response": {"url": "https://example.com/", "remoteIPAddress": "127.0.0.1", "status": 200},
                },
            }
        )
        [violation] = guard.violations()
        self.assertEqual(violation["code"], "resolved_non_public")
        self.assertEqual(violation["detected"], "post_response")
        self.assertTrue(violation["fatal"])

    def test_cached_or_addressless_responses_are_not_violations(self):
        guard, _wire = self.guard()
        for response in ({"url": "https://example.com/"}, {"url": "https://example.com/", "remoteIPAddress": ""}):
            guard.handle({"method": "Network.responseReceived", "params": {"requestId": "1", "type": "Image", "response": response}})
        guard.handle(
            {
                "method": "Network.responseReceived",
                "params": {
                    "requestId": "2",
                    "type": "Document",
                    "response": {"url": "https://example.com/", "remoteIPAddress": "93.184.216.34"},
                },
            }
        )
        self.assertEqual(guard.violations(), [])

    def test_websocket_to_a_private_destination_is_detected(self):
        guard, _wire = self.guard()
        guard.handle({"method": "Network.webSocketCreated", "params": {"requestId": "1", "url": "wss://127.0.0.1/s"}})
        guard.handle({"method": "Network.webSocketCreated", "params": {"requestId": "2", "url": "wss://example.com/s"}})
        [violation] = guard.violations()
        self.assertEqual(violation["resource_type"], "WebSocket")
        self.assertEqual(violation["detected"], "post_request")
        self.assertTrue(violation["fatal"])

    def test_evidence_is_sanitized(self):
        guard, _wire = self.guard()
        guard.handle(paused("1", "https://user:hunter2@10.0.0.9:9443/private/path?token=abc"))
        text = json.dumps([guard.violations(), guard.report()])
        for leaked in ("hunter2", "user", "private/path", "token", "abc", "9443"):
            self.assertNotIn(leaked, text)
        self.assertIn("10.0.0.9", text)

    def test_wait_idle_drains_asynchronous_handlers(self):
        from concurrent.futures import ThreadPoolExecutor

        release = threading.Event()

        def slow_resolver(_host):
            release.wait(2)
            return ["93.184.216.34"]

        wire = Wire()
        with ThreadPoolExecutor(max_workers=2) as pool:
            guard = DestinationGuard(wire, resolver=slow_resolver, executor=pool)
            guard.submit(paused("1", "https://example.com/"))
            self.assertFalse(guard.wait_idle(0.05))
            self.assertTrue(any(v["code"] == "interception_unavailable" for v in guard.violations()))
            self.assertFalse(guard.report()["interception_active"])
            release.set()
            # Integrity already failed; a late decision must stay fail-closed.
            self.assertTrue(guard.wait_idle(2.0))
        self.assertEqual(len(wire.of("Fetch.continueRequest")), 0)
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)


    def test_setup_ack_arriving_before_registration_is_not_dropped(self):
        """Response-before-registration must still block resume until processed."""
        events = []
        ids = {"n": 200}

        def racing_send(method, params=None, session_id=None):
            ids["n"] += 1
            message_id = ids["n"]
            events.append(("send", method, message_id, session_id))
            # Deliver the ack before DestinationGuard can register the id.
            if method != "Runtime.runIfWaitingForDebugger":
                events.append(("early-ack", message_id))
                guard.handle({"id": message_id, "sessionId": session_id, "result": {}})
            return message_id

        guard = DestinationGuard(racing_send, resolver=_public_resolver)
        guard.handle(
            {
                "method": "Target.attachedToTarget",
                "params": {
                    "sessionId": "S1",
                    "targetInfo": {"type": "iframe"},
                    "waitingForDebugger": True,
                },
            }
        )
        resumes = [item for item in events if item[0] == "send" and item[1] == "Runtime.runIfWaitingForDebugger"]
        self.assertEqual(len(resumes), 1, events)
        self.assertEqual(resumes[0][3], "S1")
        self.assertTrue(guard.report()["interception_active"])

    def test_fatal_violation_is_preserved_after_evidence_cap(self):
        guard, _wire = self.guard()
        with mock.patch.object(destination_policy, "MAX_RECORDED_VIOLATIONS", 3):
            for index in range(3):
                guard.handle(paused(str(index), f"https://10.0.0.{index}/x.png", resource_type="Image"))
            self.assertEqual(len(guard.violations()), 3)
            self.assertTrue(all(not v["fatal"] for v in guard.violations()))
            guard.handle(paused("nav", "https://127.0.0.1/secret"))
            fatal = [v for v in guard.violations() if v["fatal"]]
            self.assertEqual(len(fatal), 1, guard.violations())
            self.assertEqual(fatal[0]["code"], "non_public_address")
            self.assertTrue(fatal[0]["navigation"])

    def test_idle_timeout_fails_closed_for_undecided_requests(self):
        from concurrent.futures import ThreadPoolExecutor

        release = threading.Event()

        def slow_resolver(_host):
            release.wait(2)
            return ["10.0.0.1"]

        wire = Wire()
        with ThreadPoolExecutor(max_workers=2) as pool:
            guard = DestinationGuard(wire, resolver=slow_resolver, executor=pool)
            guard.submit(paused("1", "https://evil.example/"))
            self.assertFalse(guard.wait_idle(0.05))
            release.set()
            guard.wait_idle(2.0)
        self.assertTrue(any(v["code"] == "interception_unavailable" and v["fatal"] for v in guard.violations()))
        self.assertEqual(len(wire.of("Fetch.continueRequest")), 0)
        self.assertEqual(len(wire.of("Fetch.failRequest")), 1)




# --------------------------------------------------------------------------- #
# Loop
# --------------------------------------------------------------------------- #
class ScriptedClient:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    @contextmanager
    def request_budget(self, max_requests=256, *, deadline_seconds=None):
        yield

    def decide(self, state, questions, **_kwargs):
        self.calls.append(state)
        choice = self.script[len(self.calls) - 1]
        answers = {"operation": _choice(choice, questions["operation"]["criteria"])}
        if "click_target" in questions:
            answers["click_target"] = _choice(next(iter(questions["click_target"]["criteria"])), questions["click_target"]["criteria"])
        return {"answers": answers, "latency_ms": 5, "model": "jev-latest", "usage": {}}


def _choice(choice, criteria):
    others = [key for key in criteria if key != choice]
    probabilities = {choice: 0.91}
    probabilities.update({key: 0.09 / len(others) for key in others})
    return {"choice": choice, "confidence": 0.91, "probabilities": probabilities}


class GuardedSession:
    """A fake session that exposes the guard-facing evidence API."""

    def __init__(self, *, on_click=None, page_after=None, violations_after_click=(), raise_on_click=None):
        self.url = "https://example.com/start"
        self.on_click = on_click
        self.page_after = page_after
        self.raise_on_click = raise_on_click
        self.clicked = 0
        self._violations: list[dict] = []
        self._pending = list(violations_after_click)
        self.report = {
            "policy": "public_https_only",
            "version": 1,
            "enforcement": "cdp_fetch_interception",
            "interception_active": True,
            "requests_checked": 3,
            "requests_blocked": 0,
            "navigation_blocks": 0,
            "subresource_blocks": 0,
            "redirect_hops": 0,
            "cross_origin_redirects": 0,
            "blocked": [],
        }

    def observe(self):
        return {
            "url": self.url,
            "title": "Start" if self.url.endswith("/start") else "Next",
            "text": "page " + self.url,
            "elements": [{"id": "1", "role": "link", "label": "Next page", "href": "https://example.com/next", "kind": "click"}],
        }

    def click(self, element_id, label="", href=""):
        self.clicked += 1
        self._violations.extend(self._pending)
        if self.page_after is not None:
            self.url = self.page_after
        if self.raise_on_click is not None:
            raise self.raise_on_click

    def scroll(self, direction):
        return None

    def wait(self, seconds=0.2):
        return None

    def close(self):
        return None

    def destination_violations(self):
        return list(self._violations)

    def destination_report(self):
        report = dict(self.report)
        report["blocked"] = [dict(v) for v in self._violations]
        report["navigation_blocks"] = sum(1 for v in self._violations if v["navigation"])
        report["subresource_blocks"] = sum(1 for v in self._violations if not v["navigation"])
        report["requests_blocked"] = len(self._violations)
        return report


def _violation(seq, code="non_public_address", *, navigation=True, fatal=None, resource_type="Document", detected="pre_request"):
    return {
        "seq": seq,
        "code": code,
        "scheme": "https",
        "host": "10.0.0.5",
        "resource_type": resource_type,
        "navigation": navigation,
        "redirected": False,
        "fatal": navigation if fatal is None else fatal,
        "detected": detected,
    }


class DestinationLoopTests(unittest.TestCase):
    def run_goal(self, session, script):
        client = ScriptedClient(script)
        result = run_browser_goal(goal="Follow the public page", client=client, session=session, max_steps=4)
        return result, client

    def test_navigation_block_after_dispatch_stops_with_reconcile(self):
        session = GuardedSession(violations_after_click=[_violation(1)])
        result, client = self.run_goal(session, ["CLICK", "CLICK", "DONE"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "destination_blocked")
        self.assertEqual(result["failure_reason"], "non_public_address")
        self.assertTrue(result["reconcile_before_retry"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["goal_verified"])
        self.assertEqual(len(client.calls), 1, "no further provider decision after a blocked navigation")
        [action] = result["actions"]
        self.assertTrue(action["action_dispatched"])
        self.assertEqual(action["effect_status"], "destination_blocked")
        self.assertFalse(action["goal_verified"])
        self.assertEqual(result["destination_policy"]["navigation_blocks"], 1)

    def test_navigation_block_when_the_action_itself_errors_keeps_partial_evidence(self):
        session = GuardedSession(
            violations_after_click=[_violation(1, "resolved_non_public")],
            raise_on_click=DestinationPolicyError("resolved_non_public"),
        )
        result, _client = self.run_goal(session, ["CLICK", "DONE"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "destination_blocked")
        self.assertTrue(result["reconcile_before_retry"])
        self.assertEqual(result["attempted_action_count"], 1)
        self.assertTrue(result["actions"][0]["action_dispatched"])

    def test_private_landing_url_is_blocked_and_never_echoed_with_credentials(self):
        session = GuardedSession(page_after="https://user:hunter2@127.0.0.1:8443/admin?token=abc")
        result, _client = self.run_goal(session, ["CLICK", "DONE"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "unsafe_url")
        self.assertTrue(result["reconcile_before_retry"])
        text = json.dumps(result)
        for leaked in ("hunter2", "token=abc", "/admin", "8443"):
            self.assertNotIn(leaked, text)

    def test_blocked_subresources_are_reported_but_do_not_stop_a_public_run(self):
        session = GuardedSession(
            violations_after_click=[_violation(1, navigation=False, resource_type="Image")],
            page_after="https://example.com/next",
        )
        result, client = self.run_goal(session, ["CLICK", "DONE"])
        self.assertEqual(result["status"], "completion_candidate")
        self.assertFalse(result["verified"])
        self.assertEqual(result["destination_policy"]["subresource_blocks"], 1)
        self.assertEqual(result["destination_policy"]["navigation_blocks"], 0)
        self.assertEqual(len(client.calls), 2)

    def test_navigation_that_happens_while_the_provider_decides_blocks_a_done(self):
        session = GuardedSession(page_after="https://example.com/next")

        class LateClient(ScriptedClient):
            def decide(self, state, questions, **kwargs):
                if len(self.calls) == 1:  # the page navigates on its own during the second decision
                    session._violations.append(_violation(1))
                return super().decide(state, questions, **kwargs)

        client = LateClient(["CLICK", "DONE"])
        result = run_browser_goal(goal="Follow the public page", client=client, session=session, max_steps=4)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "destination_blocked")
        self.assertNotEqual(result["status"], "completion_candidate")
        self.assertTrue(result["reconcile_before_retry"])
        self.assertFalse(result["goal_verified"])

    def test_navigation_between_steps_stops_before_the_next_request(self):
        session = GuardedSession(page_after="https://example.com/next")
        original_scroll = session.scroll

        def scroll_then_drift(direction):
            original_scroll(direction)
            session._violations.append(_violation(1))

        session.scroll = scroll_then_drift  # type: ignore[method-assign]
        result, client = self.run_goal(session, ["SCROLL_DOWN", "CLICK", "DONE"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "destination_blocked")
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(session.clicked, 0)

    def test_interception_failure_stops_before_dispatch(self):
        session = GuardedSession()
        session._violations.append(_violation(1, "interception_unavailable", navigation=False, fatal=True, detected="integrity"))
        result, client = self.run_goal(session, ["CLICK", "DONE"])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "destination_blocked")
        self.assertEqual(result["failure_reason"], "interception_unavailable")
        self.assertEqual(session.clicked, 0)
        self.assertEqual(client.calls, [], "fail closed before any provider request")

    def test_allowed_public_control_proceeds_and_reports_a_clean_policy_block(self):
        session = GuardedSession(page_after="https://example.com/next")
        result, client = self.run_goal(session, ["CLICK", "DONE"])
        self.assertEqual(result["status"], "completion_candidate")
        self.assertFalse(result["verified"])
        self.assertFalse(result["goal_verified"])
        self.assertEqual(result["actions"][0]["effect_status"], "url_changed")
        policy = result["destination_policy"]
        self.assertEqual(policy["policy"], "public_https_only")
        self.assertEqual(policy["requests_blocked"], 0)
        self.assertTrue(policy["interception_active"])
        self.assertEqual(len(client.calls), 2)

    def test_session_without_a_guard_is_reported_as_not_enforced(self):
        class Bare(GuardedSession):
            destination_violations = None  # type: ignore[assignment]
            destination_report = None  # type: ignore[assignment]

        session = Bare(page_after="https://example.com/next")
        result, _client = self.run_goal(session, ["CLICK", "DONE"])
        self.assertEqual(result["destination_policy"]["enforcement"], "session_did_not_report")
        self.assertFalse(result["destination_policy"]["interception_active"])

    def test_offered_targets_apply_the_full_policy(self):
        raw = [
            {"id": "1", "label": "Loopback", "href": "https://0x7f.0.0.1/"},
            {"id": "2", "label": "Metadata", "href": "https://metadata.google.internal/"},
            {"id": "3", "label": "Creds", "href": "https://u:p@example.com/"},
            {"id": "4", "label": "Public", "href": "https://example.com/ok"},
        ]
        self.assertEqual([item["id"] for item in browser_use._safe_elements(raw)], ["4"])


class StartUrlTests(unittest.TestCase):
    def test_start_url_resolving_to_loopback_is_refused_before_launch_and_before_any_request(self):
        client = ScriptedClient(["DONE"])
        with mock.patch.object(destination_policy, "default_resolver", return_value=["127.0.0.1"]):
            with mock.patch.object(browser_use.subprocess, "Popen") as popen:
                result = run_browser_goal(
                    goal="Read the public page",
                    client=client,
                    start_url="https://rebind.example/",
                    max_steps=2,
                )
        popen.assert_not_called()
        self.assertEqual(client.calls, [])
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "destination_policy")
        self.assertEqual(result["failure_reason"], "resolved_non_public")
        self.assertEqual(result["attempted_request_count"], 0)
        self.assertFalse(result["reconcile_before_retry"])
        self.assertEqual(result["destination_policy"]["policy"], "public_https_only")

    def test_unresolvable_start_url_fails_closed(self):
        client = ScriptedClient(["DONE"])
        with mock.patch.object(destination_policy, "default_resolver", side_effect=OSError("nxdomain")):
            with mock.patch.object(browser_use.subprocess, "Popen") as popen:
                result = run_browser_goal(goal="Read", client=client, start_url="https://nope.example/", max_steps=2)
        popen.assert_not_called()
        self.assertEqual(result["failure_phase"], "destination_policy")
        self.assertEqual(result["failure_reason"], "resolution_failed")

    def test_open_browser_session_refuses_before_launching_a_process(self):
        with mock.patch.object(destination_policy, "default_resolver", return_value=["10.0.0.4"]):
            with mock.patch.object(browser_use.subprocess, "Popen") as popen:
                with self.assertRaises(DestinationPolicyError) as raised:
                    with browser_use.open_browser_session("https://rebind.example/"):
                        pass
        self.assertEqual(raised.exception.code, "resolved_non_public")
        popen.assert_not_called()


# --------------------------------------------------------------------------- #
# Real browser (synthetic loopback listener that must never be reached)
# --------------------------------------------------------------------------- #
class LoopbackTrap:
    """A local TCP listener that counts connections. Zero is the passing value."""

    def __init__(self):
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.1)
        self.port = self._sock.getsockname()[1]
        self.connections = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            self.connections += 1
            conn.close()

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        self._sock.close()


def _online() -> bool:
    try:
        socket.getaddrinfo("example.com", 443, type=socket.SOCK_STREAM)
        return True
    except OSError:
        return False


class RealBrowserDestinationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os

        # Offline discovery and hosted CI stay offline. Real Chromium + public
        # origin navigations require an explicit opt-in.
        if os.environ.get("SWITCHYARD_LIVE_BROWSER_TESTS") != "1":
            raise unittest.SkipTest(
                "set SWITCHYARD_LIVE_BROWSER_TESTS=1 to run live external-browser destination tests"
            )
        binary, _, _ = browser_use._browser_binary_details()
        if binary is None:
            raise unittest.SkipTest("no Chromium-family browser is available")
        if not _online():
            raise unittest.SkipTest("the public example.com origin does not resolve here")

    def setUp(self):
        self.trap = LoopbackTrap()
        self.addCleanup(self.trap.close)

    def _settle(self, session, seconds=1.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            time.sleep(0.05)
        session._guard.wait_idle(2.0)

    def test_initial_navigation_is_intercepted_and_reports_evidence(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            report = session.destination_report()
            self.assertTrue(report["interception_active"])
            self.assertGreaterEqual(report["requests_checked"], 1)
            self.assertEqual(session.destination_violations(), [])

    def test_navigation_after_dispatch_to_loopback_is_blocked_before_connect(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(f"setTimeout(() => {{ location.href = 'https://127.0.0.1:{self.trap.port}/x'; }}, 0); true")
            self._settle(session)
            violations = session.destination_violations()
            self.assertTrue(any(v["navigation"] and v["code"] == "non_public_address" for v in violations), violations)
            self.assertEqual(self.trap.connections, 0)
            with self.assertRaises(DestinationPolicyError):
                session.raise_if_destination_blocked()

    def test_subresource_requests_to_loopback_are_blocked_before_connect(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(
                f"""(() => {{
                  const base = 'https://127.0.0.1:{self.trap.port}';
                  fetch(base + '/f').catch(() => {{}});
                  const x = new XMLHttpRequest(); x.open('GET', base + '/x'); try {{ x.send(); }} catch (e) {{}}
                  const i = new Image(); i.src = base + '/i.png';
                  const s = document.createElement('script'); s.src = base + '/s.js'; document.head.appendChild(s);
                  return true;
                }})()"""
            )
            self._settle(session)
            blocked = [v for v in session.destination_violations() if not v["navigation"]]
            self.assertGreaterEqual(len(blocked), 3, session.destination_violations())
            self.assertEqual(self.trap.connections, 0)
            self.assertTrue(all(not v["fatal"] for v in blocked))

    def test_worker_requests_to_loopback_are_blocked_before_connect(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(
                f"""(() => {{
                  const src = "fetch('https://127.0.0.1:{self.trap.port}/w').catch(() => {{}});";
                  const url = URL.createObjectURL(new Blob([src], {{type: 'text/javascript'}}));
                  new Worker(url);
                  return true;
                }})()"""
            )
            self._settle(session, 2.0)
            self.assertEqual(self.trap.connections, 0)
            self.assertTrue(session.destination_report()["interception_active"])

    def test_new_windows_are_blocked_and_single_tab_holds(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(f"window.open('https://127.0.0.1:{self.trap.port}/p'); true")
            self._settle(session)
            self.assertEqual(self.trap.connections, 0)
            self.assertEqual(session.extra_page_targets(), 0)

    def test_worker_requests_are_decided_on_the_parent_session(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(
                f"""(() => {{
                  const src = "fetch('https://127.0.0.1:{self.trap.port}/w').catch(() => {{}});";
                  new Worker(URL.createObjectURL(new Blob([src], {{type: 'text/javascript'}})));
                  return true;
                }})()"""
            )
            self._settle(session, 2.0)
            self.assertEqual(self.trap.connections, 0)
            self.assertTrue(session.destination_report()["interception_active"])
            self.assertTrue(
                any(v["code"] == "non_public_address" and not v["navigation"] for v in session.destination_violations())
            )

    def test_cross_site_public_frame_attaches_without_breaking_interception(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(
                "(() => { const f = document.createElement('iframe'); f.src = 'https://example.org/'; document.body.appendChild(f); return true; })()"
            )
            self._settle(session, 3.0)
            report = session.destination_report()
            self.assertTrue(report["interception_active"], report)
            self.assertEqual(session.destination_violations(), [])
            self.assertGreaterEqual(report["requests_checked"], 2)

    def test_frame_navigation_to_loopback_is_blocked_before_connect(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(
                f"(() => {{ const f = document.createElement('iframe'); f.src = 'https://127.0.0.1:{self.trap.port}/f'; document.body.appendChild(f); return true; }})()"
            )
            self._settle(session, 2.0)
            self.assertEqual(self.trap.connections, 0)
            self.assertTrue(any(v["navigation"] and v["fatal"] for v in session.destination_violations()))

    def test_link_click_to_loopback_is_blocked_before_connect(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(
                f"(() => {{ const a = document.createElement('a'); a.id = 'x'; a.href = 'https://127.0.0.1:{self.trap.port}/l'; a.textContent = 'go'; document.body.appendChild(a); a.click(); return true; }})()"
            )
            self._settle(session, 2.0)
            self.assertEqual(self.trap.connections, 0)
            self.assertTrue(any(v["navigation"] and v["fatal"] for v in session.destination_violations()))

    def test_full_loop_stops_on_a_click_that_navigates_to_loopback(self):
        # A link with an approved public href whose handler navigates elsewhere: the
        # offered-target filter cannot see this, only the request boundary can.
        with browser_use.ChromiumSession("https://example.com") as session:
            session._evaluate(
                f"""(() => {{
                  const a = document.createElement('a');
                  a.href = 'https://example.com/next';
                  a.textContent = 'Next page';
                  a.addEventListener('click', e => {{ e.preventDefault(); location.href = 'https://127.0.0.1:{self.trap.port}/x'; }});
                  document.body.appendChild(a);
                  return true;
                }})()"""
            )
            client = ScriptedClient(["CLICK", "DONE"])
            result = run_browser_goal(goal="Follow the public page", client=client, session=session, max_steps=3)
            self.assertEqual(self.trap.connections, 0)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["failure_phase"], "destination_blocked")
        self.assertEqual(result["failure_reason"], "non_public_address")
        self.assertTrue(result["reconcile_before_retry"])
        self.assertFalse(result["verified"])
        self.assertFalse(result["goal_verified"])
        self.assertEqual(len(client.calls), 1)
        [action] = result["actions"]
        self.assertTrue(action["action_dispatched"])
        self.assertEqual(action["effect_status"], "destination_blocked")
        policy = result["destination_policy"]
        self.assertEqual(policy["navigation_blocks"], 1)
        _assert_example_origin(self, result["url"])
        self.assertNotIn(str(self.trap.port), json.dumps(result))

    def test_allowed_public_control_navigates_without_a_violation(self):
        with browser_use.ChromiumSession("https://example.com") as session:
            page = session.observe()
            _assert_example_origin(self, page["url"])
            self._settle(session, 0.5)
            self.assertEqual(session.destination_violations(), [])


if __name__ == "__main__":
    unittest.main()
