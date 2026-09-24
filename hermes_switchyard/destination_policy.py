"""Code-owned destination policy for the DOM browser backend.

The approved destination class is a public https origin without credentials.
Everything else is refused by code, before any provider decides anything:
private, loopback, link-local and other non-global addresses, local-only names,
credentialed URLs, and every scheme that is not https (file, data, javascript,
about, blob, ftp, chrome, http, ws).

Three layers use this policy:

* :func:`check_destination` is the pure decision. It is lexical by default and
  adds a host-resolution check when asked. Resolution that fails or returns an
  empty answer is a refusal, never a pass.
* :class:`ValidatingProxy` pins the connection. Chrome resolves a host again to
  connect, so a rebinding server can answer differently the second time. Chrome
  is pointed at this loopback proxy instead: it resolves once, validates every
  address, and connects to the validated address literal, so there is no second
  resolution to rebind.
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
import select
import socket
import struct
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
# With connection pinning the DNS gap and the after-the-fact address check no
# longer apply. What remains is traffic that does not use the HTTP proxy.
RESIDUAL_RISKS_PINNED = ("non_proxied_udp_is_restricted_by_launch_flags_not_by_the_proxy",)
MAX_PROXY_HEAD_BYTES = 8192
_NON_FATAL_PROXY_REFUSALS = frozenset({"resolution_failed", "scheme_not_allowed"})
_HTTP_VERSION = re.compile(r"HTTP/1\.[01]\Z")
# CONNECT authority only: host:port or [ipv6]:port. A path, userinfo, or other
# spelling is malformed, not a destination the policy should dial.
_CONNECT_AUTHORITY = re.compile(r"\A(?:[A-Za-z0-9._-]+|\[[A-Fa-f0-9:.]+\])\:[0-9]{1,5}\Z")

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


def _connect_pinned(address: str, port: int, timeout: float) -> socket.socket:
    """Dial an address literal. A hostname is refused so nothing is resolved here."""
    ipaddress.ip_address(address)  # raises ValueError for anything but a literal
    return socket.create_connection((address, port), timeout=timeout)


class ValidatingProxy:
    """A loopback HTTP CONNECT proxy that pins every tunnel to a validated address.

    For each tunnel the proxy applies the lexical policy to the target, resolves
    the host once, requires every returned address to be public, and connects to
    one of those address literals. Plain HTTP and malformed requests are refused.
    ``on_refusal(code, host)`` is called for a refused, well-formed request before
    the refusal is sent, so evidence exists by the time the browser sees a failure.
    """

    def __init__(
        self,
        *,
        resolver: Callable[[str], list[str]] | None = None,
        connector: Callable[[str, int, float], socket.socket] | None = None,
        on_refusal: Callable[[str, str], None] | None = None,
        max_tunnels: int = 128,
        idle_seconds: float = 120.0,
        head_timeout: float = 10.0,
        connect_timeout: float = 10.0,
    ):
        self._resolver = resolver
        self._connector = connector
        self._on_refusal = on_refusal
        self._idle = idle_seconds
        self._head_timeout = head_timeout
        self._connect_timeout = connect_timeout
        self._slots = threading.BoundedSemaphore(max_tunnels)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._active: set[socket.socket] = set()
        self._stats = {"tunnels_opened": 0, "tunnels_refused": 0, "tunnels_failed": 0}
        self.port = 0

    def start(self) -> int:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.settimeout(0.2)
        self._listener = listener
        self.port = int(listener.getsockname()[1])
        self._thread = threading.Thread(target=self._accept_loop, name="switchyard-pin-proxy", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            active = list(self._active)
        for sock in active:
            self._close(sock)

    def stats(self) -> dict[str, int]:
        with self._lock:
            return dict(self._stats)

    # -- serving -------------------------------------------------------------

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            listener = self._listener
            if listener is None:
                return
            try:
                conn, _ = listener.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            # The slot is taken here so a flood cannot create unbounded threads; the
            # serving thread owns it and releases it when the tunnel ends.
            if not self._slots.acquire(blocking=False):
                # Windows aborts a socket that still holds an unread request.
                # Drain the CONNECT before the refusal so the status can be read.
                self._drain_unread(conn)
                self._reply(conn, "503 Service Unavailable")
                self._close(conn)
                continue
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    @staticmethod
    def _drain_unread(sock: socket.socket) -> None:
        sock.settimeout(1.0)
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk or b"\r\n\r\n" in chunk:
                    return
        except OSError:
            return

    @staticmethod
    def _close(sock: socket.socket | None) -> None:
        if sock is None:
            return
        try:
            # Windows aborts an unread response if close() races the send.
            # A short linger lets the refusal status leave the host.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 1))
        except (OSError, AttributeError):
            pass
        try:
            sock.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        try:
            sock.close()
        except OSError:
            pass

    @staticmethod
    def _reply(conn: socket.socket, status: str) -> None:
        try:
            conn.sendall(f"HTTP/1.1 {status}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n".encode("ascii"))
        except OSError:
            pass

    def _refuse(self, conn: socket.socket, status: str, code: str, host: str) -> None:
        with self._lock:
            self._stats["tunnels_refused"] += 1
        if self._on_refusal is not None:
            try:
                self._on_refusal(code, host)
            except Exception:  # noqa: BLE001 -- evidence reporting must not open the tunnel
                pass
        self._reply(conn, status)

    def _read_head(self, conn: socket.socket) -> tuple[bytes, bytes] | None:
        deadline = time.monotonic() + self._head_timeout
        buffer = b""
        while b"\r\n\r\n" not in buffer:
            if len(buffer) > MAX_PROXY_HEAD_BYTES:
                self._reply(conn, "431 Request Header Fields Too Large")
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            conn.settimeout(remaining)
            try:
                chunk = conn.recv(4096)
            except OSError:
                return None
            if not chunk:
                return None
            buffer += chunk
        head, _, rest = buffer.partition(b"\r\n\r\n")
        if len(head) > MAX_PROXY_HEAD_BYTES:
            self._reply(conn, "431 Request Header Fields Too Large")
            return None
        return head, rest

    def _serve(self, conn: socket.socket) -> None:
        upstream: socket.socket | None = None
        with self._lock:
            self._active.add(conn)
        try:
            parsed = self._read_head(conn)
            if parsed is None:
                return
            head, leftover = parsed
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            if len(parts) != 3 or _HTTP_VERSION.fullmatch(parts[2]) is None:
                self._reply(conn, "400 Bad Request")
                return
            method, target = parts[0].upper(), parts[1]
            if method != "CONNECT":
                # Only https tunnels are approved; a plain HTTP request is a scheme refusal.
                host = urlsplit(target).hostname or ""
                self._refuse(conn, "403 Forbidden", "scheme_not_allowed", host if _HOST_CHARS.fullmatch(host) else "")
                return
            if _CONNECT_AUTHORITY.fullmatch(target) is None:
                self._reply(conn, "400 Bad Request")
                return
            host_text, sep, port_text = target.rpartition(":")
            if not sep or not port_text.isascii() or not port_text.isdigit() or not 1 <= int(port_text) <= 65535 or not host_text:
                self._reply(conn, "400 Bad Request")
                return
            port = int(port_text)
            decision = check_destination(f"https://{target}/")
            if not decision.allowed:
                if decision.code in {"invalid_url", "missing_host", "invalid_host_characters"}:
                    self._reply(conn, "400 Bad Request")
                    return
                self._refuse(conn, "403 Forbidden", decision.code, decision.host)
                return
            addresses = self._validated_addresses(decision.host)
            if isinstance(addresses, str):
                status = "502 Bad Gateway" if addresses == "resolution_failed" else "403 Forbidden"
                self._refuse(conn, status, addresses, decision.host)
                return
            upstream = self._dial(addresses, port)
            if upstream is None:
                with self._lock:
                    self._stats["tunnels_failed"] += 1
                self._reply(conn, "502 Bad Gateway")
                return
            with self._lock:
                self._stats["tunnels_opened"] += 1
                self._active.add(upstream)
            conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            if leftover:
                upstream.sendall(leftover)
            self._relay(conn, upstream)
        except OSError:
            pass
        finally:
            with self._lock:
                self._active.discard(conn)
                if upstream is not None:
                    self._active.discard(upstream)
            self._close(upstream)
            self._close(conn)
            self._slots.release()

    def _validated_addresses(self, host: str) -> list[str] | str:
        """Return the addresses to dial, or a refusal code. Resolves at most once."""
        literal = _parse_address(host)
        if literal is not None:
            return [str(literal)]
        lookup = self._resolver if self._resolver is not None else default_resolver
        try:
            answers = list(lookup(host))
        except Exception:  # noqa: BLE001 -- unavailable evidence is a refusal
            return "resolution_failed"
        parsed = [_parse_address(str(item)) for item in answers]
        if not parsed or any(item is None for item in parsed):
            return "resolution_failed"
        if not all(_is_public_ip(item) for item in parsed if item is not None):
            return "resolved_non_public"
        return list(dict.fromkeys(str(item) for item in parsed))

    def _dial(self, addresses: list[str], port: int) -> socket.socket | None:
        connector = self._connector if self._connector is not None else _connect_pinned
        for address in addresses:
            try:
                return connector(address, port, self._connect_timeout)
            except (OSError, ValueError):
                continue
        return None

    def _relay(self, client: socket.socket, upstream: socket.socket) -> None:
        client.settimeout(self._idle)
        upstream.settimeout(self._idle)
        while not self._stop.is_set():
            try:
                ready, _, _ = select.select([client, upstream], [], [], self._idle)
            except (OSError, ValueError):
                return
            if not ready:
                return
            for sock in ready:
                try:
                    data = sock.recv(65536)
                    if not data:
                        return
                    (upstream if sock is client else client).sendall(data)
                except OSError:
                    return




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
        pinned: bool = False,
    ):
        # With pinning, the address a response came from is the local proxy's, so
        # the proxy, not the response address, is what judges the connection.
        self._pinned = pinned
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
        self._hops: OrderedDict[tuple[str | None, str], tuple[int, tuple[str, str, int | None], bool]] = OrderedDict()
        self._setup: dict[int, str | None] = {}
        self.integrity_reasons: list[str] = []
        self._unexpected_target = False
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._early_acks: dict[int, dict[str, Any]] = {}
        self._child_pending: dict[str, dict[str, Any]] = {}
        self._latest_fatal: dict[str, Any] | None = None
        self._setup_expecting: str | None = None

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
        """Wait until every submitted message has been handled.

        A timeout means a paused request may still be undecided. That is an
        interception-integrity failure: the request stays fail-closed and the
        run must not treat the session as clean.
        """
        with self._idle:
            ready = self._idle.wait_for(lambda: self._inflight == 0, timeout=timeout)
        if not ready:
            self._fail_integrity("idle_timeout")
        return ready

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
        prior_origin = ("", "", None)
        prior_approved = False
        with self._lock:
            self._checked += 1
            if redirected:
                prior_hop, prior_origin, prior_approved = self._hops.get(
                    (session_id, str(previous)), (0, ("", "", None), False)
                )
                hop = prior_hop + 1
                self._redirect_hops += 1
                if origin != prior_origin:
                    self._cross_origin_redirects += 1
            self._hops[(session_id, request_id)] = (hop, origin, False)
            while len(self._hops) > MAX_TRACKED_REQUESTS:
                self._hops.popitem(last=False)
        code: str | None
        decision: DestinationDecision | None = None
        upgraded_url: str | None = None
        try:
            # A same-host redirect may advertise HTTP even though its HTTPS
            # endpoint exists. Rewrite only the pending request; never send HTTP.
            # The ordinary destination policy still validates the exact upgraded
            # host/path/query and the validating proxy pins the connection.
            if redirected and prior_approved and navigation and hop <= self._max_redirects:
                parts = urlsplit(url)
                if parts.scheme == "http" and parts.netloc and parts.port is None:
                    candidate = urlunsplit(("https", parts.netloc, parts.path, parts.query, parts.fragment))
                    if _origin(candidate) == prior_origin:
                        upgraded_url = candidate
            decision = check_destination(
                upgraded_url or url, resource_type=resource_type, resolve=True, resolver=self._lookup
            )
            code = None if decision.allowed else decision.code
        except Exception:  # noqa: BLE001 -- no decision is a refusal
            code = "policy_error"
        if code is None and hop > self._max_redirects:
            code = "redirect_limit"
        if code is None:
            if not self.active:
                self._answer(
                    "Fetch.failRequest",
                    {"requestId": request_id, "errorReason": "BlockedByClient"},
                    session_id,
                )
                return
            with self._lock:
                self._hops[(session_id, request_id)] = (hop, _origin(upgraded_url) if upgraded_url else origin, True)
                while len(self._hops) > MAX_TRACKED_REQUESTS:
                    self._hops.popitem(last=False)
            if upgraded_url is not None:
                # Continuing with a URL override fetches HTTPS but leaves the
                # address bar at HTTP. A local redirect changes both without
                # allowing a plaintext HTTP request onto the wire.
                self._answer("Fetch.fulfillRequest", {
                    "requestId": request_id,
                    "responseCode": 307,
                    "responseHeaders": [{"name": "Location", "value": upgraded_url}],
                }, session_id)
            else:
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
        waiting = bool(params.get("waitingForDebugger"))
        pending: set[int] = set()
        early_messages: list[dict[str, Any]] = []
        try:
            # The child record exists before any setup send, so an ack processed
            # between _register_setup and this publication finds its owner and is
            # consumed inline instead of being lost to a missing child record.
            with self._lock:
                self._child_pending[session_id] = {"waiting": waiting, "pending": set()}
            for method, body in self.root_setup():
                if covered and method == "Fetch.enable":
                    continue
                with self._lock:
                    self._setup_expecting = session_id
                try:
                    message_id = self._send(method, body, session_id)
                finally:
                    with self._lock:
                        self._setup_expecting = None
                early = self._register_setup(message_id, session_id)
                pending.add(message_id)
                with self._lock:
                    child = self._child_pending.get(session_id)
                    if child is not None:
                        child["pending"].add(message_id)
                if early is not None:
                    early_messages.append(early)
            for early in early_messages:
                self._consume_setup_ack(early, session_id)
            with self._lock:
                child = self._child_pending.get(session_id)
                if child is not None and not child["pending"]:
                    waiting_flag = bool(child.get("waiting"))
                    self._child_pending.pop(session_id, None)
                else:
                    waiting_flag = None
            if waiting_flag is not None:
                self._resume_child(session_id, waiting_flag)
        except Exception:  # noqa: BLE001 -- a child that cannot be intercepted is not covered
            with self._lock:
                self._setup_expecting = None
                self._child_pending.pop(session_id, None)
            self._fail_integrity("child_setup_failed")

    def _register_setup(self, message_id: int, session_id: str) -> dict[str, Any] | None:
        """Track a setup ack id, reclaiming any response that won the race."""
        with self._lock:
            self._setup[message_id] = session_id
            return self._early_acks.pop(message_id, None)

    def _resume_child(self, session_id: str, waiting: bool) -> None:
        if not waiting:
            return
        try:
            self._send("Runtime.runIfWaitingForDebugger", {}, session_id)
        except Exception:  # noqa: BLE001 -- a child that cannot be resumed is not covered
            self._fail_integrity("child_setup_failed")

    def _consume_setup_ack(self, message: dict[str, Any], owner: str) -> None:
        message_id = message.get("id")
        if not isinstance(message_id, int):
            return
        with self._lock:
            self._setup.pop(message_id, None)
            child = self._child_pending.get(owner)
            if child is not None:
                child["pending"].discard(message_id)
        if "error" in message:
            with self._lock:
                self._child_pending.pop(owner, None)
            self._fail_integrity("child_setup_failed")
            return
        with self._lock:
            child = self._child_pending.get(owner)
            if child is None:
                return
            done = not child["pending"]
            waiting = bool(child.get("waiting"))
            if done:
                self._child_pending.pop(owner, None)
        if done:
            self._resume_child(owner, waiting)

    def _on_ack(self, message: dict[str, Any]) -> None:
        message_id = message.get("id")
        if not isinstance(message_id, int):
            return
        with self._lock:
            owner = self._setup.get(message_id)
            if owner is None:
                if self._setup_expecting is not None and len(self._early_acks) < 64:
                    self._early_acks[message_id] = message
                return
        self._consume_setup_ack(message, owner)

    def _on_response(self, params: dict[str, Any], session_id: str | None) -> None:
        response = params.get("response") if isinstance(params.get("response"), dict) else {}
        if self._pinned:
            return
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
            entry = {
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
            if len(self._violations) < MAX_RECORDED_VIOLATIONS:
                self._violations.append(entry)
            elif fatal:
                # Bounded nonfatal evidence must not hide a later fatal refusal.
                for index, existing in enumerate(self._violations):
                    if not existing.get("fatal"):
                        self._violations.pop(index)
                        self._violations.append(entry)
                        break
            if fatal:
                self._latest_fatal = entry

    def _fail_integrity(self, reason: str) -> None:
        # The specific reason stays local; receipts carry only the bounded code.
        with self._lock:
            self._active = False
            if len(self.integrity_reasons) < 16:
                self.integrity_reasons.append(reason)
        self._record("interception_unavailable", fatal=True, detected="integrity")

    def record_proxy_refusal(self, code: str, host: str) -> None:
        """Record that the proxy refused a tunnel the request boundary had allowed.

        A refused address is fatal: a name that the request check accepted resolved
        somewhere else at connect time. A resolution failure made no connection, and
        a plain-HTTP request is browser housekeeping (a page's own http request is
        already refused at the request boundary), so both are evidence only.
        """
        self._record(
            code,
            scheme="https",
            host=host,
            resource_type="Tunnel",
            navigation=False,
            redirected=False,
            fatal=code not in _NON_FATAL_PROXY_REFUSALS,
            detected="proxy_connect",
        )

    def tracks_ack(self, message_id: int) -> bool:
        """Return whether *message_id* answers, or may answer, a setup command.

        While a setup send is in flight, unknown ids are accepted so a response
        that arrives before registration can be buffered instead of dropped.
        """
        with self._lock:
            return (
                message_id in self._setup
                or message_id in self._early_acks
                or self._setup_expecting is not None
            )

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
            items = [dict(item) for item in self._violations if item["seq"] > since]
            latest = self._latest_fatal
            if latest is not None and latest["seq"] > since:
                if not any(item["seq"] == latest["seq"] for item in items):
                    items.append(dict(latest))
            return items

    def report(self) -> dict[str, Any]:
        with self._lock:
            # The bounded report must surface the terminal refusal: when the
            # latest fatal violation falls outside the first window, it replaces
            # the oldest nonfatal entry instead of being absent from the receipt.
            blocked = [dict(item) for item in self._violations[:MAX_REPORTED_VIOLATIONS]]
            latest = self._latest_fatal
            if (
                latest is not None
                and MAX_REPORTED_VIOLATIONS < len(self._violations)
                and latest["seq"] > self._violations[MAX_REPORTED_VIOLATIONS - 1]["seq"]
            ):
                for index in range(MAX_REPORTED_VIOLATIONS - 1, -1, -1):
                    if not blocked[index].get("fatal"):
                        blocked[index] = dict(latest)
                        break
                else:
                    blocked[-1] = dict(latest)
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
                "connection_pinning": self._pinned,
                "post_response_address_check": not self._pinned,
                "residual_risks": list(RESIDUAL_RISKS_PINNED if self._pinned else RESIDUAL_RISKS),
                "blocked": blocked,
            }
