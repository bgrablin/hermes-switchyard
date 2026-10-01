"""Lossless run-length reduction of successful terminal stdout.

No semantic keep/drop choices, transcript rewrites, or compaction changes.
Unique lines, stderr, failures, and JSON fields retain their original values.
"""

from __future__ import annotations

import itertools
import json
from typing import Any

MARKER = "[Switchyard: preceding line repeated {count} additional times]"
MAX_RESULT = 256_000


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
    *, tool_name: str = "", result: Any = None, status: str = "", **_: Any
) -> str | None:
    if (
        tool_name != "terminal"
        or status not in {"ok", "success"}
        or not isinstance(result, str)
        or len(result) > MAX_RESULT
    ):
        return None
    try:
        data = json.loads(result)
    except ValueError:
        return None
    if (
        not isinstance(data, dict)
        or type(data.get("exit_code")) is not int
        or data["exit_code"] != 0
    ):
        return None
    if (
        data.get("stderr")
        or data.get("error")
        or data.get("truncated")
        or data.get("status") in {"running", "error"}
    ):
        return None
    # Native terminal currently calls this field output. Support stdout-shaped
    # providers only when exactly one of the two fields is present.
    fields = [k for k in ("output", "stdout") if isinstance(data.get(k), str)]
    if len(fields) != 1:
        return None
    field = fields[0]
    reduced, count = compact_repeated_lines(data[field])
    if not count:
        return None
    return json.dumps({**data, field: reduced}, ensure_ascii=False)
