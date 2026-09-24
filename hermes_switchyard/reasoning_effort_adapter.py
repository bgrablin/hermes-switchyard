"""Jev adaptive reasoning-effort picker for Hermes llm_request middleware.

Jev chooses a wire-safe effort at or below the user's requested level; an
explicit allow_raise setting permits one higher level after a failed tool.
Only request-scoped effort fields change, preserving the prompt-cache prefix.
Jev failures keep the user's level. Model routing stays advisory.
"""
from __future__ import annotations

import fnmatch
import json
import os
import stat
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .client import request_budget_scope
from .routing import _choice_metrics, _criteria, _decision_metadata

# Hermes hermes_constants.VALID_REASONING_EFFORTS plus "none" (disabled).
HERMES_REASONING_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
)
ALLOWED_EFFORTS = frozenset(HERMES_REASONING_EFFORTS)
DEFAULT_EFFORT = "medium"
DEFAULT_ADAPTIVE_REASONING_EFFORT = True
DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS = 1.5
MAX_TASK_CHARS = 1_200
MAX_TOOL_OUTCOMES = 6
MAX_OUTCOME_CHARS = 160
_DEFAULT_SESSION_KEY = "_default"

_EFFORT_CRITERIA: dict[str, str] = {
    "none": "No extended reasoning; trivial lookup, ack, or formatting.",
    "minimal": "Very light thinking; short factual or mechanical reply.",
    "low": "Routine task with clear steps; little ambiguity.",
    "medium": "Default balanced effort for ordinary multi-step work.",
    "high": "Hard reasoning, debugging, or careful tradeoffs.",
    "xhigh": "Very hard; prior tools failed or deep analysis needed.",
    "max": "Maximum available effort; stuck or safety-critical judgment.",
    "ultra": "Highest Hermes tier when the provider exposes ultra.",
}

_FAILURE_STATUSES = frozenset(
    {"error", "failed", "blocked", "cancelled", "canceled", "timeout", "timed_out"}
)

_LAST_REGISTRATION: dict[str, Any] = {
    "registered": False,
    "mode": "uninitialized",
    "hermes_seam": None,
    "reason": "register_reasoning_effort_adapter_not_called",
    "enabled": False,
}
_LAST_RECEIPT: dict[str, Any] = {
    "effort": DEFAULT_EFFORT,
    "reason_code": "default_no_prior",
    "applied": False,
    "source": "uninitialized",
}


def last_registration() -> dict[str, Any]:
    """Return a copy of the most recent middleware registration receipt."""
    return dict(_LAST_REGISTRATION)


def last_receipt() -> dict[str, Any]:
    """Return a copy of the most recent effort-routing receipt."""
    return dict(_LAST_RECEIPT)


def probe_llm_request_middleware_seam(ctx: Any) -> dict[str, Any]:
    """Probe PluginContext for Hermes llm_request middleware registration."""
    method = getattr(ctx, "register_middleware", None)
    if callable(method):
        return {
            "available": True,
            "kind": "ctx_method",
            "name": "register_middleware",
            "middleware_kind": "llm_request",
            "can_apply": True,
        }
    return {
        "available": False,
        "kind": None,
        "name": None,
        "middleware_kind": None,
        "can_apply": False,
        "reason": "hermes_llm_request_middleware_unavailable",
    }


def normalize_effort(value: Any, *, default: str = DEFAULT_EFFORT) -> str:
    """Return a Hermes-allowed effort level, or *default* when unrecognized."""
    if value is True:
        return default if default in ALLOWED_EFFORTS else DEFAULT_EFFORT
    if value is False:
        return "none"
    if value is None:
        return default if default in ALLOWED_EFFORTS else DEFAULT_EFFORT
    text = str(value).strip().lower()
    if text in {"false", "disabled", "off"}:
        return "none"
    if text in ALLOWED_EFFORTS:
        return text
    return default if default in ALLOWED_EFFORTS else DEFAULT_EFFORT


def _truncate(text: Any, limit: int) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _session_key(*, session_id: Any = None, task_id: Any = None) -> str:
    for candidate in (session_id, task_id):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        if candidate is not None and not isinstance(candidate, (bool, bytes)):
            text = str(candidate).strip()
            if text:
                return text
    return _DEFAULT_SESSION_KEY


def _content_text(content: Any) -> str:
    if isinstance(content, str) and content.strip():
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, Mapping):
                for key in ("text", "input_text", "content"):
                    value = block.get(key)
                    if isinstance(value, str) and value.strip():
                        parts.append(value)
                        break
                else:
                    nested = block.get("content")
                    if nested is not None and nested is not content:
                        nested_text = _content_text(nested)
                        if nested_text:
                            parts.append(nested_text)
            elif isinstance(block, str) and block.strip():
                parts.append(block)
        return " ".join(parts).strip()
    return ""


def _messages_task_snippet(messages: Sequence[Any]) -> str:
    for message in reversed(list(messages)):
        if not isinstance(message, Mapping):
            continue
        role = str(message.get("role") or "").strip().lower()
        msg_type = str(message.get("type") or "").strip().lower()
        # Chat Completions: role=user. Responses/Codex: type=message|input_text
        # with role=user (or omitted on bare input_text items).
        if role and role != "user":
            continue
        if not role and msg_type and msg_type not in {"message", "input_text"}:
            continue
        text = _content_text(message.get("content"))
        if not text and isinstance(message.get("text"), str):
            text = message["text"].strip()
        if text:
            return _truncate(text, MAX_TASK_CHARS)
    return ""


def _extract_task_snippet(request: Mapping[str, Any] | None, explicit: Any = None) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return _truncate(explicit, MAX_TASK_CHARS)
    if not isinstance(request, Mapping):
        return ""
    messages = request.get("messages")
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes, bytearray)):
        snippet = _messages_task_snippet(messages)
        if snippet:
            return snippet
    # Hermes Responses/Codex llm_request uses `input` (list or direct string).
    raw_input = request.get("input")
    if isinstance(raw_input, str) and raw_input.strip():
        return _truncate(raw_input, MAX_TASK_CHARS)
    if isinstance(raw_input, Sequence) and not isinstance(raw_input, (str, bytes, bytearray)):
        return _messages_task_snippet(raw_input)
    return ""


def summarize_tool_outcome(
    *,
    tool_name: Any = None,
    ok: Any = None,
    error: Any = None,
    result_preview: Any = None,
) -> dict[str, str]:
    """Build a bounded local tool-outcome record for the next Jev choice."""
    name = _truncate(tool_name or "tool", 64) or "tool"
    if error:
        status = "error"
        detail = _truncate(error, MAX_OUTCOME_CHARS)
    elif ok is False:
        status = "failed"
        detail = _truncate(result_preview or "tool reported failure", MAX_OUTCOME_CHARS)
    else:
        status = "ok"
        detail = _truncate(result_preview or "ok", MAX_OUTCOME_CHARS)
    return {"tool": name, "status": status, "detail": detail}


def _parse_structured_result(result: Any) -> Any:
    if isinstance(result, Mapping):
        return result
    if isinstance(result, str):
        text = result.strip()
        if not text:
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return parsed
    return None


def derive_tool_failure(
    *,
    status: Any = None,
    error_type: Any = None,
    error_message: Any = None,
    error: Any = None,
    result: Any = None,
    ok: Any = None,
) -> tuple[bool, str]:
    """Derive stuck/failure from Hermes post_tool_call fields + structured results."""
    status_text = str(status or "").strip().lower()
    if status_text in _FAILURE_STATUSES:
        detail = error_message or error_type or status_text
        return True, _truncate(detail, MAX_OUTCOME_CHARS)

    if error_message or error_type:
        return True, _truncate(error_message or error_type, MAX_OUTCOME_CHARS)

    if error:
        return True, _truncate(error, MAX_OUTCOME_CHARS)

    if ok is False:
        return True, _truncate(
            error_message or result or "tool reported failure", MAX_OUTCOME_CHARS
        )

    parsed = _parse_structured_result(result)
    if isinstance(parsed, Mapping):
        if parsed.get("error") or parsed.get("ok") is False:
            detail = (
                parsed.get("error")
                or parsed.get("error_message")
                or parsed.get("status")
                or "tool reported failure"
            )
            return True, _truncate(detail, MAX_OUTCOME_CHARS)
        preview = parsed.get("status") or parsed.get("detail") or "ok"
        return False, _truncate(preview, MAX_OUTCOME_CHARS)

    if result is not None:
        return False, _truncate(result, MAX_OUTCOME_CHARS)
    return False, "ok"


def clamp_effort_for_provider(
    effort: Any,
    *,
    provider: Any = None,
    model: Any = None,
    api_mode: Any = None,
) -> str:
    """Map an internal Hermes effort onto a provider-safe wire value.

    Mirrors Hermes transport clamps so middleware never reinserts internal-only
    levels (especially ``ultra``) after the host has already shaped kwargs.

    Codex / Responses / Astra reject ``none`` (and often ``minimal``) on the
    wire — map those to ``low`` so adaptive effort never writes HTTP 400.
    """
    level = normalize_effort(effort)

    provider_s = str(provider or "").strip().lower()
    model_s = str(model or "").strip().lower()
    api_mode_s = str(api_mode or "").strip().lower()

    is_codex = api_mode_s in {"codex_responses", "responses"} or "codex" in provider_s
    # Astra models: same wire set as Codex (low..max), even when
    # api_mode/provider labels omit "codex".
    is_astra = "astra" in model_s or "astra" in provider_s
    is_codex_family = is_codex or is_astra
    is_xai = (
        provider_s in {"xai", "x-ai"}
        or "xai" in provider_s
        or model_s.startswith("grok")
        or "/grok" in model_s
        or model_s.startswith("x-ai/")
    )
    is_anthropic = (
        api_mode_s in {"anthropic_messages", "anthropic"}
        or provider_s in {"anthropic", "claude"}
        or "anthropic" in provider_s
    )
    is_lmstudio = provider_s in {"lmstudio", "lm-studio", "lm_studio"} or "lmstudio" in provider_s

    if level == "none":
        # Anthropic Messages uses output_config.effort for adaptive thinking;
        # unlike a host-side disabled reasoning_config, that wire field cannot
        # carry "none". Do not change OpenRouter Chat Completions merely because
        # its model id happens to include "claude".
        is_anthropic_wire = api_mode_s in {"anthropic_messages", "anthropic"} or "anthropic" in provider_s or provider_s == "claude"
        if is_codex_family or is_anthropic_wire:
            level = "low"
        else:
            return "none"

    if is_codex_family:
        if is_xai and level in {"xhigh", "max", "ultra"}:
            return "high"
        # Ask the installed host's model capability policy; unknown models use
        # its conservative legacy set, never a plugin-maintained model list.
        try:
            from agent.reasoning_effort import codex_supported_efforts, clamp_effort

            supported = codex_supported_efforts(model_s or None)
            return clamp_effort(level, supported)
        except (ImportError, AttributeError, TypeError):
            if level in {"none", "minimal"}:
                return "low"
            if level == "ultra":
                return "xhigh"
            return level

    if is_anthropic:
        if level == "minimal":
            level = "low"
        if level == "ultra":
            level = "max"
        # Use the provider-wide adaptive subset instead of a model-version list.
        # Some native Anthropic models reject xhigh; max is accepted by both
        # those models and newer adaptive models without guessing their IDs.
        if level == "xhigh":
            level = "max"
        return level

    if is_lmstudio:
        if level in {"max", "ultra"}:
            return "xhigh"
        return level

    # Chat Completions / OpenAI-compat / OpenRouter: ultra is internal-only.
    if level == "ultra":
        return "max"
    return level


def wire_efforts_for_provider(
    *,
    provider: Any = None,
    model: Any = None,
    api_mode: Any = None,
    allowed_efforts: Sequence[str] | None = None,
) -> tuple[str, ...]:
    """Return de-duplicated wire-safe efforts for Jev / request writes."""
    source = [
        level
        for level in (allowed_efforts or HERMES_REASONING_EFFORTS)
        if level in ALLOWED_EFFORTS
    ] or list(HERMES_REASONING_EFFORTS)
    out: list[str] = []
    seen: set[str] = set()
    for level in source:
        wire = clamp_effort_for_provider(
            level, provider=provider, model=model, api_mode=api_mode
        )
        if wire not in seen:
            seen.add(wire)
            out.append(wire)
    return tuple(out) or (DEFAULT_EFFORT,)


def _set_effort_mapping(mapping: dict[str, Any], level: str) -> dict[str, Any]:
    out = dict(mapping)
    if level == "none":
        out["enabled"] = False
        out.pop("effort", None)
    else:
        out["enabled"] = True
        out["effort"] = level
    return out


def _set_codex_wire_effort(mapping: Mapping[str, Any], level: str) -> dict[str, Any]:
    """Codex Responses accepts reasoning.effort, not reasoning.enabled."""
    out = dict(mapping)
    out.pop("enabled", None)
    out["effort"] = level
    if level == "none":
        out.pop("summary", None)
    return out


def apply_effort_to_request(
    request: Mapping[str, Any],
    effort: str,
    *,
    provider: Any = None,
    model: Any = None,
    api_mode: Any = None,
) -> dict[str, Any]:
    """Rewrite provider kwargs effort fields only — never touch messages/input.

    Prompt-cache friendliness: messages / input / tools / system stay
    byte-identical; only provider-facing effort twins change. Uses Hermes'
    provider/model/api-mode clamps so internal-only levels never hit the wire.
    """
    out = dict(request)
    api_mode_s = str(api_mode or "").strip().lower()
    provider_s = str(provider or "").strip().lower()
    if api_mode_s == "bedrock_converse":
        return out

    level = clamp_effort_for_provider(
        effort, provider=provider, model=model, api_mode=api_mode
    )
    is_codex_wire = api_mode_s in {"codex_responses", "responses"} or "codex" in provider_s

    is_anthropic_wire = (
        api_mode_s in {"anthropic_messages", "anthropic"}
        or provider_s in {"anthropic", "claude"}
        or "anthropic" in provider_s
    )
    if is_anthropic_wire:
        out.pop("reasoning_effort", None)
        out.pop("reasoning_config", None)
        out.pop("reasoning", None)
        extra_body = out.get("extra_body")
        if isinstance(extra_body, Mapping):
            extra_out = dict(extra_body)
            extra_out.pop("reasoning_effort", None)
            extra_out.pop("reasoning_config", None)
            extra_out.pop("reasoning", None)
            if extra_out:
                out["extra_body"] = extra_out
            else:
                out.pop("extra_body", None)
        output_config = out.get("output_config")
        thinking = out.get("thinking")
        if (
            isinstance(output_config, Mapping)
            and "effort" in output_config
            and isinstance(thinking, Mapping)
            and thinking.get("type") == "adaptive"
        ):
            out["output_config"] = {**output_config, "effort": level}
        return out

    touched = False

    if is_codex_wire:
        out.pop("reasoning_effort", None)
        out.pop("reasoning_config", None)
        extra_body = out.get("extra_body")
        if isinstance(extra_body, Mapping):
            extra_out = dict(extra_body)
            extra_out.pop("reasoning_effort", None)
            extra_out.pop("reasoning_config", None)
            extra_out.pop("reasoning", None)
            if extra_out:
                out["extra_body"] = extra_out
            else:
                out.pop("extra_body", None)

    if "reasoning_effort" in out:
        out["reasoning_effort"] = level
        touched = True

    nested = out.get("reasoning")
    if isinstance(nested, Mapping):
        out["reasoning"] = (
            _set_codex_wire_effort(nested, level)
            if is_codex_wire
            else _set_effort_mapping(dict(nested), level)
        )
        touched = True

    reasoning_config = out.get("reasoning_config")
    if isinstance(reasoning_config, Mapping):
        out["reasoning_config"] = _set_effort_mapping(dict(reasoning_config), level)
        touched = True

    extra = out.get("extra_body")
    if isinstance(extra, Mapping):
        extra_out = dict(extra)
        extra_touched = False
        reasoning = extra_out.get("reasoning")
        if isinstance(reasoning, Mapping):
            extra_out["reasoning"] = (
                _set_codex_wire_effort(reasoning, level)
                if is_codex_wire
                else _set_effort_mapping(dict(reasoning), level)
            )
            extra_touched = True
        if "reasoning_effort" in extra_out:
            extra_out["reasoning_effort"] = level
            extra_touched = True
        if extra_touched:
            out["extra_body"] = extra_out
            touched = True

    if is_codex_wire:
        return out

    if not touched:
        return out

    return out


EFFORT_MODES = ("auto", "pinned")
DEFAULT_EFFORT_MODE = "auto"
EFFORT_HISTORY_FILE_NAME = "effort-history.jsonl"
EFFORT_HISTORY_LOCK_NAME = "effort-history.lock"
EFFORT_HISTORY_SCHEMA = 1
EFFORT_HISTORY_MAX_RECORDS = 2_000
EFFORT_HISTORY_MAX_BYTES = 1024 * 1024
_JEV_FAILURE_REASONS = frozenset(
    {"kept_requested_on_jev_failure", "kept_requested_ack_required", "invalid_choice"}
)
_FAILED_OUTCOME_STATUSES = frozenset({"error", "failed"})
_TRUE_STRINGS = frozenset({"1", "true", "yes", "on"})


def normalize_mode(value: Any) -> str:
    """Return ``auto`` or ``pinned``; ``pin`` is accepted as an alias."""
    text = str(value or "").strip().lower()
    if text in {"pin", "pinned"}:
        return "pinned"
    return "auto"


def parse_bool_setting(value: Any) -> bool:
    """Accept true, 1, "1", "true", "yes" and "on" as true; everything else is false."""
    if value is True:
        return True
    if value is False or value is None:
        return False
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in _TRUE_STRINGS


def parse_exclude_models(value: Any) -> tuple[str, ...]:
    """Return lowercase fnmatch patterns from a list or a comma-separated string."""
    if value is None:
        return ()
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return ()
    patterns: list[str] = []
    for item in items:
        text = str(item or "").strip().lower()
        if text and len(text) <= 200 and text not in patterns:
            patterns.append(text)
    return tuple(patterns[:64])


def model_is_excluded(model: Any, patterns: Sequence[str]) -> bool:
    name = str(model or "").strip().lower()
    return bool(name) and any(fnmatch.fnmatchcase(name, pattern) for pattern in patterns)


def _session_env(name: str) -> str:
    """Read Hermes' session context variable, falling back to the process environment."""
    try:
        from gateway.session_context import get_session_env

        return str(get_session_env(name, "") or "")
    except Exception:  # noqa: BLE001 -- older hosts or no gateway package
        return str(os.environ.get(name, "") or "")


def _latest_failed(outcomes: Sequence[Mapping[str, Any]]) -> bool:
    return bool(outcomes) and str(outcomes[-1].get("status")) in _FAILED_OUTCOME_STATUSES


def choose_reasoning_effort(
    *,
    task: str,
    recent_tool_outcomes: Sequence[Mapping[str, Any]] | None,
    client: Any,
    requested_effort: str | None = None,
    prior_effort: str | None = None,
    public_or_sanitized_data_ack: bool = True,
    deadline_seconds: float = DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS,
    allowed_efforts: Sequence[str] | None = None,
    turn_phase: str = "new_turn",
) -> dict[str, Any]:
    """Ask Jev for one effort among *allowed_efforts*; fail closed to the requested level.

    ``allowed_efforts`` is the candidate list. The controller normally caps it at
    the user's level; explicit allow_raise can extend it one wire level while
    the latest tool failed. ``prior_effort`` is a deprecated alias.
    """
    requested = normalize_effort(requested_effort if requested_effort is not None else prior_effort)
    levels = [
        level
        for level in (allowed_efforts or HERMES_REASONING_EFFORTS)
        if level in ALLOWED_EFFORTS
    ]
    if not levels:
        levels = list(HERMES_REASONING_EFFORTS)
    raised_ceiling = any(
        HERMES_REASONING_EFFORTS.index(level) > HERMES_REASONING_EFFORTS.index(requested)
        for level in levels
    )

    base = {
        "applied": False,
        "requested_effort": requested,
        "confidence": None,
        "probabilities": None,
    }
    if public_or_sanitized_data_ack is not True:
        return {
            **base,
            "status": "kept_requested",
            "effort": requested,
            "reason_code": "kept_requested_ack_required",
        }

    outcomes: list[dict[str, str]] = []
    for item in list(recent_tool_outcomes or [])[-MAX_TOOL_OUTCOMES:]:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "unknown").strip().lower()
        outcomes.append({"status": status if status in {"ok", "error", "failed"} else "unknown"})

    stuck = _latest_failed(outcomes)
    candidates = [
        {"id": level, "description": _EFFORT_CRITERIA.get(level, level)} for level in levels
    ]
    criteria = _criteria(candidates, "id")
    state = {
        "task_present": bool(task),
        "task_length_bucket": (
            "short" if len(task or "") < 80 else "medium" if len(task or "") < 400 else "long"
        ),
        "requested_effort": requested,
        "turn_phase": "after_tool" if turn_phase == "after_tool" else "new_turn",
        "recent_tool_outcomes": outcomes,
        "stuck_signal": stuck,
        "policy": {
            "ceiling_is_user_level": not raised_ceiling,
            "lower_for_routine": True,
            "fail_closed_keep_user_level": True,
            "prompt_cache_friendly": True,
        },
    }
    questions = {
        "reasoning_effort": {
            "type": "choice",
            "instructions": (
                "Pick the Hermes reasoning_effort for the next model generation. The "
                + (
                    "candidates may include one wire level above the user selection after a failed tool call. "
                    if raised_ceiling else
                    "candidates stop at the level the user selected. "
                )
                + "Choose a lower level only "
                "when the next step is clearly routine. Keep the highest candidate when the "
                "task is hard, when the latest tool call failed, or when you are not sure."
            ),
            "criteria": criteria,
        }
    }

    started = time.perf_counter()
    try:
        with request_budget_scope(client, 1, deadline_seconds=float(deadline_seconds)):
            result = client.decide(
                state,
                questions,
                public_or_sanitized_data_ack=True,
            )
        metadata = _decision_metadata(result)
        answers = result.get("answers") if isinstance(result, Mapping) else None
        if not isinstance(answers, Mapping):
            raise TypeError("Jev response has no answers object")
        try:
            selected, confidence, probabilities = _choice_metrics(
                answers.get("reasoning_effort"), criteria, "reasoning_effort"
            )
        except (TypeError, ValueError):
            return {
                **base,
                "status": "kept_requested",
                "effort": requested,
                "reason_code": "invalid_choice",
                "jev_latency_ms": round((time.perf_counter() - started) * 1000, 1),
            }
        latency = round((time.perf_counter() - started) * 1000, 1)
        effort = normalize_effort(selected, default=requested)
        if effort not in criteria:
            return {
                **base,
                **metadata,
                "status": "kept_requested",
                "effort": requested,
                "reason_code": "invalid_choice",
                "confidence": confidence,
                "probabilities": probabilities,
                "jev_latency_ms": latency,
            }
        return {
            **base,
            **metadata,
            "status": "selected",
            "effort": effort,
            "reason_code": "jev_selected",
            "confidence": confidence,
            "probabilities": probabilities,
            "stuck_signal": stuck,
            "jev_latency_ms": latency,
        }
    except Exception as exc:  # noqa: BLE001 -- fail closed to the requested level
        return {
            **base,
            "status": "kept_requested",
            "effort": requested,
            "reason_code": "kept_requested_on_jev_failure",
            "error_type": type(exc).__name__,
            "jev_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        }


class _SessionEffortState:
    """Per-session effort state. ``baseline`` is the user's level; the plugin never ratchets it."""

    __slots__ = (
        "lock",
        "mode",
        "baseline",
        "model",
        "outcomes",
        "stuck",
        "dirty",
        "last_turn_id",
        "last_choice",
        "choice_effort",
        "choice_cap",
        "jev_calls",
        "requests",
    )

    def __init__(self, mode: str) -> None:
        self.lock = threading.RLock()
        self.mode = normalize_mode(mode)
        self.baseline: str | None = None
        self.model: str | None = None
        self.outcomes: list[dict[str, str]] = []
        self.stuck = False
        self.dirty = True
        self.last_turn_id: str | None = None
        self.choice_effort: str | None = None
        self.choice_cap: str | None = None
        self.jev_calls = 0
        self.requests = 0
        self.last_choice: dict[str, Any] = {
            "effort": None,
            "reason_code": "no_request_yet",
            "applied": False,
            "source": "controller_init",
        }


def _explicit_effort(request: Mapping[str, Any]) -> str | None:
    """Read an explicit host effort from supported request containers."""
    value = request.get("reasoning_effort")
    if value is not None and not isinstance(value, Mapping):
        return normalize_effort(value)
    for key in ("reasoning", "reasoning_config", "output_config"):
        config = request.get(key)
        if isinstance(config, Mapping):
            effort = config.get("effort")
            if effort is not None:
                return normalize_effort(effort)
            if config.get("enabled") is False:
                return "none"
    extra = request.get("extra_body")
    if isinstance(extra, Mapping):
        value = extra.get("reasoning_effort")
        if value is not None and not isinstance(value, Mapping):
            return normalize_effort(value)
        reasoning = extra.get("reasoning")
        if isinstance(reasoning, Mapping):
            if reasoning.get("effort") is not None:
                return normalize_effort(reasoning["effort"])
            if reasoning.get("enabled") is False:
                return "none"
    return None


def _turn_key(turn_id: Any) -> str | None:
    if turn_id is None:
        return None
    text = str(turn_id).strip()
    return text or None


class ReasoningEffortController:
    """Per-session adaptive effort with the user's selected level as the cap.

    Auto mode may lower effort for routine steps; it never sends more than the
    level in the request unless ``allow_raise`` is set, and then at most one
    wire level while the latest tool call failed. A manual level change on the
    same model pins the session until ``/switchyard effort auto``.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        default_effort: str = DEFAULT_EFFORT,
        client_factory: Callable[[], Any] | None = None,
        public_or_sanitized_data_ack: bool = True,
        deadline_seconds: float = DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS,
        allowed_efforts: Sequence[str] | None = None,
        mode: str = DEFAULT_EFFORT_MODE,
        exclude_models: Any = None,
        allow_raise: Any = False,
        record_decision: Callable[[dict[str, Any]], Any] | None = None,
        session_env: Callable[[str], str] | None = None,
    ) -> None:
        self.enabled = enabled is True
        # Deprecated: the fallback is always the request's own level.
        self.default_effort = normalize_effort(default_effort)
        self.client_factory = client_factory
        self.public_or_sanitized_data_ack = public_or_sanitized_data_ack is True
        self.deadline_seconds = float(deadline_seconds)
        self.allowed_efforts = tuple(
            level
            for level in (allowed_efforts or HERMES_REASONING_EFFORTS)
            if level in ALLOWED_EFFORTS
        ) or tuple(HERMES_REASONING_EFFORTS)
        self.mode = normalize_mode(mode)
        self.exclude_models = parse_exclude_models(exclude_models)
        self.allow_raise = parse_bool_setting(allow_raise)
        self.record_decision = record_decision
        self.session_env = session_env or _session_env
        self._registry_lock = threading.RLock()
        self._sessions: dict[str, _SessionEffortState] = {}
        self._key_to_session: dict[str, str] = {}
        self._pending_modes: dict[str, str] = {}

    # -- state -----------------------------------------------------------------

    def _state_for(self, *, session_id: Any = None, task_id: Any = None) -> _SessionEffortState:
        key = _session_key(session_id=session_id, task_id=task_id)
        with self._registry_lock:
            state = self._sessions.get(key)
            if state is None:
                state = _SessionEffortState(self.mode)
                self._sessions[key] = state
            return state

    def _bind_session(self, key: str) -> _SessionEffortState:
        """Return the state for *key*, map the session key, and apply a pending mode."""
        session_key = self.session_env("HERMES_SESSION_KEY")
        with self._registry_lock:
            state = self._state_for(session_id=key)
            if session_key:
                self._key_to_session[session_key] = key
            pending = self._pending_modes.pop(key, None)
            if session_key:
                pending = self._pending_modes.pop(session_key, None) or pending
            if pending:
                with state.lock:
                    state.mode = pending
            return state

    def record_tool_outcome(
        self,
        outcome: Mapping[str, Any],
        *,
        session_id: Any = None,
        task_id: Any = None,
    ) -> None:
        state = self._state_for(session_id=session_id, task_id=task_id)
        with state.lock:
            summary = summarize_tool_outcome(
                tool_name=outcome.get("tool") or outcome.get("tool_name"),
                ok=outcome.get("ok"),
                error=outcome.get("error"),
                result_preview=outcome.get("detail") or outcome.get("result_preview"),
            )
            state.outcomes.append(summary)
            state.outcomes = state.outcomes[-MAX_TOOL_OUTCOMES:]
            stuck = summary["status"] in _FAILED_OUTCOME_STATUSES
            if stuck != state.stuck:
                state.stuck = stuck
                state.dirty = True

    def _publish(self, receipt: Mapping[str, Any], state: _SessionEffortState) -> dict[str, Any]:
        global _LAST_RECEIPT
        payload = dict(receipt)
        _LAST_RECEIPT = dict(payload)
        state.last_choice = dict(payload)
        writer = self.record_decision
        if writer is not None:
            try:
                writer(dict(payload))
            except Exception:  # noqa: BLE001 -- decision records never break a request
                pass
        return payload

    # -- modes -----------------------------------------------------------------

    def _command_session(self) -> tuple[str | None, list[str]]:
        """Return (existing session key or None, candidate keys for a pending mode)."""
        session_id = self.session_env("HERMES_SESSION_ID").strip()
        session_key = self.session_env("HERMES_SESSION_KEY").strip()
        with self._registry_lock:
            if session_id and session_id in self._sessions:
                return session_id, [session_id]
            mapped = self._key_to_session.get(session_key) if session_key else None
            if mapped and mapped in self._sessions:
                return mapped, [mapped]
            if not session_id and not session_key:
                live = [key for key in self._sessions if key != _DEFAULT_SESSION_KEY]
                if len(live) == 1:
                    return live[0], [live[0]]
            return None, [key for key in (session_id, session_key) if key]

    def set_mode(self, mode: str, *, session_id: str | None = None) -> dict[str, Any]:
        """Set ``auto`` or ``pinned`` for one session (the current one by default)."""
        wanted = normalize_mode(mode)
        if session_id is not None:
            existing, pending_keys = (session_id if session_id in self._sessions else None), [session_id]
        else:
            existing, pending_keys = self._command_session()
        if existing is None:
            if not pending_keys:
                return {"ok": False, "reason": "session_unknown", "mode": wanted}
            with self._registry_lock:
                for key in pending_keys:
                    self._pending_modes[key] = wanted
            return {"ok": True, "pending": True, "mode": wanted, "session_id": pending_keys[0]}
        state = self._sessions[existing]
        with state.lock:
            state.mode = wanted
            if wanted == "auto":
                # Re-baseline at the next request so the current level becomes the cap.
                state.baseline = None
                state.choice_effort = None
                state.choice_cap = None
            state.dirty = True
        return {"ok": True, "pending": False, "mode": wanted, "session_id": existing}

    def session_status(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is None:
            session_id, _ = self._command_session()
        base = {
            "enabled": self.enabled,
            "default_mode": self.mode,
            "exclude_models": list(self.exclude_models),
            "allow_raise": self.allow_raise,
            "deadline_seconds": self.deadline_seconds,
        }
        state = self._sessions.get(session_id) if session_id else None
        if state is None:
            return {**base, "session_id": session_id, "known": False}
        with state.lock:
            last = state.last_choice
            return {
                **base,
                "session_id": session_id,
                "known": True,
                "mode": state.mode,
                "user_level": state.baseline,
                "model": state.model,
                "last_sent": last.get("effort"),
                "last_reason": last.get("reason_code"),
                "jev_calls": state.jev_calls,
                "requests": state.requests,
            }

    def handle_command(self, raw_args: str = "") -> str:
        """``/switchyard effort auto|pin|status`` handler; never raises."""
        auto_limit = (
            "may go one level higher after a failed tool call"
            if self.allow_raise else "never above your level"
        )
        usage = (
            "Usage: /switchyard effort status | /switchyard effort pin | /switchyard effort auto\n"
            "  status  show the adaptive reasoning mode for this session\n"
            "  pin     send your selected /reasoning level unchanged\n"
            "  auto    let Switchyard lower effort for routine steps (" + auto_limit + ")"
        )
        try:
            parts = str(raw_args or "").strip().lower().split()
            if not parts or parts[0] != "effort" or len(parts) > 2:
                return usage
            action = parts[1] if len(parts) > 1 else "status"
            if action not in {"status", "pin", "auto"}:
                return usage
            if action == "status":
                return self._format_status(self.session_status())
            if action in {"pin", "auto"}:
                if not self.enabled:
                    return "Adaptive reasoning effort is disabled in the plugin settings."
                result = self.set_mode(action)
                if not result.get("ok"):
                    return "No Hermes session found for this command. Send a message first, then retry."
                label = "auto" if result["mode"] == "auto" else "pinned"
                note = (
                    " It applies from your first message."
                    if result.get("pending")
                    else ""
                )
                if label == "auto":
                    ceiling = (
                        "may send one level above it after a failed tool call because "
                        "adaptive_reasoning_effort_allow_raise is on."
                        if self.allow_raise else
                        "never sends more than your /reasoning level."
                    )
                    return (
                        "Adaptive reasoning effort: auto. Switchyard may lower effort for routine "
                        "steps and " + ceiling + note
                    )
                return "Adaptive reasoning effort: pinned. Your /reasoning level is sent unchanged." + note
            return usage
        except Exception as exc:  # noqa: BLE001 -- a command must not crash the host
            return f"Switchyard effort command failed: {type(exc).__name__}"

    @staticmethod
    def _format_status(status: Mapping[str, Any]) -> str:
        lines = ["Switchyard adaptive reasoning effort"]
        if not status.get("enabled"):
            lines.append("  enabled: no (adaptive_reasoning_effort is false)")
            return "\n".join(lines)
        if status.get("known"):
            lines.append(f"  mode: {status.get('mode')}")
            lines.append(f"  your level (cap): {status.get('user_level')}")
            lines.append(f"  last sent: {status.get('last_sent')} ({status.get('last_reason')})")
            lines.append(f"  model: {status.get('model')}")
            lines.append(f"  requests: {status.get('requests')}, Jev calls: {status.get('jev_calls')}")
        else:
            lines.append(f"  mode for new sessions: {status.get('default_mode')}")
            lines.append("  this session has not made a model request yet")
        excluded = ", ".join(status.get("exclude_models") or []) or "none"
        lines.append(f"  excluded models: {excluded}")
        lines.append(f"  allow raise: {'yes' if status.get('allow_raise') else 'no'}")
        lines.append(f"  deadline: {status.get('deadline_seconds')} seconds")
        return "\n".join(lines)

    # -- middleware ------------------------------------------------------------

    def _unchanged(
        self,
        state: _SessionEffortState,
        *,
        reason: str,
        requested: str | None,
        base: Mapping[str, Any],
        extra: Mapping[str, Any] | None = None,
    ) -> None:
        receipt = {
            **base,
            "status": "unchanged",
            "effort": requested,
            "requested_effort": requested,
            "reason_code": reason,
            "applied": False,
            "source": "host_request_preserved" if requested is not None else "host_request_unchanged",
        }
        if extra:
            receipt.update(extra)
        self._publish(receipt, state)
        return None

    def on_llm_request(
        self,
        request: Mapping[str, Any] | None = None,
        **context: Any,
    ) -> dict[str, Any] | None:
        """llm_request middleware: keep the user's level as the cap and lower only when routine."""
        raw_request = request if isinstance(request, Mapping) else {}
        session_id = context.get("session_id")
        task_id = context.get("task_id")
        turn_id = context.get("turn_id")
        provider = context.get("provider")
        model = context.get("model") or raw_request.get("model")
        api_mode = context.get("api_mode")
        key = _session_key(session_id=session_id, task_id=task_id)
        state = self._bind_session(key)
        base = {"session_id": key, "model": str(model) if model else None, "mode": state.mode}
        if turn_id is not None:
            base["turn_id"] = turn_id

        with state.lock:
            state.requests += 1
            if not self.enabled:
                return self._unchanged(state, reason="disabled", requested=_explicit_effort(raw_request), base=base)
            if str(api_mode or "").strip().lower() == "bedrock_converse":
                return self._unchanged(state, reason="unsupported_route", requested=None, base=base)
            if model_is_excluded(model, self.exclude_models):
                return self._unchanged(state, reason="excluded_model", requested=_explicit_effort(raw_request), base=base)
            requested = _explicit_effort(raw_request)
            if requested is None:
                return self._unchanged(state, reason="no_host_effort", requested=None, base=base)

            # Turn boundary: stored outcomes belong to the previous turn.
            turn_key = _turn_key(turn_id)
            if turn_key is not None and turn_key != state.last_turn_id:
                if state.last_turn_id is not None:
                    state.outcomes = []
                    state.stuck = False
                state.last_turn_id = turn_key
                state.dirty = True

            model_key = str(model or "").strip().lower() or None
            reason_prefix = None
            if state.baseline is None:
                state.baseline = requested
                state.model = model_key
                state.dirty = True
            elif model_key != state.model:
                # Model switches and fallbacks change the level on their own: re-baseline, keep the mode.
                state.baseline = requested
                state.model = model_key
                state.dirty = True
            elif requested != state.baseline:
                state.baseline = requested
                state.mode = "pinned"
                state.dirty = True
                reason_prefix = "pinned_by_user_change"
            base["mode"] = state.mode

            if requested == "none":
                return self._unchanged(
                    state, reason=reason_prefix or "reasoning_disabled", requested=requested, base=base
                )

            if state.mode == "pinned":
                return self._unchanged(
                    state, reason=reason_prefix or "pinned", requested=requested, base=base
                )

            ladder = [
                level
                for level in wire_efforts_for_provider(
                    provider=provider,
                    model=model,
                    api_mode=api_mode,
                    allowed_efforts=self.allowed_efforts,
                )
                if level != "none"
            ]
            requested_wire = clamp_effort_for_provider(
                requested, provider=provider, model=model, api_mode=api_mode
            )
            if requested_wire not in ladder:
                return self._unchanged(state, reason="no_room", requested=requested, base=base)
            cap_index = ladder.index(requested_wire)
            if self.allow_raise and state.stuck and cap_index + 1 < len(ladder):
                cap_index += 1
            candidates = ladder[: cap_index + 1]
            cap = candidates[-1]
            base["cap"] = cap
            if len(candidates) < 2:
                return self._unchanged(state, reason="no_room", requested=requested, base=base)

            jev_called = False
            if state.dirty or state.choice_cap != cap:
                choice = self._ask_jev(state, raw_request, context, requested_wire, candidates)
                jev_called = choice.get("jev_called") is True
                state.jev_calls += int(jev_called)
                state.dirty = False
                state.choice_cap = cap
                if choice.get("reason_code") in _JEV_FAILURE_REASONS:
                    state.choice_effort = None
                    return self._unchanged(
                        state,
                        reason=str(choice.get("reason_code")),
                        requested=requested,
                        base=base,
                        extra={
                            "jev_called": jev_called,
                            "jev_latency_ms": choice.get("jev_latency_ms"),
                            "error_type": choice.get("error_type"),
                            "stuck_signal": state.stuck,
                        },
                    )
                effort = str(choice.get("effort"))
                if effort not in candidates:
                    state.choice_effort = None
                    return self._unchanged(state, reason="invalid_choice", requested=requested, base=base)
                state.choice_effort = effort
                receipt = {
                    **base,
                    "status": "selected",
                    "reason_code": "jev_selected",
                    "confidence": choice.get("confidence"),
                    "jev_latency_ms": choice.get("jev_latency_ms"),
                }
            else:
                if state.choice_effort is None:
                    return self._unchanged(state, reason="cached_unchanged", requested=requested, base=base)
                effort = state.choice_effort
                receipt = {**base, "status": "cached", "reason_code": "cached"}

            receipt.update(
                {
                    "effort": effort,
                    "requested_effort": requested,
                    "jev_called": jev_called,
                    "stuck_signal": state.stuck,
                }
            )
            modified = apply_effort_to_request(
                raw_request, effort, provider=provider, model=model, api_mode=api_mode
            )
            receipt["applied"] = self._effort_changed(raw_request, modified, provider, api_mode)
            if modified == dict(raw_request):
                receipt["source"] = "host_request_unchanged"
                self._publish(receipt, state)
                return None
            receipt["source"] = "llm_request_middleware" if receipt["applied"] else "wire_sanitization"
            self._publish(receipt, state)
            return {
                "request": modified,
                "source": "hermes-switchyard",
                "reason": f"reasoning_effort:{effort}",
                "name": "adaptive_reasoning_effort",
            }

    def _ask_jev(
        self,
        state: _SessionEffortState,
        raw_request: Mapping[str, Any],
        context: Mapping[str, Any],
        requested_wire: str,
        candidates: Sequence[str],
    ) -> dict[str, Any]:
        if not self.public_or_sanitized_data_ack:
            return {"reason_code": "kept_requested_ack_required", "jev_called": False}
        if self.client_factory is None:
            return {"reason_code": "kept_requested_on_jev_failure", "error_type": "client_unavailable", "jev_called": False}
        try:
            client = self.client_factory()
        except Exception as exc:  # noqa: BLE001
            return {"reason_code": "kept_requested_on_jev_failure", "error_type": type(exc).__name__, "jev_called": False}
        if client is None:
            return {"reason_code": "kept_requested_on_jev_failure", "error_type": "client_unavailable", "jev_called": False}
        task = _extract_task_snippet(raw_request, context.get("task") or context.get("user_message"))
        try:
            choice = choose_reasoning_effort(
                task=task,
                recent_tool_outcomes=list(state.outcomes),
                requested_effort=requested_wire,
                client=client,
                public_or_sanitized_data_ack=self.public_or_sanitized_data_ack,
                deadline_seconds=self.deadline_seconds,
                allowed_efforts=candidates,
                turn_phase="after_tool" if state.outcomes else "new_turn",
            )
            return {**choice, "jev_called": True}
        finally:
            close = getattr(client, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001
                    pass

    @staticmethod
    def _effort_changed(
        raw_request: Mapping[str, Any],
        modified: Mapping[str, Any],
        provider: Any,
        api_mode: Any,
    ) -> bool:
        """True only when the selected effort reached a provider-supported field."""
        api_mode_s = str(api_mode or "").strip().lower()
        provider_s = str(provider or "").strip().lower()
        if api_mode_s in {"anthropic_messages", "anthropic"} or provider_s in {"anthropic", "claude"} or "anthropic" in provider_s:
            old_config = raw_request.get("output_config")
            new_config = modified.get("output_config")
            return bool(
                isinstance(raw_request.get("thinking"), Mapping)
                and raw_request["thinking"].get("type") == "adaptive"
                and isinstance(old_config, Mapping)
                and "effort" in old_config
                and isinstance(new_config, Mapping)
                and new_config.get("effort") != old_config["effort"]
            )
        if api_mode_s in {"codex_responses", "responses"} or "codex" in provider_s:
            old_reasoning = raw_request.get("reasoning")
            new_reasoning = modified.get("reasoning")
            return bool(
                isinstance(old_reasoning, Mapping)
                and isinstance(new_reasoning, Mapping)
                and new_reasoning.get("effort") != old_reasoning.get("effort")
            )
        return modified != dict(raw_request)

    def build_post_tool_call_hook(self) -> Callable[..., Any]:
        def on_post_tool_call(
            tool_name: Any = None,
            result: Any = None,
            error: Any = None,
            status: Any = None,
            error_type: Any = None,
            error_message: Any = None,
            session_id: Any = None,
            task_id: Any = None,
            **kwargs: Any,
        ) -> None:
            failed, preview = derive_tool_failure(
                status=status if status is not None else kwargs.get("status"),
                error_type=error_type if error_type is not None else kwargs.get("error_type"),
                error_message=(
                    error_message
                    if error_message is not None
                    else kwargs.get("error_message")
                ),
                error=error,
                result=result,
                ok=kwargs.get("ok"),
            )
            self.record_tool_outcome(
                {
                    "tool": tool_name or kwargs.get("name") or "tool",
                    "ok": not failed,
                    "error": preview if failed else None,
                    "detail": preview,
                },
                session_id=session_id if session_id is not None else kwargs.get("session_id"),
                task_id=task_id if task_id is not None else kwargs.get("task_id"),
            )

        return on_post_tool_call


# ---------------------------------------------------------------------------
# Decision records and statistics
# ---------------------------------------------------------------------------

_RECORD_ENUMS = {
    "mode": frozenset(EFFORT_MODES),
    "requested": ALLOWED_EFFORTS,
    "sent": ALLOWED_EFFORTS,
    "cap": ALLOWED_EFFORTS,
}


def _finite_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return round(number, 4) if number == number and number not in (float("inf"), float("-inf")) else None


def build_effort_record(receipt: Mapping[str, Any], *, now: Any = None) -> dict[str, Any]:
    """Return one sanitized, closed-set decision record (no text)."""
    from . import receipt_history, receipt_state

    moment = now or datetime.now(timezone.utc)
    record: dict[str, Any] = {
        "schema": EFFORT_HISTORY_SCHEMA,
        "recorded_at": receipt_history.format_timestamp(moment),
        "session_id": receipt_state.safe_identifier(receipt.get("session_id")),
        "turn_id": receipt_state.safe_identifier(
            str(receipt.get("turn_id")) if receipt.get("turn_id") is not None else None
        ),
        "model": receipt_state.safe_identifier(receipt.get("model")),
        "mode": receipt.get("mode"),
        "requested": receipt.get("requested_effort"),
        "sent": receipt.get("effort"),
        "cap": receipt.get("cap"),
        "reason_code": receipt_state.safe_identifier(receipt.get("reason_code"), max_length=64),
        "jev_called": receipt.get("jev_called") is True,
        "jev_latency_ms": _finite_or_none(receipt.get("jev_latency_ms")),
        "confidence": _finite_or_none(receipt.get("confidence")),
        "stuck": receipt.get("stuck_signal") is True,
    }
    for key, allowed in _RECORD_ENUMS.items():
        if record[key] not in allowed:
            record[key] = None
    return record


def effort_history_path(data_dir: Any = None) -> Path | None:
    from . import receipt_state

    if data_dir is not None:
        return Path(data_dir) / EFFORT_HISTORY_FILE_NAME
    resolved = receipt_state._plugin_data_dir()
    return resolved / EFFORT_HISTORY_FILE_NAME if resolved is not None else None


def append_effort_record(
    receipt: Mapping[str, Any],
    *,
    data_dir: Any = None,
    now: Any = None,
    max_records: int = EFFORT_HISTORY_MAX_RECORDS,
    max_bytes: int = EFFORT_HISTORY_MAX_BYTES,
) -> bool:
    """Append one decision record to the bounded history; never raises."""
    try:
        from . import receipt_history

        path = effort_history_path(data_dir)
        if path is None:
            return False
        encoded = json.dumps(
            build_effort_record(receipt, now=now),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        with receipt_history._HistoryLock(path.with_name(EFFORT_HISTORY_LOCK_NAME)) as locked:
            if not locked:
                return False
            try:
                before = path.lstat()
            except FileNotFoundError:
                before = None
            if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
                return False
            lines = receipt_history._read_lines(path, max_bytes) if before is not None else []
            if (
                before is not None
                and before.st_size + len(encoded) + 1 <= max_bytes
                and receipt_history._ends_with_newline(path, before.st_size)
                and (os.name == "nt" or not before.st_mode & 0o077)
                and len(lines) < max_records
            ):
                return receipt_history._append_line(path, encoded, before)
            lines.append(encoded)
            return receipt_history._write_lines(path, receipt_history._trim(lines, max_records, max_bytes))
    except Exception:  # noqa: BLE001 -- diagnostic only
        return False


def read_effort_history(*, data_dir: Any = None, since: Any = None, now: Any = None) -> list[dict[str, Any]]:
    from . import receipt_history

    path = effort_history_path(data_dir)
    if path is None:
        return []
    try:
        info = path.lstat()
    except OSError:
        return []
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return []
    records: list[dict[str, Any]] = []
    cutoff = None
    if since is not None:
        cutoff = (now or datetime.now(timezone.utc)) - since
    for line in receipt_history._read_lines(path, EFFORT_HISTORY_MAX_BYTES):
        if len(line) > 4096:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict) or record.get("schema") != EFFORT_HISTORY_SCHEMA:
            continue
        stamp = receipt_history.parse_timestamp(record.get("recorded_at"))
        if stamp is None:
            continue
        if cutoff is not None and stamp < cutoff:
            continue
        records.append(record)
    return records


def compute_effort_stats(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    from collections import Counter

    from .receipt_history import _percentile, _rate

    items = [item for item in records if isinstance(item, Mapping)]
    order = {level: index for index, level in enumerate(HERMES_REASONING_EFFORTS)}
    lowered = raised = unchanged = 0
    for item in items:
        requested, sent = item.get("requested"), item.get("sent")
        if requested in order and sent in order:
            if order[sent] < order[requested]:
                lowered += 1
            elif order[sent] > order[requested]:
                raised += 1
            else:
                unchanged += 1
    jev = [item for item in items if item.get("jev_called") is True]
    latencies = [
        float(item["jev_latency_ms"])
        for item in jev
        if isinstance(item.get("jev_latency_ms"), (int, float)) and not isinstance(item.get("jev_latency_ms"), bool)
    ]
    return {
        "records": len(items),
        "sessions": len({item.get("session_id") for item in items if item.get("session_id")}),
        "by_mode": dict(sorted(Counter(str(item.get("mode")) for item in items).items())),
        "by_reason": dict(sorted(Counter(str(item.get("reason_code")) for item in items).items())),
        "requested_levels": dict(sorted(Counter(str(item.get("requested")) for item in items).items())),
        "sent_levels": dict(sorted(Counter(str(item.get("sent")) for item in items).items())),
        "lowered": lowered,
        "unchanged": unchanged,
        "raised": raised,
        "lowered_rate": _rate(lowered, lowered + unchanged + raised),
        "jev_calls": len(jev),
        "jev_calls_per_record": round(len(jev) / len(items), 4) if items else None,
        "jev_latency_ms_p50": _percentile(latencies, 0.50),
        "jev_latency_ms_p95": _percentile(latencies, 0.95),
    }


def effort_stats(*, since: Any = None, data_dir: Any = None, now: Any = None) -> dict[str, Any]:
    return compute_effort_stats(read_effort_history(data_dir=data_dir, since=since, now=now))


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_reasoning_effort_adapter(
    ctx: Any,
    *,
    enabled: bool = True,
    default_effort: str = DEFAULT_EFFORT,
    client_factory: Callable[[], Any] | None = None,
    public_or_sanitized_data_ack: bool = True,
    deadline_seconds: float = DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS,
    mode: str = DEFAULT_EFFORT_MODE,
    exclude_models: Any = None,
    allow_raise: Any = False,
    record_decision: Callable[[dict[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Register llm_request middleware, post_tool_call, and ``/switchyard`` when Hermes exposes them."""
    global _LAST_REGISTRATION
    seam = probe_llm_request_middleware_seam(ctx)
    settings = {
        "mode": normalize_mode(mode),
        "exclude_models": list(parse_exclude_models(exclude_models)),
        "allow_raise": parse_bool_setting(allow_raise),
        "deadline_seconds": float(deadline_seconds),
    }
    if not enabled:
        receipt = {
            "registered": True,
            "mode": "disabled",
            "hermes_seam": seam,
            "reason": "adaptive_reasoning_effort_disabled",
            "enabled": False,
            "can_apply": False,
            "settings": settings,
        }
        _LAST_REGISTRATION = dict(receipt)
        return receipt

    if not seam.get("available") or not seam.get("can_apply"):
        receipt = {
            "registered": True,
            "mode": "noop_seam_unavailable",
            "hermes_seam": seam,
            "reason": "hermes_llm_request_middleware_unavailable",
            "enabled": True,
            "can_apply": False,
            "settings": settings,
        }
        _LAST_REGISTRATION = dict(receipt)
        return receipt

    controller = ReasoningEffortController(
        enabled=True,
        default_effort=default_effort,
        client_factory=client_factory,
        public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        deadline_seconds=deadline_seconds,
        mode=mode,
        exclude_models=exclude_models,
        allow_raise=allow_raise,
        record_decision=record_decision,
    )
    register_middleware = getattr(ctx, "register_middleware")
    register_middleware("llm_request", controller.on_llm_request)

    register_hook = getattr(ctx, "register_hook", None)
    post_tool_registered = False
    if callable(register_hook):
        try:
            register_hook("post_tool_call", controller.build_post_tool_call_hook())
            post_tool_registered = True
        except Exception:  # noqa: BLE001 -- post_tool_call is optional context
            post_tool_registered = False

    command_registered = False
    register_command = getattr(ctx, "register_command", None)
    if callable(register_command):
        try:
            register_command(
                "switchyard",
                controller.handle_command,
                description="Switchyard controls: effort auto | pin | status",
                args_hint="effort auto|pin|status",
            )
            command_registered = True
        except Exception:  # noqa: BLE001 -- the command is optional
            command_registered = False

    receipt = {
        "registered": True,
        "mode": "llm_request_middleware",
        "hermes_seam": seam,
        "reason": None,
        "enabled": True,
        "can_apply": True,
        "settings": settings,
        "post_tool_call_registered": post_tool_registered,
        "command_registered": command_registered,
        "integration_point": (
            "hermes_switchyard.reasoning_effort_adapter.register_reasoning_effort_adapter"
        ),
    }
    # Keep the live controller on the function for tests; never put it in receipts.
    register_reasoning_effort_adapter.last_controller = controller  # type: ignore[attr-defined]
    _LAST_REGISTRATION = dict(receipt)
    return receipt
