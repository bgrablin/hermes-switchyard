"""Jev adaptive reasoning-effort picker for Hermes llm_request middleware.

Codex-style: per turn / after tools, Jev chooses a Hermes-supported reasoning
effort (none|minimal|low|medium|high|xhigh|max|ultra). Raise when stuck, lower
for routine work. Apply by rewriting only request-scoped effort fields so the
prompt-cache prefix stays untouched. Fail closed: keep the previous effort when
Jev fails. Model apply stays out of scope — jev_model_route remains advisory.
"""
from __future__ import annotations

import json
import threading
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
DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS = 8.0
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
        or "claude" in model_s
    )
    is_lmstudio = provider_s in {"lmstudio", "lm-studio", "lm_studio"} or "lmstudio" in provider_s

    if level == "none":
        if is_codex_family:
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
        no_xhigh = (
            "claude-opus-4-6" in model_s
            or "claude-opus-4.6" in model_s
            or "claude-sonnet-4-6" in model_s
            or "claude-sonnet-4.6" in model_s
        )
        if no_xhigh and level == "xhigh":
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


def choose_reasoning_effort(
    *,
    task: str,
    recent_tool_outcomes: Sequence[Mapping[str, Any]] | None,
    prior_effort: str,
    client: Any,
    public_or_sanitized_data_ack: bool = True,
    deadline_seconds: float = DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS,
    allowed_efforts: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Ask Jev for a typed effort Choice; fail closed to *prior_effort* on errors."""
    prior = normalize_effort(prior_effort)
    levels = [
        level
        for level in (allowed_efforts or HERMES_REASONING_EFFORTS)
        if level in ALLOWED_EFFORTS
    ]
    if not levels:
        levels = list(HERMES_REASONING_EFFORTS)

    if public_or_sanitized_data_ack is not True:
        return {
            "status": "kept_previous",
            "effort": prior,
            "reason_code": "kept_previous_ack_required",
            "applied": False,
            "prior_effort": prior,
            "confidence": None,
            "probabilities": None,
        }

    outcomes: list[dict[str, str]] = []
    for item in list(recent_tool_outcomes or [])[-MAX_TOOL_OUTCOMES:]:
        if not isinstance(item, Mapping):
            continue
        status = str(item.get("status") or "unknown").strip().lower()
        outcomes.append({"status": status if status in {"ok", "error", "failed"} else "unknown"})

    stuck = any(item.get("status") in {"error", "failed"} for item in outcomes)
    candidates = [
        {"id": level, "description": _EFFORT_CRITERIA.get(level, level)} for level in levels
    ]
    criteria = _criteria(candidates, "id")
    state = {
        "task_present": bool(task),
        "task_length_bucket": (
            "short" if len(task or "") < 80 else "medium" if len(task or "") < 400 else "long"
        ),
        "prior_effort": prior,
        "recent_tool_outcomes": outcomes,
        "stuck_signal": stuck,
        "policy": {
            "raise_when_stuck": True,
            "lower_for_routine": True,
            "fail_closed_keep_previous": True,
            "prompt_cache_friendly": True,
        },
    }
    questions = {
        "reasoning_effort": {
            "type": "choice",
            "instructions": (
                "Pick exactly one Hermes reasoning_effort for the next model generation. "
                "Raise effort when recent tools failed or the task is hard; lower for "
                "routine or trivial work. Prefer the prior effort when uncertain."
            ),
            "criteria": criteria,
        }
    }

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
        selected, confidence, probabilities = _choice_metrics(
            answers.get("reasoning_effort"),
            criteria,
            "reasoning_effort",
        )
        effort = normalize_effort(selected, default=prior)
        if effort not in criteria:
            return {
                "status": "kept_previous",
                "effort": prior,
                "reason_code": "invalid_choice",
                "applied": False,
                "prior_effort": prior,
                "confidence": confidence,
                "probabilities": probabilities,
                **metadata,
            }
        return {
            "status": "selected",
            "effort": effort,
            "reason_code": "jev_selected",
            "applied": False,
            "prior_effort": prior,
            "confidence": confidence,
            "probabilities": probabilities,
            "stuck_signal": stuck,
            **metadata,
        }
    except Exception as exc:  # noqa: BLE001 -- fail closed to prior effort
        return {
            "status": "kept_previous",
            "effort": prior,
            "reason_code": "kept_previous_on_jev_failure",
            "applied": False,
            "prior_effort": prior,
            "confidence": None,
            "probabilities": None,
            "error_type": type(exc).__name__,
        }


class _SessionEffortState:
    """Mutable per-session/task effort state + lock."""

    __slots__ = ("lock", "effort", "outcomes", "dirty", "last_choice", "last_turn_id")

    def __init__(self, default_effort: str) -> None:
        self.lock = threading.RLock()
        self.effort = default_effort
        self.outcomes: list[dict[str, str]] = []
        self.dirty = True
        self.last_turn_id: str | None = None
        self.last_choice: dict[str, Any] = {
            "effort": default_effort,
            "reason_code": "default_no_prior",
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
    return None


class ReasoningEffortController:
    """Effort controller with per-session/task state for concurrent gateways."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        default_effort: str = DEFAULT_EFFORT,
        client_factory: Callable[[], Any] | None = None,
        public_or_sanitized_data_ack: bool = True,
        deadline_seconds: float = DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS,
        allowed_efforts: Sequence[str] | None = None,
    ) -> None:
        self.enabled = enabled is True
        self.default_effort = normalize_effort(default_effort)
        self.client_factory = client_factory
        self.public_or_sanitized_data_ack = public_or_sanitized_data_ack is True
        self.deadline_seconds = float(deadline_seconds)
        self.allowed_efforts = tuple(
            level
            for level in (allowed_efforts or HERMES_REASONING_EFFORTS)
            if level in ALLOWED_EFFORTS
        ) or tuple(HERMES_REASONING_EFFORTS)
        self._registry_lock = threading.RLock()
        self._sessions: dict[str, _SessionEffortState] = {}

    def _state_for(self, *, session_id: Any = None, task_id: Any = None) -> _SessionEffortState:
        key = _session_key(session_id=session_id, task_id=task_id)
        with self._registry_lock:
            state = self._sessions.get(key)
            if state is None:
                state = _SessionEffortState(self.default_effort)
                self._sessions[key] = state
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
            state.outcomes.append(
                summarize_tool_outcome(
                    tool_name=outcome.get("tool") or outcome.get("tool_name"),
                    ok=outcome.get("ok"),
                    error=outcome.get("error"),
                    result_preview=outcome.get("detail") or outcome.get("result_preview"),
                )
            )
            state.outcomes = state.outcomes[-MAX_TOOL_OUTCOMES:]
            state.dirty = True

    def _publish(self, receipt: Mapping[str, Any], state: _SessionEffortState) -> dict[str, Any]:
        global _LAST_RECEIPT
        payload = dict(receipt)
        _LAST_RECEIPT = dict(payload)
        state.last_choice = dict(payload)
        return payload

    def ensure_choice(
        self,
        *,
        task: str,
        force: bool = False,
        session_id: Any = None,
        task_id: Any = None,
        turn_id: Any = None,
        provider: Any = None,
        model: Any = None,
        api_mode: Any = None,
    ) -> dict[str, Any]:
        state = self._state_for(session_id=session_id, task_id=task_id)
        with state.lock:
            if not self.enabled:
                return self._publish(
                    {
                        "status": "disabled",
                        "effort": state.effort,
                        "reason_code": "disabled",
                        "applied": False,
                        "prior_effort": state.effort,
                    },
                    state,
                )

            turn_key = None
            if isinstance(turn_id, str) and turn_id.strip():
                turn_key = turn_id.strip()
            elif turn_id is not None and str(turn_id).strip():
                turn_key = str(turn_id).strip()

            if turn_key is not None:
                if state.last_turn_id is not None and turn_key != state.last_turn_id:
                    state.dirty = True
                state.last_turn_id = turn_key

            if not force and not state.dirty and state.last_choice.get("effort"):
                cached = dict(state.last_choice)
                cached["status"] = "cached"
                if cached.get("reason_code") not in {
                    "kept_previous_on_jev_failure",
                    "kept_previous_ack_required",
                    "invalid_choice",
                }:
                    cached["reason_code"] = "cached"
                return self._publish(cached, state)

            prior = state.effort
            wire_levels = wire_efforts_for_provider(
                provider=provider,
                model=model,
                api_mode=api_mode,
                allowed_efforts=self.allowed_efforts,
            )
            if self.client_factory is None:
                choice: dict[str, Any] = {
                    "status": "kept_previous",
                    "effort": prior,
                    "reason_code": "kept_previous_on_jev_failure",
                    "applied": False,
                    "prior_effort": prior,
                    "error_type": "client_unavailable",
                }
            else:
                try:
                    client = self.client_factory()
                except Exception as exc:  # noqa: BLE001
                    choice = {
                        "status": "kept_previous",
                        "effort": prior,
                        "reason_code": "kept_previous_on_jev_failure",
                        "applied": False,
                        "prior_effort": prior,
                        "error_type": type(exc).__name__,
                    }
                else:
                    try:
                        choice = choose_reasoning_effort(
                            task=task,
                            recent_tool_outcomes=list(state.outcomes),
                            prior_effort=prior,
                            client=client,
                            public_or_sanitized_data_ack=self.public_or_sanitized_data_ack,
                            deadline_seconds=self.deadline_seconds,
                            allowed_efforts=wire_levels,
                        )
                    finally:
                        close = getattr(client, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:  # noqa: BLE001
                                pass

            effort = clamp_effort_for_provider(
                choice.get("effort"),
                provider=provider,
                model=model,
                api_mode=api_mode,
            )
            # Prefer prior when clamp would invent an empty/unknown value.
            if effort not in ALLOWED_EFFORTS:
                effort = normalize_effort(prior)
            state.effort = effort
            state.dirty = False
            choice = dict(choice)
            choice["effort"] = effort
            return self._publish(choice, state)

    def on_llm_request(
        self,
        request: Mapping[str, Any] | None = None,
        **context: Any,
    ) -> dict[str, Any] | None:
        """llm_request middleware: choose (if needed) and apply effort to kwargs."""
        raw_request = request if isinstance(request, Mapping) else {}
        session_id = context.get("session_id")
        task_id = context.get("task_id")
        turn_id = context.get("turn_id")
        provider = context.get("provider")
        model = context.get("model") or raw_request.get("model")
        api_mode = context.get("api_mode")
        if api_mode == "bedrock_converse":
            return None
        task = _extract_task_snippet(
            raw_request, context.get("task") or context.get("user_message")
        )
        choice = self.ensure_choice(
            task=task,
            session_id=session_id,
            task_id=task_id,
            turn_id=turn_id,
            provider=provider,
            model=model,
            api_mode=api_mode,
        )
        if choice.get("reason_code") in {
            "kept_previous_on_jev_failure",
            "kept_previous_ack_required",
            "invalid_choice",
        }:
            host_effort = _explicit_effort(raw_request)
            if host_effort is not None:
                state = self._state_for(session_id=session_id, task_id=task_id)
                with state.lock:
                    state.effort = host_effort
                    choice["effort"] = host_effort
                    choice["applied"] = False
                    choice["source"] = "host_request_preserved"
                    self._publish(choice, state)
                return None
        effort = clamp_effort_for_provider(
            choice.get("effort"),
            provider=provider,
            model=model,
            api_mode=api_mode,
        )
        modified = apply_effort_to_request(
            raw_request,
            effort,
            provider=provider,
            model=model,
            api_mode=api_mode,
        )
        state = self._state_for(session_id=session_id, task_id=task_id)
        receipt = dict(choice)
        receipt["effort"] = effort
        receipt["applied"] = True
        receipt["source"] = "llm_request_middleware"
        receipt["session_id"] = _session_key(session_id=session_id, task_id=task_id)
        if turn_id is not None:
            receipt["turn_id"] = turn_id
        self._publish(receipt, state)
        return {
            "request": modified,
            "source": "hermes-switchyard",
            "reason": f"reasoning_effort:{effort}",
            "name": "adaptive_reasoning_effort",
        }

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


def register_reasoning_effort_adapter(
    ctx: Any,
    *,
    enabled: bool = True,
    default_effort: str = DEFAULT_EFFORT,
    client_factory: Callable[[], Any] | None = None,
    public_or_sanitized_data_ack: bool = True,
    deadline_seconds: float = DEFAULT_ADAPTIVE_REASONING_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Register llm_request middleware (+ post_tool_call) when Hermes exposes them."""
    global _LAST_REGISTRATION
    seam = probe_llm_request_middleware_seam(ctx)
    if not enabled:
        receipt = {
            "registered": True,
            "mode": "disabled",
            "hermes_seam": seam,
            "reason": "adaptive_reasoning_effort_disabled",
            "enabled": False,
            "can_apply": False,
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
        }
        _LAST_REGISTRATION = dict(receipt)
        return receipt

    controller = ReasoningEffortController(
        enabled=True,
        default_effort=default_effort,
        client_factory=client_factory,
        public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        deadline_seconds=deadline_seconds,
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

    receipt = {
        "registered": True,
        "mode": "llm_request_middleware",
        "hermes_seam": seam,
        "reason": None,
        "enabled": True,
        "can_apply": True,
        "default_effort": normalize_effort(default_effort),
        "post_tool_call_registered": post_tool_registered,
        "integration_point": (
            "hermes_switchyard.reasoning_effort_adapter.register_reasoning_effort_adapter"
        ),
    }
    # Keep the live controller on the function for tests; never put it in receipts.
    register_reasoning_effort_adapter.last_controller = controller  # type: ignore[attr-defined]
    _LAST_REGISTRATION = dict(receipt)
    return receipt
