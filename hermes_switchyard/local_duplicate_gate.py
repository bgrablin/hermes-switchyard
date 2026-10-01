"""Opt-in local exact-duplicate tool-round gate (C2 / #139).

When ``local_duplicate_tool_gate`` is on, successful **read** tool outcomes are
fingerprinted per explicit session/task pair. A later call with the same tool + canonical args +
observation identity reuses the prior result via Hermes ``tool_execution``
middleware (skip ``next_call``) — **0 Jev**, replace-not-add.

Capability-first:
- Flag default **off**; missing trusted evidence always dispatches a fresh call.
- Both session and task, full source identity, current revision, and complete
  result digest are mandatory. Caller arguments never establish freshness.
- Mutations invalidate every scope; complete cached results are never truncated.

Plugin-only: uses existing ``tool_execution`` middleware + ``post_tool_call``.
No Hermes core / hermes-agent changes.
"""

from __future__ import annotations

import hashlib
import inspect
import types
import json
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .reasoning_effort_adapter import derive_tool_failure

# Purity is a closed contract, never an inferred verb prefix or catalog name.
TRUSTED_READ_TOOLS = frozenset({"read_file", "browser_snapshot"})


def _gate_tool_kind(name: Any) -> str:
    return "read" if type(name) is str and name in TRUSTED_READ_TOOLS else "other"


# No tool is fresh merely because its arguments are unchanged.
ARGS_STABLE_READ_TOOLS = frozenset()


@dataclass(frozen=True)
class ReadEvidence:
    """Trusted adapter evidence, recomputed independently of model/tool arguments.

    source_identity includes server/account/workspace and canonical resource.
    revision is an immutable version or a freshly revalidated content digest.
    result_sha256 covers the COMPLETE serialized result, not a prefix/page.
    The adapter must only attest side-effect-free reads and explicit completeness.
    """

    source_identity: str
    revision: str
    result_sha256: str
    complete: bool = False
    read_only: bool = False


def _valid_evidence(evidence: Any) -> bool:
    return (
        isinstance(evidence, ReadEvidence)
        and evidence.complete is True
        and evidence.read_only is True
        and all(
            type(value) is str and value.strip() and len(value) <= 4096
            for value in (evidence.source_identity, evidence.revision)
        )
        and type(evidence.result_sha256) is str
        and len(evidence.result_sha256) == 64
        and all(c in "0123456789abcdef" for c in evidence.result_sha256)
    )


# Combined hard ceiling: 8 sessions * 16 entries * 32 Ki characters = 4 Mi
# characters (at most 16 MiB Unicode payload, plus bounded container overhead).
DEFAULT_MAX_RESULT_CHARS = 32_768
DEFAULT_MAX_KEYS_PER_SESSION = 16
DEFAULT_MAX_SESSIONS = 8

_STORE_LOCK = threading.Lock()
_MUTATION_GENERATION = 0
_ACTIVE_MUTATIONS = 0
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
    # Both dimensions are mandatory; preserve exact identifiers without stripping.
    if not all(
        type(value) is str and value.strip() and len(value) <= 512
        for value in (session_id, task_id)
    ):
        return None
    return json.dumps([session_id, task_id], ensure_ascii=False, separators=(",", ":"))


def canonical_args(args: Any) -> str | None:
    """Canonical JSON arguments, or None when exact identity cannot be proven."""

    def json_value(value: Any) -> bool:
        if value is None or type(value) in (str, bool, int, float):
            return True
        if isinstance(value, Mapping):
            return all(
                type(key) is str and json_value(item) for key, item in value.items()
            )
        if type(value) is list:
            return all(json_value(item) for item in value)
        return False

    if args is None:
        return None
    if not isinstance(args, Mapping):
        return None
    try:
        if not json_value(args):
            return None
        return json.dumps(
            args,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError, RecursionError):
        return None


def _normalize_tool_name(tool_name: Any) -> str:
    if not isinstance(tool_name, str):
        return ""
    return tool_name if tool_name.strip() == tool_name else ""


def observation_identity(
    tool_name: Any,
    args: Any = None,
    *,
    result: Any = None,
    evidence: ReadEvidence | None = None,
) -> str | None:
    """Only a trusted adapter can establish freshness; arguments/results cannot."""
    if canonical_args(args) is None or not _valid_evidence(evidence):
        return None
    return evidence.revision


def fingerprint_for(
    tool_name: Any,
    args: Any = None,
    *,
    observation_id: str | None = None,
    result: Any = None,
    evidence: ReadEvidence | None = None,
) -> str | None:
    """Full tool, args, source identity, revision and complete-result digest."""
    name = _normalize_tool_name(tool_name)
    canonical = canonical_args(args)
    if (
        not name
        or _gate_tool_kind(name) != "read"
        or canonical is None
        or not _valid_evidence(evidence)
    ):
        return None
    payload = json.dumps(
        [
            name,
            canonical,
            evidence.source_identity,
            evidence.revision,
            evidence.result_sha256,
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return "read:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def decide_local_duplicate(
    *,
    enabled: bool,
    tool_name: Any,
    args: Any = None,
    session_id: Any = None,
    task_id: Any = None,
    evidence: ReadEvidence | None = None,
) -> dict[str, Any]:
    """Decide whether to reuse a prior successful read (no side effects beyond lookup).

    Returns a closed-set decision dict:
    ``action`` is ``reuse`` | ``dispatch``; ``reason`` explains fail-open / skip.
    """
    if enabled is not True:
        return {
            "action": "dispatch",
            "reason": "flag_off",
            "fingerprint": None,
            "cached_result": None,
        }

    name = _normalize_tool_name(tool_name)
    kind = _gate_tool_kind(name) if name else "other"
    if kind != "read":
        return {
            "action": "dispatch",
            "reason": "non_read",
            "fingerprint": None,
            "cached_result": None,
        }

    key = fingerprint_for(name, args, evidence=evidence)
    if key is None:
        return {
            "action": "dispatch",
            "reason": "missing_observation_identity",
            "fingerprint": None,
            "cached_result": None,
        }

    scope = _scope_key(session_id=session_id, task_id=task_id)
    if scope is None:
        return {
            "action": "dispatch",
            "reason": "missing_session_identity",
            "fingerprint": key,
            "cached_result": None,
        }
    with _STORE_LOCK:
        if _ACTIVE_MUTATIONS:
            return {
                "action": "dispatch",
                "reason": "mutation_in_progress",
                "fingerprint": key,
                "cached_result": None,
            }
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
    evidence: ReadEvidence | None = None,
    expected_generation: int | None = None,
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
    # Final failure status may arrive without source evidence or even scope.
    # Invalidate before fingerprint validation, including in-flight generations.
    if failed:
        with _STORE_LOCK:
            _invalidate_all_locked()
            _bump("fail_open")
        return {"recorded": False, "reason": "failed_read_invalidated"}
    if scope is None:
        return {"recorded": False, "reason": "missing_session_identity"}

    # Use the same call-time identity for recording and lookup.
    key = fingerprint_for(name, args, evidence=evidence)
    if key is None:
        with _STORE_LOCK:
            _bump("fail_open")
        return {"recorded": False, "reason": "missing_observation_identity"}

    # Reuse must preserve the complete host result and its type. Never truncate.
    max_result_chars = max(1, min(max_result_chars, DEFAULT_MAX_RESULT_CHARS))
    if (
        not isinstance(result, str)
        or len(result) > max_result_chars
        or hashlib.sha256(result.encode("utf-8")).hexdigest() != evidence.result_sha256
    ):
        with _STORE_LOCK:
            session = _SESSION_STORE.get(scope)
            if session is not None and key in session:
                del session[key]
                _bump("invalidated")
            _bump("fail_open")
        return {"recorded": False, "reason": "result_not_cacheable", "fingerprint": key}
    text = result
    max_keys_per_session = max(
        1, min(max_keys_per_session, DEFAULT_MAX_KEYS_PER_SESSION)
    )
    max_sessions = max(1, min(max_sessions, DEFAULT_MAX_SESSIONS))

    with _STORE_LOCK:
        if _ACTIVE_MUTATIONS or (
            expected_generation is not None
            and expected_generation != _MUTATION_GENERATION
        ):
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
    return {
        "recorded": True,
        "reason": "stored",
        "fingerprint": key,
        "chars": len(text),
    }


def _invalidate_all_locked() -> None:
    global _MUTATION_GENERATION
    _MUTATION_GENERATION += 1
    _COUNTERS["cleared_sessions"] += len(_SESSION_STORE)
    _SESSION_STORE.clear()
    _bump("invalidated")


def _is_terminal_middleware(next_call: Any, callback: Any) -> bool:
    """Verify the actual native chain snapshot, not the mutable plugin registry.

    Hermes currently exposes no terminal-position capability. Recognize only
    its live Python chain implementation and dispatch if that seam changes.
    Downstream middleware may rewrite arguments or results and must always run.
    """
    try:
        from hermes_cli.middleware import _run_execution_chain

        call_at_code = next(
            code
            for code in _run_execution_chain.__code__.co_consts
            if isinstance(code, types.CodeType) and code.co_name == "call_at"
        )
        next_code = next(
            code
            for code in call_at_code.co_consts
            if isinstance(code, types.CodeType) and code.co_name == "next_call"
        )
        if (
            not isinstance(next_call, types.FunctionType)
            or next_call.__code__ is not next_code
        ):
            return False
        frame = inspect.getclosurevars(next_call).nonlocals
        call_at = frame["call_at"]
        if (
            not isinstance(call_at, types.FunctionType)
            or call_at.__code__ is not call_at_code
        ):
            return False
        chain = inspect.getclosurevars(call_at).nonlocals
        callbacks, index = chain["callbacks"], frame["index"]
        return (
            type(callbacks) is list
            and type(index) is int
            and index == len(callbacks) - 1
            and callbacks[index] is callback
            and frame["callback"] is callback
            and callable(chain["terminal_call"])
        )
    except Exception:  # noqa: BLE001 -- no proven final position means fresh dispatch
        return False


def build_tool_execution_middleware(*, enabled: bool) -> Callable[..., Any]:
    """Reuse requires an eligible pure tool and trusted host source verification.

    The provider revalidates the complete source on EACH invocation. Stock
    Hermes does not supply one and therefore dispatches. Generation checks
    synchronize hit publication and recording with all local mutations.
    """

    def on_tool_execution(
        *,
        tool_name: str = "",
        args: Any = None,
        next_call: Callable[..., Any] | None = None,
        session_id: Any = None,
        task_id: Any = None,
        reuse_evidence_provider: Any = None,
        **_kwargs: Any,
    ) -> Any:
        global _ACTIVE_MUTATIONS
        if not callable(next_call):
            return None
        if enabled is not True:
            return next_call(args)
        if _gate_tool_kind(tool_name) != "read":
            with _STORE_LOCK:
                _ACTIVE_MUTATIONS += 1
                _invalidate_all_locked()
            try:
                return next_call(args)
            finally:
                with _STORE_LOCK:
                    _ACTIVE_MUTATIONS -= 1
                    _invalidate_all_locked()
        if not _is_terminal_middleware(next_call, on_tool_execution):
            return next_call(args)
        scope = _scope_key(session_id=session_id, task_id=task_id)
        if scope is None or not callable(reuse_evidence_provider):
            return next_call(args)
        with _STORE_LOCK:
            generation = _MUTATION_GENERATION
            blocked = bool(_ACTIVE_MUTATIONS)
        if blocked:
            return next_call(args)
        try:
            evidence = reuse_evidence_provider(tool_name, args)
            decision = decide_local_duplicate(
                enabled=True,
                tool_name=tool_name,
                args=args,
                session_id=session_id,
                task_id=task_id,
                evidence=evidence,
            )
            if (
                decision["action"] == "reuse"
                and reuse_evidence_provider(tool_name, args) == evidence
            ):
                with _STORE_LOCK:
                    # Linearize the hit under the same lock as mutation start.
                    # Never return the prior decision's detached cached value.
                    current = _SESSION_STORE.get(scope, {}).get(decision["fingerprint"])
                    if (
                        not _ACTIVE_MUTATIONS
                        and generation == _MUTATION_GENERATION
                        and current is not None
                    ):
                        _bump("reused")
                        return current
        except Exception:  # noqa: BLE001 -- uncertainty always dispatches
            with _STORE_LOCK:
                _bump("fail_open")
            evidence = None
        result = next_call(args)  # outside the catch: dispatch exactly once
        try:
            if (
                _valid_evidence(evidence)
                and reuse_evidence_provider(tool_name, args) == evidence
            ):
                record_tool_outcome(
                    enabled=True,
                    tool_name=tool_name,
                    args=args,
                    result=result,
                    session_id=session_id,
                    task_id=task_id,
                    evidence=evidence,
                    expected_generation=generation,
                )
        except Exception:  # noqa: BLE001 -- observer must not affect the live result
            with _STORE_LOCK:
                _bump("fail_open")
        return result

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
            if _gate_tool_kind(tool_name) != "read":
                with _STORE_LOCK:
                    _invalidate_all_locked()
                return
            record_tool_outcome(
                enabled=True,
                tool_name=tool_name,
                args=args,
                result=result,
                status=status,
                error_type=error_type,
                error_message=error_message,
                error=_kwargs.get("error"),
                ok=_kwargs.get("ok"),
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
            register_middleware(
                "tool_execution",
                build_tool_execution_middleware(enabled=enabled is True),
            )
            middleware_registered = True
        except Exception:  # noqa: BLE001
            reasons.append("tool_execution_register_failed")
    else:
        reasons.append("hermes_tool_execution_middleware_unavailable")

    if callable(register_hook):
        try:
            register_hook(
                "post_tool_call", build_post_tool_call_hook(enabled=enabled is True)
            )
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
