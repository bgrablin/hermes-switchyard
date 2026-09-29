"""Read-only, closed-schema summary of retained Switchyard observations."""
from __future__ import annotations

import json
import math
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import receipt_history, receipt_state
from .reasoning_effort_adapter import EFFORT_MODES, HERMES_REASONING_EFFORTS, effort_history_path

SCHEMA_VERSION = 1
COVERAGE = "Only retained plugin receipts; not all Hermes turns or Jev calls. No savings or outcomes measured."
_EFFORT_FIELDS = frozenset({
    "schema", "recorded_at", "session_id", "turn_id", "model", "mode", "requested",
    "sent", "cap", "reason_code", "jev_called", "jev_latency_ms", "confidence",
    "stakes", "stuck",
})


def _source_state(path: Path | None, *, max_records: int, max_bytes: int,
                  retained: list[dict[str, Any]]) -> str:
    """Classify a bounded history without treating skipped lines as an empty history."""
    if path is None:
        return "unavailable"
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            return "unavailable"
        with path.open("rb") as handle:
            handle.read(1)
        lines = receipt_history._read_lines(path, max_bytes)
        current = path.lstat()
    except OSError:
        return "unavailable"
    if (current.st_ino, current.st_size, current.st_mtime_ns) != (info.st_ino, info.st_size, info.st_mtime_ns):
        return "partial"
    if (info.st_size >= max_bytes or len(retained) >= max_records
            or len(lines) != len(retained) or (info.st_size and not lines)):
        return "partial"
    return "available"


def _valid_effort_record(record: dict[str, Any]) -> bool:
    """The existing reader checks timestamps; constrain fields used in aggregates."""
    if (set(record) != _EFFORT_FIELDS or type(record.get("schema")) is not int
            or record["schema"] != 1):
        return False
    mode = record.get("mode")
    if mode is not None and (type(mode) is not str or mode not in EFFORT_MODES):
        return False
    for key in ("session_id", "turn_id"):
        value = record.get(key)
        if value is not None and receipt_state.safe_identifier(value) != value:
            return False
    levels = set(HERMES_REASONING_EFFORTS)
    for key in ("cap", "sent"):
        value = record.get(key)
        if value is not None and (type(value) is not str or value not in levels):
            return False
    if type(record.get("jev_called")) is not bool:
        return False
    latency = record.get("jev_latency_ms")
    if latency is not None:
        if type(latency) not in (int, float):
            return False
        try:
            if not math.isfinite(float(latency)) or latency < 0:
                return False
        except (OverflowError, ValueError):
            return False
    return True


def build_report(*, days: int = 7, data_dir: Path | None = None, plugin_registered: bool = False,
                 now: datetime | None = None) -> dict[str, Any]:
    """Build a local report without changing receipt files or consulting a provider."""
    moment = now or datetime.now(timezone.utc)
    routing = receipt_history.history_path(data_dir)
    effort = effort_history_path(data_dir)
    window = timedelta(days=days)
    routing_retained = receipt_history.read_history(data_dir=data_dir)
    from .reasoning_effort_adapter import read_effort_history
    effort_retained = [r for r in read_effort_history(data_dir=data_dir) if _valid_effort_record(r)]
    routing_status = _source_state(routing, max_records=receipt_history.DEFAULT_MAX_RECORDS,
                                   max_bytes=receipt_history.DEFAULT_MAX_BYTES, retained=routing_retained)
    from .reasoning_effort_adapter import EFFORT_HISTORY_MAX_BYTES, EFFORT_HISTORY_MAX_RECORDS
    effort_status = _source_state(effort, max_records=EFFORT_HISTORY_MAX_RECORDS,
                                  max_bytes=EFFORT_HISTORY_MAX_BYTES, retained=effort_retained)
    if routing_status == "unavailable":
        routing_retained = []
    if effort_status == "unavailable":
        effort_retained = []
    cutoff = moment - window
    routing_records = [r for r in routing_retained
                       if (stamp := receipt_history.parse_timestamp(r["recorded_at"])) is not None and cutoff <= stamp <= moment]
    effort_records = [r for r in effort_retained
                      if (stamp := receipt_history.parse_timestamp(r["recorded_at"])) is not None and cutoff <= stamp <= moment]
    routing_n = len(routing_records)
    effort_n = len(effort_records)
    def count(value: int | None, n: int, source: str) -> dict[str, Any]:
        status = "unknown" if value is None else "partial" if source == "partial" else "observed"
        return {"count": value, "n": n, "status": status}

    receipts = [record["receipt"] for record in routing_records]
    comparable: dict[tuple[str, str], bool] = {}
    levels = {name: index for index, name in enumerate(HERMES_REASONING_EFFORTS)}
    for record in effort_records:
        session, turn = record.get("session_id"), record.get("turn_id")
        cap, sent = record.get("cap"), record.get("sent")
        if (type(session) is str and type(turn) is str and session and turn
                and type(cap) is str and type(sent) is str and cap in levels and sent in levels):
            key = (session, turn)
            comparable[key] = comparable.get(key, False) or levels[sent] < levels[cap]
    effort_incomparable = effort_n > len(comparable) and any(
        not (type(r.get("session_id")) is str and type(r.get("turn_id")) is str
             and r.get("cap") in levels and r.get("sent") in levels)
        for r in effort_records
    )

    latencies = [receipt["total_latency_ms"] for receipt in receipts
                 if receipt["hosted_attempted"] and receipt["request_count"] == 1
                 and receipt["total_latency_ms"] > 0]
    latencies.extend(record["jev_latency_ms"] for record in effort_records
                     if record.get("jev_called") is True and
                     type(record.get("jev_latency_ms")) in (int, float) and
                     record["jev_latency_ms"] >= 0)
    median = receipt_history._percentile(latencies, 0.5) if routing_status != "unavailable" and effort_status != "unavailable" else None
    if median is None:
        latencies = []

    return {
        "schema_version": SCHEMA_VERSION,
        "plugin_state": "registered_here" if plugin_registered else "not_registered_here",
        "window": {"days": days, "from": receipt_history.format_timestamp(moment - window),
                   "to": receipt_history.format_timestamp(moment)},
        "coverage": COVERAGE,
        "sources": {"routing": {"state": routing_status}, "effort": {"state": effort_status}},
        "metrics": {
            "observed_turns": count(routing_n if routing_status != "unavailable" else None, routing_n, routing_status),
            "skills_selected": count(sum(bool(r["selected"]) for r in receipts) if routing_status != "unavailable" else None, routing_n, routing_status),
            "skills_loaded": count(sum(r.get("consumer_status") == "loaded" and r.get("skill_load_verified") is True for r in receipts) if routing_status != "unavailable" else None, routing_n, routing_status),
            "below_cap_turns": count(sum(comparable.values()) if effort_status != "unavailable" and
                                     (comparable or not effort_n and effort_status == "available") else None,
                                     len(comparable), "partial" if effort_incomparable else effort_status),
            "light_turn_bypasses": count(sum(r.get("bypass_reason") in {"trivial_turn", "light_no_skill"} for r in receipts) if routing_status != "unavailable" else None, routing_n, routing_status),
            "hosted_failures": count(sum(r["terminal_state"] in {"hosted_failure", "hosted_failure_local_fallback"} for r in receipts) if routing_status != "unavailable" else None, routing_n, routing_status),
            "hosted_abstentions": count(sum(r["terminal_state"] == "hosted_abstention" for r in receipts) if routing_status != "unavailable" else None, routing_n, routing_status),
            "jev_calls": count((sum(r["request_count"] for r in receipts)
                                + sum(r.get("jev_called") is True for r in effort_records))
                               if routing_status != "unavailable" and effort_status != "unavailable" else None,
                               routing_n + effort_n, "partial" if "partial" in {routing_status, effort_status} else
                               "available" if routing_status == effort_status == "available" else "unavailable"),
            "median_latency_ms": {"value": median, "n": len(latencies),
                                  "status": "partial" if median is not None and "partial" in {routing_status, effort_status} else
                                  "observed" if median is not None else "unknown"},
        },
    }


def format_text(report: dict[str, Any]) -> str:
    """Render only named aggregate fields, never stored receipt content."""
    days = report["window"]["days"]
    sources = report["sources"]
    lines = [f"Switchyard wow: last {days}d of retained observations",
             f"Plugin: {report['plugin_state']} (not proof of other sessions)",
             f"Sources: routing {sources['routing']['state']}; effort {sources['effort']['state']}"]
    labels = (
        ("observed_turns", "Turns observed"),
        ("skills_selected", "Skills selected"),
        ("skills_loaded", "Skills loaded"),
        ("below_cap_turns", "Turns sent below cap"),
        ("light_turn_bypasses", "Light-turn bypasses"),
        ("jev_calls", "Jev calls in these two histories"),
        ("hosted_failures", "Hosted failures"),
        ("hosted_abstentions", "Hosted abstentions"),
        ("median_latency_ms", "Jev latency p50 (one-call samples, ms)"),
    )
    for name, label in labels:
        metric = report["metrics"][name]
        value = metric.get("count", metric.get("value"))
        shown = "unknown" if value is None else str(value)
        lines.append(f"{label}: {shown} (n={metric['n']}, {days}d, {metric['status']})")
    lines.append(report["coverage"])
    return "\n".join(lines)


def handle_slash(raw_args: str) -> str:
    """Read-only in-session counterpart of the CLI report."""
    usage = "Usage: /switchyard wow [--days N] [--json]"
    parts = raw_args.strip().split()
    if not parts or parts[0].lower() != "wow":
        return usage
    days, json_output = 7, False
    seen_days = False
    index = 1
    while index < len(parts):
        if parts[index] == "--json" and not json_output:
            json_output = True
        elif parts[index] == "--days" and not seen_days and index + 1 < len(parts):
            index += 1
            seen_days = True
            if not parts[index].isdecimal():
                return usage
            days = int(parts[index])
            if days <= 0 or days > 3650:
                return usage
        else:
            return usage
        index += 1
    report = build_report(days=days, plugin_registered=True)
    return json.dumps(report, sort_keys=True, allow_nan=False) if json_output else format_text(report)
