"""Automatic, bounded skill recommendations through Hermes ``pre_llm_call``.

The hook is advisory only: it never loads a skill, changes a toolset, or rewrites
Hermes' system prompt. Local matching supplies a deterministic fallback. Hosted
Jev evaluates every eligible turn by default when its configuration switch and
the public/sanitized-data attestation are true. It receives only the bounded
current task, exact candidate identifiers, and bounded descriptions; conversation
history and full skill bodies stay local.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from . import receipt_state
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


def _validate_name(name: Any) -> str:
    if type(name) is not str or not name or name != name.strip():
        raise ValueError("automatic skill candidate names must be non-empty exact strings")
    if len(name) > MAX_CANDIDATE_NAME_CHARS or any(ord(char) < 32 for char in name):
        raise ValueError("automatic skill candidate names must be short and control-character-free")
    return name


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
    """Bounded recommender with a small per-plugin cache and fail-open egress."""

    def __init__(
        self,
        *,
        configured_candidates: Any = None,
        hosted_enabled: bool = False,
        hosted_mode: str = "always",
        public_or_sanitized_data_ack: bool = False,
        client_factory: Callable[[], Any] | None = None,
        local_threshold: float = DEFAULT_LOCAL_THRESHOLD,
        local_margin: float = DEFAULT_LOCAL_MARGIN,
        cache_seconds: float = DEFAULT_CACHE_SECONDS,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self.configured_candidates = (
            _validate_candidates(configured_candidates, limit=None)
            if configured_candidates else ()
        )
        self.hosted_enabled = hosted_enabled is True
        if hosted_mode not in {"uncertain_only", "always"}:
            raise ValueError("hosted_mode must be 'uncertain_only' or 'always'")
        self.hosted_mode = hosted_mode
        self.public_or_sanitized_data_ack = public_or_sanitized_data_ack is True
        self.client_factory = client_factory
        self.local_threshold = local_threshold
        self.local_margin = local_margin
        self.cache_seconds = max(0.0, min(float(cache_seconds), MAX_CACHE_SECONDS))
        self.cache_size = max(1, min(int(cache_size), 128))
        self._cache: OrderedDict[tuple[Any, ...], tuple[float, dict[str, Any]]] = OrderedDict()
        self._lock = threading.RLock()
        self.last_receipt: dict[str, Any] | None = None

    def _cached(self, key: tuple[Any, ...]) -> dict[str, Any] | None:
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

    def _store(self, key: tuple[Any, ...], result: dict[str, Any]) -> None:
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

    def recommend(
        self,
        task: Any,
        *,
        candidates: Any = None,
        candidates_from_prompt: bool = False,
    ) -> dict[str, Any]:
        task_text = _coerce_text(task)
        if not task_text:
            result = {
                "status": "abstained",
                "selected": None,
                "source": "none",
                "abstention_reason": "empty_task",
                "hosted_attempted": False,
                "hosted_skipped": "empty_task",
                "cache_hit": False,
            }
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
            result = {
                "status": "abstained",
                "selected": None,
                "source": "none",
                "abstention_reason": "no_candidates",
                "hosted_attempted": False,
                "hosted_skipped": "no_candidates",
                "cache_hit": False,
            }
            self._record_receipt(result)
            return result

        fingerprint = tuple((item["name"], item["description"]) for item in candidate_set)
        key = (task_text, fingerprint, self.hosted_enabled, self.hosted_mode, self.public_or_sanitized_data_ack)
        cached = self._cached(key)
        if cached is not None:
            cached["hosted_attempted"] = False
            cached["hosted_error"] = None
            cached["hosted_skipped"] = "cache_hit"
            for field, empty in (
                ("jev_model", None),
                ("jev_request_id", None),
                ("jev_latency_ms", 0.0),
                ("jev_usage", {}),
                ("jev_total_latency_ms", 0.0),
                ("jev_total_usage", {}),
                ("jev_request_count", 0),
                ("jev_offered_count", 0),
                ("jev_excluded_count", 0),
                ("jev_shortlist_policy", None),
            ):
                cached[field] = empty
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
            "status": "abstained",
            "selected": local_selected,
            "source": "local" if local_selected else "none",
            "abstention_reason": None if local_selected else local_reason,
            "local_score": local_score,
            "candidate_count": len(candidate_set),
            "candidates_considered": [item["name"] for item in top_candidates],
            "hosted_attempted": False,
            "cache_hit": False,
        }

        should_host = self.hosted_mode == "always" or local_selected is None
        if self.hosted_enabled and self.public_or_sanitized_data_ack and self.client_factory and should_host:
            result["hosted_attempted"] = True
            # With the explicit public/sanitized attestation, descriptions are
            # useful semantic evidence. The current task and bounded descriptions
            # are sent; conversation history and full skill bodies stay local.
            hosted_candidates = [
                {"name": item["name"], "description": item["description"]}
                for item in candidate_set
            ]
            try:
                hosted = select_skill(
                    task=task_text,
                    candidates=hosted_candidates,
                    client=self.client_factory(),
                    public_or_sanitized_data_ack=True,
                )
            except Exception as exc:  # noqa: BLE001 -- automatic hook must fail open
                logger.debug("automatic Jev skill recommendation unavailable: %s", type(exc).__name__)
                result["hosted_error"] = _hosted_error_code(exc)
                hosted = None
            if isinstance(hosted, dict):
                for field in (
                    "model", "latency_ms", "usage", "request_id",
                    "total_latency_ms", "total_usage", "request_count",
                    "offered_count", "excluded_count", "shortlist_policy",
                ):
                    if field in hosted and hosted[field] is not None:
                        result[f"jev_{field}"] = hosted[field]
            if isinstance(hosted, dict) and hosted.get("selected") in {
                item["name"] for item in hosted_candidates
            }:
                result.update(
                    {
                        "status": "selected",
                        "selected": hosted["selected"],
                        "source": "jev",
                        "abstention_reason": None,
                    }
                )
            elif hosted is None and local_selected:
                # A transport/client failure is unavailable; preserve the
                # deterministic local result. A valid Jev abstention below is
                # a deliberate safety decision and must not be overridden.
                result["status"] = "selected"
                result["source"] = "local"
                result["abstention_reason"] = None
            elif isinstance(hosted, dict):
                result.update(
                    {
                        "status": "abstained",
                        "selected": None,
                        "source": "none",
                        "abstention_reason": hosted.get("abstention_reason") or "hosted_abstention",
                    }
                )
        elif not self.hosted_enabled:
            result["hosted_skipped"] = "disabled"
        elif not self.public_or_sanitized_data_ack:
            result["hosted_skipped"] = "public_or_sanitized_data_ack_required"
        elif self.client_factory is None:
            result["hosted_skipped"] = "client_unavailable"
        else:
            result["hosted_skipped"] = "local_confident"

        if result["selected"] is not None:
            result["status"] = "selected"
        self._store(key, result)
        self._record_receipt(result)
        return result


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


def build_pre_llm_call_hook(
    *,
    enabled: bool = True,
    configured_candidates: Any = None,
    hosted_enabled: bool = True,
    hosted_mode: str = "always",
    public_or_sanitized_data_ack: bool = False,
    client_factory: Callable[[], Any] | None = None,
    local_threshold: float = DEFAULT_LOCAL_THRESHOLD,
    local_margin: float = DEFAULT_LOCAL_MARGIN,
    cache_seconds: float = DEFAULT_CACHE_SECONDS,
) -> Callable[..., dict[str, str] | None] | None:
    """Build a genuine Hermes ``pre_llm_call`` callback, or disable it."""
    if enabled is not True:
        return None
    try:
        recommender = AutomaticSkillRecommender(
            configured_candidates=configured_candidates,
            hosted_enabled=hosted_enabled,
            hosted_mode=hosted_mode,
            public_or_sanitized_data_ack=public_or_sanitized_data_ack,
            client_factory=client_factory,
            local_threshold=local_threshold,
            local_margin=local_margin,
            cache_seconds=cache_seconds,
        )
    except (TypeError, ValueError) as exc:
        logger.warning("automatic skill recommendation disabled by invalid configuration: %s", type(exc).__name__)
        return None

    configured = bool(recommender.configured_candidates)

    def on_pre_llm_call(*, user_message: Any = None, conversation_history: Any = None, **_: Any) -> dict[str, str] | None:
        # Hermes' conversation_history does not include the cached system prompt
        # that advertises skills. Discover the active profile registry directly.
        catalog_candidates = () if configured else discover_available_skill_candidates()
        result = recommender.recommend(
            user_message,
            candidates=catalog_candidates,
            candidates_from_prompt=False,
        )
        setattr(on_pre_llm_call, "last_result", dict(result))
        setattr(on_pre_llm_call, "last_receipt", dict(recommender.last_receipt or {}))
        selected = result.get("selected")
        if not isinstance(selected, str) or not selected:
            return None
        return {"context": _format_recommendation(selected)}

    return on_pre_llm_call
