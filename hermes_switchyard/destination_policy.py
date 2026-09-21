"""Code-owned destination policy for the DOM browser backend.

The approved destination class is a public https origin without credentials.
Everything else is refused by code, before any provider decides anything:
private, loopback, link-local and other non-global addresses, local-only names,
credentialed URLs, and every scheme that is not https (file, data, javascript,
about, blob, ftp, chrome, http, ws).

Two layers use this policy:

* :func:`check_destination` is the pure decision. It is lexical by default and
  adds a host-resolution check when asked. Resolution that fails or returns an
  empty answer is a refusal, never a pass.
* :class:`DestinationGuard` applies the decision at the browser request boundary
  through Chrome DevTools ``Fetch`` interception, so redirect hops, subresource
  requests, and requests from frames and workers are decided before they are
  sent. It also reads the address a response actually came from and records it
  as post-hoc evidence.

The guard never needs a browser: it is driven with protocol messages and answers
through an injected ``send`` callable, which keeps the boundary testable offline.

Residual limits are stated in :data:`RESIDUAL_RISKS` and on every receipt.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

POLICY_NAME = "public_https_only"
POLICY_VERSION = 1
ENFORCEMENT = "cdp_fetch_interception"
MAX_REDIRECT_HOPS = 10
MAX_RECORDED_VIOLATIONS = 200
MAX_REPORTED_VIOLATIONS = 20
MAX_TRACKED_REQUESTS = 2000
RESOLUTION_CACHE_SECONDS = 10.0

# Named plainly so a reader of a receipt knows what this policy does not claim.
RESIDUAL_RISKS = (
    "dns_answer_may_change_between_check_and_connect",
    "websocket_handshake_is_detected_not_intercepted",
    "post_response_address_check_detects_after_the_request_was_sent",
)

_LOCAL_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".lan",
    ".home.arpa",
    ".localdomain",
    ".intranet",
    ".corp",
    ".home",
)
_NUMERIC_LABEL = re.compile(r"0x[0-9a-f]*|[0-9]+")
_HOST_CHARS = re.compile(r"[a-z0-9_.-]+")
_SCHEME_TOKEN = re.compile(r"[a-z][a-z0-9+.-]{0,31}")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")
_IPV4_COMPATIBLE = ipaddress.ip_network("::/96")

_SUBRESOURCE_SCHEMES = frozenset({"https", "data", "blob"})
_NETWORKLESS_SCHEMES = frozenset({"data", "blob"})


class DestinationPolicyError(RuntimeError):
    """A destination was refused. ``code`` is a bounded, provider-free reason."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class DestinationDecision:
    allowed: bool
    code: str
    scheme: str = ""
    host: str = ""

    def evidence(self) -> dict[str, str]:
        """Return a receipt-safe record: never credentials, path, query, or port."""
        return {"code": self.code, "scheme": self.scheme, "host": self.host}


def _refuse(code: str, scheme: str = "", host: str = "") -> DestinationDecision:
    return DestinationDecision(False, code, scheme, host)


def _is_public_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return whether an address is globally routable, unwrapping embedded IPv4."""
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return _is_public_ip(ip.ipv4_mapped)
        if ip.sixtofour is not None:
            return _is_public_ip(ip.sixtofour)
        if ip.teredo is not None:
            return all(_is_public_ip(part) for part in ip.teredo)
        if ip in _NAT64:
            return _is_public_ip(ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF))
        if ip in _NAT64_LOCAL or ip in _IPV4_COMPATIBLE:
            return False
    return bool(ip.is_global) and not ip.is_multicast


def _parse_address(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    text = value.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text = text.split("%", 1)[0]
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        return None


def _classify_host(host: str) -> tuple[str | None, str]:
    """Return ``(refusal_code_or_None, normalized_host)`` for a URL hostname."""
    folded = host.casefold().rstrip(".")
    if not folded:
        return "missing_host", ""
    if not folded.isascii():
        try:
            folded = folded.encode("idna").decode("ascii").casefold()
        except UnicodeError:
            return "invalid_host_characters", ""
    ip = _parse_address(folded) if (":" in folded or folded.replace(".", "").isdigit()) else None
    if ip is not None:
        return (None if _is_public_ip(ip) else "non_public_address"), folded
    if ":" in folded or not _HOST_CHARS.fullmatch(folded) or ".." in folded or folded.startswith("."):
        return "invalid_host_characters", ""
    labels = folded.split(".")
    if _NUMERIC_LABEL.fullmatch(labels[-1]):
        # Browsers resolve decimal, octal, hex, and short forms to an address.
        return "numeric_host_form", folded
    if folded == "localhost" or any(folded.endswith(suffix) for suffix in _LOCAL_SUFFIXES):
        return "local_hostname", folded
    if len(labels) < 2:
        return "single_label_host", folded
    return None, folded


def default_resolver(host: str) -> list[str]:
    """Resolve *host* through the system resolver. Raises OSError on failure."""
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return sorted({str(info[4][0]).split("%", 1)[0] for info in infos})


def check_destination(
    url: Any,
    *,
    resource_type: str = "Document",
    resolve: bool = False,
    resolver: Callable[[str], list[str]] | None = None,
) -> DestinationDecision:
    """Decide whether *url* is an approved destination for *resource_type*.

    Documents and other network requests must be public ``https``. A WebSocket
    must be ``wss``. A non-document subresource may also be ``data`` or ``blob``,
    which carry no network destination. ``resolve=True`` additionally requires
    every resolved address to be public; a failed or empty resolution refuses.
    """
    if type(url) is not str or not url:
        return _refuse("invalid_url")
    if any(ord(char) <= 0x20 or ord(char) == 0x7F or char == "\\" for char in url):
        return _refuse("invalid_url")
    try:
        parts = urlsplit(url)
        _ = parts.port
    except ValueError:
        return _refuse("invalid_url")
    scheme = parts.scheme.casefold()
    if resource_type == "WebSocket":
        allowed_schemes: frozenset[str] = frozenset({"wss"})
    elif resource_type == "Document":
        allowed_schemes = frozenset({"https"})
    else:
        allowed_schemes = _SUBRESOURCE_SCHEMES
    if scheme not in allowed_schemes:
        return _refuse("scheme_not_allowed", scheme if _SCHEME_TOKEN.fullmatch(scheme) else "")
    if scheme in _NETWORKLESS_SCHEMES:
        return DestinationDecision(True, "allowed", scheme, "")
    if not parts.netloc:
        return _refuse("missing_host", scheme)
    if parts.username is not None or parts.password is not None:
        # Credentials are refused even for a public host. The host is reported so
        # the receipt can name the destination, never the credential.
        _, host = _classify_host(parts.hostname or "")
        return _refuse("credentialed_url", scheme, host[:253])
    if not parts.hostname:
        return _refuse("missing_host", scheme)
    code, host = _classify_host(parts.hostname)
    if code is not None:
        return _refuse(code, scheme, host[:253])
    if resolve and _parse_address(host) is None:
        lookup = resolver if resolver is not None else default_resolver
        try:
            addresses = list(lookup(host))
        except Exception:  # noqa: BLE001 -- unavailable evidence is a refusal
            return _refuse("resolution_failed", scheme, host[:253])
        parsed = [_parse_address(str(address)) for address in addresses]
        if not parsed or any(item is None for item in parsed):
            return _refuse("resolution_failed", scheme, host[:253])
        if not all(_is_public_ip(item) for item in parsed if item is not None):
            return _refuse("resolved_non_public", scheme, host[:253])
    return DestinationDecision(True, "allowed", scheme, host[:253])


def check_address(value: Any) -> DestinationDecision:
    """Decide whether an address a response arrived from is public."""
    ip = _parse_address(value) if type(value) is str else None
    if ip is None:
        return _refuse("resolution_failed")
    if not _is_public_ip(ip):
        return _refuse("resolved_non_public")
    return DestinationDecision(True, "allowed")


def is_public_https_url(url: Any) -> bool:
    """Lexical policy check used wherever a URL is inspected without I/O."""
    return check_destination(url).allowed


def redact_url(url: Any) -> str:
    """Return *url* for a receipt: a refused URL is reduced to scheme and host.

    Credentials, path, query, fragment, and port of a refused destination never
    reach a receipt. An approved URL is returned unchanged.
    """
    if type(url) is not str:
        return ""
    decision = check_destination(url)
    if decision.allowed:
        return url
    if decision.code == "invalid_url":
        return ""
    if decision.scheme in {"http", "https", "ws", "wss"} and decision.host:
        return f"{decision.scheme}://{decision.host}/"
    if decision.scheme:
        return f"{decision.scheme}:"
    scheme = urlsplit(url).scheme.casefold()
    return f"{scheme}:" if _SCHEME_TOKEN.fullmatch(scheme) else ""


def static_report(enforcement: str, *, active: bool = False) -> dict[str, Any]:
    """Return a policy block for a path that has no live interception."""
    return {
        "policy": POLICY_NAME,
        "version": POLICY_VERSION,
        "enforcement": enforcement,
        "interception_active": active,
        "requests_checked": 0,
        "requests_blocked": 0,
        "navigation_blocks": 0,
        "subresource_blocks": 0,
        "redirect_hops": 0,
        "cross_origin_redirects": 0,
        "blocked": [],
    }


_FETCH_PATTERNS = [{"urlPattern": "*", "requestStage": "Request"}]
_AUTO_ATTACH = {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True}
# A dedicated worker rejects Fetch.enable, and the requests it makes are paused
# on the parent page's session, so the parent's interception already covers it.
_PARENT_COVERED_TARGETS = frozenset({"worker"})
_OBSERVED_METHODS = frozenset(
    {
        "Fetch.requestPaused",
        "Target.attachedToTarget",
        "Network.responseReceived",
        "Network.webSocketCreated",
    }
)


def _origin(url: str) -> tuple[str, str, int | None]:
    try:
        parts = urlsplit(url)
        return parts.scheme.casefold(), (parts.hostname or "").casefold(), parts.port
    except ValueError:
        return "", "", None


class DestinationGuard:
    """Apply the destination policy to every request a browser target makes.

    ``send(method, params, session_id)`` transmits one protocol command without
    waiting for its reply and returns its id. Messages arrive through
    :meth:`handle` (synchronous) or :meth:`submit` (through ``executor`` so DNS
    never stalls the protocol reader).
    """

    def __init__(
        self,
        send: Callable[..., int],
        *,
        resolver: Callable[[str], list[str]] | None = None,
        max_redirects: int = MAX_REDIRECT_HOPS,
        executor: Any = None,
        cache_seconds: float = RESOLUTION_CACHE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._send = send
        self._resolver = resolver
        self._max_redirects = max_redirects
        self._executor = executor
        self._cache_seconds = cache_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._idle = threading.Condition(self._lock)
        self._inflight = 0
        self._violations: list[dict[str, Any]] = []
        self._seq = 0
        self._active = True
        self._checked = 0
        self._navigation_blocks = 0
        self._subresource_blocks = 0
        self._redirect_hops = 0
        self._cross_origin_redirects = 0
        self._hops: OrderedDict[tuple[str | None, str], tuple[int, tuple[str, str, int | None]]] = OrderedDict()
        self._setup: dict[int, str | None] = {}
        self.integrity_reasons: list[str] = []
        self._unexpected_target = False
        self._cache: dict[str, tuple[float, list[str]]] = {}

    # -- setup ---------------------------------------------------------------

    def root_setup(self) -> list[tuple[str, dict[str, Any]]]:
        """Commands the owner runs (and awaits) on the root target before navigating."""
        return [
            ("Fetch.enable", {"patterns": [dict(item) for item in _FETCH_PATTERNS]}),
            ("Network.enable", {}),
            ("Target.setAutoAttach", dict(_AUTO_ATTACH)),
        ]

    # -- message intake ------------------------------------------------------

    def submit(self, message: dict[str, Any]) -> None:
        """Route one protocol message, through the executor when one was given."""
        if "id" not in message and message.get("method") not in _OBSERVED_METHODS:
            return
        if self._executor is None:
            self._run(message)
            return
        with self._lock:
            self._inflight += 1
        try:
            self._executor.submit(self._run_counted, message)
        except Exception:  # noqa: BLE001 -- a pool that cannot accept work must not pass requests
            with self._lock:
                self._inflight -= 1
                self._idle.notify_all()
            self._fail_integrity("executor_unavailable")

    def wait_idle(self, timeout: float = 2.0) -> bool:
        """Wait until every submitted message has been handled."""
        with self._idle:
            return self._idle.wait_for(lambda: self._inflight == 0, timeout=timeout)

    def handle(self, message: dict[str, Any]) -> None:
        self._run(message)

    def _run_counted(self, message: dict[str, Any]) -> None:
        try:
            self._run(message)
        finally:
            with self._lock:
                self._inflight -= 1
                self._idle.notify_all()

    def _run(self, message: dict[str, Any]) -> None:
        try:
            if "id" in message:
                self._on_ack(message)
                return
            method = message.get("method")
            params = message.get("params")
            params = params if isinstance(params, dict) else {}
            session_id = message.get("sessionId") if isinstance(message.get("sessionId"), str) else None
            if method == "Fetch.requestPaused":
                self._on_paused(params, session_id)
            elif method == "Target.attachedToTarget":
                self._on_attached(params)
            elif method == "Network.responseReceived":
                self._on_response(params, session_id)
            elif method == "Network.webSocketCreated":
                self._on_websocket(params, session_id)
        except Exception:  # noqa: BLE001 -- a handler that cannot decide leaves interception unproven
            self._fail_integrity("handler_error")

    # -- decisions -----------------------------------------------------------

    def _lookup(self, host: str) -> list[str]:
        now = self._clock()
        with self._lock:
            hit = self._cache.get(host)
            if hit is not None and now - hit[0] < self._cache_seconds:
                return list(hit[1])
        lookup = self._resolver if self._resolver is not None else default_resolver
        addresses = list(lookup(host))
        with self._lock:
            if len(self._cache) > 512:
                self._cache.clear()
            self._cache[host] = (now, list(addresses))
        return addresses

    def _on_paused(self, params: dict[str, Any], session_id: str | None) -> None:
        request = params.get("request") if isinstance(params.get("request"), dict) else {}
        url = str(request.get("url") or "")
        resource_type = str(params.get("resourceType") or "Other")
        request_id = str(params.get("requestId") or "")
        previous = params.get("redirectedRequestId")
        navigation = resource_type == "Document"
        origin = _origin(url)
        hop = 0
        redirected = previous is not None
        with self._lock:
            self._checked += 1
            if redirected:
                prior_hop, prior_origin = self._hops.get((session_id, str(previous)), (0, ("", "", None)))
                hop = prior_hop + 1
                self._redirect_hops += 1
                if origin != prior_origin:
                    self._cross_origin_redirects += 1
            self._hops[(session_id, request_id)] = (hop, origin)
            while len(self._hops) > MAX_TRACKED_REQUESTS:
                self._hops.popitem(last=False)
        code: str | None
        decision: DestinationDecision | None = None
        try:
            decision = check_destination(url, resource_type=resource_type, resolve=True, resolver=self._lookup)
            code = None if decision.allowed else decision.code
        except Exception:  # noqa: BLE001 -- no decision is a refusal
            code = "policy_error"
        if code is None and hop > self._max_redirects:
            code = "redirect_limit"
        if code is None:
            self._answer("Fetch.continueRequest", {"requestId": request_id}, session_id)
            return
        scheme = decision.scheme if decision is not None else ""
        host = decision.host if decision is not None else ""
        self._record(
            code,
            scheme=scheme,
            host=host,
            resource_type=resource_type,
            navigation=navigation,
            redirected=redirected,
            fatal=navigation or code == "policy_error",
            detected="pre_request",
            session_id=session_id,
        )
        with self._lock:
            if navigation:
                self._navigation_blocks += 1
            else:
                self._subresource_blocks += 1
        self._answer("Fetch.failRequest", {"requestId": request_id, "errorReason": "BlockedByClient"}, session_id)

    def _answer(self, method: str, params: dict[str, Any], session_id: str | None) -> None:
        try:
            self._send(method, params, session_id)
        except Exception:  # noqa: BLE001 -- an unanswered request means interception is not proven
            self._fail_integrity("send_failed")

    def _on_attached(self, params: dict[str, Any]) -> None:
        session_id = params.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            self._fail_integrity("child_target_without_session")
            return
        target = params.get("targetInfo") if isinstance(params.get("targetInfo"), dict) else {}
        covered = target.get("type") in _PARENT_COVERED_TARGETS
        try:
            for method, body in self.root_setup():
                if covered and method == "Fetch.enable":
                    continue
                message_id = self._send(method, body, session_id)
                with self._lock:
                    self._setup[message_id] = session_id
            if params.get("waitingForDebugger"):
                self._send("Runtime.runIfWaitingForDebugger", {}, session_id)
        except Exception:  # noqa: BLE001 -- a child that cannot be intercepted is not covered
            self._fail_integrity("child_setup_failed")

    def _on_ack(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        with self._lock:
            owner = self._setup.pop(message_id, None) if isinstance(message_id, int) else None
            tracked = isinstance(message_id, int) and owner is not None
        if tracked and "error" in message:
            self._fail_integrity("child_setup_failed")

    def _on_response(self, params: dict[str, Any], session_id: str | None) -> None:
        response = params.get("response") if isinstance(params.get("response"), dict) else {}
        address = response.get("remoteIPAddress")
        if not isinstance(address, str) or not address:
            return  # cache, service worker, or data response: no remote address to judge
        verdict = check_address(address)
        if verdict.allowed:
            return
        resource_type = str(params.get("type") or "Other")
        seen = check_destination(str(response.get("url") or ""), resource_type=resource_type)
        self._record(
            "resolved_non_public",
            scheme=seen.scheme,
            host=seen.host,
            resource_type=resource_type,
            navigation=resource_type == "Document",
            redirected=False,
            fatal=True,
            detected="post_response",
            session_id=session_id,
        )

    def _on_websocket(self, params: dict[str, Any], session_id: str | None) -> None:
        decision = check_destination(str(params.get("url") or ""), resource_type="WebSocket", resolve=True, resolver=self._lookup)
        if decision.allowed:
            return
        self._record(
            decision.code,
            scheme=decision.scheme,
            host=decision.host,
            resource_type="WebSocket",
            navigation=False,
            redirected=False,
            fatal=True,
            detected="post_request",
            session_id=session_id,
        )

    # -- evidence ------------------------------------------------------------

    def _record(
        self,
        code: str,
        *,
        scheme: str = "",
        host: str = "",
        resource_type: str = "",
        navigation: bool = False,
        redirected: bool = False,
        fatal: bool = False,
        detected: str = "pre_request",
        session_id: str | None = None,
    ) -> None:
        with self._lock:
            self._seq += 1
            if len(self._violations) < MAX_RECORDED_VIOLATIONS:
                self._violations.append(
                    {
                        "seq": self._seq,
                        "code": code,
                        "scheme": scheme[:32],
                        "host": host[:253],
                        "resource_type": resource_type[:32],
                        "navigation": navigation,
                        "redirected": redirected,
                        "fatal": fatal,
                        "detected": detected,
                        "session": "child" if session_id else "root",
                    }
                )

    def _fail_integrity(self, reason: str) -> None:
        # The specific reason stays local; receipts carry only the bounded code.
        with self._lock:
            self._active = False
            if len(self.integrity_reasons) < 16:
                self.integrity_reasons.append(reason)
        self._record("interception_unavailable", fatal=True, detected="integrity")

    def tracks_ack(self, message_id: int) -> bool:
        """Return whether *message_id* answers a setup command this guard sent."""
        with self._lock:
            return message_id in self._setup

    def note_unexpected_target(self) -> None:
        """Record, once, that a page target other than the session's exists."""
        with self._lock:
            if self._unexpected_target:
                return
            self._unexpected_target = True
        self._record("unexpected_target", fatal=True, detected="integrity")

    def mark_lost(self, reason: str = "connection_lost") -> None:
        """Record that the protocol connection ended, so interception is unproven."""
        self._fail_integrity(reason)

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def violations(self, since: int = 0) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(item) for item in self._violations if item["seq"] > since]

    def report(self) -> dict[str, Any]:
        with self._lock:
            blocked = [dict(item) for item in self._violations[:MAX_REPORTED_VIOLATIONS]]
            return {
                "policy": POLICY_NAME,
                "version": POLICY_VERSION,
                "enforcement": ENFORCEMENT,
                "interception_active": self._active,
                "requests_checked": self._checked,
                "requests_blocked": self._navigation_blocks + self._subresource_blocks,
                "navigation_blocks": self._navigation_blocks,
                "subresource_blocks": self._subresource_blocks,
                "redirect_hops": self._redirect_hops,
                "cross_origin_redirects": self._cross_origin_redirects,
                "post_response_address_check": True,
                "residual_risks": list(RESIDUAL_RISKS),
                "blocked": blocked,
            }
