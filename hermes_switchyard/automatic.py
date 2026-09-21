"""Automatic, bounded skill routing through Hermes ``pre_llm_call``.

The hook defaults to advisory mode. Its opt-in typed consumer can load one
accepted skill through Hermes' normal skill loader without changing a toolset or
rewriting the cached system prompt. Local matching supplies a deterministic
fallback. Automatic routing defaults to local-only matching. Hosted Jev is
available only when hosted_sanitized mode is explicitly selected. Standing
acknowledgement is retained for explicit hosted opt-in; it does not authorize
hosted automatic routing by itself. A host envelope is optional strengthening and may
provide a narrower sanitized payload. The automatic hosted payload then contains only the accepted task and
exact candidate identifiers. Conversation history, candidate descriptions, and
full skill bodies stay local.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from . import receipt_state
from .client import PartialAccountingError
from .egress import (
    ROUTING_MODES,
    TurnEgressEvaluation,
    evaluate_turn_egress_policy,
    is_routing_mode,
)
from .routing import select_skill

logger = logging.getLogger(__name__)

LOCAL_DIAGNOSTIC_TOP_K = 32
MAX_TASK_CHARS = 4_000
MAX_CANDIDATE_NAME_CHARS = 128
MAX_DESCRIPTION_CHARS = 1_000
DEFAULT_LOCAL_THRESHOLD = 0.20
DEFAULT_LOCAL_MARGIN = 0.05
DEFAULT_CACHE_SECONDS = 30.0
MAX_CACHE_SECONDS = 300.0
DEFAULT_CACHE_SIZE = 32

# Stable, privacy-safe terminal states for the routing-receipt surface. These
# names identify every automatic-routing outcome without carrying task text,
# candidate descriptions, conversation history, or credentials.
RECEIPT_TERMINAL_STATES = receipt_state.RECEIPT_TERMINAL_STATES
# Stable local error codes for hosted failures. Provider exception text, local
# paths, usernames, and hostnames are never placed in a receipt.
HOSTED_ERROR_CODES = receipt_state.HOSTED_ERROR_CODES
# Field values that must never survive into a receipt, even if they are present
# on an intermediate recommendation result.
_RECEIPT_FORBIDDEN_MARKERS = (
    "SYNTHETIC_TASK_MARKER",
    "SYNTHETIC_CANDIDATE_DESCRIPTION_MARKER",
    "SYNTHETIC_CREDENTIAL_MARKER",
    "PRIVATE_HISTORY_MARKER",
    "Jev transport failed",
    "Jev connection failed",
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_:+.-]*")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_PROMPT_INJECTION_RE = re.compile(
    r"\b(?:ignore\s+(?:all\s+)?previous|system\s+prompt|developer\s+message|"
    r"jailbreak|exfiltrat(?:e|ion)|reveal\s+(?:the\s+)?(?:secret|token|prompt))\b",
    re.IGNORECASE,
)
_PAYMENT_RE = re.compile(
    r"\b(?:credit\s+card|card\s+number|cvv|cvc|bank\s+account|routing\s+number)\b|"
    r"\b(?:\d[ -]?){13,19}\b",
    re.IGNORECASE,
)
_VERIFICATION_RE = re.compile(
    r"\b(?:one[- ]time|verification|authenticator|mfa|2fa|otp)\b.{0,32}\b\d{4,10}\b",
    re.IGNORECASE,
)
_CONTACT_RE = re.compile(
    r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b|\b(?:\+?\d[\d(). -]{7,}\d)\b|\b(?:ssn|social\s+security)\b",
    re.IGNORECASE,
)
_SECRET_VALUE_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|bearer\s+[A-Za-z0-9._~+/=-]{16,})\b|"
    r"\b(?:api[_ -]?key|access[_ -]?token|secret)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_RESTRICTED_WORD_RE = re.compile(
    r"\b(?:credential|password|passphrase|hipaa|phi|"
    r"classified|confidential|export[- ]controlled|"
    r"Controlled Unclassified Information|CUI(?:\b|//))\b",
    re.IGNORECASE,
)

# These words do not identify a specialist skill. Keeping this list local makes
# the default path deterministic and avoids an auxiliary model call.
_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "can", "do", "for",
        "from", "how", "i", "in", "is", "it", "me", "my", "of", "on", "or",
        "that", "the", "this", "to", "use", "with", "you", "your",
    }
)


def _coerce_bounded_text(value: Any, max_chars: int) -> str:
    """Return bounded text from a message value without reading other roles."""
    if isinstance(value, str):
        return value.strip()[:max_chars]
    if isinstance(value, list):
        parts: list[str] = []
        remaining = max_chars
        for block in value:
            if remaining <= 0:
                break
            if isinstance(block, str):
                text = block
            elif isinstance(block, Mapping) and isinstance(block.get("text"), str):
                text = block["text"]
            else:
                continue
            if parts:
                remaining -= 1
                if remaining <= 0:
                    break
            bounded = text[:remaining]
            parts.append(bounded)
            remaining -= len(bounded)
        return "\n".join(parts).strip()
    return ""


def _coerce_text(value: Any) -> str:
    """Return bounded user-task text for the hosted decision boundary."""
    return _coerce_bounded_text(value, MAX_TASK_CHARS)


def _tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in _TOKEN_RE.findall(value.casefold()):
        if raw not in _STOPWORDS:
            tokens.add(raw)
        for part in re.split(r"[-_:+./]+", raw):
            if part and part not in _STOPWORDS:
                tokens.add(part)
    return tokens


def _local_scan_reason(value: Any, text: str) -> str | None:
    """Classify obvious restricted content before any hosted client exists."""
    if isinstance(value, Mapping):
        return "local_scan_unknown_structured"
    if not isinstance(value, (str, list, tuple)):
        return "local_scan_unclassifiable"
    if _CONTROL_CHAR_RE.search(text):
        return "local_scan_control_character"
    if _PROMPT_INJECTION_RE.search(text):
        return "local_scan_prompt_injection"
    if _PAYMENT_RE.search(text):
        return "local_scan_payment_data"
    if _VERIFICATION_RE.search(text):
        return "local_scan_verification_data"
    if _CONTACT_RE.search(text):
        return "local_scan_contact_identifier"
    if _SECRET_VALUE_RE.search(text):
        return "local_scan_secret_like_value"
    if _RESTRICTED_WORD_RE.search(text):
        return "local_scan_restricted_data"
    stripped = text.strip()
    structured_candidates = [stripped]
    embedded = re.search(r":\s*(?=[{\[])", stripped)
    if embedded:
        structured_candidates.append(stripped[embedded.end():])
    for candidate in structured_candidates:
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, (dict, list)):
            return "local_scan_unknown_structured"
    return None


def _validate_name(name: Any) -> str:
    if type(name) is not str or not name or name != name.strip():
        raise ValueError("automatic skill candidate names must be non-empty exact strings")
    if len(name) > MAX_CANDIDATE_NAME_CHARS or any(ord(char) < 32 for char in name):
        raise ValueError("automatic skill candidate names must be short and control-character-free")
    return name


def _hosted_payload_scan_reason(task: str, candidates: list[dict[str, str]]) -> str | None:
    """Scan the exact fields that would cross the hosted boundary."""
    reason = _local_scan_reason(task, task)
    if reason is not None:
        return reason
    for candidate in candidates:
        name = candidate["name"]
        reason = _local_scan_reason(name, name)
        if reason is not None:
            return reason
    return None


def _validate_candidates(raw: Any, *, limit: int | None) -> tuple[dict[str, str], ...]:
    if not isinstance(raw, (list, tuple)) or not raw or (limit is not None and len(raw) > limit):
        bound = f"1 to {limit}" if limit is not None else "at least 1"
        raise ValueError(f"automatic skill candidates must contain {bound} entries")
    result: list[dict[str, str]] = []
    names: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            name = _validate_name(item)
            description = name
        elif isinstance(item, Mapping):
            name = _validate_name(item.get("name"))
            description = item.get("description", name)
            if type(description) is not str:
                raise ValueError(f"automatic skill candidate {name!r} description must be a string")
            description = description[:MAX_DESCRIPTION_CHARS]
        else:
            raise ValueError("automatic skill candidates must be strings or objects")
        if name in names:
            raise ValueError("automatic skill candidate names must be unique")
        names.add(name)
        result.append({"name": name, "description": description})
    return tuple(result)


def discover_available_skill_candidates() -> tuple[dict[str, str], ...]:
    """Discover the active profile's skills through Hermes' public skills API.

    ``pre_llm_call`` receives the conversation messages before Hermes prepends
    the cached system prompt, so that payload is not a reliable skill catalog.
    ``tools.skills_tool.skills_list()`` is the supported profile-scoped registry
    surface and already filters disabled/platform-ineligible skills. Descriptions
    remain local ranking metadata and are bounded before use.
    """
    try:
        from tools.skills_tool import skills_list

        response = skills_list()
        payload = json.loads(response) if isinstance(response, str) else response
        rows = payload.get("skills") if isinstance(payload, Mapping) else None
        if not isinstance(rows, list):
            return ()
        candidates: list[dict[str, str]] = []
        for item in rows:
            if not isinstance(item, Mapping):
                continue
            try:
                candidate = _validate_candidates(
                    [{
                        "name": item.get("name"),
                        "description": item.get("description") or item.get("name") or "",
                    }],
                    limit=1,
                )[0]
            except ValueError:
                continue
            candidates.append(candidate)
        return tuple(candidates)
    except Exception as exc:  # noqa: BLE001 -- catalog discovery is fail-open
        logger.debug("skill registry discovery failed: %s", type(exc).__name__)
        return ()


def _rank_candidates(task: str, candidates: tuple[dict[str, str], ...]) -> list[tuple[float, int, dict[str, str]]]:
    task_tokens = _tokens(task)
    ranked: list[tuple[float, int, dict[str, str]]] = []
    for position, candidate in enumerate(candidates):
        candidate_tokens = _tokens(f"{candidate['name']} {candidate['description']}")
        overlap = task_tokens & candidate_tokens
        if not task_tokens or not candidate_tokens:
            score = 0.0
        else:
            task_coverage = len(overlap) / len(task_tokens)
            candidate_coverage = len(overlap) / len(candidate_tokens)
            score = round((task_coverage * 0.6) + (candidate_coverage * 0.4), 6)
        ranked.append((score, position, candidate))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return ranked


def _local_decision(
    ranked: list[tuple[float, int, dict[str, str]]],
    *,
    threshold: float,
    margin: float,
) -> tuple[str | None, str, float]:
    if not ranked or ranked[0][0] < threshold:
        return None, "no_local_match", ranked[0][0] if ranked else 0.0
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < margin:
        return None, "ambiguous_local_match", ranked[0][0]
    return ranked[0][2]["name"], "local_match", ranked[0][0]


class AutomaticSkillRecommender:
    """Bounded recommender with explicit local/hosted routing and safe caching."""

    def __init__(
        self,
        *,
        configured_candidates: Any = None,
        routing_mode: str | None = None,
        hosted_enabled: bool | None = None,
        hosted_mode: str = "always",
        public_or_sanitized_data_ack: bool = True,
        client_factory: Callable[[], Any] | None = None,
        cache_identity: Callable[[], Any] | None = None,
        local_threshold: float = DEFAULT_LOCAL_THRESHOLD,
        local_margin: float = DEFAULT_LOCAL_MARGIN,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self.configured_candidates = (
            _validate_candidates(configured_candidates, limit=None)
            if configured_candidates else ()
        )
        # ``hosted_enabled`` is retained only for callers using the legacy
        # constructor API. The plugin registration path always supplies an
        # explicit mode, whose unset configuration fallback is local-only.
        if routing_mode is None:
            routing_mode = "hosted_sanitized" if hosted_enabled is True else "local_only"
        if not is_routing_mode(routing_mode):
            raise ValueError(f"routing_mode must be one of {sorted(ROUTING_MODES)!r}")
        self.routing_mode = routing_mode
        self.hosted_enabled = routing_mode == "hosted_sanitized"
        if hosted_mode not in {"uncertain_only", "always"}:
            raise ValueError("hosted_mode must be 'uncertain_only' or 'always'")
        self.hosted_mode = hosted_mode
        # Standing acknowledgement is on after install. recommend() returns
        # ack_required and skips the client only when it is explicitly false.
        self.public_or_sanitized_data_ack = public_or_sanitized_data_ack is True
        self.client_factory = client_factory
        self.cache_identity = cache_identity
        self.local_threshold = local_threshold
        self.local_margin = local_margin
        self.cache_seconds = max(0.0, min(float(cache_seconds), MAX_CACHE_SECONDS))
        self.cache_size = max(1, min(int(cache_size), 128))
        self._cache: OrderedDict[str, tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()
        self._client: Any | None = None
        self._client_identity: str | None = None
        self.last_receipt: dict[str, Any] | None = None

    def _route_identity_digest(self) -> str:
        """Hash the live route/profile identity without retaining raw values."""
        identity: Any = None
        if self.cache_identity is not None:
            try:
                identity = self.cache_identity()
            except Exception:  # noqa: BLE001 - identity failure must not block local routing
                identity = {"unavailable": True}
        try:
            encoded = json.dumps(
                identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            encoded = repr(identity).encode("utf-8", "backslashreplace")
        return hashlib.sha256(encoded).hexdigest()

    def _pooled_client(self) -> Any:
        """Return the client owned by this recommender for the live route."""
        if self.client_factory is None:
            raise RuntimeError("hosted client is unavailable")
        identity = self._route_identity_digest()
        with self._lock:
            if self._client is not None and self._client_identity == identity:
                return self._client
            if self._client is not None:
                close = getattr(self._client, "close", None)
                if callable(close):
                    close()
            self._client = self.client_factory()
            self._client_identity = identity
            return self._client

    def close(self) -> None:
        """Close the explicitly owned pooled client."""
        with self._lock:
            client = self._client
            self._client = None
            self._client_identity = None
            if client is not None:
                close = getattr(client, "close", None)
                if callable(close):
                    close()

    def __enter__(self) -> "AutomaticSkillRecommender":
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.close()

    def _cache_key(
        self,
        task_text: str,
        candidate_set: tuple[dict[str, str], ...],
        policy_key: Any = None,
    ) -> str:
        material = {
            "task": task_text,
            "candidates": [(item["name"], item["description"]) for item in candidate_set],
            "routing_mode": self.routing_mode,
            "hosted_mode": self.hosted_mode,
            "policy": policy_key,
            "route_identity": self._route_identity_digest(),
        }
        try:
            encoded = json.dumps(
                material,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            encoded = repr(material).encode("utf-8", "backslashreplace")
        return hashlib.sha256(encoded).hexdigest()

    def _cached(self, key: str) -> dict[str, Any] | None:
        if self.cache_seconds <= 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(key)
            if entry is None:
                return None
            expires, result = entry
            if expires <= now:
                self._cache.pop(key, None)
                return None
            self._cache.move_to_end(key)
            return {**result, "cache_hit": True}

    def _store(self, key: str, result: dict[str, Any]) -> None:
        if self.cache_seconds <= 0:
            return
        with self._lock:
            self._cache[key] = (time.monotonic() + self.cache_seconds, dict(result))
            self._cache.move_to_end(key)
            while len(self._cache) > self.cache_size:
                self._cache.popitem(last=False)

    def _record_receipt(self, result: dict[str, Any]) -> None:
        receipt = build_routing_receipt(result)
        self.last_receipt = receipt
        receipt_state.store_latest_receipt(receipt)

    @staticmethod
    def _empty_result(
        *, routing_mode: str, reason: str, status: str = "abstained", routing_status: str = "hosted_skipped"
    ) -> dict[str, Any]:
        return {
            "status": status,
            "selected": None,
            "source": "none",
            "abstention_reason": reason,
            "routing_mode": routing_mode,
            "routing_status": routing_status,
            "routing_reason": reason,
            "hosted_attempted": False,
            "hosted_skipped": reason,
            "candidate_count": 0,
            "candidates_considered": [],
            "cache_hit": False,
        }

    def recommend(
        self,
        task: Any,
        *,
        candidates: Any = None,
        candidates_from_prompt: bool = False,
        turn_egress_policy: Any = None,
        egress_policy: Any = None,
    ) -> dict[str, Any]:
        del candidates_from_prompt  # retained for callback compatibility
        if self.routing_mode == "off":
            result = self._empty_result(
                routing_mode=self.routing_mode,
                reason="routing_mode_off",
                routing_status="disabled",
            )
            self._record_receipt(result)
            return result

        task_text = _coerce_text(task)
        if not task_text:
            result = self._empty_result(
                routing_mode=self.routing_mode,
                reason="empty_task",
                routing_status="local_abstention",
            )
            self._record_receipt(result)
            return result

        if self.configured_candidates:
            candidate_set = self.configured_candidates
        else:
            try:
                candidate_set = _validate_candidates(candidates, limit=None)
            except ValueError:
                candidate_set = ()
        if not candidate_set:
            result = self._empty_result(
                routing_mode=self.routing_mode,
                reason="no_candidates",
                routing_status="local_abstention",
            )
            self._record_receipt(result)
            return result

        evaluation: TurnEgressEvaluation | None = None
        if self.routing_mode == "hosted_sanitized":
            policy = turn_egress_policy if turn_egress_policy is not None else egress_policy
            if policy is not None:
                supplied = evaluate_turn_egress_policy(policy)
                if supplied.decision == "deny" or supplied.status == "unknown":
                    evaluation = supplied
                elif not self.public_or_sanitized_data_ack:
                    evaluation = TurnEgressEvaluation(
                        allowed=False,
                        decision="deny",
                        data_class=supplied.data_class,
                        status="denied",
                        reason_code="ack_required",
                        version=supplied.version,
                    )
                else:
                    evaluation = supplied
            elif not self.public_or_sanitized_data_ack:
                evaluation = TurnEgressEvaluation(
                    allowed=False,
                    decision="deny",
                    data_class=None,
                    status="denied",
                    reason_code="ack_required",
                    version=1,
                )
            else:
                scan_reason = _local_scan_reason(task, task_text)
                if scan_reason is None:
                    evaluation = TurnEgressEvaluation(
                        allowed=True,
                        decision="allow",
                        data_class="sanitized",
                        status="allowed",
                        reason_code="local_scan_allowed",
                        allowed_payload=task_text,
                        version=1,
                    )
                else:
                    evaluation = TurnEgressEvaluation(
                        allowed=False,
                        decision="deny",
                        data_class="unknown",
                        status="denied",
                        reason_code=scan_reason,
                        version=1,
                    )

        policy_key = evaluation.cache_key if evaluation is not None else None
        key = self._cache_key(task_text, candidate_set, policy_key)
        cached = self._cached(key)
        if cached is not None:
            cached["hosted_attempted"] = False
            self._record_receipt(cached)
            return cached

        ranked = _rank_candidates(task_text, candidate_set)
        local_selected, local_reason, local_score = _local_decision(
            ranked,
            threshold=self.local_threshold,
            margin=self.local_margin,
        )
        top_candidates = [item[2] for item in ranked[:LOCAL_DIAGNOSTIC_TOP_K]]
        result: dict[str, Any] = {
            "status": "selected" if local_selected else "abstained",
            "selected": local_selected,
            "source": "local" if local_selected else "none",
            "abstention_reason": None if local_selected else local_reason,
            "local_score": local_score,
            "candidate_count": len(candidate_set),
            "candidates_considered": [item["name"] for item in top_candidates],
            "routing_mode": self.routing_mode,
            "routing_status": "local_selection" if local_selected else "local_abstention",
            "routing_reason": local_reason if not local_selected else "local_match",
            "hosted_attempted": False,
            "cache_hit": False,
        }
        if evaluation is not None:
            result.update(evaluation.metadata)

        should_host = self.hosted_mode == "always" or local_selected is None
        hosted_candidates = [{"name": item["name"]} for item in candidate_set]
        outbound_scan_reason = (
            _hosted_payload_scan_reason(
                evaluation.allowed_payload if evaluation is not None and evaluation.allowed_payload is not None else "",
                hosted_candidates,
            )
            if should_host and evaluation is not None and evaluation.allowed
            else None
        )
        if self.routing_mode == "local_only":
            result["hosted_skipped"] = "routing_mode_local_only"
            result["routing_status"] = "local_selection" if local_selected else "local_abstention"
            result["routing_reason"] = local_reason if not local_selected else "local_match"
        elif evaluation is not None and not evaluation.allowed:
            # This branch occurs before client_factory() by design. Local
            # matching is still allowed because it does not cross the boundary.
            result["hosted_skipped"] = evaluation.reason_code
            result["routing_status"] = "hosted_skipped"
            result["routing_reason"] = evaluation.reason_code
        elif not should_host:
            result["hosted_skipped"] = "local_confident"
            result["routing_status"] = "hosted_skipped"
            result["routing_reason"] = "local_confident"
        elif outbound_scan_reason is not None:
            result["hosted_skipped"] = outbound_scan_reason
            result["routing_status"] = "hosted_skipped"
            result["routing_reason"] = outbound_scan_reason
        elif self.client_factory is None:
            result["hosted_skipped"] = "client_unavailable"
            result["routing_status"] = "hosted_skipped"
            result["routing_reason"] = "client_unavailable"
        else:
            # Only the host-provided bounded payload crosses this boundary. The
            # original task, history, descriptions, and skill bodies do not.
            result["hosted_attempted"] = True
            try:
                hosted = select_skill(
                    task=evaluation.allowed_payload if evaluation is not None else "",
                    candidates=hosted_candidates,
                    client=self._pooled_client(),
                    public_or_sanitized_data_ack=True,
                )
            except PartialAccountingError as exc:
                logger.debug("automatic Jev skill recommendation unavailable: %s", type(exc).__name__)
                result["hosted_error"] = _hosted_error_code(exc)
                _copy_redacted_jev_metadata(result, _partial_accounting_metadata(exc.partial))
                hosted = None
            except Exception as exc:  # noqa: BLE001 -- automatic hook must fail open
                logger.debug("automatic Jev skill recommendation unavailable: %s", type(exc).__name__)
                result["hosted_error"] = _hosted_error_code(exc)
                hosted = None
            if isinstance(hosted, dict):
                _copy_redacted_jev_metadata(result, hosted)
            offered_names = {item["name"] for item in hosted_candidates}
            if isinstance(hosted, dict) and hosted.get("selected") in offered_names:
                result.update(
                    {
                        "status": "selected",
                        "selected": hosted["selected"],
                        "source": "jev",
                        "abstention_reason": None,
                        "routing_status": "hosted_selection",
                        "routing_reason": "hosted_selection",
                    }
                )
            elif hosted is None:
                # A transport/client failure is unavailable; preserve a local
                # result if one exists. A valid Jev abstention below is never
                # overridden by this fallback.
                result["hosted_error"] = "transport_or_execution_failure"
                result["hosted_error_code"] = "transport_or_execution_failure"
                result["routing_status"] = "hosted_failure_local_fallback" if local_selected else "hosted_failure"
                result["routing_reason"] = "hosted_request_failed"
                if local_selected:
                    result["status"] = "selected"
                    result["source"] = "local"
                    result["abstention_reason"] = None
            else:
                # Any structurally valid hosted response without an offered
                # selection is a deliberate hosted abstention.
                result.update(
                    {
                        "status": "abstained",
                        "selected": None,
                        "source": "none",
                        "abstention_reason": "hosted_abstention",
                        "routing_status": "hosted_abstention",
                        "routing_reason": "hosted_abstention",
                    }
                )

        if result["selected"] is not None:
            result["status"] = "selected"
        self._store(key, result)
        self._record_receipt(result)
        return result


def _copy_redacted_jev_metadata(result: dict[str, Any], hosted: Mapping[str, Any]) -> None:
    """Copy only bounded non-payload Jev metadata into an internal result."""
    for field in (
        "latency_ms", "total_latency_ms", "request_count", "offered_count", "excluded_count",
    ):
        value = hosted.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            result[f"jev_{field}"] = value
    for field in ("model", "request_id", "shortlist_policy"):
        value = hosted.get(field)
        if isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable():
            result[f"jev_{field}"] = value
    for field in ("usage", "total_usage"):
        usage = hosted.get(field)
        if isinstance(usage, Mapping):
            bounded_usage = receipt_state.safe_usage(usage)
            if bounded_usage:
                result[f"jev_{field}"] = bounded_usage


def _partial_accounting_metadata(partial: Any) -> dict[str, Any]:
    """Summarize successful hosted calls recorded before a later failure."""
    if not isinstance(partial, list) or not partial:
        return {}
    total_latency_ms = 0.0
    total_usage: dict[str, float] = {}
    request_count = 0
    last: Mapping[str, Any] | None = None
    for item in partial:
        if not isinstance(item, Mapping):
            continue
        has_latency = (
            isinstance(item.get("latency_ms"), (int, float))
            and not isinstance(item.get("latency_ms"), bool)
            and math.isfinite(item.get("latency_ms"))
            and item.get("latency_ms") >= 0
        ) or (
            isinstance(item.get("total_latency_ms"), (int, float))
            and not isinstance(item.get("total_latency_ms"), bool)
            and math.isfinite(item.get("total_latency_ms"))
            and item.get("total_latency_ms") >= 0
        )
        has_identifier = any(isinstance(item.get(field), str) and item.get(field) for field in ("model", "request_id"))
        has_usage = isinstance(item.get("total_usage"), Mapping) or isinstance(item.get("usage"), Mapping)
        entry_request_count = item.get("request_count")
        if isinstance(entry_request_count, int) and not isinstance(entry_request_count, bool) and entry_request_count > 0:
            count = entry_request_count
        elif has_latency or has_identifier or has_usage:
            count = 1
        else:
            continue
        last = item
        request_count += count
        latency = item.get("total_latency_ms", item.get("latency_ms"))
        if isinstance(latency, (int, float)) and not isinstance(latency, bool) and math.isfinite(latency) and latency >= 0:
            total_latency_ms += float(latency)
        usage = item.get("total_usage")
        if not isinstance(usage, Mapping):
            usage = item.get("usage")
        if isinstance(usage, Mapping):
            for key, value in usage.items():
                if (
                    isinstance(key, str)
                    and isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(value)
                ):
                    total_usage[key] = total_usage.get(key, 0.0) + float(value)
    if request_count == 0 or last is None:
        return {}
    metadata: dict[str, Any] = {
        "request_count": request_count,
        "total_latency_ms": total_latency_ms,
    }
    if total_usage:
        metadata["total_usage"] = total_usage
    for field in ("latency_ms", "model", "request_id", "usage"):
        value = last.get(field)
        if value is not None:
            metadata[field] = value
    return metadata


def redacted_routing_metadata(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return status/reason metadata without task, payload, or provider text."""
    fields = (
        "routing_mode", "routing_status", "routing_reason", "status", "source",
        "selected", "hosted_attempted", "hosted_skipped", "hosted_error_code",
        "candidate_count", "cache_hit", "policy_status", "policy_reason",
        "policy_data_class", "policy_version",
    )
    metadata: dict[str, Any] = {}
    for field in fields:
        value = result.get(field)
        if value is not None:
            metadata[field] = value
    return metadata


def _config_float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    if type(value) not in (int, float) or value != value or value in (float("inf"), float("-inf")):
        return default
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return float(value)


def _config_bool(value: Any, default: bool) -> bool:
    return value if type(value) is bool else default


def _hosted_error_code(exc: Exception) -> str:
    # Map any hosted failure to a stable local code. Provider, transport, and
    # executor details are never surfaced in a receipt.
    if isinstance(exc, PermissionError):
        return "ack_required"
    if isinstance(exc, ValueError):
        return "validation_failure"
    if isinstance(exc, TypeError):
        return "typed_response_failure"
    if isinstance(exc, RuntimeError):
        return "transport_or_execution_failure"
    return "plugin_error"


def _terminal_state(result: dict) -> str:
    """Map a recommendation result onto exactly one stable receipt terminal state.

    The mapping reads only local, stable result fields. It never inspects task
    text, candidate descriptions, or conversation history.
    """
    if result.get("cache_hit") is True:
        return "cache_hit"
    selected = result.get("selected")
    if (
        result.get("hosted_attempted") is True
        and result.get("source") == "local"
        and isinstance(selected, str)
        and selected
        and result.get("hosted_error")
    ):
        return "hosted_failure_local_fallback"
    if (
        result.get("hosted_attempted") is True
        and result.get("hosted_error")
        and not (isinstance(selected, str) and selected)
    ):
        return "hosted_failure"
    if result.get("source") == "local" and isinstance(selected, str) and selected:
        return "local_selection"
    if result.get("hosted_skipped") is not None:
        return "hosted_skipped"
    attempted = result.get("hosted_attempted") is True
    if not attempted:
        return "hosted_skipped"
    if isinstance(selected, str) and selected:
        if result.get("source") == "jev":
            return "hosted_selection"
        return "hosted_abstention"
    return "hosted_abstention"


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str) or any(marker in value for marker in _RECEIPT_FORBIDDEN_MARKERS):
        return None
    return receipt_state.safe_identifier(value)


def _safe_reason(value: Any) -> str | None:
    if not isinstance(value, str) or any(marker in value for marker in _RECEIPT_FORBIDDEN_MARKERS):
        return None
    return receipt_state.safe_reason(value)


def _result_value(result: Mapping[str, Any], name: str) -> Any:
    """Read hosted metadata without requiring callers to know its prefix."""
    prefixed = result.get(f"jev_{name}")
    return prefixed if prefixed is not None else result.get(name)


def build_routing_receipt(result: dict) -> dict:
    """Build a privacy-safe, typed routing receipt from a recommendation result.

    The receipt is operator-visible local evidence only. It exposes stable typed
    fields for every automatic-routing terminal state without carrying task text,
    candidate descriptions, conversation history, credentials, or provider
    exception text. It never implies a skill was loaded or a GUI action ran; the
    receipt is advisory and `verified` is always false.
    """
    if not isinstance(result, Mapping):
        result = {}
    raw_selected = result.get("selected")
    selected = _safe_identifier(raw_selected)
    if raw_selected is not None and selected is None:
        result = {
            **result,
            "selected": None,
            "source": "none",
            "hosted_attempted": False,
            "hosted_error": None,
            "hosted_skipped": "diagnostic_value_unavailable",
        }
    hosted_attempted = result.get("hosted_attempted") is True and result.get("cache_hit") is not True
    hosted_error = result.get("hosted_error")
    if hosted_error is not None and hosted_error not in HOSTED_ERROR_CODES:
        hosted_error = "transport_or_execution_failure"
    terminal_state = _terminal_state(result)
    source = result.get("source") if result.get("source") in {"local", "jev", "none"} else "none"
    if result.get("cache_hit") is True:
        hosted_attempted = False
        hosted_error = None
        selected = _safe_identifier(result.get("selected"))
        hosted_skip_reason = "cache_hit"
        jev_model = None
        request_id = None
        request_count = 0
        latency_ms = 0.0
        total_latency_ms = 0.0
        total_usage: dict[str, float] = {}
    else:
        hosted_skip_reason = result.get("hosted_skipped")
        if hosted_skip_reason not in receipt_state.HOSTED_SKIP_REASONS:
            hosted_skip_reason = "diagnostic_value_unavailable" if terminal_state == "hosted_skipped" else None
        jev_model = _safe_identifier(_result_value(result, "model"))
        request_id = _safe_identifier(_result_value(result, "request_id"))
        request_count = receipt_state.nonnegative_int(_result_value(result, "request_count"))
        latency_ms = receipt_state.finite_nonnegative(_result_value(result, "latency_ms"))
        total_latency_ms = receipt_state.finite_nonnegative(
            _result_value(result, "total_latency_ms"), default=latency_ms
        )
        total_usage = receipt_state.safe_usage(
            _result_value(result, "total_usage") or _result_value(result, "usage") or {}
        )
    hosted_succeeded = bool(
        hosted_attempted
        and hosted_error is None
        and terminal_state in {"hosted_selection", "hosted_abstention"}
    )
    identity = receipt_state.plugin_identity()

    return {
        "terminal_state": terminal_state,
        "source": source,
        "selected": selected,
        "hosted_attempted": hosted_attempted,
        "hosted_succeeded": hosted_succeeded,
        "hosted_error": hosted_error,
        "hosted_skip_reason": hosted_skip_reason,
        "abstention_reason": _safe_reason(result.get("abstention_reason")),
        "jev_model": jev_model,
        "request_id": request_id,
        "request_count": request_count,
        "latency_ms": latency_ms,
        "total_latency_ms": total_latency_ms,
        "total_usage": total_usage,
        "candidate_count": receipt_state.nonnegative_int(result.get("candidate_count")),
        "offered_count": receipt_state.nonnegative_int(_result_value(result, "offered_count")),
        "excluded_count": receipt_state.nonnegative_int(_result_value(result, "excluded_count")),
        "shortlist_policy": _safe_identifier(_result_value(result, "shortlist_policy")),
        "verified": False,
        "advisory_only": True,
        "plugin_identity": identity,
        "source_sha": identity["source_sha"],
    }


def _format_recommendation(name: str) -> str:
    return (
        "Advisory skill recommendation: consider the exact skill identifier "
        f"{json.dumps(name)} if it fits this request. The plugin did not load it. "
        "Mandatory skills, explicit instructions, safety controls, and the user's preferences take precedence."
    )


def _explicit_skill_override(task: Any, candidates: Any) -> str | None:
    """Return an explicitly requested candidate, if the turn names one."""
    text = _coerce_bounded_text(task, MAX_TASK_CHARS).lower()
    for candidate in candidates or ():
        name = candidate.get("name") if isinstance(candidate, Mapping) else candidate
        if not isinstance(name, str) or not name:
            continue
        escaped = re.escape(name.lower())
        if re.search(rf"(?:^|\s)/{escaped}(?:\s|$)", text) or re.search(
            rf"\b(?:use|load)\s+(?:the\s+)?(?:skill\s+)?[`'\"]?{escaped}(?:[`'\"]|\b)",
            text,
        ):
            return name
    return None


def discover_mandatory_skills(configured: Any = None) -> tuple[str, ...]:
    """Return exact mandatory skill identifiers without repairing names.

    An explicit configured list, including an empty list, wins. ``None``
    reads Hermes ``skills.always_load`` when that config is available and
    fails closed to no mandatory skills otherwise.
    """
    names: list[str] = []
    if isinstance(configured, (list, tuple)):
        for item in configured:
            if isinstance(item, str) and item:
                names.append(item)
        return tuple(names)
    if configured is not None:
        return ()
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
    except Exception:
        return ()
    skills = cfg.get("skills") if isinstance(cfg, dict) else None
    always_load = skills.get("always_load") if isinstance(skills, dict) else None
    if not isinstance(always_load, list):
        return ()
    for item in always_load:
        if isinstance(item, str) and item:
            names.append(item)
    return tuple(names)


def _mandatory_skill_conflict(selected: str, mandatory_skills: Any) -> bool:
    names = discover_mandatory_skills(mandatory_skills if isinstance(mandatory_skills, (list, tuple)) else ())
    return bool(names) and selected not in names


def build_pre_llm_call_hook(
    *,
    enabled: bool = True,
    configured_candidates: Any = None,
    routing_mode: str | None = None,
    hosted_enabled: bool | None = None,
    hosted_mode: str = "always",
    public_or_sanitized_data_ack: bool = True,
    client_factory: Callable[[], Any] | None = None,
    cache_identity: Callable[[], Any] | None = None,
    local_threshold: float = DEFAULT_LOCAL_THRESHOLD,
    local_margin: float = DEFAULT_LOCAL_MARGIN,
    cache_seconds: float = DEFAULT_CACHE_SECONDS,
    consumer_mode: str = "advisory",
    skill_loader: Callable[..., str] | None = None,
    mandatory_skills: Any = (),
) -> Callable[..., dict[str, Any] | None] | None:
    """Build a genuine Hermes ``pre_llm_call`` callback, or disable it."""
    if enabled is not True:
        return None
    if consumer_mode not in {"advisory", "load"}:
        raise ValueError("consumer_mode must be advisory or load")
    if consumer_mode == "load" and skill_loader is None:
        raise ValueError("load consumer mode requires a skill_loader")
    active_skill_loader = skill_loader
    try:
        recommender = AutomaticSkillRecommender(
            configured_candidates=configured_candidates,
            routing_mode=routing_mode,
            hosted_enabled=hosted_enabled,
            hosted_mode=hosted_mode,
            public_or_sanitized_data_ack=public_or_sanitized_data_ack,
            client_factory=client_factory,
            cache_identity=cache_identity,
            local_threshold=local_threshold,
            local_margin=local_margin,
            cache_seconds=cache_seconds,
        )
    except (TypeError, ValueError) as exc:
        logger.warning("automatic skill recommendation disabled by invalid configuration: %s", type(exc).__name__)
        return None

    configured = bool(recommender.configured_candidates)
    consumed_turns: OrderedDict[tuple[str, str], dict[str, Any]] = OrderedDict()

    def on_pre_llm_call(
        *,
        user_message: Any = None,
        conversation_history: Any = None,
        turn_egress_policy: Any = None,
        egress_policy: Any = None,
        session_id: Any = None,
        turn_id: Any = None,
        **_: Any,
    ) -> dict[str, Any] | None:
        turn_key = (
            (str(session_id), str(turn_id))
            if isinstance(session_id, str) and session_id and isinstance(turn_id, str) and turn_id
            else None
        )
        if turn_key is not None and turn_key in consumed_turns:
            return dict(consumed_turns[turn_key])
        # Hermes' conversation_history does not include the cached system prompt
        # that advertises skills. Discover the active profile registry directly.
        del conversation_history  # local-only input; never part of an egress payload
        catalog_candidates = (
            ()
            if configured or recommender.routing_mode == "off"
            else discover_available_skill_candidates()
        )
        result = recommender.recommend(
            user_message,
            candidates=catalog_candidates,
            candidates_from_prompt=False,
            turn_egress_policy=turn_egress_policy,
            egress_policy=egress_policy,
        )
        setattr(on_pre_llm_call, "last_result", dict(result))
        setattr(on_pre_llm_call, "last_receipt", dict(recommender.last_receipt or {}))
        metadata = redacted_routing_metadata(result)
        setattr(on_pre_llm_call, "last_metadata", dict(metadata))
        setattr(on_pre_llm_call, "last_routing_metadata", dict(metadata))
        selected = result.get("selected")
        if not isinstance(selected, str) or not selected:
            metadata["skill_recommendation"] = {
                "status": "abstained",
                "selected": None,
                "source": result.get("source", "none"),
                "loaded_once": False,
            }
            response = {"metadata": metadata}
        elif consumer_mode == "advisory":
            metadata["skill_recommendation"] = {
                "status": "advisory",
                "selected": selected,
                "source": result.get("source", "none"),
                "loaded_once": False,
            }
            response = {"context": _format_recommendation(selected), "metadata": metadata}
        else:
            candidates = recommender.configured_candidates if configured else catalog_candidates
            override = _explicit_skill_override(user_message, candidates)
            if override is not None:
                status, loaded_context = "explicit_override", None
            elif _mandatory_skill_conflict(selected, mandatory_skills):
                status, loaded_context = "mandatory_conflict", None
            else:
                try:
                    if active_skill_loader is None:
                        raise RuntimeError("skill loader is unavailable")
                    loaded_context = active_skill_loader(selected, task_id=session_id)
                    if not isinstance(loaded_context, str) or not loaded_context.strip():
                        raise ValueError("skill loader returned no content")
                    status = "loaded"
                except Exception:  # noqa: BLE001 -- consumer fails closed to advisory context
                    logger.warning("automatic skill consumer rejected %s", selected, exc_info=True)
                    status, loaded_context = "load_failed", None
            loaded_once = status == "loaded"
            metadata["skill_recommendation"] = {
                "status": status,
                "selected": selected,
                "source": result.get("source", "none"),
                "loaded_once": loaded_once,
            }
            receipt = dict(recommender.last_receipt or {})
            receipt.update(
                {
                    "consumer_status": status,
                    "loaded_skill": selected if loaded_once else None,
                    "loaded_source": result.get("source", "none") if loaded_once else None,
                    "skill_load_verified": loaded_once,
                    "advisory_only": not loaded_once,
                }
            )
            recommender.last_receipt = receipt
            receipt_state.store_latest_receipt(receipt)
            setattr(on_pre_llm_call, "last_receipt", dict(receipt))
            response = {
                "context": loaded_context or _format_recommendation(selected),
                "metadata": metadata,
            }
        if turn_key is not None:
            consumed_turns[turn_key] = dict(response)
            while len(consumed_turns) > DEFAULT_CACHE_SIZE:
                consumed_turns.popitem(last=False)
        return response

    return on_pre_llm_call
