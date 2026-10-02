"""Lossless run-length reduction of successful terminal stdout.

No semantic keep/drop choices, transcript rewrites, or compaction changes.
Unique lines, stderr, failures, and JSON fields retain their original values.
"""

from __future__ import annotations

import itertools
import json
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

MARKER = "[Switchyard: preceding line repeated {count} additional times]"
MAX_RESULT = 256_000
_FULL_OUTPUT = re.compile(
    r"\b(?:full\s+(?:dump|output|stdout)|(?:output|stdout)\s+in\s+full|verbatim|untruncated|"
    r"do\s+not\s+(?:truncate|compress|compact)|no\s+(?:truncation|compaction)|"
    r"(?:entire|whole|complete|raw|exact|unaltered|unmodified)\s+(?:output|stdout)|"
    r"show\s+(?:me\s+)?(?:everything|all\s+output))\b",
    re.IGNORECASE,
)


def _preserve_request(value: Any) -> bool:
    if isinstance(value, str):
        return not value.strip() or bool(_FULL_OUTPUT.search(value))
    if isinstance(value, (list, tuple)):
        texts = []
        for block in value:
            if (
                not isinstance(block, Mapping)
                or block.get("type") not in ("text", "input_text")
                or not isinstance(block.get("text"), str)
            ):
                return True
            texts.append(block["text"])
        return _preserve_request("\n".join(texts))
    # Uninspectable requests cannot authorize a model-facing rewrite.
    return True


class OutputCompactionGuard:
    """Keep only bounded preservation decisions, isolated by session and task.

    Missing, expired, or mismatched captures leave the result unchanged. No user
    text is retained, sent to a provider, or persisted by this observer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._turns: OrderedDict[tuple[str, ...], tuple[str, bool, float]] = OrderedDict()

    @staticmethod
    def _identity(value: Any) -> str | None:
        return value if isinstance(value, str) and value.strip() and len(value) <= 128 else None

    @staticmethod
    def _scope(session_id: Any, task_id: Any) -> tuple[str, ...] | None:
        session = OutputCompactionGuard._identity(session_id)
        task = OutputCompactionGuard._identity(task_id)
        if session_id not in (None, "") and session is None:
            return None
        if task_id not in (None, "") and task is None:
            return None
        if task:
            return ("task", session or "", task)
        return ("session", session) if session else None

    def capture(
        self, *, user_message: Any = None, session_id: Any = None,
        task_id: Any = None, turn_id: Any = None, **_: Any,
    ) -> None:
        scope = self._scope(session_id, task_id)
        turn = self._identity(turn_id)
        if scope is None or turn is None:
            return
        preserve = _preserve_request(user_message)
        with self._lock:
            self._turns[scope] = (turn, preserve, time.monotonic())
            self._turns.move_to_end(scope)
            while len(self._turns) > 128:
                self._turns.popitem(last=False)

    def prune(
        self, *, session_id: Any = None, task_id: Any = None,
        turn_id: Any = None, args: Any = None, **kwargs: Any,
    ) -> str | None:
        if isinstance(args, Mapping) and any(
            _FULL_OUTPUT.search(value)
            for key in ("command", "cmd", "script", "code", "input", "query")
            if isinstance(value := args.get(key), str)
        ):
            return None
        scope = self._scope(session_id, task_id)
        with self._lock:
            capture = self._turns.get(scope) if scope is not None else None
        if capture is None:
            return None
        captured_turn, preserve, captured_at = capture
        if preserve or captured_turn != turn_id or time.monotonic() - captured_at > 600:
            return None
        return prune_terminal_result(**kwargs)


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    data: dict[str, Any] = {}
    for key, value in pairs:
        if key in data:
            raise ValueError("ambiguous result envelope")
        data[key] = value
    return data


def _valid_result_status(value: Any) -> bool:
    return isinstance(value, str) and value in {"ok", "success", "completed"}


def compact_repeated_lines(text: str) -> tuple[str, int]:
    """Collapse only consecutive identical complete lines, keeping multiplicity."""
    if len(text) < 4000 or len(text) > MAX_RESULT or "[Switchyard:" in text:
        return text, 0
    output: list[str] = []
    removed = 0
    for line, values in itertools.groupby(text.splitlines(keepends=True)):
        count = sum(1 for _ in values)
        if count >= 4 and line.endswith("\n") and len(line.strip()) >= 8:
            replacement = line + MARKER.format(count=count - 1) + "\n"
            if len(replacement) < len(line) * count:
                output.append(replacement)
                removed += count - 1
                continue
        output.append(line * count)
    result = "".join(output)
    return (result, removed) if len(result) < len(text) else (text, 0)


def prune_terminal_result(
    *, tool_name: str = "", result: Any = None, status: str = "",
    error: Any = None, error_type: Any = None, error_message: Any = None,
    ok: Any = None, **_: Any,
) -> str | None:
    if (
        tool_name != "terminal"
        or not isinstance(status, str) or status not in {"ok", "success"}
        or not isinstance(result, str)
        or len(result) > MAX_RESULT
        or any(value is not None and (not isinstance(value, str) or value != "")
               for value in (error, error_type, error_message))
        or ok is not None and ok is not True
    ):
        return None
    try:
        data = json.loads(result, object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        return None
    if (
        not isinstance(data, dict)
        or type(data.get("exit_code")) is not int
        or data["exit_code"] != 0
    ):
        return None
    if (
        any(key in data and (not isinstance(data[key], str) or data[key] != "")
            for key in ("stderr", "error", "error_type", "error_message"))
        or "truncated" in data and data["truncated"] is not False
        or any(key in data and data[key] is not True for key in ("ok", "success"))
        or not _valid_result_status(data.get("status", "ok"))
        or any(
            key in data and (type(data[key]) is not int or data[key] != 0)
            for key in ("returncode", "exitcode")
        )
    ):
        return None
    # Native terminal currently calls this field output. Support stdout-shaped
    # providers only when exactly one of the two fields is present.
    fields = [k for k in ("output", "stdout") if k in data]
    if len(fields) != 1 or not isinstance(data[fields[0]], str):
        return None
    field = fields[0]
    reduced, count = compact_repeated_lines(data[field])
    if not count:
        return None
    try:
        replacement = json.dumps({**data, field: reduced}, ensure_ascii=False, allow_nan=False)
        replacement.encode("utf-8")
        return replacement
    except (TypeError, ValueError, RecursionError):
        return None
