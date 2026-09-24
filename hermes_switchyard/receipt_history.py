"""Bounded per-session receipt history, routing statistics, and git source SHA.

The history is a profile-owned JSONL file next to the latest-receipt file.
Each line holds one canonical routing receipt plus sanitized turn metadata:
``session_id``, ``turn_id``, ``platform``, and a UTC ``recorded_at`` time.
Task text, conversation history, candidate descriptions, and provider text
are never stored: the only receipt accepted is the canonical receipt that
``receipt_state.canonicalize_receipt`` validates, and every metadata field
passes a closed-form sanitizer or becomes ``None``.

The file is bounded by record count and byte size. Ordinary turns append one
line under an advisory lock without an fsync on the hook's critical path.
Corruption, duplicate turns, and limit crossings compact the retained tail
to a private temporary file and publish it with an atomic ``os.replace``.

The git source SHA is read from ``.git`` files directly (``HEAD``, loose refs,
and ``packed-refs``) with no subprocess. Linked worktrees (``.git`` is a file
that names a gitdir with a ``commondir``) are supported.
"""
from __future__ import annotations

import functools
import json
import math
import os
import re
import stat
import tempfile
import time
from collections import Counter
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import receipt_state

HISTORY_FILE_NAME = "receipt-history.jsonl"
HISTORY_LOCK_NAME = "receipt-history.lock"
HISTORY_SCHEMA = 1
DEFAULT_MAX_RECORDS = 500
DEFAULT_MAX_BYTES = 1024 * 1024
# A single record is small; anything larger is malformed or hostile.
MAX_RECORD_BYTES = 16 * 1024
LOCK_TIMEOUT_SECONDS = 1.0

HISTORY_RECORD_FIELDS = frozenset(
    {"schema", "recorded_at", "session_id", "turn_id", "platform", "receipt"}
)
PLATFORM_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
TIMESTAMP_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")
SINCE_RE = re.compile(r"^([1-9][0-9]{0,5})([smhdw])$")
_SINCE_UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days", "w": "weeks"}


# Git parsing limits. Refs and HEAD are tiny; packed-refs can be large on big
# repositories but a plugin checkout stays well below this bound.
_GIT_SMALL_FILE_LIMIT = 4096
_GIT_PACKED_REFS_LIMIT = 8 * 1024 * 1024
_GIT_MAX_SYMREF_DEPTH = 5
_GIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_GIT_REF_RE = re.compile(r"^refs/[A-Za-z0-9._/+-]{1,240}$")


# ---------------------------------------------------------------------------
# Sanitizers
# ---------------------------------------------------------------------------


def sanitize_session_id(value: Any) -> str | None:
    """Return a bounded ASCII session identifier, otherwise None."""
    return receipt_state.safe_identifier(value)


def sanitize_turn_id(value: Any) -> str | None:
    """Return a bounded ASCII turn identifier, otherwise None."""
    return receipt_state.safe_identifier(value)


def sanitize_platform(value: Any) -> str | None:
    """Return a short lowercase platform name such as ``cli`` or ``discord``."""
    if type(value) is not str:
        return None
    candidate = value.strip().lower()
    return candidate if PLATFORM_RE.fullmatch(candidate) else None


def format_timestamp(moment: datetime) -> str:
    """Return a second-precision UTC timestamp in ``YYYY-MM-DDTHH:MM:SSZ`` form."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_timestamp(value: Any) -> datetime | None:
    """Parse a timestamp written by :func:`format_timestamp`, otherwise None."""
    if type(value) is not str or not TIMESTAMP_RE.fullmatch(value):
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def parse_since(value: Any) -> timedelta | None:
    """Parse a window such as ``30m``, ``24h``, ``7d``, or ``2w``."""
    if type(value) is not str:
        return None
    match = SINCE_RE.fullmatch(value.strip())
    if match is None:
        return None
    try:
        return timedelta(**{_SINCE_UNITS[match.group(2)]: int(match.group(1))})
    except OverflowError:
        return None


# ---------------------------------------------------------------------------
# Record construction and validation
# ---------------------------------------------------------------------------


def build_history_record(
    receipt: Any,
    *,
    session_id: Any = None,
    turn_id: Any = None,
    platform: Any = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return one sanitized history record, or None for an invalid receipt."""
    canonical = receipt_state.canonicalize_receipt(receipt)
    if canonical is None:
        return None
    return {
        "schema": HISTORY_SCHEMA,
        "recorded_at": format_timestamp(now or datetime.now(timezone.utc)),
        "session_id": sanitize_session_id(session_id),
        "turn_id": sanitize_turn_id(turn_id),
        "platform": sanitize_platform(platform),
        "receipt": canonical,
    }


def validate_history_record(record: Any) -> bool:
    """Validate one history record exactly as stored."""
    if not isinstance(record, dict) or set(record) != HISTORY_RECORD_FIELDS:
        return False
    if record["schema"] != HISTORY_SCHEMA or type(record["schema"]) is not int:
        return False
    if parse_timestamp(record["recorded_at"]) is None:
        return False
    for key, sanitizer in (
        ("session_id", sanitize_session_id),
        ("turn_id", sanitize_turn_id),
        ("platform", sanitize_platform),
    ):
        value = record[key]
        if value is not None and sanitizer(value) != value:
            return False
    receipt = record["receipt"]
    return receipt_state.canonicalize_receipt(receipt) == receipt


def _encode(record: Mapping[str, Any]) -> str:
    return json.dumps(record, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------


def history_path(data_dir: Path | str | None = None) -> Path | None:
    """Return the profile-owned history path, never the installed source tree."""
    if data_dir is not None:
        return Path(data_dir) / HISTORY_FILE_NAME
    resolved = receipt_state._plugin_data_dir()
    return resolved / HISTORY_FILE_NAME if resolved is not None else None


class _HistoryLock:
    """Bounded-wait advisory lock on a sidecar file. Never blocks a turn long."""

    def __init__(self, path: Path, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self._path = path
        self._timeout = timeout
        self._fd: int | None = None

    def __enter__(self) -> bool:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._fd = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            self._fd = None
            return False
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(self._fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    return False
                time.sleep(0.01)

    def __exit__(self, *_: Any) -> None:
        if self._fd is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            os.close(self._fd)
            self._fd = None


def _read_lines(path: Path, max_bytes: int) -> list[str]:
    """Read at most the last ``max_bytes`` of complete lines from ``path``."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            start = max(0, size - max_bytes)
            handle.seek(start)
            data = handle.read(max_bytes)
    except OSError:
        return []
    if start > 0:
        # Drop the leading partial line created by the tail read.
        newline = data.find(b"\n")
        data = data[newline + 1 :] if newline >= 0 else b""
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        text = data.decode("ascii", errors="replace")
    return [line for line in text.split("\n") if line]


def _parse_records(lines: Iterable[str]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in lines:
        if len(line) > MAX_RECORD_BYTES:
            continue
        try:
            record = json.loads(line)
            valid = validate_history_record(record)
        except (ValueError, RecursionError):
            continue
        if valid:
            records.append(record)
    return records


def _trim(lines: list[str], max_records: int, max_bytes: int) -> list[str]:
    lines = lines[-max_records:] if max_records > 0 else []
    total = sum(len(line) + 1 for line in lines)
    index = 0
    while index < len(lines) and total > max_bytes:
        total -= len(lines[index]) + 1
        index += 1
    return lines[index:]


def _write_lines(path: Path, lines: list[str]) -> bool:
    temporary: Path | None = None
    descriptor: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(prefix=".receipt-history-", suffix=".tmp", dir=path.parent)
        temporary = Path(raw_path)
        receipt_state._apply_private_permissions(temporary)
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            descriptor = None  # The file object now owns and closes it.
            for line in lines:
                handle.write(line)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    except Exception:  # noqa: BLE001 -- history is diagnostic; never break a turn
        return False
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def _append_line(path: Path, line: str, before: os.stat_result) -> bool:
    """Append only to the regular file inspected under the history lock.

    This best-effort diagnostic write does not fsync every turn. A partial
    write is reported as a failure and repaired on the next compaction.
    """
    flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return False
    try:
        opened = os.fstat(fd)
        current = path.lstat()
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            return False
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return False
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            return False
        if os.name != "nt" and opened.st_mode & 0o077:
            return False
        payload = (line + "\n").encode("ascii")
        return os.write(fd, payload) == len(payload)
    except OSError:
        return False
    finally:
        os.close(fd)


def _ends_with_newline(path: Path, size: int) -> bool:
    if size == 0:
        return True
    try:
        with open(path, "rb") as handle:
            handle.seek(-1, os.SEEK_END)
            return handle.read(1) == b"\n"
    except OSError:
        return False


def append_receipt_history(
    receipt: Any,
    *,
    session_id: Any = None,
    turn_id: Any = None,
    platform: Any = None,
    now: datetime | None = None,
    data_dir: Path | str | None = None,
    max_records: int = DEFAULT_MAX_RECORDS,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> bool:
    """Append one sanitized record and compact the bounded history as needed.

    A later record for the same ``(session_id, turn_id)`` pair replaces the
    earlier one, so repeated terminal writes for one turn count once. Returns
    False (never raises) when the receipt is invalid or storage fails.
    """
    try:
        record = build_history_record(
            receipt, session_id=session_id, turn_id=turn_id, platform=platform, now=now
        )
        if record is None:
            return False
        path = history_path(data_dir)
        if path is None:
            return False
        encoded = _encode(record)
        if len(encoded) > MAX_RECORD_BYTES or len(encoded) + 1 > max_bytes:
            return False
        with _HistoryLock(path.with_name(HISTORY_LOCK_NAME)) as locked:
            if not locked:
                return False
            try:
                before = path.lstat()
            except FileNotFoundError:
                before = None
            if before is not None and (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1):
                return False
            raw_lines = _read_lines(path, max_bytes)
            existing = _parse_records(raw_lines)
            key = (record["session_id"], record["turn_id"])
            duplicate = key[0] is not None and key[1] is not None and any(
                (item["session_id"], item["turn_id"]) == key for item in existing
            )
            if duplicate:
                existing = [
                    item for item in existing if (item["session_id"], item["turn_id"]) != key
                ]
            if before is None:
                compact = True
            else:
                compact = (
                    duplicate
                    or len(raw_lines) != len(existing)
                    or len(existing) + 1 > max_records
                    or before.st_size + len(encoded) + 1 > max_bytes
                    or not _ends_with_newline(path, before.st_size)
                    or (os.name != "nt" and bool(before.st_mode & 0o077))
                )
            if not compact and before is not None:
                return _append_line(path, encoded, before)
            lines = [_encode(item) for item in existing]
            lines.append(encoded)
            return _write_lines(path, _trim(lines, max_records, max_bytes))
    except Exception:  # noqa: BLE001 -- history is diagnostic; never break a turn
        return False


def record_turn_receipt(
    receipt: Any,
    *,
    session_id: Any = None,
    turn_id: Any = None,
    platform: Any = None,
) -> bool:
    """Hook-facing entry point: append one turn receipt, never raising."""
    return append_receipt_history(
        receipt, session_id=session_id, turn_id=turn_id, platform=platform
    )


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def read_history(
    *,
    data_dir: Path | str | None = None,
    session_id: str | None = None,
    platform: str | None = None,
    since: timedelta | None = None,
    last: int | None = None,
    now: datetime | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> list[dict[str, Any]]:
    """Return valid records, oldest first, filtered and optionally limited.

    ``last`` keeps the newest N records after filtering. Invalid lines are
    skipped, never repaired or surfaced.
    """
    path = history_path(data_dir)
    if path is None:
        return []
    try:
        info = path.lstat()
    except OSError:
        return []
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return []
    records = _parse_records(_read_lines(path, max_bytes))
    if session_id is not None:
        wanted = sanitize_session_id(session_id)
        if wanted is None:
            return []
        records = [item for item in records if item["session_id"] == wanted]
    if platform is not None:
        wanted_platform = sanitize_platform(platform)
        if wanted_platform is None:
            return []
        records = [item for item in records if item["platform"] == wanted_platform]
    if since is not None:
        cutoff = (now or datetime.now(timezone.utc)) - since
        records = [
            item for item in records if (parse_timestamp(item["recorded_at"]) or cutoff) >= cutoff
        ]
    if last is not None:
        if type(last) is not int or last <= 0:
            return []
        records = records[-last:]
    return records


def session_receipts(session_id: str, **kwargs: Any) -> list[dict[str, Any]]:
    """Return every retained record for one session, oldest first."""
    return read_history(session_id=session_id, **kwargs)


def last_receipts(count: int, **kwargs: Any) -> list[dict[str, Any]]:
    """Return the newest ``count`` retained records, oldest first."""
    return read_history(last=count, **kwargs)


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


def _percentile(values: list[float], fraction: float) -> float | None:
    """Nearest-rank percentile; None for an empty sample."""
    if not values:
        return None
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _turn_cost(receipt: Mapping[str, Any]) -> float | None:
    """Return a known cost, 0.0 when no hosted request ran, or None if unknown."""
    usage = receipt.get("total_usage")
    if not isinstance(usage, Mapping):
        return None
    cost = usage.get("cost")
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        return float(cost)
    if "cost" not in usage and receipt.get("request_count") == 0:
        return 0.0
    return None


def compute_stats(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize routing outcomes over already-filtered history records.

    Rates are fractions rounded to four places, or None when the denominator
    is zero. Cost totals include only turns whose cost is known; the number
    of unknown-cost turns is reported so a partial total is never presented
    as complete.
    """
    items = [item for item in records if isinstance(item, Mapping) and isinstance(item.get("receipt"), Mapping)]
    receipts = [item["receipt"] for item in items]
    turns = len(receipts)
    terminal_states = Counter(str(r.get("terminal_state")) for r in receipts)
    sources = Counter(str(r.get("source")) for r in receipts)
    # A selection is any turn that ended with a skill, including a cache hit
    # or a local fallback after a hosted failure. An abstention is the hosted
    # model declining to pick; no_selections counts every turn without a skill.
    selections = sum(1 for r in receipts if r.get("selected"))
    no_selections = turns - selections
    abstentions = terminal_states.get("hosted_abstention", 0)
    hosted_attempts = sum(1 for r in receipts if r.get("hosted_attempted") is True)
    hosted_successes = sum(1 for r in receipts if r.get("hosted_succeeded") is True)
    failure_codes = Counter(
        str(r.get("hosted_error_detail") or r["hosted_error"])
        for r in receipts if r.get("hosted_error_detail") or r.get("hosted_error")
    )
    skip_reasons = Counter(str(r["hosted_skip_reason"]) for r in receipts if r.get("hosted_skip_reason"))
    abstention_reasons = Counter(
        str(r["abstention_reason"]) for r in receipts if not r.get("selected") and r.get("abstention_reason")
    )
    consumer = Counter(str(r["consumer_status"]) for r in receipts if r.get("consumer_status"))
    consumer_total = sum(consumer.values())
    latencies = [
        float(r["total_latency_ms"])
        for r in receipts
        if type(r.get("total_latency_ms")) in (int, float) and math.isfinite(float(r["total_latency_ms"]))
    ]
    hosted_latencies = [
        float(r["total_latency_ms"]) for r in receipts if r.get("hosted_attempted") is True
        and type(r.get("total_latency_ms")) in (int, float)
    ]
    requests = sum(r["request_count"] for r in receipts if type(r.get("request_count")) is int)
    costs = [_turn_cost(r) for r in receipts]
    known_costs = [cost for cost in costs if cost is not None]
    total_cost = round(sum(known_costs), 8) if known_costs else (0.0 if turns == 0 else None)
    timestamps: list[str] = [
        str(item["recorded_at"]) for item in items if parse_timestamp(item.get("recorded_at")) is not None
    ]
    sessions = {item.get("session_id") for item in items if item.get("session_id")}
    platforms = Counter(str(item.get("platform") or "unknown") for item in items)
    return {
        "turns": turns,
        "sessions": len(sessions),
        "first_recorded_at": min(timestamps) if timestamps else None,
        "last_recorded_at": max(timestamps) if timestamps else None,
        "platforms": dict(sorted(platforms.items())),
        "terminal_states": dict(sorted(terminal_states.items())),
        "sources": dict(sorted(sources.items())),
        "selections": selections,
        "selection_rate": _rate(selections, turns),
        "no_selections": no_selections,
        "no_selection_rate": _rate(no_selections, turns),
        "abstentions": abstentions,
        "abstention_rate": _rate(abstentions, turns),
        "abstention_reasons": dict(sorted(abstention_reasons.items())),
        "hosted_attempts": hosted_attempts,
        "hosted_attempt_rate": _rate(hosted_attempts, turns),
        "hosted_successes": hosted_successes,
        "hosted_failures": sum(failure_codes.values()),
        "hosted_failure_rate": _rate(sum(failure_codes.values()), hosted_attempts),
        "hosted_failures_by_code": dict(sorted(failure_codes.items())),
        "hosted_skip_reasons": dict(sorted(skip_reasons.items())),
        "consumer_statuses": dict(sorted(consumer.items())),
        "skill_loads": consumer.get("loaded", 0),
        "skill_load_rate": _rate(consumer.get("loaded", 0), consumer_total),
        "latency_ms_p50": _percentile(latencies, 0.50),
        "latency_ms_p95": _percentile(latencies, 0.95),
        "hosted_latency_ms_p50": _percentile(hosted_latencies, 0.50),
        "hosted_latency_ms_p95": _percentile(hosted_latencies, 0.95),
        "requests": requests,
        "requests_per_turn": round(requests / turns, 4) if turns else None,
        "cost_total_known": total_cost,
        "cost_known_turns": len(known_costs),
        "cost_unknown_turns": turns - len(known_costs),
        "cost_per_turn_known": round(sum(known_costs) / len(known_costs), 8) if known_costs else None,
    }


def routing_stats(
    *,
    since: timedelta | None = None,
    session_id: str | None = None,
    platform: str | None = None,
    data_dir: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Read the retained history and return :func:`compute_stats` for it."""
    records = read_history(
        data_dir=data_dir, session_id=session_id, platform=platform, since=since, now=now
    )
    stats = compute_stats(records)
    stats["window_seconds"] = int(since.total_seconds()) if since is not None else None
    return stats


# ---------------------------------------------------------------------------
# Git source SHA (no subprocess)
# ---------------------------------------------------------------------------


def _read_small(path: Path, limit: int = _GIT_SMALL_FILE_LIMIT) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _safe_ref_name(ref: str) -> bool:
    if not _GIT_REF_RE.fullmatch(ref):
        return False
    parts = ref.split("/")
    return all(part not in ("", ".", "..") and not part.endswith(".lock") for part in parts)


def _resolve_git_dir(root: Path) -> Path | None:
    dot_git = root / ".git"
    if dot_git.is_dir():
        return dot_git
    content = _read_small(dot_git)
    if content is None:
        return None
    first = content.splitlines()[0].strip() if content.splitlines() else ""
    if not first.startswith("gitdir:"):
        return None
    target = first[len("gitdir:") :].strip()
    if not target:
        return None
    git_dir = Path(target)
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    return git_dir if git_dir.is_dir() else None


def _common_dir(git_dir: Path) -> Path:
    content = _read_small(git_dir / "commondir")
    if content is None or not content.strip():
        return git_dir
    common = Path(content.strip())
    if not common.is_absolute():
        common = git_dir / common
    return common if common.is_dir() else git_dir


def _packed_ref(common_dir: Path, ref: str) -> str | None:
    content = _read_small(common_dir / "packed-refs", _GIT_PACKED_REFS_LIMIT)
    if content is None:
        return None
    for line in content.splitlines():
        if not line or line[0] in "#^":
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref and _GIT_SHA_RE.fullmatch(sha):
            return sha
    return None


def resolve_git_source_sha(repo_dir: Path | str | None = None) -> str:
    """Return the checked-out commit SHA from ``.git`` files, or ``unavailable``.

    Reads ``HEAD``; follows a symbolic ref through the worktree gitdir, the
    common dir, and ``packed-refs``. Never spawns ``git``. SHA-256 object
    names and malformed data return the unavailable sentinel.
    """
    unavailable = receipt_state.RECEIPT_SOURCE_SHA_UNAVAILABLE
    try:
        root = receipt_state._plugin_root(repo_dir)
        git_dir = _resolve_git_dir(root)
        if git_dir is None:
            return unavailable
        common_dir = _common_dir(git_dir)
        content = _read_small(git_dir / "HEAD")
        for _ in range(_GIT_MAX_SYMREF_DEPTH):
            if content is None:
                return unavailable
            value = content.strip()
            if _GIT_SHA_RE.fullmatch(value):
                return value
            if not value.startswith("ref:"):
                return unavailable
            ref = value[len("ref:") :].strip()
            if not _safe_ref_name(ref):
                return unavailable
            content = _read_small(git_dir.joinpath(*ref.split("/")))
            if content is None and common_dir != git_dir:
                content = _read_small(common_dir.joinpath(*ref.split("/")))
            if content is None:
                packed = _packed_ref(common_dir, ref)
                return packed if packed is not None else unavailable
        return unavailable
    except (OSError, ValueError):
        return unavailable


def resolve_receipt_source_sha(repo_dir: Path | str | None = None) -> str:
    """Prefer the release manifest SHA; fall back to the git checkout SHA."""
    manifest_sha = receipt_state.resolve_source_sha(repo_dir)
    if manifest_sha != receipt_state.RECEIPT_SOURCE_SHA_UNAVAILABLE:
        return manifest_sha
    return resolve_git_source_sha(repo_dir)


@functools.lru_cache(maxsize=1)
def process_source_sha() -> str:
    """Return the source SHA once per process.

    Caching binds the SHA to the code that was imported. A later
    ``hermes plugins update`` changes ``.git/HEAD`` on disk, but the running
    process still executes the old code and must keep reporting its SHA.
    """
    return resolve_receipt_source_sha()
