"""Closed-set skill and model routing with fail-closed policy gates.

Jev is an advisory decision service. This module performs all identifier, policy,
and cost checks locally before it asks Jev for an uncalibrated fit signal. It
never loads a skill, edits a prompt, or changes the runtime model.
"""
from __future__ import annotations

import json
import math
from typing import Any

from .client import (
    DEFAULT_OPERATION_DEADLINE_SECONDS,
    EXPECTED_MODEL,
    MAX_DECISION_REQUESTS,
    MAX_QUESTIONS_PER_REQUEST,
    MAX_REQUEST_BYTES,
    operation_remaining_deadline,
    request_budget_scope,
)


# These are conservative local policy thresholds. Choice confidence is output
# concentration; Noul values are intended yes/no probabilities, but calibration
# for correctness is not independently established.
DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD = 0.80
DEFAULT_SKILL_NEEDS_THRESHOLD = 0.80
DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD = 0.80
DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD = 0.80

_MODEL_REQUIREMENT_KEYS = frozenset({
    "data_classes",
    "tool_capabilities",
    "context_limit",
    "budget",
})

_CHOICE_MAX_OPTIONS = 255  # Jev's per-Choice contract; large catalogs are reduced hierarchically.
_SKILL_PARTITION_SIZE = 200  # Leave room for an explicit no-match option.
_PARTITION_CRITERIA_BYTES = 72_000
_SKILL_NONE = "__jev_none_of_these__"
_MULTI_SKILL_PREFIX = "skill_"


def _request_size(state: Any, questions: dict[str, Any]) -> int:
    payload = {
        "model": EXPECTED_MODEL,
        "state": state,
        "questions": questions,
        "provider": {"allow_fallbacks": False},
    }
    try:
        return len(json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError):
        raise ValueError("Jev request contains a non-JSON value") from None


def _require_public_data_ack(acknowledged: bool) -> None:
    if acknowledged is not True:
        raise PermissionError(
            "public_or_sanitized_data_ack must be true: this is a caller attestation, not DLP; "
            "do not send private, employer, or regulated UI/data to a model"
        )


def _bounded_number(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a finite number in [0, 1]")
    return float(value)


def _positive_integer(value: Any, name: str) -> int:
    if type(value) is not int or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _nonnegative_number(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError(f"{name} must be a finite non-negative number")
    return float(value)


def _string_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or any(type(item) is not str or not item for item in value):
        raise ValueError(f"{name} must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError(f"{name} must not contain duplicate values")
    return list(value)


def _criteria(candidates: list[dict], key: str, *, max_entries: int | None = _CHOICE_MAX_OPTIONS) -> dict[str, str]:
    """Build Jev criteria without normalizing candidate identifiers."""
    if not isinstance(candidates, list) or not candidates or (max_entries is not None and len(candidates) > max_entries):
        bound = f"1 to {max_entries}" if max_entries is not None else "at least 1"
        raise ValueError(f"candidates must contain {bound} entries")
    out: dict[str, str] = {}
    for item in candidates:
        if not isinstance(item, dict):
            raise ValueError("each candidate must be an object")
        identifier = item.get(key)
        if type(identifier) is not str or not identifier:
            raise ValueError("candidate identifiers must be non-empty strings")
        if identifier != identifier.strip():
            raise ValueError("candidate identifiers must not have leading or trailing whitespace")
        if identifier in out:
            raise ValueError("candidate identifiers must be unique and exact")
        description = item.get("description", "")
        if type(description) is not str:
            raise ValueError("candidate descriptions must be strings")
        out[identifier] = description or identifier
    return out


def _choice_metrics(answer: Any, criteria: dict[str, str], name: str) -> tuple[str, float, dict[str, float]]:
    if not isinstance(answer, dict):
        raise TypeError(f"Jev response is missing answer {name}")
    if set(answer) != {"choice", "probabilities", "confidence"}:
        raise ValueError(f"Jev choice {name} has unexpected response fields")
    choice = answer.get("choice")
    probabilities = answer.get("probabilities")
    confidence = answer.get("confidence")
    if choice not in criteria or not isinstance(probabilities, dict) or set(probabilities) != set(criteria):
        raise ValueError(f"Jev choice {name} is outside the offered criteria")
    parsed = {key: _bounded_number(value, f"{name}.probabilities[{key!r}]") for key, value in probabilities.items()}
    if abs(sum(parsed.values()) - 1.0) >= 0.02:
        raise ValueError(f"Jev choice {name} probabilities do not form a distribution")
    confidence_value = _bounded_number(confidence, f"{name}.confidence")
    if parsed[choice] < max(parsed.values()) - 1e-6:
        raise ValueError(f"Jev choice {name} is not the winning choice")
    return choice, confidence_value, parsed


def _decision_metadata(result: Any) -> dict[str, Any]:
    if not isinstance(result, dict):
        raise TypeError("Jev response must be an object")
    answers = result.get("answers")
    if not isinstance(answers, dict):
        raise TypeError("Jev response has no answers object")
    usage = result.get("usage") or {}
    if not isinstance(usage, dict):
        raise TypeError("Jev response usage must be an object")
    return {
        "model": result.get("model"),
        "latency_ms": result.get("latency_ms"),
        "usage": usage,
        "request_count": int(result.get("request_count") or 1),
        "total_latency_ms": result.get("total_latency_ms", result.get("latency_ms")),
        "total_usage": result.get("total_usage", usage),
    }


def _aggregate_metadata(calls: list[dict[str, Any]]) -> dict[str, Any]:
    usage: dict[str, Any] = {}
    latency = 0.0
    request_count = 0
    for call in calls:
        value = call.get("latency_ms")
        if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
            latency += float(value)
        request_count += int(call.get("request_count") or 1)
        for key, item in (call.get("usage") or {}).items():
            if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item):
                usage[key] = float(usage.get(key, 0.0)) + float(item)
            elif key not in usage:
                usage[key] = item
    return {"total_latency_ms": latency, "total_usage": usage, "request_count": request_count}


def _skill_request_parts(
    chunk: list[dict],
    *,
    task: str,
    total_count: int,
    partition: int,
    include_needs_skill: bool,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    criteria = _criteria(chunk, "name", max_entries=None)
    if _SKILL_NONE in criteria:
        raise ValueError(f"candidate name {_SKILL_NONE!r} is reserved")
    criteria[_SKILL_NONE] = "No candidate in this partition materially fits the task"
    name = f"skill_chunk_{partition}"
    questions: dict[str, dict[str, Any]] = {
        name: {
            "type": "choice",
            "instructions": (
                "Which offered skill best matches this task? Choose the none option when no skill "
                "in this partition adds material value. The result is advisory."
            ),
            "criteria": criteria,
        }
    }
    if include_needs_skill:
        questions["needs_skill"] = {
            "type": "noul",
            "instructions": "Does this task need one of the offered skills?",
            "criteria": {
                "true": "A candidate provides specialized procedure or constraints needed for the task",
                "false": "The task is simple or none of the candidates adds material value",
            },
        }
    state = {
        "task": task,
        "candidate_count": total_count,
        "partition": partition,
        "skills": chunk,
    }
    return state, questions


def _skill_chunks(candidates: list[dict], *, task: str = "") -> list[list[dict]]:
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for candidate in candidates:
        while True:
            partition = len(chunks)
            trial = current + [candidate]
            state, questions = _skill_request_parts(
                trial,
                task=task,
                total_count=len(candidates),
                partition=partition,
                include_needs_skill=partition == 0,
            )
            criteria_size = len(
                json.dumps(
                    questions[f"skill_chunk_{partition}"]["criteria"],
                    ensure_ascii=False,
                    allow_nan=False,
                ).encode("utf-8")
            )
            too_large = _request_size(state, questions) > MAX_REQUEST_BYTES
            if not current and too_large:
                raise ValueError("a single skill candidate exceeds the bounded serialized request budget")
            if current and (
                len(trial) > _SKILL_PARTITION_SIZE
                or criteria_size > _PARTITION_CRITERIA_BYTES
                or too_large
            ):
                chunks.append(current)
                current = []
                continue
            current = trial
            break
    if current:
        chunks.append(current)
    return chunks


def _small_skill_request_parts(
    task: str, candidates: list[dict]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    criteria = _criteria(candidates, "name")
    questions = {
        "skill": {
            "type": "choice",
            "instructions": (
                "Which available skill best matches this task? Choose only one offered candidate. "
                "The result is advisory and must not load a skill automatically."
            ),
            "criteria": criteria,
        },
        "needs_skill": {
            "type": "noul",
            "instructions": (
                "Does this task need one of the offered skills? Answer the literal yes/no "
                "question using the Noul probability primitive."
            ),
            "criteria": {
                "true": "A candidate provides specialized procedure or constraints needed for the task",
                "false": "The task is simple or none of the candidates adds material value",
            },
        },
    }
    state = {
        "task": task,
        "skills": [{"name": key, "description": value} for key, value in criteria.items()],
    }
    return state, questions


def _select_skill_small(
    *,
    task: str,
    candidates: list[dict],
    client: Any,
    choice_confidence_threshold: float = DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD,
    needs_skill_threshold: float = DEFAULT_SKILL_NEEDS_THRESHOLD,
    winning_probability_threshold: float = DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD,
    public_or_sanitized_data_ack: bool = False,
) -> dict:
    """Return an advisory skill choice or an explicit abstention.

    Thresholds are bounded local policy values and are deliberately not presented
    as calibrated probabilities. The caller remains responsible for deciding
    whether to load a skill; this function never performs that mutation.
    """
    _require_public_data_ack(public_or_sanitized_data_ack)
    thresholds = {
        "choice_confidence": _bounded_number(choice_confidence_threshold, "choice_confidence_threshold"),
        "needs_skill": _bounded_number(needs_skill_threshold, "needs_skill_threshold"),
        "winning_probability": _bounded_number(winning_probability_threshold, "winning_probability_threshold"),
    }
    state, questions = _small_skill_request_parts(task, candidates)
    criteria = questions["skill"]["criteria"]
    operation_remaining_deadline()
    result = client.decide(
        state,
        questions,
        public_or_sanitized_data_ack=True,
    )
    operation_remaining_deadline()
    metadata = _decision_metadata(result)
    answers = result["answers"]
    if set(answers) != set(questions):
        raise ValueError("Jev skill answer keys do not exactly match the request")
    selected_candidate, confidence, probabilities = _choice_metrics(answers.get("skill"), criteria, "skill")
    needs_answer = answers.get("needs_skill")
    if not isinstance(needs_answer, dict):
        raise TypeError("Jev response is missing answer needs_skill")
    needs_score = _bounded_number(needs_answer.get("noul"), "needs_skill.noul")
    reasons: list[str] = []
    if confidence < thresholds["choice_confidence"]:
        reasons.append("choice_confidence_below_threshold")
    if needs_score < thresholds["needs_skill"]:
        reasons.append("needs_skill_below_threshold")
    winning_probability = probabilities[selected_candidate]
    if winning_probability < thresholds["winning_probability"]:
        reasons.append("winning_probability_below_threshold")
    selected = None if reasons else selected_candidate
    return {
        "status": "selected" if selected is not None else "abstained",
        "selected": selected,
        "abstention_reason": ";".join(reasons) if reasons else None,
        "needs_skill_noul": needs_score,
        # Compatibility name retained as a score label, not a calibrated probability claim.
        "needs_skill_probability": needs_score,
        "confidence": confidence,
        "winning_probability": winning_probability,
        "probabilities": probabilities,
        "thresholds": thresholds,
        "candidate_count": len(candidates),
        "offered_count": len(candidates),
        "excluded_count": 0,
        "shortlist_policy": "complete_candidate_set",
        **metadata,
    }


def _skill_partition_winners(
    *,
    task: str,
    candidates: list[dict],
    client: Any,
    include_needs_skill: bool,
) -> tuple[list[dict], list[dict[str, Any]], float | None]:
    """Evaluate every bounded partition and retain one finalist from each."""
    if not candidates:
        return [], [], None
    winners: list[dict] = []
    metadata: list[dict[str, Any]] = []
    needs_score: float | None = None
    for partition, chunk in enumerate(_skill_chunks(candidates, task=task)):
        state, questions = _skill_request_parts(
            chunk,
            task=task,
            total_count=len(candidates),
            partition=partition,
            include_needs_skill=include_needs_skill and partition == 0,
        )
        name = f"skill_chunk_{partition}"
        criteria = questions[name]["criteria"]
        operation_remaining_deadline()
        result = client.decide(
            state,
            questions,
            public_or_sanitized_data_ack=True,
        )
        operation_remaining_deadline()
        answers = result.get("answers") if isinstance(result, dict) else None
        if not isinstance(answers, dict):
            raise TypeError("Jev large-skill response has no answers object")
        if set(answers) != set(questions):
            raise ValueError("Jev large-skill answer keys do not exactly match the partition")
        choice, _confidence, _probabilities = _choice_metrics(answers.get(name), criteria, name)
        if choice != _SKILL_NONE:
            winners.append(next(item for item in chunk if item["name"] == choice))
        metadata.append(_decision_metadata(result))
        if "needs_skill" in questions:
            needs = answers.get("needs_skill")
            if not isinstance(needs, dict):
                raise TypeError("Jev large-skill response is missing needs_skill")
            needs_score = _bounded_number(needs.get("noul"), "needs_skill.noul")
    return winners, metadata, needs_score


def _skill_direct_request_fits(task: str, candidates: list[dict]) -> bool:
    state, questions = _small_skill_request_parts(task, candidates)
    return _request_size(state, questions) <= MAX_REQUEST_BYTES


def _select_skill_impl(
    *,
    task: str,
    candidates: list[dict],
    client: Any,
    choice_confidence_threshold: float = DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD,
    needs_skill_threshold: float = DEFAULT_SKILL_NEEDS_THRESHOLD,
    winning_probability_threshold: float = DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD,
    public_or_sanitized_data_ack: bool = False,
) -> dict:
    """Select a skill, reducing arbitrarily large catalogs through Jev fan-out.

    A normal Choice remains one request for small catalogs. Larger catalogs are
    searched in full through partition questions and recursive reduction, then a
    final bounded Choice applies the same policy thresholds.
    """
    if not isinstance(candidates, list) or not candidates:
        return _select_skill_small(
            task=task, candidates=candidates, client=client,
            choice_confidence_threshold=choice_confidence_threshold,
            needs_skill_threshold=needs_skill_threshold,
            winning_probability_threshold=winning_probability_threshold,
            public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        )
    if len(candidates) <= _CHOICE_MAX_OPTIONS and _skill_direct_request_fits(task, candidates):
        return _select_skill_small(
            task=task, candidates=candidates, client=client,
            choice_confidence_threshold=choice_confidence_threshold,
            needs_skill_threshold=needs_skill_threshold,
            winning_probability_threshold=winning_probability_threshold,
            public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        )

    _require_public_data_ack(public_or_sanitized_data_ack)
    # Validate the complete catalog before any network call. This prevents a
    # malformed tail from being hidden by partitioning.
    _criteria(candidates, "name", max_entries=None)
    pool = list(candidates)
    rounds = 0
    reduction_metadata: list[dict[str, Any]] = []
    first_needs_score: float | None = None
    while len(pool) > _CHOICE_MAX_OPTIONS or not _skill_direct_request_fits(task, pool):
        pool, metadata, needs_score = _skill_partition_winners(
            task=task,
            candidates=pool,
            client=client,
            include_needs_skill=rounds == 0,
        )
        rounds += 1
        reduction_metadata.extend(metadata)
        if first_needs_score is None:
            first_needs_score = needs_score
        if not pool:
            thresholds = {
                "choice_confidence": _bounded_number(choice_confidence_threshold, "choice_confidence_threshold"),
                "needs_skill": _bounded_number(needs_skill_threshold, "needs_skill_threshold"),
                "winning_probability": _bounded_number(winning_probability_threshold, "winning_probability_threshold"),
            }
            needs = first_needs_score
            return {
                "status": "abstained", "selected": None,
                "abstention_reason": "no_partition_candidate",
                "needs_skill_noul": needs, "needs_skill_probability": needs,
                "confidence": 0.0, "winning_probability": 0.0,
                "probabilities": {}, "thresholds": thresholds,
                "candidate_count": len(candidates), "reduction_rounds": rounds,
                "offered_count": len(candidates), "excluded_count": 0,
                "shortlist_policy": "full_partition_fan_out",
                "reduction_metadata": reduction_metadata,
                **_aggregate_metadata(reduction_metadata),
            }
    result = _select_skill_small(
        task=task, candidates=pool, client=client,
        choice_confidence_threshold=choice_confidence_threshold,
        needs_skill_threshold=needs_skill_threshold,
        winning_probability_threshold=winning_probability_threshold,
        public_or_sanitized_data_ack=True,
    )
    result["candidate_count"] = len(candidates)
    result["offered_count"] = len(candidates)
    result["excluded_count"] = 0
    result["shortlist_policy"] = "full_partition_fan_out"
    result["reduction_rounds"] = rounds
    result["reduction_metadata"] = reduction_metadata
    final_metadata = {
        "model": result.get("model"),
        "latency_ms": result.get("latency_ms"),
        "usage": result.get("usage") or {},
        "request_count": int(result.get("request_count") or 1),
    }
    result.update(_aggregate_metadata(reduction_metadata + [final_metadata]))
    return result


def select_skill(
    *, task: str, candidates: list[dict], client: Any,
    choice_confidence_threshold: float = DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD,
    needs_skill_threshold: float = DEFAULT_SKILL_NEEDS_THRESHOLD,
    winning_probability_threshold: float = DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD,
    public_or_sanitized_data_ack: bool = False,
    deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
) -> dict:
    with request_budget_scope(
        client, MAX_DECISION_REQUESTS, deadline_seconds=deadline_seconds
    ):
        return _select_skill_impl(
            task=task, candidates=candidates, client=client,
            choice_confidence_threshold=choice_confidence_threshold,
            needs_skill_threshold=needs_skill_threshold,
            winning_probability_threshold=winning_probability_threshold,
            public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        )


def _multi_skill_batch_questions(
    batch: list[dict], *, offset: int, task: str
) -> dict[str, dict[str, Any]]:
    """Build the exact multi-skill questions sent to Jev for one batch.

    Every question carries its candidate's exact identifier and description, so
    the caller can size a request using the same structure it will serialize.
    """
    return {
        f"{_MULTI_SKILL_PREFIX}{offset + index}": {
            "type": "noul",
            "instructions": (
                "Does this exact offered skill materially help with the task? "
                f"Candidate identifier: {item['name']}. "
                f"Candidate description: {item.get('description', '')}"
            ),
            "criteria": {"true": "The skill helps", "false": "The skill does not help"},
        }
        for index, item in enumerate(batch)
    }


def _multi_skill_batches(candidates: list[dict], *, task: str) -> list[list[dict]]:
    batches: list[list[dict]] = []
    current: list[dict] = []
    offset = 0
    for candidate in candidates:
        while True:
            trial = current + [candidate]
            questions = _multi_skill_batch_questions(
                trial, offset=offset, task=task
            )
            too_large = _request_size(
                {"task": task, "skills": trial}, questions
            ) > MAX_REQUEST_BYTES
            if not current and too_large:
                raise ValueError(
                    "a single skill candidate exceeds the bounded serialized request budget"
                )
            if current and (len(trial) > MAX_QUESTIONS_PER_REQUEST or too_large):
                batches.append(current)
                offset += len(current)
                current = []
                continue
            current = trial
            break
    if current:
        batches.append(current)
    return batches


def select_skills(
    *,
    task: str,
    candidates: list[dict],
    client: Any,
    selection_threshold: float = DEFAULT_SKILL_NEEDS_THRESHOLD,
    max_selections: int | None = None,
    public_or_sanitized_data_ack: bool = False,
    deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
) -> dict:
    """Return a typed list of advisory skill identifiers.

    Multi-selection is deliberately separate from ``select_skill``: each exact
    candidate receives an independent Noul fit score, and no skill is loaded.
    """
    _require_public_data_ack(public_or_sanitized_data_ack)
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates must contain at least 1 entry")
    _criteria(candidates, "name", max_entries=None)
    threshold = _bounded_number(selection_threshold, "selection_threshold")
    if max_selections is not None and (
        type(max_selections) is not int or not 1 <= max_selections <= len(candidates)
    ):
        raise ValueError("max_selections must be a positive bound within the candidate set")
    with request_budget_scope(
        client, MAX_DECISION_REQUESTS, deadline_seconds=deadline_seconds
    ):
        scores: dict[str, float] = {}
        metadata: list[dict[str, Any]] = []
        offset = 0
        for batch in _multi_skill_batches(candidates, task=task):
            # Reuse the exact shared construction so the size checked during
            # batching equals the payload actually sent to Jev.
            questions = _multi_skill_batch_questions(batch, offset=offset, task=task)
            operation_remaining_deadline()
            result = client.decide(
                {"task": task, "skills": batch},
                questions,
                public_or_sanitized_data_ack=True,
            )
            operation_remaining_deadline()
            metadata.append(_decision_metadata(result))
            answers = result.get("answers") if isinstance(result, dict) else None
            if not isinstance(answers, dict):
                raise TypeError("Jev multi-skill response has no answers object")
            expected_names = {
                f"{_MULTI_SKILL_PREFIX}{offset + index}"
                for index in range(len(batch))
            }
            if set(answers) != expected_names:
                raise ValueError("Jev multi-skill answer keys do not exactly match the batch")
            for index, candidate in enumerate(batch):
                name = f"{_MULTI_SKILL_PREFIX}{offset + index}"
                scores[candidate["name"]] = _noul_score(answers.get(name), name)
            offset += len(batch)
    selected = [
        candidate["name"]
        for candidate in candidates
        if scores[candidate["name"]] >= threshold
    ]
    selected.sort(key=lambda name: (-scores[name], next(
        index for index, item in enumerate(candidates) if item["name"] == name
    )))
    if max_selections is not None:
        selected = selected[:max_selections]
    aggregate = _aggregate_metadata(metadata)
    return {
        "status": "selected" if selected else "abstained",
        "selected": selected,
        "scores": scores,
        "selection_threshold": threshold,
        "max_selections": max_selections,
        "candidate_count": len(candidates),
        "shortlist_policy": "independent_multi_skill_scores",
        "abstention_reason": None if selected else "no_skill_met_selection_threshold",
        **aggregate,
    }


def _model_requirements(requirements: dict) -> dict[str, Any]:
    if not isinstance(requirements, dict):
        raise ValueError("requirements must be an object")
    unknown = set(requirements) - _MODEL_REQUIREMENT_KEYS
    if unknown:
        raise ValueError(f"unsupported model requirement fields: {sorted(unknown)!r}")
    parsed: dict[str, Any] = {}
    if "data_classes" in requirements:
        parsed["data_classes"] = _string_list(requirements["data_classes"], "requirements.data_classes")
    if "tool_capabilities" in requirements:
        parsed["tool_capabilities"] = _string_list(
            requirements["tool_capabilities"], "requirements.tool_capabilities"
        )
    if "context_limit" in requirements:
        parsed["context_limit"] = _positive_integer(requirements["context_limit"], "requirements.context_limit")
    if "budget" in requirements:
        parsed["budget"] = _nonnegative_number(requirements["budget"], "requirements.budget")
    return parsed


def _model_candidates(candidates: list[dict]) -> list[dict[str, Any]]:
    if not isinstance(candidates, list) or not candidates:
        raise ValueError("candidates must contain at least 1 entry")
    # Validate exact identifiers and common descriptions first.
    _criteria(candidates, "id", max_entries=None)
    records: list[dict[str, Any]] = []
    for position, item in enumerate(candidates):
        identifier = item["id"]
        approved = item.get("approved")
        if type(approved) is not bool:
            raise ValueError(f"candidate {identifier!r} requires an explicit boolean approved field")
        description = item.get("description", "")
        if type(description) is not str:
            raise ValueError(f"candidate {identifier!r} description must be a string")
        allowed = None
        if "data_classes_allowed" in item:
            allowed = _string_list(item["data_classes_allowed"], f"candidate {identifier!r}.data_classes_allowed")
        capabilities = None
        if "tool_capabilities" in item:
            capabilities = _string_list(item["tool_capabilities"], f"candidate {identifier!r}.tool_capabilities")
        context_limit = None
        if "context_limit" in item:
            context_limit = _positive_integer(item["context_limit"], f"candidate {identifier!r}.context_limit")
        cost = None
        if "cost" in item:
            cost = _nonnegative_number(item["cost"], f"candidate {identifier!r}.cost")
        records.append({
            "id": identifier,
            "description": description,
            "approved": approved,
            "data_classes_allowed": allowed,
            "tool_capabilities": capabilities,
            "context_limit": context_limit,
            "cost": cost,
            "position": position,
        })
    return records


def _eligible_model_candidates(
    candidates: list[dict[str, Any]], requirements: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    required_classes = set(requirements.get("data_classes", []))
    required_tools = set(requirements.get("tool_capabilities", []))
    required_context = requirements.get("context_limit")
    budget = requirements.get("budget")
    for candidate in candidates:
        reasons: list[str] = []
        if candidate["approved"] is not True:
            reasons.append("not_approved")
        if candidate["cost"] is None:
            # A cost is always required because the final choice is code-owned and
            # must be provably the cheapest qualified candidate.
            reasons.append("missing_cost")
        if required_classes:
            allowed = candidate["data_classes_allowed"]
            if allowed is None:
                reasons.append("missing_data_classes_allowed")
            elif not required_classes.issubset(allowed):
                reasons.append("data_class_not_allowed")
        if required_tools:
            capabilities = candidate["tool_capabilities"]
            if capabilities is None:
                reasons.append("missing_tool_capabilities")
            elif not required_tools.issubset(capabilities):
                reasons.append("tool_capability_missing")
        if required_context is not None:
            context_limit = candidate["context_limit"]
            if context_limit is None:
                reasons.append("missing_context_limit")
            elif context_limit < required_context:
                reasons.append("context_limit_too_small")
        if budget is not None and candidate["cost"] is not None and candidate["cost"] > budget:
            reasons.append("over_budget")
        if reasons:
            excluded.append({"id": candidate["id"], "reasons": reasons})
        else:
            eligible.append(candidate)
    return eligible, excluded


def _noul_score(answer: Any, name: str) -> float:
    if not isinstance(answer, dict):
        raise TypeError(f"Jev response is missing answer {name}")
    if set(answer) != {"noul"}:
        raise ValueError(f"Jev noul {name} has unexpected response fields")
    return _bounded_number(answer.get("noul"), f"{name}.noul")


def _route_metadata(result: Any) -> dict[str, Any]:
    metadata = _decision_metadata(result)
    if metadata["usage"].get("cost") is not None:
        metadata["usage"] = dict(metadata["usage"])
        metadata["usage"]["cost"] = _nonnegative_number(metadata["usage"]["cost"], "usage.cost")
    return metadata


def _model_request_parts(
    batch: list[dict[str, Any]],
    *,
    task: str = "",
    requirements: dict[str, Any] | None = None,
    offset: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    questions: dict[str, dict[str, Any]] = {}
    eligible_state: list[dict[str, Any]] = []
    for index, candidate in enumerate(batch):
        question_name = f"fit_{offset + index}"
        questions[question_name] = {
            "type": "noul",
            "instructions": (
                f"Does eligible_candidates[{index}] with id {candidate['id']!r} have the capability fit "
                "for the task? Answer the literal yes/no question using the Noul probability primitive."
            ),
            "criteria": {"true": "The candidate capabilities fit the task", "false": "They do not"},
        }
        eligible_state.append({
            "question": question_name,
            "id": candidate["id"],
            "description": candidate["description"],
            "data_classes_allowed": candidate["data_classes_allowed"],
            "tool_capabilities": candidate["tool_capabilities"],
            "context_limit": candidate["context_limit"],
            "cost": candidate["cost"],
        })
    state = {"task": task, "requirements": requirements or {}, "eligible_candidates": eligible_state}
    return state, questions


def _model_batches(
    candidates: list[dict[str, Any]],
    *,
    task: str = "",
    requirements: dict[str, Any] | None = None,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    offset = 0
    for candidate in candidates:
        while True:
            trial = current + [candidate]
            state, questions = _model_request_parts(
                trial, task=task, requirements=requirements, offset=offset
            )
            criteria_size = len(
                json.dumps(
                    state["eligible_candidates"], ensure_ascii=False, allow_nan=False
                ).encode("utf-8")
            )
            too_large = _request_size(state, questions) > MAX_REQUEST_BYTES
            if not current and too_large:
                raise ValueError("a single model candidate exceeds the bounded serialized request budget")
            if current and (
                len(trial) > 200
                or criteria_size > _PARTITION_CRITERIA_BYTES
                or too_large
            ):
                batches.append(current)
                offset += len(current)
                current = []
                continue
            current = trial
            break
    if current:
        batches.append(current)
    return batches


def _route_model_impl(
    *,
    task: str,
    candidates: list[dict],
    requirements: dict,
    client: Any,
    capability_fit_threshold: float = DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD,
    public_or_sanitized_data_ack: bool = False,
) -> dict:
    """Filter model candidates locally, ask Jev for fit scores, then pick cheapest.

    Policy metadata is never inferred from descriptions. No runtime model is
    changed and no automatic fallback is attempted when the route abstains.
    """
    _require_public_data_ack(public_or_sanitized_data_ack)
    fit_threshold = _bounded_number(capability_fit_threshold, "capability_fit_threshold")
    parsed_requirements = _model_requirements(requirements)
    records = _model_candidates(candidates)
    eligible, excluded = _eligible_model_candidates(records, parsed_requirements)
    base = {
        "status": "abstained",
        "selected": None,
        "eligible_candidates": [candidate["id"] for candidate in eligible],
        "excluded_candidates": excluded,
        "qualified_candidates": [],
        "capability_fit_scores": {},
        "capability_fit_threshold": fit_threshold,
        "selection_policy": "cheapest qualified candidate; advisory only; runtime model is unchanged",
        "model": None,
        "latency_ms": None,
        "usage": {},
    }
    if not eligible:
        base["abstention_reason"] = "no_eligible_candidates"
        return base

    scores: dict[str, float] = {}
    qualified: list[dict[str, Any]] = []
    call_metadata: list[dict[str, Any]] = []
    global_offset = 0
    for batch in _model_batches(
        eligible, task=task, requirements=parsed_requirements
    ):
        eligible_state, questions = _model_request_parts(
            batch, task=task, requirements=parsed_requirements, offset=global_offset
        )
        operation_remaining_deadline()
        result = client.decide(
            eligible_state,
            questions,
            public_or_sanitized_data_ack=True,
        )
        operation_remaining_deadline()
        call_metadata.append(_route_metadata(result))
        answers = result["answers"]
        expected_names = {f"fit_{global_offset + index}" for index in range(len(batch))}
        if set(answers) != expected_names:
            raise ValueError("Jev model-routing answer keys do not exactly match the batch")
        for index, candidate in enumerate(batch):
            name = f"fit_{global_offset + index}"
            score = _noul_score(answers.get(name), name)
            scores[candidate["id"]] = score
            if score >= fit_threshold:
                qualified.append(candidate)
        global_offset += len(batch)
    if call_metadata:
        base.update(call_metadata[-1])
        base.update(_aggregate_metadata(call_metadata))
    base["capability_fit_scores"] = scores
    base["qualified_candidates"] = [candidate["id"] for candidate in qualified]
    if not qualified:
        base["abstention_reason"] = "no_candidate_met_capability_fit_threshold"
        return base
    selected = min(qualified, key=lambda candidate: (candidate["cost"], candidate["position"]))
    base.update({"status": "selected", "selected": selected["id"], "abstention_reason": None})
    return base


def route_model(
    *, task: str, candidates: list[dict], requirements: dict, client: Any,
    capability_fit_threshold: float = DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD,
    public_or_sanitized_data_ack: bool = False,
    deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
) -> dict:
    with request_budget_scope(
        client, MAX_DECISION_REQUESTS, deadline_seconds=deadline_seconds
    ):
        return _route_model_impl(
            task=task, candidates=candidates, requirements=requirements, client=client,
            capability_fit_threshold=capability_fit_threshold,
            public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        )
