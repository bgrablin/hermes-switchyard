"""Opt-in local exact-duplicate tool-round gate (C2 / #139).

When ``local_duplicate_tool_gate`` is on, successful **read** tool outcomes are
fingerprinted per session. A later call with the same tool + canonical args +
observation identity reuses the prior result via Hermes ``tool_execution``
middleware (skip ``next_call``) — **0 Jev**, replace-not-add.

Capability-first:
- Flag default **off**.
- Fail-open when unsure (non-read, missing observation identity, errors, writes).
- Mutations and failed reads never skip; writes/exec clear the session store.
- Empty observation identity never silently skips (except closed-set args-stable
  read tools where identity is derived from args explicitly as ``args_stable:…``).

Plugin-only: uses existing ``tool_execution`` middleware + ``post_tool_call``.
No Hermes core / hermes-agent changes.
"""
from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any, Callable, Mapping

from .reasoning_effort_adapter import classify_tool_kind, derive_tool_failure

# Closed-set reads whose resource is fully named by args (mining: skill_view).
# Observation identity is derived as args_stable:<digest>, never empty.
ARGS_STABLE_READ_TOOLS = frozenset(
    {
        # Catalog / schema reads named entirely by args. Live-state integrations
        # (HA entity lists, Kanban boards) are intentionally excluded — they need
        # an explicit observation identity so a later mutation cannot stale-reuse.
        "skill_view",
        "skills_list",
        "tool_search",
        "tool_describe",
    }
)

# Explicit observation-id field names (first non-empty wins).
_OBS_ARG_KEYS = (
    "observation_id",
    "obs_id",
    "document_id",
    "page_id",
    "snapshot_id",
    "state_hash",
)

# Bound stored payloads so a large read cannot pin unbounded RAM.
DEFAULT_MAX_RESULT_CHARS = 262_144
DEFAULT_MAX_KEYS_PER_SESSION = 128
DEFAULT_MAX_SESSIONS = 32

_STORE_LOCK = threading.Lock()
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
    with _STORE_LOCK:
        _SESSION_STORE.clear()
        for key in _COUNTERS:
            _COUNTERS[key] = 0


def gate_counters() -> dict[str, int]:
    """Return a copy of in-process gate counters (debug / tests)."""
    with _STORE_LOCK:
        return dict(_COUNTERS)


def _bump(name: str) -> None:
    _COUNTERS[name] = int(_COUNTERS.get(name, 0)) + 1


def _scope_key(*, session_id: Any = None, task_id: Any = None) -> str:
    for value in (session_id, task_id):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (bytes, bytearray)):
            text = str(value).strip()
            if text:
                return text
    return "default"


def canonical_args(args: Any) -> str:
    """Stable JSON for fingerprinting. Non-mappings become a typed placeholder."""
    if args is None:
        return "{}"
    if isinstance(args, Mapping):
        try:
            return json.dumps(args, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return json.dumps({"_uncanonicalizable": True}, sort_keys=True, separators=(",", ":"))
    try:
        return json.dumps({"_non_mapping": str(args)}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return "{\"_uncanonicalizable\":true}"


def _normalize_tool_name(tool_name: Any) -> str:
    if not isinstance(tool_name, str):
        return ""
    name = tool_name.strip()
    if name.startswith("mcp__"):
        name = name.rsplit("__", 1)[-1]
    return name


def observation_identity(
    tool_name: Any,
    args: Any = None,
    *,
    result: Any = None,
) -> str | None:
    """Return observation identity for fingerprinting, or None to fail-open.

    Prefer explicit ids in args (and structured result metadata when recording).
    Args-stable read tools derive ``args_stable:<16-hex>`` from canonical args so
    empty transcript obs fields never become silent skips for other tools.
    """
    name = _normalize_tool_name(tool_name)
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

    if name in ARGS_STABLE_READ_TOOLS:
        digest = hashlib.sha256(canonical_args(args).encode("utf-8")).hexdigest()[:16]
        return f"args_stable:{digest}"

    return None


def fingerprint_for(
    tool_name: Any,
    args: Any = None,
    *,
    observation_id: str | None = None,
    result: Any = None,
) -> str | None:
    """Build ``kind:sha256(tool|args|obs)[:16]`` or None when identity is missing."""
    name = _normalize_tool_name(tool_name)
    if not name:
        return None
    kind = classify_tool_kind(name)
    if kind != "read":
        return None
    obs = observation_id if isinstance(observation_id, str) and observation_id.strip() else None
    if obs is None:
        obs = observation_identity(name, args, result=result)
    if not obs:
        return None
    payload = f"{name}|{canonical_args(args)}|{obs}"
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def _result_as_text(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(result)


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
    kind = classify_tool_kind(name) if name else "other"
    if kind != "read":
        return {"action": "dispatch", "reason": "non_read", "fingerprint": None, "cached_result": None}

    key = fingerprint_for(name, args)
    if key is None:
        return {"action": "dispatch", "reason": "missing_observation_identity", "fingerprint": None, "cached_result": None}

    scope = _scope_key(session_id=session_id, task_id=task_id)
    with _STORE_LOCK:
        session = _SESSION_STORE.get(scope)
        cached = session.get(key) if session is not None else None
        if cached is None:
            return {
                "action": "dispatch",
                "reason": "no_prior_success",
                "fingerprint": key,
                "cached_result": None,
            }
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
    max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    max_keys_per_session: int = DEFAULT_MAX_KEYS_PER_SESSION,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
) -> dict[str, Any]:
    """Record or invalidate fingerprints after a tool round.

    - Successful read → store bounded result under fingerprint.
    - Failed read → drop that fingerprint (retry must dispatch).
    - write/exec → clear the whole session store (freshness).
    """
    if enabled is not True:
        return {"recorded": False, "reason": "flag_off"}

    name = _normalize_tool_name(tool_name)
    kind = classify_tool_kind(name) if name else "other"
    scope = _scope_key(session_id=session_id, task_id=task_id)

    failed, _detail = derive_tool_failure(
        status=status,
        error_type=error_type,
        error_message=error_message,
        error=error,
        result=result,
        ok=ok,
    )

    if kind in {"write", "exec"}:
        with _STORE_LOCK:
            if scope in _SESSION_STORE:
                del _SESSION_STORE[scope]
                _bump("cleared_sessions")
                _bump("invalidated")
        return {"recorded": False, "reason": "mutation_cleared_session", "kind": kind}

    if kind != "read":
        return {"recorded": False, "reason": "non_read"}

    key = fingerprint_for(name, args, result=result)
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

    text = _result_as_text(result)
    if max_result_chars < 64:
        max_result_chars = 64
    if len(text) > max_result_chars:
        text = text[:max_result_chars]

    with _STORE_LOCK:
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


def build_tool_execution_middleware(*, enabled: bool) -> Callable[..., Any]:
    """Hermes ``tool_execution`` middleware: reuse on exact local_duplicate, else next_call."""

    def on_tool_execution(
        *,
        tool_name: str = "",
        args: Any = None,
        next_call: Callable[..., Any] | None = None,
        session_id: Any = None,
        task_id: Any = None,
        **_kwargs: Any,
    ) -> Any:
        if not callable(next_call):
            return None
        if enabled is not True:
            return next_call(args)

        try:
            decision = decide_local_duplicate(
                enabled=True,
                tool_name=tool_name,
                args=args,
                session_id=session_id,
                task_id=task_id,
            )
        except Exception:  # noqa: BLE001 -- fail-open
            with _STORE_LOCK:
                _bump("fail_open")
            return next_call(args)

        if decision.get("action") == "reuse" and isinstance(decision.get("cached_result"), str):
            with _STORE_LOCK:
                _bump("reused")
            return decision["cached_result"]

        return next_call(args)

    return on_tool_execution


def build_post_tool_call_hook(*, enabled: bool) -> Callable[..., Any]:
    """Observer: record successful reads / invalidate failures / clear on mutation."""

    def on_post_tool_call(
        tool_name: str = "",
        args: Any = None,
        result: Any = None,
        *,
        session_id: Any = None,
        task_id: Any = None,
        status: Any = None,
        error_type: Any = None,
        error_message: Any = None,
        **_kwargs: Any,
    ) -> None:
        if enabled is not True:
            return
        try:
            record_tool_outcome(
                enabled=True,
                tool_name=tool_name,
                args=args,
                result=result,
                status=status,
                error_type=error_type,
                error_message=error_message,
                session_id=session_id,
                task_id=task_id,
            )
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
