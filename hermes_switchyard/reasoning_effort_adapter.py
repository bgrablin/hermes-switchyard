"""Jev adaptive reasoning-effort picker for Hermes llm_request middleware.

Codex-style: per turn / after tools, Jev chooses a Hermes-supported reasoning
effort (none|minimal|low|medium|high|xhigh|max|ultra). Raise when stuck, lower
for routine work. Apply by rewriting only request-scoped effort fields so the
prompt-cache prefix stays untouched. Fail closed: keep the previous effort when
Jev fails. Model apply stays out of scope — jev_model_route remains advisory.
"""
from __future__ import annotations

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


def _extract_task_snippet(request: Mapping[str, Any] | None, explicit: Any = None) -> str:
    if isinstance(explicit, str) and explicit.strip():
        return _truncate(explicit, MAX_TASK_CHARS)
    if not isinstance(request, Mapping):
        return ""
    messages = request.get("messages")
    if not isinstance(messages, Sequence):
        return ""
    for message in reversed(list(messages)):
        if not isinstance(message, Mapping):
            continue
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return _truncate(content, MAX_TASK_CHARS)
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, Mapping) and isinstance(block.get("text"), str):
                    parts.append(block["text"])
                elif isinstance(block, str):
                    parts.append(block)
            joined = " ".join(parts).strip()
            if joined:
                return _truncate(joined, MAX_TASK_CHARS)
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


def apply_effort_to_request(request: Mapping[str, Any], effort: str) -> dict[str, Any]:
    """Rewrite provider kwargs effort fields only — never touch messages.

    Prompt-cache friendliness: messages / tools / system stay byte-identical;
    only reasoning_effort (and nested effort twins already present) change.
    """
    level = normalize_effort(effort)
    out = dict(request)
    out["reasoning_effort"] = level

    extra = out.get("extra_body")
    if isinstance(extra, Mapping):
        extra_out = dict(extra)
        reasoning = extra_out.get("reasoning")
        if isinstance(reasoning, Mapping):
            reasoning_out = dict(reasoning)
            if level == "none":
                reasoning_out["enabled"] = False
                reasoning_out.pop("effort", None)
            else:
                reasoning_out["enabled"] = True
                reasoning_out["effort"] = level
            extra_out["reasoning"] = reasoning_out
        elif "reasoning_effort" in extra_out or level != "none":
            extra_out["reasoning_effort"] = level
        out["extra_body"] = extra_out

    nested = out.get("reasoning")
    if isinstance(nested, Mapping):
        nested_out = dict(nested)
        if level == "none":
            nested_out["enabled"] = False
            nested_out.pop("effort", None)
        else:
            nested_out["enabled"] = True
            nested_out["effort"] = level
        out["reasoning"] = nested_out

    reasoning_config = out.get("reasoning_config")
    if isinstance(reasoning_config, Mapping):
        cfg = dict(reasoning_config)
        if level == "none":
            cfg["enabled"] = False
            cfg.pop("effort", None)
        else:
            cfg["enabled"] = True
            cfg["effort"] = level
        out["reasoning_config"] = cfg

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
        outcomes.append(
            {
                "tool": _truncate(item.get("tool") or "tool", 64),
                "status": _truncate(item.get("status") or "unknown", 32),
                "detail": _truncate(item.get("detail") or "", MAX_OUTCOME_CHARS),
            }
        )

    stuck = any(item.get("status") in {"error", "failed"} for item in outcomes)
    candidates = [
        {"id": level, "description": _EFFORT_CRITERIA[level]} for level in levels
    ]
    criteria = _criteria(candidates, "id")
    state = {
        "task": _truncate(task or "", MAX_TASK_CHARS),
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


class ReasoningEffortController:
    """Process-local effort state shared by middleware and post_tool_call."""

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
        self._lock = threading.RLock()
        self._effort = self.default_effort
        self._outcomes: list[dict[str, str]] = []
        self._dirty = True
        self._last_choice: dict[str, Any] = {
            "effort": self._effort,
            "reason_code": "default_no_prior",
            "applied": False,
            "source": "controller_init",
        }

    def record_tool_outcome(self, outcome: Mapping[str, Any]) -> None:
        with self._lock:
            self._outcomes.append(
                summarize_tool_outcome(
                    tool_name=outcome.get("tool") or outcome.get("tool_name"),
                    ok=outcome.get("ok"),
                    error=outcome.get("error"),
                    result_preview=outcome.get("detail") or outcome.get("result_preview"),
                )
            )
            self._outcomes = self._outcomes[-MAX_TOOL_OUTCOMES:]
            self._dirty = True

    def _publish(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        global _LAST_RECEIPT
        payload = dict(receipt)
        _LAST_RECEIPT = dict(payload)
        self._last_choice = dict(payload)
        return payload

    def ensure_choice(self, *, task: str, force: bool = False) -> dict[str, Any]:
        with self._lock:
            if not self.enabled:
                return self._publish(
                    {
                        "status": "disabled",
                        "effort": self._effort,
                        "reason_code": "disabled",
                        "applied": False,
                        "prior_effort": self._effort,
                    }
                )
            if not force and not self._dirty and self._last_choice.get("effort"):
                cached = dict(self._last_choice)
                cached["reason_code"] = "cached"
                cached["status"] = "cached"
                return self._publish(cached)

            prior = self._effort
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
                            recent_tool_outcomes=list(self._outcomes),
                            prior_effort=prior,
                            client=client,
                            public_or_sanitized_data_ack=self.public_or_sanitized_data_ack,
                            deadline_seconds=self.deadline_seconds,
                            allowed_efforts=self.allowed_efforts,
                        )
                    finally:
                        close = getattr(client, "close", None)
                        if callable(close):
                            try:
                                close()
                            except Exception:  # noqa: BLE001
                                pass

            effort = normalize_effort(choice.get("effort"), default=prior)
            self._effort = effort
            self._dirty = False
            choice = dict(choice)
            choice["effort"] = effort
            return self._publish(choice)

    def on_llm_request(
        self,
        request: Mapping[str, Any] | None = None,
        **context: Any,
    ) -> dict[str, Any]:
        """llm_request middleware: choose (if needed) and apply effort to kwargs."""
        raw_request = request if isinstance(request, Mapping) else {}
        task = _extract_task_snippet(
            raw_request, context.get("task") or context.get("user_message")
        )
        choice = self.ensure_choice(task=task)
        effort = normalize_effort(choice.get("effort"), default=self.default_effort)
        modified = apply_effort_to_request(raw_request, effort)
        receipt = dict(choice)
        receipt["effort"] = effort
        receipt["applied"] = True
        receipt["source"] = "llm_request_middleware"
        self._publish(receipt)
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
            **kwargs: Any,
        ) -> None:
            ok = error is None
            preview = ""
            if isinstance(result, Mapping):
                preview = str(result.get("error") or result.get("status") or "")[
                    :MAX_OUTCOME_CHARS
                ]
                if result.get("ok") is False or result.get("error"):
                    ok = False
            elif result is not None:
                preview = _truncate(result, MAX_OUTCOME_CHARS)
            self.record_tool_outcome(
                {
                    "tool": tool_name or kwargs.get("name") or "tool",
                    "ok": ok,
                    "error": error,
                    "detail": preview,
                }
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
