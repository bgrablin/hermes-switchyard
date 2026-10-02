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
import threading
from collections import OrderedDict
from typing import Any, Mapping

from .reasoning_effort_adapter import classify_tool_kind, derive_tool_failure

# Bounded per-process map: session/task key -> latest user message for this turn.
# transform_tool_result does not receive the user request; pre_llm_call does.
_USER_TEXT_LIMIT = 64
_user_text_lock = threading.Lock()
_latest_user_text: OrderedDict[str, str] = OrderedDict()

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
    "middle content is unavailable — ask for a full dump to keep verbatim] …\n\n"
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


def _scope_key(*, session_id: Any = None, task_id: Any = None) -> str:
    # Prefer task_id so delegated/background siblings sharing a session stay isolated
    # (matches reasoning_effort_adapter capture scope).
    for value in (task_id, session_id):
        if isinstance(value, str) and value.strip():
            return value.strip()
        if value is not None and not isinstance(value, (bytes, bytearray)):
            text = str(value).strip()
            if text:
                return text
    return "default"


def _clean_user_text(value: Any) -> str | None:
    """Return text from a Hermes user_message, including multimodal text parts."""
    if isinstance(value, str):
        return value
    if not isinstance(value, (list, tuple)):
        return None
    parts: list[str] = []
    for block in value:
        if isinstance(block, Mapping):
            kind = block.get("type")
            text = block.get("text")
            if kind in ("text", "input_text") and isinstance(text, str):
                parts.append(text)
    return "\n".join(parts) if parts else None


def note_user_text(
    user_message: Any,
    *,
    session_id: Any = None,
    task_id: Any = None,
) -> None:
    """Store the turn's user message for later full-dump preserve checks."""
    cleaned_raw = _clean_user_text(user_message)
    if cleaned_raw is None:
        return
    cleaned = cleaned_raw.strip()
    if not cleaned:
        return
    # Inspect the complete request before bounding storage. A preservation cue
    # after pasted context has the same authority as one at the start.
    stored = "full dump" if _FULL_DUMP_ASK_RE.search(cleaned) else cleaned[:4_000]
    key = _scope_key(session_id=session_id, task_id=task_id)
    with _user_text_lock:
        _latest_user_text[key] = stored
        _latest_user_text.move_to_end(key)
        while len(_latest_user_text) > _USER_TEXT_LIMIT:
            _latest_user_text.popitem(last=False)


def latest_user_text(*, session_id: Any = None, task_id: Any = None) -> str:
    """Return the latest captured user message for this session/task, if any."""
    key = _scope_key(session_id=session_id, task_id=task_id)
    with _user_text_lock:
        value = _latest_user_text.get(key)
        if value:
            return value
        # Fall back to task-only / session-only when the other id was used at capture.
        for alt in (session_id, task_id):
            alt_key = _scope_key(session_id=alt, task_id=None)
            if alt_key != key and alt_key in _latest_user_text:
                return _latest_user_text[alt_key]
    return ""


def clear_user_text_for_tests() -> None:
    """Test helper: drop captured user text."""
    with _user_text_lock:
        _latest_user_text.clear()


def user_asks_full_dump(
    *,
    args: Any = None,
    result: Any = None,
    user_text: Any = None,
    session_id: Any = None,
    task_id: Any = None,
) -> bool:
    """True when the user turn or tool args request an untruncated dump.

    ``transform_tool_result`` does not receive the user message; callers should
    pass ``user_text`` or rely on ``note_user_text`` from ``pre_llm_call``.
    """
    captured = user_text if isinstance(user_text, str) else latest_user_text(
        session_id=session_id, task_id=task_id
    )
    blob = f"{captured}\n{_args_text(args)}\n{_as_text(result)[:500]}"
    return bool(_FULL_DUMP_ASK_RE.search(blob))


def looks_security_or_failure_relevant(text: str) -> bool:
    """Conservative preserve: do not soft-cap security- or failure-shaped text.

    Scan the complete payload. Soft-cap must never omit a security/failure marker
    that falls outside a sampled window.
    """
    if not text:
        return False
    return bool(_SECURITY_OR_FAILURE_RE.search(text))


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


def _structured_output_field(result: Any) -> tuple[Any, str, str] | None:
    """If result is JSON with exactly one of ``output``/``stdout`` string, return it.

    Matches ``output_pruning``: rewrite only the selected field and keep the rest
    of the envelope (exit metadata, stderr, …) intact.
    """
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
    fields = [key for key in ("output", "stdout") if isinstance(parsed.get(key), str)]
    if len(fields) != 1:
        return None
    field = fields[0]
    return parsed, field, parsed[field]


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
    user_text: Any = None,
    session_id: Any = None,
    task_id: Any = None,
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

    if user_asks_full_dump(
        args=args,
        result=result,
        user_text=user_text,
        session_id=session_id,
        task_id=task_id,
    ):
        return False

    # Measure the disposable payload (structured output field when present).
    structured = _structured_output_field(result)
    if structured is None:
        # A missing recognized stdout field does not make an envelope plain text.
        # Preserve process lists, multiple output fields, and unfamiliar schemas.
        # Slicing their serialized representation would corrupt JSON and metadata.
        if not isinstance(result, str) or result.lstrip().startswith(("{", "[")):
            return False
        try:
            json.loads(result)
        except ValueError:
            pass
        else:
            return False
    payload = structured[2] if structured is not None else result
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
    user_text: Any = None,
    session_id: Any = None,
    task_id: Any = None,
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
        user_text=user_text,
        session_id=session_id,
        task_id=task_id,
    ):
        return None

    cap = soft_cap_chars() if soft_cap is None else max(64, int(soft_cap))
    structured = _structured_output_field(result)
    if structured is not None:
        parsed, field, output = structured
        capped = soft_cap_text(output, soft_cap=cap)
        if capped is None:
            return None
        updated = dict(parsed)
        updated[field] = capped
        updated["switchyard_output_filtered"] = True
        try:
            return json.dumps(updated, ensure_ascii=False)
        except (TypeError, ValueError):
            # Fail open: keep the original envelope rather than dropping exit metadata.
            return None

    text = _as_text(result)
    return soft_cap_text(text, soft_cap=cap)


def build_pre_llm_call_capture_hook():
    """Build a ``pre_llm_call`` observer that stores the turn's user message."""

    def on_pre_llm_call(
        user_message: Any = None,
        session_id: Any = None,
        task_id: Any = None,
        **_kwargs: Any,
    ) -> None:
        try:
            note_user_text(user_message, session_id=session_id, task_id=task_id)
        except Exception:  # noqa: BLE001 -- capture never breaks a turn
            return None
        return None

    return on_pre_llm_call


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
        session_id: Any = None,
        task_id: Any = None,
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
                session_id=session_id if session_id is not None else kwargs.get("session_id"),
                task_id=task_id if task_id is not None else kwargs.get("task_id"),
            )
        except Exception:  # noqa: BLE001 -- never break tool result delivery
            return None

    return on_transform_tool_result


def register_tool_output_filter(ctx: Any, *, enabled: bool) -> dict[str, Any]:
    """Register ``transform_tool_result`` only when the opt-in flag is on.

    Default off adds no listener (matches other optional tool hooks). When on,
    also register ``pre_llm_call`` to capture the turn's user message for
    full-dump preserve. Plugin-only — no Hermes core changes. Callers that also
    enable repeated-output compaction or stuck advice should compose one
    ``transform_tool_result`` listener themselves (Hermes first-string-wins).
    """
    register_hook = getattr(ctx, "register_hook", None)
    if not callable(register_hook):
        return {
            "registered": False,
            "reason": "hermes_transform_tool_result_unavailable",
            "enabled": bool(enabled),
            "scope": "exec_soft_cap",
            "pre_llm_call_capture": False,
        }
    if enabled is not True:
        return {
            "registered": False,
            "reason": "disabled",
            "enabled": False,
            "scope": "exec_soft_cap",
            "pre_llm_call_capture": False,
            "soft_cap_chars": soft_cap_chars(),
        }
    callback = build_transform_tool_result_hook(enabled=True)
    register_hook("transform_tool_result", callback)
    capture_registered = False
    try:
        register_hook("pre_llm_call", build_pre_llm_call_capture_hook())
        capture_registered = True
    except Exception:  # noqa: BLE001 -- filter still works with args-only cues
        capture_registered = False
    return {
        "registered": True,
        "reason": "ok",
        "enabled": True,
        "scope": "exec_soft_cap",
        "soft_cap_chars": soft_cap_chars(),
        "pre_llm_call_capture": capture_registered,
    }
