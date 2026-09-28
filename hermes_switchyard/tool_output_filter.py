"""Opt-in disposable tool-output filter (SPIKE #151).

When ``filter_disposable_tool_output`` is on, a ``transform_tool_result`` hook
may soft-cap high-volume **exec** tool stdout (terminal / shell / bash / …)
before it re-enters the main model context. This is replace-not-add: it shrinks
tokens the main model would otherwise re-read every subsequent turn.

Capability-first: default **off**. When on, preserve errors, small outputs,
security-relevant text, and user-asked full dumps. This vertical slice does
**not** filter read/write/other kinds (follow-ups).
"""
from __future__ import annotations

import json
import re
from typing import Any, Mapping

from .reasoning_effort_adapter import classify_tool_kind, derive_tool_failure

# Soft-cap for successful exec firehose (npm install, test logs, long ls).
# Head+tail stay under this budget so the model still sees start and end.
DEFAULT_SOFT_CAP_CHARS = 6_000
DEFAULT_HEAD_CHARS = 2_500
DEFAULT_TAIL_CHARS = 2_000

# This slice: exec kinds only (terminal stdout soft-cap).
FILTERABLE_KINDS = frozenset({"exec"})

_OMISSION_TEMPLATE = (
    "\n\n… [switchyard: omitted {omitted} chars of disposable exec tool output "
    "({original} total; filter_disposable_tool_output); "
    "errors and small outputs are never cut — ask for a full dump to keep verbatim] …\n\n"
)

# Never soft-cap when the captured text looks security- or failure-relevant.
_SECURITY_OR_FAILURE_RE = re.compile(
    r"(?i)(?:"
    r"permission\s+denied|access\s+denied|authentication\s+failed|"
    r"unauthorized|forbidden|certificate\s+error|ssl\s+error|"
    r"private\s+key|secret\s+key|credentials?\s+(?:invalid|expired|rejected)|"
    r"token\s+(?:expired|revoked|invalid)|password\s+(?:incorrect|rejected)|"
    r"\bcve-\d{4}-\d+\b|"
    r"traceback\s*\(most\s+recent\s+call\s+last\)|"
    r"fatal\s+error|segmentation\s+fault|out\s+of\s+memory|"
    r"npm\s+err!|error:\s+command\s+failed|"
    r"exit\s+code\s+[1-9]\d*|returned\s+non-zero\s+exit"
    r")"
)

# User (or command) asked to keep the full dump — do not soft-cap.
_FULL_DUMP_ASK_RE = re.compile(
    r"(?i)(?:"
    r"\bfull\s+dump\b|\bfull\s+output\b|\bverbatim\b|\buntruncated\b|"
    r"\bdo\s+not\s+truncate\b|\bno\s+truncat(?:e|ion)\b|"
    r"\bkeep\s+(?:the\s+)?(?:entire|whole|full)\s+output\b|"
    r"\bshow\s+(?:me\s+)?(?:everything|all\s+output)\b"
    r")"
)

_FAILURE_STATUSES = frozenset({"error", "failed", "blocked", "cancelled", "canceled"})


def soft_cap_chars() -> int:
    return DEFAULT_SOFT_CAP_CHARS


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _args_text(args: Any) -> str:
    if not isinstance(args, Mapping):
        return _as_text(args)
    parts: list[str] = []
    for key in ("command", "cmd", "script", "code", "input", "query"):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
    if not parts:
        return _as_text(args)
    return "\n".join(parts)


def user_asks_full_dump(*, args: Any = None, result: Any = None) -> bool:
    """True when tool args (or result cue) request an untruncated dump."""
    blob = f"{_args_text(args)}\n{_as_text(result)[:500]}"
    return bool(_FULL_DUMP_ASK_RE.search(blob))


def looks_security_or_failure_relevant(text: str) -> bool:
    """Conservative preserve: do not soft-cap security- or failure-shaped text."""
    if not text:
        return False
    # Scan a bounded window (head + mid cue + tail) for linear-time safety.
    if len(text) <= 12_000:
        return bool(_SECURITY_OR_FAILURE_RE.search(text))
    sample = text[:4_000] + "\n" + text[len(text) // 2 : len(text) // 2 + 2_000] + "\n" + text[-4_000:]
    return bool(_SECURITY_OR_FAILURE_RE.search(sample))


def _structured_returncode(result: Any) -> int | None:
    """Best-effort returncode from a JSON tool result mapping or string."""
    parsed: Any = result
    if isinstance(result, str):
        text = result.strip()
        if not text or text[0] not in "{[":
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(parsed, Mapping):
        return None
    for key in ("returncode", "exit_code", "exitcode", "code"):
        value = parsed.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            try:
                return int(value.strip())
            except ValueError:
                continue
    return None


def _structured_output_field(result: Any) -> tuple[Any, str] | None:
    """If result is JSON with an ``output`` string, return (parsed, output)."""
    parsed: Any = result
    if isinstance(result, str):
        text = result.strip()
        if not text or text[0] not in "{[":
            return None
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
    if not isinstance(parsed, Mapping):
        return None
    output = parsed.get("output")
    if isinstance(output, str):
        return parsed, output
    return None


def soft_cap_text(
    text: str,
    *,
    soft_cap: int = DEFAULT_SOFT_CAP_CHARS,
    head_chars: int = DEFAULT_HEAD_CHARS,
    tail_chars: int = DEFAULT_TAIL_CHARS,
) -> str | None:
    """Return a head+tail soft-cap, or None when ``text`` is already within budget."""
    if soft_cap < 64:
        soft_cap = 64
    if len(text) <= soft_cap:
        return None
    head = max(32, min(head_chars, soft_cap // 2))
    tail = max(32, min(tail_chars, soft_cap - head - 80))
    omitted = len(text) - head - tail
    if omitted <= 0:
        return None
    marker = _OMISSION_TEMPLATE.format(omitted=omitted, original=len(text))
    return text[:head] + marker + text[-tail:]


def should_filter_tool_result(
    *,
    enabled: bool,
    tool_name: Any,
    result: Any = None,
    args: Any = None,
    status: Any = None,
    error: Any = None,
    error_type: Any = None,
    error_message: Any = None,
    ok: Any = None,
) -> bool:
    """Return whether this result is eligible for soft-cap under current preserve rules."""
    if enabled is not True:
        return False
    kind = classify_tool_kind(tool_name)
    if kind not in FILTERABLE_KINDS:
        return False

    status_text = str(status or "").strip().lower()
    if status_text in _FAILURE_STATUSES:
        return False

    failed, _preview = derive_tool_failure(
        status=status,
        error_type=error_type,
        error_message=error_message,
        error=error,
        result=result,
        ok=ok,
    )
    if failed:
        return False

    returncode = _structured_returncode(result)
    if returncode is not None and returncode != 0:
        return False

    if user_asks_full_dump(args=args, result=result):
        return False

    # Measure the disposable payload (structured output field when present).
    structured = _structured_output_field(result)
    payload = structured[1] if structured is not None else _as_text(result)
    if len(payload) <= soft_cap_chars():
        return False
    if looks_security_or_failure_relevant(payload):
        return False
    return True


def filter_tool_result_text(
    *,
    enabled: bool,
    tool_name: Any,
    result: Any = None,
    args: Any = None,
    status: Any = None,
    error: Any = None,
    error_type: Any = None,
    error_message: Any = None,
    ok: Any = None,
    soft_cap: int | None = None,
) -> str | None:
    """Return a replacement result string, or None to leave the original unchanged."""
    if not should_filter_tool_result(
        enabled=enabled,
        tool_name=tool_name,
        result=result,
        args=args,
        status=status,
        error=error,
        error_type=error_type,
        error_message=error_message,
        ok=ok,
    ):
        return None

    cap = soft_cap_chars() if soft_cap is None else max(64, int(soft_cap))
    structured = _structured_output_field(result)
    if structured is not None:
        parsed, output = structured
        capped = soft_cap_text(output, soft_cap=cap)
        if capped is None:
            return None
        updated = dict(parsed)
        updated["output"] = capped
        updated["switchyard_output_filtered"] = True
        try:
            return json.dumps(updated, ensure_ascii=False)
        except (TypeError, ValueError):
            return capped

    text = _as_text(result)
    return soft_cap_text(text, soft_cap=cap)


def build_transform_tool_result_hook(*, enabled: bool):
    """Build a ``transform_tool_result`` callback (always callable; no-op when off)."""

    def on_transform_tool_result(
        tool_name: Any = None,
        args: Any = None,
        result: Any = None,
        status: Any = None,
        error: Any = None,
        error_type: Any = None,
        error_message: Any = None,
        **kwargs: Any,
    ) -> str | None:
        if enabled is not True:
            return None
        try:
            return filter_tool_result_text(
                enabled=True,
                tool_name=tool_name if tool_name is not None else kwargs.get("name"),
                result=result,
                args=args,
                status=status if status is not None else kwargs.get("status"),
                error=error,
                error_type=error_type if error_type is not None else kwargs.get("error_type"),
                error_message=(
                    error_message
                    if error_message is not None
                    else kwargs.get("error_message")
                ),
                ok=kwargs.get("ok"),
            )
        except Exception:  # noqa: BLE001 -- never break tool result delivery
            return None

    return on_transform_tool_result


def register_tool_output_filter(ctx: Any, *, enabled: bool) -> dict[str, Any]:
    """Register ``transform_tool_result`` when the Hermes hook seam exists.

    The hook is always registered when available so plugin.yaml ``provides_hooks``
    stays in sync with ``register()``. Behavior is gated by ``enabled`` (default off).
    """
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        return {
            "registered": False,
            "reason": "hermes_transform_tool_result_unavailable",
            "enabled": bool(enabled),
            "scope": "exec_soft_cap",
        }
    callback = build_transform_tool_result_hook(enabled=enabled is True)
    register_hook("transform_tool_result", callback)
    return {
        "registered": True,
        "reason": "ok",
        "enabled": enabled is True,
        "scope": "exec_soft_cap",
        "soft_cap_chars": soft_cap_chars(),
    }
