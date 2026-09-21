"""Bounded record-triage workflow built on the jev_assess decision primitive.

Public or synthetic records are qualified in bounded batches. Each batch is one
`jev_assess`-shaped call: the same `client.decide` boundary, request budget, and
aggregate deadline the registered tool uses, with code-defined alternatives.
Deterministic local rules decide some records without a provider request. A
deterministic consumer then acts only on accepted decisions and writes a local
work-queue artifact that `verify_artifact` re-checks from disk.

Four things stay separate in every result: the provider attempt, the accepted
decision, the consumer action, and the independently verifiable outcome. `run_record_triage`
never reports its own outcome as verified. There is no fallback: a failed or
late batch leaves its records unassessed and held, never locally guessed, and
provider or transport text is never copied into a result or artifact.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
from pathlib import Path
from typing import Any, Sequence

from . import receipt_state
from .client import (
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    MAX_REQUEST_BYTES,
    PartialAccountingError,
    _validate_deadline_seconds,
    operation_remaining_deadline,
    request_budget_scope,
)
from .routing import _request_size

WORKFLOW_ID = "record_triage.v1"
TASK = (
    "Triage synthetic public bug reports for a small software project. For each record decide "
    "whether it is ready for engineering, lacks information, or is out of scope, and rate its severity."
)

# Code-defined alternatives. Jev may only choose among these keys.
DISPOSITIONS = {
    "qualified": "A clear, in-scope report with enough detail for an engineer to act on.",
    "needs_info": "A plausible in-scope report that lacks reproduction steps or expected behaviour.",
    "out_of_scope": "Not a defect in this project, a question, a request, or spam.",
}
ACTION_FOR = {
    "qualified": "queue_qualified",
    "needs_info": "request_info",
    "out_of_scope": "close_out_of_scope",
}
SEVERITY_LEVELS = ("cosmetic", "minor", "major", "critical")
PRIORITY_FOR = {"critical": "p0", "major": "p1", "minor": "p2", "cosmetic": "p3"}
PRIORITY_RANK = {"p0": 0, "p1": 1, "p2": 2, "p3": 3, "unrated": 4}
COMPONENTS = frozenset({"parser", "cli", "docs", "network"})

MAX_RECORDS = 64
RECORDS_PER_BATCH = 8
MAX_WORKFLOW_REQUESTS = 16
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 2000
DEFAULT_DISPOSITION_THRESHOLD = 0.80
DEFAULT_SEVERITY_THRESHOLD = 0.80

_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,62}")
_REQUIRED_FIELDS = frozenset({"id", "title", "body", "data_class"})
_ALLOWED_FIELDS = _REQUIRED_FIELDS | {"component"}
_DATA_CLASSES = frozenset({"public", "synthetic"})
_SENT_FIELDS = ("id", "title", "body", "component")
# Every provider request repeats `state`, so the workflow keeps each batch inside one request
# instead of letting decide() split it and lose completed answers when a later split fails.
_REQUEST_SIZE_MARGIN = 1024
UNPROCESSED_REASONS = frozenset({
    "deadline_exceeded", "provider_failed", "invalid_response", "malformed_answer",
    "request_budget_exhausted",
})
_MANIFEST = "manifest.json"
_ACTIONS = "actions"


def _unit(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a finite number in [0, 1]")
    return float(value)


def _bounded_distribution(probabilities: Any, expected_keys: set[str], name: str) -> dict[str, float]:
    if not isinstance(probabilities, dict) or set(probabilities) != expected_keys:
        raise ValueError(name)
    parsed = {key: _unit(value, "probability") for key, value in probabilities.items()}
    if abs(sum(parsed.values()) - 1.0) >= 0.02:
        raise ValueError(name)
    return parsed


def _validate_records(records: Any) -> list[dict[str, Any]]:
    if not isinstance(records, (list, tuple)) or not 1 <= len(records) <= MAX_RECORDS:
        raise ValueError(f"records must be a list of 1 to {MAX_RECORDS} objects")
    validated: list[dict[str, Any]] = []
    seen: set[str] = set()
    for record in records:
        if not isinstance(record, dict) or not _REQUIRED_FIELDS <= set(record) <= _ALLOWED_FIELDS:
            raise ValueError("record fields must be id, title, body, data_class, and optional component")
        rid, title, body = record["id"], record["title"], record["body"]
        if type(rid) is not str or not _ID_RE.fullmatch(rid) or rid in seen:
            raise ValueError("record ids must be unique lowercase identifiers")
        if type(title) is not str or not 1 <= len(title) <= MAX_TITLE_CHARS:
            raise ValueError("record title is outside the bounded size")
        if type(body) is not str or len(body) > MAX_BODY_CHARS:
            raise ValueError("record body is outside the bounded size")
        if type(record["data_class"]) is not str or record["data_class"] not in _DATA_CLASSES:
            raise ValueError("every record must declare data_class public or synthetic")
        component = record.get("component")
        if component is not None and (type(component) is not str or not 1 <= len(component) <= 64):
            raise ValueError("record component is outside the bounded size")
        seen.add(rid)
        validated.append(dict(record))
    return validated


def _input_digest(records: list[dict[str, Any]]) -> str:
    canonical = json.dumps(records, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _fingerprint(record: dict[str, Any]) -> str:
    text = " ".join(f"{record['title']} {record['body']}".lower().split())
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _local_rules(records: list[dict[str, Any]]) -> dict[str, tuple[str, str]]:
    """Return {record id: (disposition, rule)} for records local code already decides."""
    decided: dict[str, tuple[str, str]] = {}
    first_seen: dict[str, str] = {}
    for record in records:
        if not record["body"].strip():
            decided[record["id"]] = ("needs_info", "empty_body")
            continue
        fingerprint = _fingerprint(record)
        original = first_seen.setdefault(fingerprint, record["id"])
        if original != record["id"]:
            decided[record["id"]] = ("out_of_scope", f"exact_duplicate_of:{original}")
    return decided


def build_assessment_request(records: Sequence[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return the (state, questions) pair for one jev_assess call over these records."""
    state_records = [{key: record[key] for key in _SENT_FIELDS if key in record} for record in records]
    questions: dict[str, Any] = {}
    for record in records:
        rid = record["id"]
        questions[f"disposition__{rid}"] = {
            "type": "choice",
            "instructions": (
                f"For record {rid}: is this report ready for engineering (qualified), missing key "
                "information (needs_info), or outside this project's scope (out_of_scope)?"
            ),
            "criteria": dict(DISPOSITIONS),
        }
        questions[f"severity__{rid}"] = {
            "type": "score",
            "instructions": f"For record {rid}: if the report is valid, how severe is its impact?",
            "criteria": list(SEVERITY_LEVELS),
        }
    return {"task": TASK, "records": state_records}, questions


def _plan_batches(records: list[dict[str, Any]], records_per_batch: int) -> list[list[dict[str, Any]]]:
    """Group records into batches that each fit one bounded provider request."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        trial = current + [record]
        fits = len(trial) <= records_per_batch and _request_size(
            *build_assessment_request(trial)
        ) <= MAX_REQUEST_BYTES - _REQUEST_SIZE_MARGIN
        if fits or not current:
            current = trial
        else:
            batches.append(current)
            current = [record]
    if current:
        batches.append(current)
    return batches


def _decision(status: str, **fields: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "status": status, "source": None, "disposition": None, "confidence": None,
        "winning_probability": None, "severity": None, "severity_probability": None,
        "rule": None, "reason": None,
    }
    base.update(fields)
    return base


def _parse_answers(
    rid: str, answers: Any, disposition_threshold: float, severity_threshold: float
) -> dict[str, Any]:
    """Strictly parse one record's answers; raise ValueError on anything malformed."""
    if not isinstance(answers, dict):
        raise ValueError("answers")
    choice_answer = answers.get(f"disposition__{rid}")
    score_answer = answers.get(f"severity__{rid}")
    if not isinstance(choice_answer, dict) or not isinstance(score_answer, dict):
        raise ValueError("answer missing")
    choice = choice_answer.get("choice")
    if choice not in DISPOSITIONS:
        raise ValueError("disposition")
    parsed = _bounded_distribution(choice_answer.get("probabilities"), set(DISPOSITIONS), "disposition")
    confidence = _unit(choice_answer.get("confidence"), "confidence")
    if parsed[choice] < max(parsed.values()) - 1e-6:
        raise ValueError("disposition is not the winning choice")
    expected_keys = {str(index) for index in range(len(SEVERITY_LEVELS))}
    severity_parsed = _bounded_distribution(score_answer.get("probabilities"), expected_keys, "severity")
    winner = max(expected_keys, key=lambda key: (severity_parsed[key], -int(key)))
    severity_probability = severity_parsed[winner]
    level = SEVERITY_LEVELS[int(winner)]
    if parsed[choice] < disposition_threshold or confidence < disposition_threshold:
        return _decision(
            "abstained", source="jev", disposition=choice, confidence=confidence,
            winning_probability=parsed[choice], reason="below_threshold",
        )
    fields: dict[str, Any] = {
        "source": "jev", "disposition": choice, "confidence": confidence,
        "winning_probability": parsed[choice],
    }
    if choice == "qualified":
        fields["severity_probability"] = severity_probability
        fields["severity"] = level if severity_probability >= severity_threshold else None
    return _decision("accepted", **fields)


def _priority(decision: dict[str, Any]) -> str:
    return PRIORITY_FOR.get(decision.get("severity") or "", "unrated")


def _routable(record: dict[str, Any]) -> bool:
    return record.get("component") in COMPONENTS


def _consume(record: dict[str, Any], decision: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Deterministic consumer: act only on accepted decisions that local policy also allows."""
    def outcome(status: str, action: str | None = None, reason: str | None = None):
        return {"status": status, "action": action, "reason": reason, "file": None, "sha256": None}

    if decision["status"] != "accepted":
        return outcome("skipped", reason=decision["reason"]), None
    disposition = decision["disposition"]
    action = ACTION_FOR[disposition]
    payload: dict[str, Any] = {
        "record_id": record["id"], "action": action, "decision_source": decision["source"],
    }
    if disposition == "qualified":
        if not _routable(record):
            return outcome("rejected", reason="component_not_routable"), None
        payload.update({"queue": record["component"], "priority": _priority(decision)})
    elif disposition == "needs_info":
        missing = [name for name, present in (("body", record["body"].strip()), ("component", _routable(record))) if not present]
        payload.update({
            "missing_fields": missing,
            "message": f"Record {record['id']}: please add reproduction steps and expected behaviour.",
        })
    else:
        rule = decision.get("rule") or ""
        if decision.get("source") == "local_rule" and rule.startswith("exact_duplicate_of:"):
            payload["reason"] = rule
        else:
            payload["reason"] = "judged_out_of_scope"
    return outcome("acted", action=action), payload


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_atomic(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _queues(entries: list[dict[str, Any]], payloads: dict[str, dict[str, Any]]) -> dict[str, list[str]]:
    queues: dict[str, list[tuple[int, str]]] = {}
    for entry in entries:
        payload = payloads.get(entry["id"])
        if payload and payload["action"] == "queue_qualified":
            queues.setdefault(payload["queue"], []).append((PRIORITY_RANK[payload["priority"]], entry["id"]))
    return {queue: [rid for _rank, rid in sorted(items)] for queue, items in sorted(queues.items())}


def _safe_request_id(value: Any) -> str | None:
    return receipt_state.safe_identifier(value, max_length=128)


class _Accounting:
    def __init__(self) -> None:
        self.attempted = self.completed = self.failed = self.not_attempted = 0
        self.requests_completed = 0
        self.usage: dict[str, float] = {}
        self.cost_reported: list[float] = []
        self.cost_missing = False
        self.usage_incomplete = False
        self.request_ids: list[str] = []
        self.latency_ms = 0.0
        self.model: str | None = None

    @property
    def requests_used(self) -> int:
        # A failed batch made at least one attempt whose cost and count are unknown.
        return self.requests_completed + self.failed

    def add_usage(self, usage: Any) -> None:
        if not isinstance(usage, dict):
            self.cost_missing = True
            return
        bounded = receipt_state.safe_usage(usage)
        cost = bounded.get("cost")
        if cost is None or type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
            self.cost_missing = True
        else:
            self.cost_reported.append(float(cost))
        for key, value in bounded.items():
            if key == "cost" or value is None:
                continue
            self.usage[key] = self.usage.get(key, 0.0) + float(value)

    def complete(self, result: dict[str, Any]) -> None:
        self.completed += 1
        count = result.get("request_count")
        self.requests_completed += count if type(count) is int and count >= 1 else 1
        self.add_usage(result.get("total_usage", result.get("usage")))
        latency = result.get("total_latency_ms", result.get("latency_ms"))
        if type(latency) in (int, float) and math.isfinite(latency) and latency >= 0:
            self.latency_ms += float(latency)
        request_id = _safe_request_id(result.get("request_id"))
        if request_id:
            self.request_ids.append(request_id)
        model = receipt_state.safe_identifier(result.get("model"), max_length=128)
        if model:
            self.model = model

    def fail(self, partial: Sequence[dict[str, Any]] = ()) -> None:
        self.failed += 1
        self.usage_incomplete = True
        for item in partial:
            if isinstance(item, dict):
                self.requests_completed += 1
                self.add_usage(item.get("usage"))
                request_id = _safe_request_id(item.get("request_id"))
                if request_id:
                    self.request_ids.append(request_id)

    def report(self) -> dict[str, Any]:
        subtotal = sum(self.cost_reported)
        known = not self.cost_missing and not self.usage_incomplete
        return {
            "batches_attempted": self.attempted,
            "batches_completed": self.completed,
            "batches_failed": self.failed,
            "batches_not_attempted": self.not_attempted,
            "requests_completed": self.requests_completed,
            "usage_incomplete": self.usage_incomplete,
            "cost_known": known,
            "total_cost": subtotal if known else None,
            "known_cost_subtotal": subtotal,
            "total_usage": dict(self.usage),
            "request_ids": list(self.request_ids),
            "total_latency_ms": self.latency_ms,
            "model": self.model,
        }


def _failure_reason(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "deadline_exceeded"
    if isinstance(exc, PartialAccountingError):
        return "provider_failed"
    if isinstance(exc, (ValueError, TypeError)):
        return "invalid_response"
    return "provider_failed"


def _prepare_out_dir(out_dir: Path) -> None:
    if out_dir.exists():
        if not out_dir.is_dir() or any(out_dir.iterdir()):
            raise FileExistsError("out_dir must not exist or must be an empty directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / _ACTIONS).mkdir()


def run_record_triage(
    records: Sequence[dict[str, Any]],
    *,
    client: Any,
    out_dir: str | Path,
    deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
    records_per_batch: int = RECORDS_PER_BATCH,
    disposition_threshold: float = DEFAULT_DISPOSITION_THRESHOLD,
    severity_threshold: float = DEFAULT_SEVERITY_THRESHOLD,
    public_or_sanitized_data_ack: bool = True,
) -> dict[str, Any]:
    """Qualify records with bounded jev_assess batches, act on accepted decisions, write an artifact.

    Invalid input, a refused acknowledgement, or a bad deadline raises before any provider request.
    Provider failures never raise: they leave the affected records unassessed and are reported.
    """
    if public_or_sanitized_data_ack is not True:
        raise PermissionError("public_or_sanitized_data_ack is false; this call was refused")
    validated = _validate_records(records)
    _validate_deadline_seconds(deadline_seconds)
    if type(records_per_batch) is not int or not 1 <= records_per_batch <= RECORDS_PER_BATCH:
        raise ValueError(f"records_per_batch must be an integer from 1 to {RECORDS_PER_BATCH}")
    disposition_threshold = _unit(disposition_threshold, "disposition_threshold")
    severity_threshold = _unit(severity_threshold, "severity_threshold")
    destination = Path(out_dir)
    _prepare_out_dir(destination)

    local = _local_rules(validated)
    decisions: dict[str, dict[str, Any]] = {}
    attempts: dict[str, dict[str, Any]] = {}
    for record in validated:
        rid = record["id"]
        if rid in local:
            disposition, rule = local[rid]
            decisions[rid] = _decision("accepted", source="local_rule", disposition=disposition, rule=rule)
            attempts[rid] = {"batch": None, "requested": False, "completed": False}
    pending = [record for record in validated if record["id"] not in local]
    batches = _plan_batches(pending, records_per_batch)

    accounting = _Accounting()
    halted: str | None = None
    with request_budget_scope(client, MAX_WORKFLOW_REQUESTS, deadline_seconds=deadline_seconds):
        for index, batch in enumerate(batches):
            if halted is None:
                try:
                    operation_remaining_deadline()
                except TimeoutError:
                    halted = "deadline_exceeded"
                else:
                    if accounting.requests_used >= MAX_WORKFLOW_REQUESTS:
                        halted = "request_budget_exhausted"
            if halted is not None:
                accounting.not_attempted += 1
                for record in batch:
                    attempts[record["id"]] = {
                        "batch": index, "requested": False, "completed": False, "reason": halted,
                    }
                    decisions[record["id"]] = _decision("unassessed", reason=halted)
                continue
            accounting.attempted += 1
            state, questions = build_assessment_request(batch)
            try:
                result = client.decide(state, questions, public_or_sanitized_data_ack=True)
                if not isinstance(result, dict) or not isinstance(result.get("answers"), dict):
                    raise TypeError("Jev response must be an object with answers")
            except Exception as exc:  # noqa: BLE001 -- provider and transport text never leaves this boundary
                reason = _failure_reason(exc)
                accounting.fail(exc.partial if isinstance(exc, PartialAccountingError) else ())
                for record in batch:
                    attempts[record["id"]] = {
                        "batch": index, "requested": True, "completed": False, "reason": reason,
                    }
                    decisions[record["id"]] = _decision("unassessed", reason=reason)
                continue
            try:
                accounting.complete(result)
            except Exception:  # noqa: BLE001 -- malformed accounting must not discard the answers
                accounting.usage_incomplete = True
                accounting.cost_missing = True
            for record in batch:
                attempts[record["id"]] = {"batch": index, "requested": True, "completed": True}
                try:
                    decisions[record["id"]] = _parse_answers(
                        record["id"], result["answers"], disposition_threshold, severity_threshold
                    )
                except Exception:  # noqa: BLE001 -- one hostile answer must not lose the rest of the run
                    decisions[record["id"]] = _decision("unassessed", reason="malformed_answer")

    entries: list[dict[str, Any]] = []
    payloads: dict[str, dict[str, Any]] = {}
    for record in validated:
        rid = record["id"]
        try:
            consumer, payload = _consume(record, decisions[rid])
        except Exception:  # noqa: BLE001 -- a consumer fault must not lose other records' actions
            consumer = {"status": "failed", "action": None, "reason": "consumer_error",
                        "file": None, "sha256": None}
            payload = None
        if payload is not None:
            data = _canonical_bytes(payload)
            relative = f"{_ACTIONS}/{rid}.json"
            try:
                _write_atomic(destination / relative, data)
            except OSError:
                consumer = {"status": "failed", "action": consumer["action"], "reason": "write_failed",
                            "file": None, "sha256": None}
            else:
                consumer["file"] = relative
                consumer["sha256"] = hashlib.sha256(data).hexdigest()
                payloads[rid] = payload
        entries.append({"id": rid, "attempt": attempts[rid], "decision": decisions[rid], "consumer": consumer})

    unprocessed = {
        entry["id"]: entry["decision"]["reason"]
        for entry in entries if entry["decision"]["status"] == "unassessed"
    }
    report = accounting.report()
    report["records_unprocessed"] = len(unprocessed)
    manifest = {
        "workflow": WORKFLOW_ID,
        "input_sha256": _input_digest(validated),
        "thresholds": {"disposition": disposition_threshold, "severity": severity_threshold},
        "records": entries,
        "queues": _queues(entries, payloads),
        "unprocessed": unprocessed,
        "accounting": report,
    }
    try:
        _write_atomic(destination / _MANIFEST, _canonical_bytes(manifest))
        artifact_status = "written"
    except OSError:
        artifact_status = "incomplete"

    settled = [entry["decision"]["status"] in {"accepted", "abstained"} for entry in entries]
    status = "complete" if all(settled) else ("partial" if any(settled) else "failed")
    return {
        "workflow": WORKFLOW_ID,
        "status": status,
        "records": [dict(entry) for entry in entries],
        "accounting": manifest["accounting"],
        "artifact": {
            "status": artifact_status,
            "manifest": _MANIFEST,
            "actions": sum(1 for entry in entries if entry["consumer"]["status"] == "acted"),
        },
        "verified": False,
        "verification": "not_run",
    }


def _read_json(path: Path) -> Any:
    return json.loads(path.read_bytes().decode("utf-8"))


def verify_artifact(
    out_dir: str | Path,
    records: Sequence[dict[str, Any]],
    *,
    disposition_threshold: float = DEFAULT_DISPOSITION_THRESHOLD,
    severity_threshold: float = DEFAULT_SEVERITY_THRESHOLD,
) -> dict[str, Any]:
    """Re-check a run's artifact from disk against the caller's original records.

    This verifies artifact integrity and that every recorded action follows the code-defined policy
    (thresholds, local rules, routable components, hashes, no stray files). It does not judge
    whether Jev's assessments were correct.
    """
    errors: list[str] = []

    def report() -> dict[str, Any]:
        return {"verified": not errors, "errors": errors, "checked_records": len(entries) if manifest else 0}

    manifest: Any = None
    entries: list[dict[str, Any]] = []
    root = Path(out_dir)
    try:
        validated = _validate_records(records)
    except ValueError:
        errors.append("records_invalid")
        return report()
    try:
        manifest = _read_json(root / _MANIFEST)
        entries = manifest["records"]
        if not isinstance(entries, list) or manifest["workflow"] != WORKFLOW_ID:
            raise ValueError("manifest")
    except (OSError, ValueError, KeyError, TypeError):
        errors.append("manifest_unreadable")
        manifest = None
        return report()

    if manifest.get("input_sha256") != _input_digest(validated):
        errors.append("input_digest_mismatch")
    if manifest.get("thresholds") != {"disposition": disposition_threshold, "severity": severity_threshold}:
        errors.append("threshold_mismatch")
    by_id = {record["id"]: record for record in validated}
    if any(not isinstance(entry, dict) for entry in entries) or [
        entry.get("id") for entry in entries
    ] != [record["id"] for record in validated]:
        errors.append("record_set_mismatch")
        return report()
    local = _local_rules(validated)
    payloads: dict[str, dict[str, Any]] = {}
    expected_files: set[str] = set()

    for entry in entries:
        rid = entry["id"]
        record = by_id[rid]
        try:
            decision, consumer = entry["decision"], entry["consumer"]
            status, source, disposition = decision["status"], decision["source"], decision["disposition"]
            if status == "accepted":
                if source == "local_rule":
                    if local.get(rid) != (disposition, decision["rule"]):
                        errors.append(f"local_rule_not_rederivable:{rid}")
                elif source == "jev":
                    if rid in local:
                        errors.append(f"local_rule_not_applied:{rid}")
                    if decision.get("rule") is not None:
                        errors.append(f"jev_rule_not_allowed:{rid}")
                    if disposition not in DISPOSITIONS:
                        errors.append(f"disposition_invalid:{rid}")
                    if not (
                        type(decision["confidence"]) in (int, float) and decision["confidence"] >= disposition_threshold
                        and type(decision["winning_probability"]) in (int, float)
                        and decision["winning_probability"] >= disposition_threshold
                    ):
                        errors.append(f"accepted_below_threshold:{rid}")
                    level = decision["severity"]
                    if level is not None and not (
                        level in PRIORITY_FOR and type(decision["severity_probability"]) in (int, float)
                        and decision["severity_probability"] >= severity_threshold
                    ):
                        errors.append(f"severity_below_threshold:{rid}")
                else:
                    errors.append(f"decision_source_invalid:{rid}")
            elif status == "unassessed":
                if decision["reason"] not in UNPROCESSED_REASONS:
                    errors.append(f"unassessed_reason_invalid:{rid}")
                if source is not None or disposition is not None:
                    errors.append(f"unassessed_carries_a_decision:{rid}")
            elif status == "abstained":
                confidence = decision.get("confidence")
                winning = decision.get("winning_probability")
                below_threshold = (
                    (type(confidence) in (int, float) and confidence < disposition_threshold)
                    or (type(winning) in (int, float) and winning < disposition_threshold)
                )
                if not (
                    source == "jev"
                    and disposition in DISPOSITIONS
                    and decision.get("reason") == "below_threshold"
                    and below_threshold
                ):
                    errors.append(f"abstention_evidence_invalid:{rid}")
            else:
                errors.append(f"decision_status_invalid:{rid}")
            attempt = entry["attempt"]
            if source == "local_rule" and attempt.get("requested") is not False:
                errors.append(f"attempt_mismatch:{rid}")
            elif source == "jev" and attempt.get("completed") is not True:
                errors.append(f"attempt_mismatch:{rid}")
            elif status == "unassessed" and attempt.get("completed") is not True and (
                attempt.get("reason") != decision["reason"]
            ):
                errors.append(f"attempt_mismatch:{rid}")

            state = consumer["status"]
            if state == "acted":
                if status != "accepted" or disposition not in ACTION_FOR:
                    errors.append(f"acted_without_accepted_decision:{rid}")
                    continue
                if consumer["action"] != ACTION_FOR[disposition]:
                    errors.append(f"action_disposition_mismatch:{rid}")
                    continue
                relative = f"{_ACTIONS}/{rid}.json"
                expected_files.add(f"{rid}.json")
                if consumer["file"] != relative:
                    errors.append(f"action_file_path_mismatch:{rid}")
                    continue
                data = (root / relative).read_bytes()
                if hashlib.sha256(data).hexdigest() != consumer["sha256"]:
                    errors.append(f"action_file_hash_mismatch:{rid}")
                    continue
                payload = json.loads(data.decode("utf-8"))
                payloads[rid] = payload
                if payload.get("record_id") != rid or payload.get("action") != consumer["action"]:
                    errors.append(f"action_file_content_mismatch:{rid}")
                if payload.get("decision_source") != source:
                    errors.append(f"action_file_source_mismatch:{rid}")
                if disposition == "qualified" and (
                    not _routable(record) or payload.get("queue") != record["component"]
                ):
                    errors.append(f"queue_not_routable:{rid}")
                if disposition == "qualified" and payload.get("priority") != _priority(decision):
                    errors.append(f"priority_mismatch:{rid}")
                _, expected_payload = _consume(record, decision)
                if expected_payload != payload:
                    errors.append(f"action_payload_mismatch:{rid}")
            elif state == "rejected":
                if not (status == "accepted" and disposition == "qualified" and not _routable(record)):
                    errors.append(f"rejection_not_justified:{rid}")
            elif state == "skipped":
                if status == "accepted":
                    errors.append(f"accepted_decision_dropped:{rid}")
            elif state != "failed":
                errors.append(f"consumer_status_invalid:{rid}")
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            errors.append(f"entry_unreadable:{rid}")

    try:
        for path in sorted((root / _ACTIONS).iterdir()):
            if path.name not in expected_files:
                errors.append(f"unexpected_action_file:{path.name}")
        for path in sorted(root.iterdir()):
            if path.name not in {_MANIFEST, _ACTIONS}:
                errors.append(f"unexpected_artifact_entry:{path.name}")
    except OSError:
        errors.append("artifact_directory_unreadable")
    try:
        if manifest.get("queues") != _queues(entries, payloads):
            errors.append("queues_mismatch")
        expected_unprocessed = {
            entry["id"]: entry["decision"]["reason"]
            for entry in entries if entry["decision"]["status"] == "unassessed"
        }
        if manifest.get("unprocessed") != expected_unprocessed:
            errors.append("unprocessed_mismatch")
    except (KeyError, TypeError, AttributeError):
        errors.append("manifest_structure_invalid")
    return report()
