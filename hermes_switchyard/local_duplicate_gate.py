"""Default-off exact reuse of explicitly versioned, trusted read results.

Only native read_file and browser_snapshot names qualify, and both require an
explicit observation/snapshot identity. Catalog reads, names inferred from verb
prefixes, and unrecognized extensions always dispatch. Runtime recording requires
a matched tool-call identity and an unchanged mutation generation.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, Callable, Mapping

from .reasoning_effort_adapter import derive_tool_failure

# Gate purity is a closed contract, not the effort adapter's name heuristic.
# Catalog tools can preprocess content or change registry state, so are excluded.
TRUSTED_READ_TOOLS = frozenset({"read_file", "browser_snapshot"})
ARGS_STABLE_READ_TOOLS = frozenset()  # No resource is versioned by arguments alone.


def _gate_tool_kind(name: str) -> str:
    return "read" if name in TRUSTED_READ_TOOLS else "other"


# Explicit observation-id field names (first non-empty wins).
_OBS_ARG_KEYS = (
    "observation_id",
    "obs_id",
    "snapshot_id",
    "state_hash",
)

# Combined hard ceiling: 8 sessions * 16 entries * 32 Ki characters = 4 Mi
# characters (at most 16 MiB Unicode payload, plus bounded container overhead).
DEFAULT_MAX_RESULT_CHARS = 32_768
DEFAULT_MAX_KEYS_PER_SESSION = 16
DEFAULT_MAX_SESSIONS = 8

_STORE_LOCK = threading.Lock()
_MUTATION_GENERATION = 0
_ACTIVE_MUTATIONS = 0
_PENDING_READS: OrderedDict[tuple[str, str], tuple[int, str]] = OrderedDict()
_MAX_PENDING_READS = 512
# session_key -> OrderedDict[fingerprint -> cached result text]
_SESSION_STORE: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
_COUNTERS = {
    "recorded": 0,
    "reused": 0,
    "invalidated": 0,
    "cleared_sessions": 0,
    "fail_open": 0,
}


def reset_store_for_tests() -> None:
    """Test helper: drop all session fingerprints and counters."""
    global _MUTATION_GENERATION, _ACTIVE_MUTATIONS
    with _STORE_LOCK:
        _MUTATION_GENERATION = _ACTIVE_MUTATIONS = 0
        _PENDING_READS.clear()
        _SESSION_STORE.clear()
        for key in _COUNTERS:
            _COUNTERS[key] = 0


def gate_counters() -> dict[str, int]:
    """Return a copy of in-process gate counters (debug / tests)."""
    with _STORE_LOCK:
        return dict(_COUNTERS)


def _bump(name: str) -> None:
    _COUNTERS[name] = int(_COUNTERS.get(name, 0)) + 1


def _scope_key(*, session_id: Any = None, task_id: Any = None) -> str | None:
    parts = []
    for label, value in (("session", session_id), ("task", task_id)):
        if value is None or value == "":
            continue
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            return None
        parts.append((label, value))
    return json.dumps(parts, separators=(",", ":")) if parts else None


def canonical_args(args: Any) -> str | None:
    """Canonical JSON arguments, or None when exact identity cannot be proven."""
    def json_value(value: Any) -> bool:
        if value is None or type(value) in (str, bool, int, float):
            return True
        if isinstance(value, Mapping):
            return all(type(key) is str and json_value(item) for key, item in value.items())
        if type(value) is list:
            return all(json_value(item) for item in value)
        return False

    if not isinstance(args, Mapping):
        return None
    try:
        if not json_value(args):
            return None
        return json.dumps(args, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return None


def _normalize_tool_name(tool_name: Any) -> str:
    if not isinstance(tool_name, str):
        return ""
    name = tool_name.strip()
    return name


def observation_identity(
    tool_name: Any,
    args: Any = None,
    *,
    result: Any = None,
) -> str | None:
    """Return observation identity for fingerprinting, or None to fail-open.

    Prefer explicit ids in args (and structured result metadata when recording).
    Resource identifiers alone are insufficient; catalog reads never qualify.
    """
    canonical = canonical_args(args)
    if canonical is None:
        return None
    if isinstance(args, Mapping):
        for key in _OBS_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if value is not None and not isinstance(value, (str, bytes, bytearray, bool)):
                text = str(value).strip()
                if text:
                    return text

    if result is not None:
        parsed: Any = result
        if isinstance(result, str):
            text = result.strip()
            if text and text[0] in "{[":
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError, json.JSONDecodeError):
                    parsed = None
        if isinstance(parsed, Mapping):
            for key in _OBS_ARG_KEYS:
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()

    return None


def fingerprint_for(
    tool_name: Any,
    args: Any = None,
    *,
    observation_id: str | None = None,
    result: Any = None,
) -> str | None:
    """Build ``kind:sha256(tool|args|obs)`` or None when identity is missing."""
    name = _normalize_tool_name(tool_name)
    if not name:
        return None
    kind = _gate_tool_kind(name)
    if kind != "read":
        return None
    canonical = canonical_args(args)
    if canonical is None:
        return None
    obs = observation_id if isinstance(observation_id, str) and observation_id.strip() else None
    if obs is None:
        obs = observation_identity(name, args, result=result)
    if not obs:
        return None
    payload = json.dumps([name, canonical, obs], ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{kind}:{digest}"


def decide_local_duplicate(
    *,
    enabled: bool,
    tool_name: Any,
    args: Any = None,
    session_id: Any = None,
    task_id: Any = None,
) -> dict[str, Any]:
    """Decide whether to reuse a prior successful read (no side effects beyond lookup).

    Returns a closed-set decision dict:
    ``action`` is ``reuse`` | ``dispatch``; ``reason`` explains fail-open / skip.
    """
    if enabled is not True:
        return {"action": "dispatch", "reason": "flag_off", "fingerprint": None, "cached_result": None}

    name = _normalize_tool_name(tool_name)
    kind = _gate_tool_kind(name) if name else "other"
    if kind != "read":
        return {"action": "dispatch", "reason": "non_read", "fingerprint": None, "cached_result": None}

    key = fingerprint_for(name, args)
    if key is None:
        return {"action": "dispatch", "reason": "missing_observation_identity", "fingerprint": None, "cached_result": None}

    scope = _scope_key(session_id=session_id, task_id=task_id)
    if scope is None:
        return {"action": "dispatch", "reason": "missing_session_identity", "fingerprint": key, "cached_result": None}
    with _STORE_LOCK:
        if _ACTIVE_MUTATIONS:
            return {"action": "dispatch", "reason": "mutation_in_progress", "fingerprint": key, "cached_result": None}
        session = _SESSION_STORE.get(scope)
        cached = session.get(key) if session is not None else None
        if cached is None:
            return {
                "action": "dispatch",
                "reason": "no_prior_success",
                "fingerprint": key,
                "cached_result": None,
            }
        session.move_to_end(key)
        _SESSION_STORE.move_to_end(scope)
        return {
            "action": "reuse",
            "reason": "local_duplicate",
            "fingerprint": key,
            "cached_result": cached,
        }


def record_tool_outcome(
    *,
    enabled: bool,
    tool_name: Any,
    args: Any = None,
    result: Any = None,
    status: Any = None,
    error_type: Any = None,
    error_message: Any = None,
    error: Any = None,
    ok: Any = None,
    session_id: Any = None,
    task_id: Any = None,
    expected_generation: int | None = None,
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    max_keys_per_session: int = DEFAULT_MAX_KEYS_PER_SESSION,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> dict[str, Any]:
    """Record or invalidate fingerprints after a tool round.

    - Successful read → store bounded result under fingerprint.
    - Failed read → drop that fingerprint (retry must dispatch).
    - write/exec → clear all session stores (freshness).
    """
    if enabled is not True:
        return {"recorded": False, "reason": "flag_off"}

    name = _normalize_tool_name(tool_name)
    kind = _gate_tool_kind(name) if name else "other"
    scope = _scope_key(session_id=session_id, task_id=task_id)
    failed, _detail = derive_tool_failure(
        status=status,
        error_type=error_type,
        error_message=error_message,
        error=error,
        result=result,
        ok=ok,
    )

    if kind != "read":
        with _STORE_LOCK:
            _invalidate_all_locked()
        return {"recorded": False, "reason": "mutation_cleared_session", "kind": kind}

    if scope is None:
        return {"recorded": False, "reason": "missing_session_identity"}

    # Use the same call-time identity for recording and lookup.
    key = fingerprint_for(name, args)
    if key is None:
        with _STORE_LOCK:
            _bump("fail_open")
        return {"recorded": False, "reason": "missing_observation_identity"}

    if failed:
        with _STORE_LOCK:
            session = _SESSION_STORE.get(scope)
            if session is not None and key in session:
                del session[key]
                _bump("invalidated")
            _bump("fail_open")
        return {"recorded": False, "reason": "failed_read_invalidated", "fingerprint": key}

    # Reuse must preserve the complete host result and its type. Never truncate.
    max_result_chars = max(1, min(max_result_chars, DEFAULT_MAX_RESULT_CHARS))
    if not isinstance(result, str) or len(result) > max_result_chars:
        with _STORE_LOCK:
            session = _SESSION_STORE.get(scope)
            if session is not None and key in session:
                del session[key]
                _bump("invalidated")
            _bump("fail_open")
        return {"recorded": False, "reason": "result_not_cacheable", "fingerprint": key}
    text = result
    max_keys_per_session = max(1, min(max_keys_per_session, DEFAULT_MAX_KEYS_PER_SESSION))
    max_sessions = max(1, min(max_sessions, DEFAULT_MAX_SESSIONS))

    with _STORE_LOCK:
        if _ACTIVE_MUTATIONS or (expected_generation is not None and expected_generation != _MUTATION_GENERATION):
            _bump("fail_open")
            return {"recorded": False, "reason": "mutation_generation_changed"}
        if scope not in _SESSION_STORE:
            _SESSION_STORE[scope] = OrderedDict()
            _SESSION_STORE.move_to_end(scope)
            while len(_SESSION_STORE) > max(1, max_sessions):
                _SESSION_STORE.popitem(last=False)
        session = _SESSION_STORE[scope]
        session[key] = text
        session.move_to_end(key)
        while len(session) > max(1, max_keys_per_session):
            session.popitem(last=False)
        _SESSION_STORE.move_to_end(scope)
        _bump("recorded")
    return {"recorded": True, "reason": "stored", "fingerprint": key, "chars": len(text)}


def _invalidate_all_locked() -> None:
    global _MUTATION_GENERATION
    _MUTATION_GENERATION += 1
    _COUNTERS["cleared_sessions"] += len(_SESSION_STORE)
    _SESSION_STORE.clear()
    _bump("invalidated")


def _pending_key(session_id: Any, task_id: Any, tool_call_id: Any) -> tuple[str, str] | None:
    scope = _scope_key(session_id=session_id, task_id=task_id)
    if scope and isinstance(tool_call_id, str) and tool_call_id.strip() and len(tool_call_id) <= 512:
        return scope, tool_call_id
    return None


def build_tool_execution_middleware(*, enabled: bool) -> Callable[..., Any]:
    """Reuse only explicitly versioned trusted reads; track dispatch generations."""
    def on_tool_execution(*, tool_name: str = "", args: Any = None,
                          next_call: Callable[..., Any] | None = None,
                          session_id: Any = None, task_id: Any = None,
                          tool_call_id: Any = None, **_kwargs: Any) -> Any:
        global _ACTIVE_MUTATIONS
        if not callable(next_call):
            return None
        if enabled is not True:
            return next_call(args)
        if _gate_tool_kind(_normalize_tool_name(tool_name)) != "read":
            with _STORE_LOCK:
                _ACTIVE_MUTATIONS += 1
                _invalidate_all_locked()
            try:
                return next_call(args)
            finally:
                with _STORE_LOCK:
                    _ACTIVE_MUTATIONS -= 1
                    _invalidate_all_locked()
        try:
            decision = decide_local_duplicate(enabled=True, tool_name=tool_name, args=args,
                                              session_id=session_id, task_id=task_id)
            if decision["action"] == "reuse":
                with _STORE_LOCK:
                    # Recheck under the same lock as dispatch-generation capture.
                    current = _SESSION_STORE.get(_scope_key(session_id=session_id, task_id=task_id), {})
                    cached = current.get(decision["fingerprint"])
                    if not _ACTIVE_MUTATIONS and cached is not None:
                        _bump("reused")
                        return cached
            pending = _pending_key(session_id, task_id, tool_call_id)
            fingerprint = fingerprint_for(tool_name, args)
            if pending is not None and fingerprint is not None:
                with _STORE_LOCK:
                    if not _ACTIVE_MUTATIONS:
                        _PENDING_READS[pending] = (_MUTATION_GENERATION, fingerprint)
                        _PENDING_READS.move_to_end(pending)
                        while len(_PENDING_READS) > _MAX_PENDING_READS:
                            _PENDING_READS.popitem(last=False)
        except Exception:  # noqa: BLE001 -- malformed calls always dispatch
            with _STORE_LOCK:
                _bump("fail_open")
            return next_call(args)
        try:
            return next_call(args)
        except BaseException:
            with _STORE_LOCK:
                if pending is not None:
                    _PENDING_READS.pop(pending, None)
            raise
    return on_tool_execution


def build_post_tool_call_hook(*, enabled: bool) -> Callable[..., Any]:
    """Record only reads tied to an unchanged dispatch generation and identity."""
    def on_post_tool_call(tool_name: str = "", args: Any = None, result: Any = None,
                          *, session_id: Any = None, task_id: Any = None,
                          tool_call_id: Any = None, status: Any = None,
                          error_type: Any = None, error_message: Any = None,
                          **kwargs: Any) -> None:
        if enabled is not True:
            return
        try:
            if _gate_tool_kind(_normalize_tool_name(tool_name)) != "read":
                with _STORE_LOCK:
                    _invalidate_all_locked()
                return
            pending_key = _pending_key(session_id, task_id, tool_call_id)
            with _STORE_LOCK:
                pending = _PENDING_READS.pop(pending_key, None)
            expected = pending[0] if pending and pending[1] == fingerprint_for(tool_name, args) else -1
            record_tool_outcome(enabled=True, tool_name=tool_name, args=args, result=result,
                                status=status, error_type=error_type, error_message=error_message,
                                error=kwargs.get("error"), ok=kwargs.get("ok"),
                                session_id=session_id, task_id=task_id, expected_generation=expected)
        except Exception:  # noqa: BLE001 -- recorder must never break the host
            with _STORE_LOCK:
                _bump("fail_open")
    return on_post_tool_call


def register_local_duplicate_gate(ctx: Any, *, enabled: bool) -> dict[str, Any]:
    """Register ``tool_execution`` + ``post_tool_call`` when Hermes seams exist.

    Middleware/hook callbacks are always wired when available so plugin.yaml
    declarations stay honest; behavior is gated by ``enabled`` (default off).
    """
    register_middleware = getattr(ctx, "register_middleware", None)
    register_hook = getattr(ctx, "register_hook", None)

    middleware_registered = False
    post_tool_registered = False
    reasons: list[str] = []

    if callable(register_middleware):
        try:
            register_middleware("tool_execution", build_tool_execution_middleware(enabled=enabled is True))
            middleware_registered = True
        except Exception:  # noqa: BLE001
            reasons.append("tool_execution_register_failed")
    else:
        reasons.append("hermes_tool_execution_middleware_unavailable")

    if callable(register_hook):
        try:
            register_hook("post_tool_call", build_post_tool_call_hook(enabled=enabled is True))
            post_tool_registered = True
        except Exception:  # noqa: BLE001
            reasons.append("post_tool_call_register_failed")
    else:
        reasons.append("hermes_post_tool_call_unavailable")

    status: dict[str, Any] = {
        "enabled": enabled is True,
        "tool_execution_registered": middleware_registered,
        "post_tool_call_registered": post_tool_registered,
        "mode": "tool_execution_reuse",
        "flag": "local_duplicate_tool_gate",
    }
    if reasons:
        status["reason"] = ",".join(reasons)
    elif not enabled:
        status["reason"] = "flag_off"
    else:
        status["reason"] = "active"
    return status
