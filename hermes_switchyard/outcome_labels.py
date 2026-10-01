"""Offline, local-only, evidence-censored labels from retained routing receipts.

This module is not imported by plugin registration or any runtime hook. Raw
next-user text exists only in the caller's ephemeral input; output is closed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

from . import receipt_history, receipt_state

SCHEMA = "switchyard-outcome-label/1"
UNKNOWN = "UNKNOWN"
FIELDS = (
    "skill_loaded_after_selection", "tool_error_in_turn",
    "next_turn_user_correction", "turn_completed",
)
_EVIDENCE_FIELDS = frozenset({
    "session_id", "turn_id", "turn_completed", "tool_trace_complete",
    "tool_events", "assistant_message_id", "user_message", "retried",
    "undone", "reactions",
})
_CORRECTION = re.compile(
    r"^(?:no,\s+i asked (?:for|you to)\b|"
    r"that's not what i asked (?:for|you to)\b|"
    r"(?P<complete>you (?:misread|misunderstood) my request\b))", re.IGNORECASE,
)
_AMBIGUOUS = re.compile(
    r"\b(?:no|wrong|incorrect|mistake|i asked|instead|retry|undo|try again|i meant|actually)\b",
    re.IGNORECASE,
)


def split_for_turn(turn_id: str) -> str:
    """Return a stable 20% offline holdout by turn ID alone."""
    if receipt_history.sanitize_turn_id(turn_id) != turn_id or not turn_id:
        raise ValueError("invalid turn identifier")
    digest = hashlib.sha256((SCHEMA + "|holdout-v1|" + turn_id).encode("utf-8")).digest()
    return "holdout" if int.from_bytes(digest[:8], "big") % 5 == 0 else "train"


def _evidence_index(items: Sequence[Mapping], keys: set[tuple[str, str]]) -> dict[tuple[str, str], Mapping]:
    result = {}
    for item in items:
        if not isinstance(item, dict) or set(item) - _EVIDENCE_FIELDS:
            raise ValueError("invalid evidence schema")
        session, turn = item.get("session_id"), item.get("turn_id")
        if (not session or not turn or receipt_history.sanitize_session_id(session) != session
                or receipt_history.sanitize_turn_id(turn) != turn):
            raise ValueError("invalid evidence identity")
        key = (session, turn)
        if key in result or key not in keys:
            raise ValueError("duplicate or unbound evidence identity")
        for flag in ("turn_completed", "tool_trace_complete", "retried", "undone"):
            if flag in item and type(item[flag]) is not bool:
                raise ValueError("invalid evidence flag")
        events = item.get("tool_events", [])
        if not isinstance(events, list):
            raise ValueError("invalid tool events")
        for event in events:
            if not isinstance(event, dict) or set(event) != {"session_id", "turn_id", "status"}:
                raise ValueError("invalid tool event schema")
        message = item.get("user_message")
        if message is not None and (
            not isinstance(message, dict) or set(message) != {"reply_to_message_id", "text"}
        ):
            raise ValueError("invalid user-message schema")
        reactions = item.get("reactions", [])
        if not isinstance(reactions, list) or any(
            not isinstance(reaction, dict)
            or set(reaction) != {"target_message_id", "kind"}
            or reaction["kind"] not in ("positive", "negative")
            for reaction in reactions
        ):
            raise ValueError("invalid reaction schema")
        result[key] = item
    return result


def _skill_label(receipt: Mapping) -> bool | str:
    selected = receipt["selected"]
    if selected is None:
        return UNKNOWN
    status = receipt.get("consumer_status")
    if status == "load_failed":
        return False
    if status == "loaded" and receipt.get("skill_load_verified") is True and receipt.get("loaded_skill") == selected:
        return True
    return UNKNOWN


def _tool_label(session: str, turn: str, evidence: Mapping) -> bool | str:
    events = evidence.get("tool_events")
    if not isinstance(events, list):
        return UNKNOWN
    if any(event["session_id"] != session or event["turn_id"] != turn
           or event["status"] not in ("ok", "error") for event in events):
        return UNKNOWN
    if any(event["status"] == "error" for event in events):
        return True
    return False if evidence.get("tool_trace_complete") is True else UNKNOWN


def _correction_label(current: Mapping, following: Mapping | None) -> bool | str:
    if current.get("turn_completed") is not True or following is None:
        return UNKNOWN
    if following.get("retried") is True or following.get("undone") is True:
        return UNKNOWN
    assistant_id = current.get("assistant_message_id")
    message = following.get("user_message")
    if (receipt_state.safe_identifier(assistant_id) is None
            or not isinstance(message, dict)
            or receipt_state.safe_identifier(message.get("reply_to_message_id")) != assistant_id):
        return UNKNOWN
    text = message.get("text")
    if type(text) is not str or len(text) > 4096 or not text.strip():
        return UNKNOWN
    text = text.strip()
    match = _CORRECTION.match(text)
    if (match and (match.group("complete") or any(char.isalnum() for char in text[match.end():]))
            and not _AMBIGUOUS.search(text[match.end():])):
        return True
    if _AMBIGUOUS.search(text):
        return UNKNOWN
    return False


def generate(records: Sequence[Mapping], evidence: Sequence[Mapping]) -> dict:
    """Label validated retained receipts; report only enums, booleans, counts.

    Both inputs are explicit and in memory. `records` must be in retained
    append order. Duplicate or absent IDs are censored, not deduplicated into
    invented turns. Invalid evidence schemas fail before any output exists.
    """
    if not isinstance(records, (list, tuple)) or not isinstance(evidence, (list, tuple)):
        raise ValueError("expected ordered records and evidence")
    if not all(receipt_history.validate_history_record(record) for record in records):
        raise ValueError("invalid retained receipt")
    keys = [(record["session_id"], record["turn_id"]) for record in records]
    counts = Counter(key for key in keys if all(key))
    index = _evidence_index(evidence, set(counts))
    # The next record in the same session, not a different interleaved session.
    next_in_session = {}
    last_by_session = {}
    for position, (session, _) in enumerate(keys):
        if session:
            if session in last_by_session:
                next_in_session[last_by_session[session]] = position
            last_by_session[session] = position
    labels = []
    for position, (session, turn) in enumerate(keys):
        label: dict[str, bool | str] = {"split": UNKNOWN, **dict.fromkeys(FIELDS, UNKNOWN)}
        if session and turn and counts[(session, turn)] == 1:
            label["split"] = split_for_turn(turn)
            receipt = records[position]["receipt"]
            current = index.get((session, turn), {})
            label["skill_loaded_after_selection"] = _skill_label(receipt)
            if current:
                label["tool_error_in_turn"] = _tool_label(session, turn, current)
                completed = current.get("turn_completed")
                if type(completed) is bool:
                    label["turn_completed"] = completed
                next_position = next_in_session.get(position)
                if next_position is not None:
                    next_key = keys[next_position]
                    if (counts[next_key] == 1 and next_key[1] is not None
                            and split_for_turn(next_key[1]) == label["split"]):
                        label["next_turn_user_correction"] = _correction_label(
                            current, index.get(next_key)
                        )
        labels.append(label)
    coverage = {
        field: {
            "denominator": len(labels),
            "labeled": sum(type(label[field]) is bool for label in labels),
            "unknown": sum(label[field] == UNKNOWN for label in labels),
        }
        for field in FIELDS
    }
    splits = Counter(label["split"] for label in labels)
    return {
        "schema": SCHEMA, "labels": labels, "coverage": coverage,
        "splits": {"train": splits["train"], "holdout": splits["holdout"], "unknown": splits[UNKNOWN]},
    }


def _read_retained_history(path: Path) -> list[dict]:
    """Read the bounded canonical tail, raising on source refusal or I/O failure."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise OSError("history must be a regular single-link file")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            opened = os.fstat(handle.fileno())
            current = path.lstat()
            if (not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                    or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                    or (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino)):
                raise OSError("history changed while opening")
            start = max(0, opened.st_size - receipt_history.DEFAULT_MAX_BYTES)
            handle.seek(start)
            data = handle.read(receipt_history.DEFAULT_MAX_BYTES)
            after = os.fstat(handle.fileno())
            final_path = path.lstat()
            if (not stat.S_ISREG(after.st_mode) or after.st_nlink != 1
                    or not stat.S_ISREG(final_path.st_mode) or final_path.st_nlink != 1
                    or (final_path.st_dev, final_path.st_ino) != (opened.st_dev, opened.st_ino)
                    or (after.st_size, after.st_mtime_ns) != (opened.st_size, opened.st_mtime_ns)):
                raise OSError("history changed while reading")
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if start:
        newline = data.find(b"\n")
        data = data[newline + 1:] if newline >= 0 else b""
    return receipt_history._parse_records(data.decode("ascii", errors="replace").splitlines())


def _publish_report(path: Path, payload: str) -> None:
    """Protect and flush a temporary file, then publish without replacing a file."""
    if path.exists() or path.is_symlink():
        raise FileExistsError("output already exists")
    descriptor, name = tempfile.mkstemp(prefix=".outcome-labels-", suffix=".tmp", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            descriptor = None
            receipt_state._apply_private_permissions(temporary)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        # Atomic no-clobber publication, including a destination-creation race.
        os.link(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except OSError:
            # Preserve the publication result (or original failure). The temp
            # file is private and may be cleaned up after a filesystem failure.
            pass


def main(argv: list[str] | None = None) -> int:
    """Explicit local-file entry point; no default profile, hook, or network."""
    parser = argparse.ArgumentParser(description="Generate local offline labels from retained receipts")
    parser.add_argument("--history-dir", type=Path, required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    source = receipt_history.history_path(args.history_dir)
    if source is None or not source.is_file():
        parser.error("receipt history file is absent")
    if args.output.resolve() in (source.resolve(), args.evidence.resolve()):
        parser.error("output cannot replace an input")
    records = _read_retained_history(source)
    supplied = json.loads(args.evidence.read_text(encoding="utf-8"))
    report = generate(records, supplied)
    payload = json.dumps(report, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    _publish_report(args.output, payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
