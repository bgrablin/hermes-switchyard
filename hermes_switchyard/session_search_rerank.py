"""Jev re-rank of Hermes session_search FTS shortlists.

Stock Hermes ``session_search`` is lexical/FTS. For recall-style questions the
wrong session often ranks first. This module accepts the ordered FTS shortlist
(compact cards only) plus the user's recall question, asks Jev for a Choice
among session ids, and optionally a second Choice among message-id anchors.

Fail-open policy: when Jev is unavailable, returns an invalid response, or
falls below local confidence / winning-probability thresholds, return the first
FTS candidate (input order) with an explicit ``fail_open_reason``. Empty
shortlists return a structured empty result with no provider call.

Full session transcripts are never sent by default — only redacted, length-capped
card text.
"""
from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence

from .client import (
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    MAX_DECISION_REQUESTS,
    PartialAccountingError,
    operation_remaining_deadline,
    request_budget_scope,
)
from .routing import (
    DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD,
    DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD,
    _choice_metrics,
    _decision_metadata,
)

WORKFLOW_ID = "session_search_rerank.v1"

DEFAULT_CHOICE_CONFIDENCE_THRESHOLD = DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD
DEFAULT_WINNING_PROBABILITY_THRESHOLD = DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD
DEFAULT_MAX_CARD_CHARS = 480
MAX_CANDIDATES = 64
MAX_QUERY_CHARS = 1_200
MAX_SESSION_ID_CHARS = 128
MAX_MESSAGE_ID_CHARS = 128
MAX_MESSAGE_ANCHORS = 16
MAX_TITLE_CHARS = 160

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"\b(?:\+?\d[\d(). -]{7,}\d)\b")
_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{12,}|"
    r"bearer\s+[A-Za-z0-9._~+/=-]{16,})\b|"
    r"\b(?:api[_ -]?key|access[_ -]?token|secret|password)\s*[:=]\s*[^\s,;]+",
    re.IGNORECASE,
)
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")

_FAIL_OPEN_REASONS = frozenset({
    "provider_failed",
    "invalid_response",
    "choice_confidence_below_threshold",
    "winning_probability_below_threshold",
    "partial_accounting_failed",
})


def _require_public_data_ack(acknowledged: bool = True) -> None:
    if acknowledged is not True:
        raise PermissionError(
            "public_or_sanitized_data_ack is false; this call was refused"
        )


def _bounded_number(value: Any, name: str) -> float:
    if type(value) not in (int, float) or isinstance(value, bool) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not 0.0 <= number <= 1.0:
        raise ValueError(f"{name} must be in [0, 1]")
    return number


def redact_card_text(text: str) -> str:
    """Redact emails, phones, and common token/secret patterns from card text."""
    if not isinstance(text, str) or not text:
        return ""
    redacted = _EMAIL_RE.sub("[email]", text)
    redacted = _PHONE_RE.sub("[phone]", redacted)
    redacted = _TOKEN_RE.sub("[secret]", redacted)
    return redacted


def _coerce_text(value: Any, max_chars: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()[:max_chars]


def _validate_session_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("session_id must be a string")
    session_id = value.strip()
    if not session_id or len(session_id) > MAX_SESSION_ID_CHARS:
        raise ValueError("session_id must be 1 to 128 characters")
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError("session_id has unsupported characters")
    return session_id


def _validate_message_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("match_message_id must be a string")
    message_id = value.strip()
    if not message_id or len(message_id) > MAX_MESSAGE_ID_CHARS:
        raise ValueError("match_message_id must be 1 to 128 characters")
    if not _MESSAGE_ID_RE.match(message_id):
        raise ValueError("match_message_id has unsupported characters")
    return message_id


def _normalize_candidates(
    candidates: Sequence[Any],
    *,
    max_card_chars: int,
) -> list[dict[str, Any]]:
    if not isinstance(candidates, (list, tuple)):
        raise ValueError("candidates must be a list")
    if len(candidates) > MAX_CANDIDATES:
        raise ValueError(f"candidates must contain at most {MAX_CANDIDATES} entries")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw in enumerate(candidates):
        if not isinstance(raw, Mapping):
            raise ValueError(f"candidates[{index}] must be an object")
        session_id = _validate_session_id(raw.get("session_id"))
        if session_id in seen:
            raise ValueError(f"duplicate session_id in candidates: {session_id}")
        seen.add(session_id)
        title = redact_card_text(_coerce_text(raw.get("title"), MAX_TITLE_CHARS))
        snippet = redact_card_text(_coerce_text(raw.get("snippet"), max_card_chars))
        # Prefer snippet; fall back to title for the card body sent to Jev.
        body = snippet or title
        if len(body) > max_card_chars:
            body = body[:max_card_chars]
        anchors_raw = raw.get("match_message_ids") or raw.get("match_message_id")
        anchors: list[str] = []
        if anchors_raw is None:
            anchors = []
        elif isinstance(anchors_raw, str):
            anchors = [_validate_message_id(anchors_raw)]
        elif isinstance(anchors_raw, (list, tuple)):
            if len(anchors_raw) > MAX_MESSAGE_ANCHORS:
                raise ValueError(
                    f"candidates[{index}].match_message_ids exceeds {MAX_MESSAGE_ANCHORS}"
                )
            for anchor in anchors_raw:
                mid = _validate_message_id(anchor)
                if mid not in anchors:
                    anchors.append(mid)
        else:
            raise ValueError(f"candidates[{index}].match_message_ids must be a string or list")
        normalized.append(
            {
                "session_id": session_id,
                "title": title,
                "snippet": snippet,
                "card_text": body,
                "match_message_ids": anchors,
                "fts_index": index,
            }
        )
    return normalized


def _empty_result(*, query: str) -> dict[str, Any]:
    return {
        "workflow_id": WORKFLOW_ID,
        "status": "empty",
        "selected_session_id": None,
        "match_message_id": None,
        "confidence": 0.0,
        "winning_probability": None,
        "fail_open_reason": None,
        "shortlist_size": 0,
        "fts_order_preserved": True,
        "query_chars": len(query),
        "model": None,
        "request_id": None,
        "latency_ms": 0.0,
        "total_latency_ms": 0.0,
        "request_count": 0,
        "usage": {},
        "thresholds": None,
        "redaction": {
            "emails_phones_tokens": True,
            "max_card_chars": DEFAULT_MAX_CARD_CHARS,
            "full_transcripts_sent": False,
        },
        "pick_match_message": False,
    }


def _fail_open_result(
    *,
    candidates: list[dict[str, Any]],
    reason: str,
    query: str,
    thresholds: dict[str, float],
    max_card_chars: int,
    pick_match_message: bool,
    metadata: Mapping[str, Any] | None = None,
    confidence: float = 0.0,
    winning_probability: float | None = None,
) -> dict[str, Any]:
    if reason not in _FAIL_OPEN_REASONS:
        reason = "provider_failed"
    winner = candidates[0]
    meta = dict(metadata or {})
    return {
        "workflow_id": WORKFLOW_ID,
        "status": "fail_open",
        "selected_session_id": winner["session_id"],
        "match_message_id": None,
        "confidence": float(confidence),
        "winning_probability": winning_probability,
        "fail_open_reason": reason,
        "shortlist_size": len(candidates),
        "fts_order_preserved": True,
        "query_chars": len(query),
        "model": meta.get("model"),
        "request_id": meta.get("request_id"),
        "latency_ms": meta.get("latency_ms", 0.0),
        "total_latency_ms": meta.get("total_latency_ms", meta.get("latency_ms", 0.0)),
        "request_count": int(meta.get("request_count") or 0),
        "usage": dict(meta.get("usage") or {}),
        "thresholds": thresholds,
        "redaction": {
            "emails_phones_tokens": True,
            "max_card_chars": max_card_chars,
            "full_transcripts_sent": False,
        },
        "pick_match_message": pick_match_message,
    }


def _selected_result(
    *,
    session_id: str,
    match_message_id: str | None,
    confidence: float,
    winning_probability: float,
    candidates: list[dict[str, Any]],
    query: str,
    thresholds: dict[str, float],
    max_card_chars: int,
    pick_match_message: bool,
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "workflow_id": WORKFLOW_ID,
        "status": "selected",
        "selected_session_id": session_id,
        "match_message_id": match_message_id,
        "confidence": confidence,
        "winning_probability": winning_probability,
        "fail_open_reason": None,
        "shortlist_size": len(candidates),
        "fts_order_preserved": session_id == candidates[0]["session_id"],
        "query_chars": len(query),
        "model": metadata.get("model"),
        "request_id": metadata.get("request_id"),
        "latency_ms": metadata.get("latency_ms"),
        "total_latency_ms": metadata.get("total_latency_ms", metadata.get("latency_ms")),
        "request_count": int(metadata.get("request_count") or 1),
        "usage": dict(metadata.get("usage") or {}),
        "thresholds": thresholds,
        "redaction": {
            "emails_phones_tokens": True,
            "max_card_chars": max_card_chars,
            "full_transcripts_sent": False,
        },
        "pick_match_message": pick_match_message,
    }


def _session_criteria(candidates: list[dict[str, Any]]) -> dict[str, str]:
    criteria: dict[str, str] = {}
    for item in candidates:
        label = item["card_text"] or item["title"] or "(no preview)"
        criteria[item["session_id"]] = label
    return criteria


def _pick_match_message_id(
    *,
    client: Any,
    query: str,
    session_id: str,
    anchors: Sequence[str],
    public_or_sanitized_data_ack: bool,
    metadata_bucket: list[dict[str, Any]],
) -> str | None:
    """Optional second Choice among message-id anchors; returns None on any failure."""
    if len(anchors) == 0:
        return None
    if len(anchors) == 1:
        return anchors[0]
    criteria = {anchor: f"Message anchor {index + 1}" for index, anchor in enumerate(anchors)}
    state = {
        "recall_question": query,
        "selected_session_id": session_id,
        "message_anchors": list(anchors),
    }
    questions = {
        "match_message": {
            "type": "choice",
            "instructions": (
                "Which message anchor best answers the recall question within the "
                "already-selected session? Choose only one offered message id."
            ),
            "criteria": criteria,
        }
    }
    try:
        operation_remaining_deadline()
        result = client.decide(
            state,
            questions,
            public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        )
        meta = _decision_metadata(result)
        metadata_bucket.append(meta)
        answers = result.get("answers") or {}
        choice, _confidence, _probabilities = _choice_metrics(
            answers.get("match_message"), criteria, "match_message"
        )
        return choice
    except Exception:  # noqa: BLE001 -- optional second pick must not break discovery
        return None


def rerank_session_search(
    *,
    query: str,
    candidates: Sequence[Any],
    client: Any,
    choice_confidence_threshold: float = DEFAULT_CHOICE_CONFIDENCE_THRESHOLD,
    winning_probability_threshold: float = DEFAULT_WINNING_PROBABILITY_THRESHOLD,
    max_card_chars: int = DEFAULT_MAX_CARD_CHARS,
    pick_match_message: bool = True,
    public_or_sanitized_data_ack: bool = True,
    deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
) -> dict[str, Any]:
    """Re-rank FTS session cards with Jev; fail open to stock FTS order."""
    _require_public_data_ack(public_or_sanitized_data_ack)
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    query_text = query.strip()[:MAX_QUERY_CHARS]
    if type(max_card_chars) is not int or not 64 <= max_card_chars <= 2_000:
        raise ValueError("max_card_chars must be an integer between 64 and 2000")
    thresholds = {
        "choice_confidence": _bounded_number(
            choice_confidence_threshold, "choice_confidence_threshold"
        ),
        "winning_probability": _bounded_number(
            winning_probability_threshold, "winning_probability_threshold"
        ),
    }
    normalized = _normalize_candidates(candidates, max_card_chars=max_card_chars)
    if not normalized:
        return _empty_result(query=query_text)

    with request_budget_scope(
        client, MAX_DECISION_REQUESTS, deadline_seconds=deadline_seconds
    ):
        criteria = _session_criteria(normalized)
        state = {
            "recall_question": query_text,
            "fts_shortlist": [
                {
                    "session_id": item["session_id"],
                    "preview": criteria[item["session_id"]],
                    "fts_rank": item["fts_index"],
                }
                for item in normalized
            ],
        }
        questions = {
            "session": {
                "type": "choice",
                "instructions": (
                    "Which past session best matches the recall question? "
                    "Choose only one offered session_id. Prefer semantic fit over "
                    "exact keyword overlap. The result is advisory discovery only."
                ),
                "criteria": criteria,
            }
        }
        metadata: dict[str, Any] = {}
        try:
            operation_remaining_deadline()
            result = client.decide(
                state,
                questions,
                public_or_sanitized_data_ack=True,
            )
            metadata = _decision_metadata(result)
            answers = result.get("answers") or {}
            if set(answers) != set(questions):
                return _fail_open_result(
                    candidates=normalized,
                    reason="invalid_response",
                    query=query_text,
                    thresholds=thresholds,
                    max_card_chars=max_card_chars,
                    pick_match_message=pick_match_message,
                    metadata=metadata,
                )
            selected_id, confidence, probabilities = _choice_metrics(
                answers.get("session"), criteria, "session"
            )
            winning_probability = probabilities[selected_id]
            if confidence < thresholds["choice_confidence"]:
                return _fail_open_result(
                    candidates=normalized,
                    reason="choice_confidence_below_threshold",
                    query=query_text,
                    thresholds=thresholds,
                    max_card_chars=max_card_chars,
                    pick_match_message=pick_match_message,
                    metadata=metadata,
                    confidence=confidence,
                    winning_probability=winning_probability,
                )
            if winning_probability < thresholds["winning_probability"]:
                return _fail_open_result(
                    candidates=normalized,
                    reason="winning_probability_below_threshold",
                    query=query_text,
                    thresholds=thresholds,
                    max_card_chars=max_card_chars,
                    pick_match_message=pick_match_message,
                    metadata=metadata,
                    confidence=confidence,
                    winning_probability=winning_probability,
                )
        except PartialAccountingError as exc:
            partial_meta = {}
            if exc.partial:
                partial_meta = dict(exc.partial[-1])
            return _fail_open_result(
                candidates=normalized,
                reason="partial_accounting_failed",
                query=query_text,
                thresholds=thresholds,
                max_card_chars=max_card_chars,
                pick_match_message=pick_match_message,
                metadata=partial_meta,
            )
        except Exception:  # noqa: BLE001 -- discovery must fail open
            return _fail_open_result(
                candidates=normalized,
                reason="provider_failed",
                query=query_text,
                thresholds=thresholds,
                max_card_chars=max_card_chars,
                pick_match_message=pick_match_message,
                metadata=metadata,
            )

        match_message_id: str | None = None
        if pick_match_message:
            winner = next(item for item in normalized if item["session_id"] == selected_id)
            extra_meta: list[dict[str, Any]] = []
            match_message_id = _pick_match_message_id(
                client=client,
                query=query_text,
                session_id=selected_id,
                anchors=winner["match_message_ids"],
                public_or_sanitized_data_ack=True,
                metadata_bucket=extra_meta,
            )
            if extra_meta:
                # Fold optional second-call accounting into the receipt totals.
                extra = extra_meta[-1]
                try:
                    base_latency = float(metadata.get("total_latency_ms") or metadata.get("latency_ms") or 0.0)
                    extra_latency = float(extra.get("total_latency_ms") or extra.get("latency_ms") or 0.0)
                    metadata["total_latency_ms"] = base_latency + extra_latency
                    metadata["request_count"] = int(metadata.get("request_count") or 1) + int(
                        extra.get("request_count") or 1
                    )
                    if extra.get("request_id"):
                        metadata["request_id"] = extra.get("request_id")
                    if extra.get("model"):
                        metadata["model"] = extra.get("model")
                except (TypeError, ValueError):
                    pass

        return _selected_result(
            session_id=selected_id,
            match_message_id=match_message_id,
            confidence=confidence,
            winning_probability=winning_probability,
            candidates=normalized,
            query=query_text,
            thresholds=thresholds,
            max_card_chars=max_card_chars,
            pick_match_message=pick_match_message,
            metadata=metadata,
        )
