"""Plain-text rendering for local routing receipts."""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from . import receipt_history, receipt_state

_DECISION_LABELS = {
    "local_selection": "Selected locally",
    "hosted_selection": "Selected by Jev",
    "hosted_abstention": "No skill recommended",
    "hosted_failure": "Jev decision failed",
    "hosted_failure_local_fallback": "Used local fallback after Jev failed",
    "hosted_skipped": "Skipped hosted routing",
    "cache_hit": "Reused cached selection",
}


def _humanize_code(value: str) -> str:
    return value.replace("_", " ")


def format_routing_receipt(
    receipt: Any,
    *,
    history_record: Mapping[str, Any] | None = None,
) -> str:
    """Render one canonical receipt without task text or unbounded fields."""
    canonical = receipt_state.canonicalize_receipt(receipt)
    if canonical is None:
        raise ValueError("cannot render an invalid routing receipt")

    lines = ["Routing receipt"]
    if history_record is not None:
        record = dict(history_record)
        if not receipt_history.validate_history_record(record) or record["receipt"] != canonical:
            raise ValueError("cannot render invalid receipt history metadata")
        lines.append(f"Recorded: {record['recorded_at']}")
        for label, field in (("Session", "session_id"), ("Turn", "turn_id"), ("Platform", "platform")):
            value = record[field]
            if value is not None:
                lines.append(f"{label}: {value}")

    terminal_state = canonical["terminal_state"]
    decision = _DECISION_LABELS.get(terminal_state, "Recorded decision")
    lines.append(f"Decision: {decision}")
    selected = canonical["selected"]
    lines.append(f"Selected skill: {selected if selected else 'None'}")
    lines.append(f"Decision source: {_humanize_code(canonical['source'])}")

    consumer_status = canonical.get("consumer_status")
    if consumer_status == "loaded":
        source = _humanize_code(canonical["loaded_source"])
        lines.append(f"Skill load: Loaded {canonical['loaded_skill']} via {source} (verified)")
    elif consumer_status == "load_failed":
        lines.append("Skill load: Failed (not verified)")
    elif consumer_status == "explicit_override":
        lines.append("Skill load: Skipped (explicit override)")
    elif consumer_status == "mandatory_conflict":
        lines.append("Skill load: Skipped (mandatory skill conflict)")
    elif canonical["advisory_only"]:
        lines.append("Skill load: Not loaded (advisory only)")
    else:
        lines.append("Skill load: Not recorded")

    if canonical["hosted_attempted"]:
        count = canonical["request_count"]
        detail = f"{count} request" + ("s" if count != 1 else "")
        latency = canonical["total_latency_ms"]
        if latency > 0:
            detail += f" in {latency:g} ms"
        model = canonical.get("jev_model")
        if model:
            detail += f" ({model})"
        error = canonical.get("hosted_error")
        if error:
            detail += f"; error: {_humanize_code(error)}"
        lines.append(f"Jev: {detail}")
    else:
        reason = canonical.get("hosted_skip_reason") or canonical.get("bypass_reason")
        suffix = f" ({_humanize_code(reason)})" if reason else ""
        lines.append(f"Jev: Not called{suffix}")

    reason = canonical.get("abstention_reason") or canonical.get("hosted_error")
    if reason and not selected:
        lines.append(f"Reason: {_humanize_code(reason)}")
    lines.append("Task outcome: Unverified")
    return "\n".join(lines)


def format_routing_history(records: Iterable[Mapping[str, Any]]) -> str:
    """Render validated history records as separate readable receipts."""
    return "\n\n".join(
        format_routing_receipt(record.get("receipt"), history_record=record)
        for record in records
    )
