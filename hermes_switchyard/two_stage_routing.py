"""Two-stage automatic skill routing planner (issue #94).

This module is a pure planner plus a bounded executor. It does not register a
hook, read Hermes configuration, or load a skill. ``automatic.py`` owns wiring.

Stage 1 sends exact candidate names only, in bounded partitions. Partition 0
also asks the ``needs_skill`` Noul. When that score is below the early-stop
threshold, routing stops after one request. Otherwise the remaining partitions
run in parallel inside the aggregate operation deadline.

Stage 2 re-checks the global top K stage-1 candidates in one request. The
``hosted_detail`` level decides what crosses the hosted boundary in stage 2:

- ``names`` (default): exact names only. The data boundary does not change.
- ``descriptions``: bounded local descriptions for the top K only.
- ``excerpt``: descriptions plus a bounded SKILL.md excerpt for the top K only.

Detail text is opt-in. Each detail field is scanned locally with the same
restricted-pattern scan that gates task text. A field that fails the scan is
withheld and only the exact name is sent. Task text, conversation history, and
full skill bodies never enter this module's hosted payloads beyond the
caller-authorized ``task`` string.

The platform gate skips non-interactive turns (API server, cron, batch,
webhook, Kanban worker) by default. ``automatic_skill_platforms`` overrides it.
"""
from __future__ import annotations

import contextvars
import json
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Any

from .client import (
    MAX_DECISION_REQUESTS,
    MAX_REQUEST_BYTES,
    DeadlineExceeded,
    PartialAccountingError,
    operation_deadline_scope,
    operation_remaining_deadline,
    request_budget_scope,
)
from .routing import (
    DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD,
    DEFAULT_SKILL_NEEDS_THRESHOLD,
    DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD,
    _SKILL_NONE,
    _aggregate_metadata,
    _bounded_number,
    _choice_metrics,
    _criteria,
    _decision_metadata,
    _CHOICE_MAX_OPTIONS,
    _PARTITION_CRITERIA_BYTES,
    _SKILL_PARTITION_SIZE,
    _request_size,
    _select_skill_small,
    _skill_direct_request_fits,
    _skill_request_parts,
)

# Platform policy -----------------------------------------------------------

PLATFORM_POLICY_INTERACTIVE = "interactive"
PLATFORM_POLICY_ALL = "all"
# Machine-generated turns that never ask for a skill. Kanban workers carry a
# normal platform key, so the caller reports them through ``kanban_worker``.
NONINTERACTIVE_PLATFORMS = frozenset({"api_server", "batch", "cron", "kanban", "webhook"})
PLATFORM_REASON_ALLOWED = "platform_allowed"
PLATFORM_REASON_NONINTERACTIVE = "noninteractive_platform"
PLATFORM_REASON_NOT_LISTED = "platform_not_listed"
PLATFORM_REASON_KANBAN_WORKER = "kanban_worker"
_UNKNOWN_PLATFORM = "unknown"
MAX_PLATFORM_NAME_CHARS = 64

# Stage-2 detail --------------------------------------------------------------

HOSTED_DETAIL_NAMES = "names"
HOSTED_DETAIL_DESCRIPTIONS = "descriptions"
HOSTED_DETAIL_EXCERPT = "excerpt"
HOSTED_DETAIL_LEVELS = (HOSTED_DETAIL_NAMES, HOSTED_DETAIL_DESCRIPTIONS, HOSTED_DETAIL_EXCERPT)
DEFAULT_HOSTED_DETAIL = HOSTED_DETAIL_NAMES
MAX_DETAIL_DESCRIPTION_CHARS = 400
MAX_DETAIL_EXCERPT_CHARS = 1_200

# Planner bounds --------------------------------------------------------------

DEFAULT_RECHECK_TOP_K = 3
MAX_RECHECK_TOP_K = 8
# Local policy value, not a calibrated probability. Stop only when the hosted
# needs_skill signal is clearly low; the final gate still uses the stricter
# DEFAULT_SKILL_NEEDS_THRESHOLD.
DEFAULT_EARLY_STOP_THRESHOLD = 0.30
DEFAULT_STAGE1_MIN_PROBABILITY = 0.05
DEFAULT_PARALLEL_REQUESTS = 4
MAX_PARALLEL_REQUESTS = 8
# Stage 1 partitions plus one stage-2 request, bounded like the legacy
# select_skill operation. 403 skills need 3 partitions.
MAX_PLANNED_REQUESTS = MAX_DECISION_REQUESTS
_POLL_SECONDS = 0.05
# Headroom for model-slug and provider-field differences between the local size
# estimate and the client's authoritative payload check.
_SIZE_MARGIN_BYTES = 512
_REQUEST_LIMIT_BYTES = MAX_REQUEST_BYTES - _SIZE_MARGIN_BYTES

SHORTLIST_POLICY_TWO_STAGE = "two_stage_recheck"
SHORTLIST_POLICY_TWO_STAGE_EARLY_STOP = "two_stage_early_stop"
SHORTLIST_POLICY_TWO_STAGE_SINGLE = "two_stage_single_request"

# Plugin config keys read by ``TwoStageConfig.from_mapping``.
TWO_STAGE_CONFIG_KEYS = (
    "automatic_skill_two_stage",
    "automatic_skill_platforms",
    "automatic_skill_hosted_detail",
    "automatic_skill_recheck_top_k",
    "automatic_skill_early_stop",
    "automatic_skill_early_stop_threshold",
    "automatic_skill_stage1_min_probability",
    "automatic_skill_parallel_requests",
)

_STAGE2_NONE_TEXT = "No offered skill materially fits the task"
# The early-stop question must judge the task against the whole catalog, not
# only partition 0. Otherwise a task whose skill sits in a later partition
# would stop early with a false "no skill needed".
_EARLY_STOP_QUESTION = {
    "type": "noul",
    "instructions": (
        "Does this task need a specialized skill from the full catalog? The state lists only "
        "one partition; candidate_count is the full catalog size. Judge the task itself, not "
        "only the listed names."
    ),
    "criteria": {
        "true": "The task needs a specialized procedure, tool, or domain constraint",
        "false": "The task is conversational, trivial, or general knowledge",
    },
}
_NEEDS_SKILL_QUESTION = {
    "type": "noul",
    "instructions": "Does this task need one of the offered skills?",
    "criteria": {
        "true": "A candidate provides specialized procedure or constraints needed for the task",
        "false": "The task is simple or none of the candidates adds material value",
    },
}


@dataclass(frozen=True)
class TwoStageConfig:
    """Validated two-stage settings. Invalid input falls back to safe defaults."""

    enabled: bool = True
    platform_policy: str = PLATFORM_POLICY_INTERACTIVE
    platform_allowlist: frozenset[str] = frozenset()
    hosted_detail: str = DEFAULT_HOSTED_DETAIL
    recheck_top_k: int = DEFAULT_RECHECK_TOP_K
    early_stop: bool = True
    early_stop_threshold: float = DEFAULT_EARLY_STOP_THRESHOLD
    stage1_min_probability: float = DEFAULT_STAGE1_MIN_PROBABILITY
    parallel_requests: int = DEFAULT_PARALLEL_REQUESTS

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any] | None) -> "TwoStageConfig":
        """Parse ``automatic_skill_*`` keys. Unknown or invalid values use defaults."""
        raw = raw if isinstance(raw, Mapping) else {}
        policy, allowlist = _parse_platforms(raw.get("automatic_skill_platforms"))
        detail = raw.get("automatic_skill_hosted_detail", DEFAULT_HOSTED_DETAIL)
        if detail not in HOSTED_DETAIL_LEVELS:
            detail = DEFAULT_HOSTED_DETAIL
        return cls(
            enabled=_bool(raw.get("automatic_skill_two_stage"), True),
            platform_policy=policy,
            platform_allowlist=allowlist,
            hosted_detail=detail,
            recheck_top_k=_bounded_int(
                raw.get("automatic_skill_recheck_top_k"), DEFAULT_RECHECK_TOP_K, 1, MAX_RECHECK_TOP_K
            ),
            early_stop=_bool(raw.get("automatic_skill_early_stop"), True),
            early_stop_threshold=_bounded_float(
                raw.get("automatic_skill_early_stop_threshold"), DEFAULT_EARLY_STOP_THRESHOLD
            ),
            stage1_min_probability=_bounded_float(
                raw.get("automatic_skill_stage1_min_probability"), DEFAULT_STAGE1_MIN_PROBABILITY
            ),
            parallel_requests=_bounded_int(
                raw.get("automatic_skill_parallel_requests"),
                DEFAULT_PARALLEL_REQUESTS,
                1,
                MAX_PARALLEL_REQUESTS,
            ),
        )


def _bool(value: Any, default: bool) -> bool:
    return value if type(value) is bool else default


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        return default
    return max(minimum, min(value, maximum))


def _bounded_float(value: Any, default: float) -> float:
    if type(value) not in (int, float) or value != value or value in (float("inf"), float("-inf")):
        return default
    return max(0.0, min(float(value), 1.0))


def _normalize_platform(value: Any) -> str:
    if type(value) is not str:
        return _UNKNOWN_PLATFORM
    text = value.strip().casefold()
    if not text or len(text) > MAX_PLATFORM_NAME_CHARS or not text.isprintable():
        return _UNKNOWN_PLATFORM
    return text


def _parse_platforms(value: Any) -> tuple[str, frozenset[str]]:
    if value is None or value == PLATFORM_POLICY_INTERACTIVE:
        return PLATFORM_POLICY_INTERACTIVE, frozenset()
    if value == PLATFORM_POLICY_ALL:
        return PLATFORM_POLICY_ALL, frozenset()
    if isinstance(value, (list, tuple)) and value and all(type(item) is str for item in value):
        names = frozenset(_normalize_platform(item) for item in value) - {_UNKNOWN_PLATFORM}
        if names == {PLATFORM_POLICY_ALL}:
            return PLATFORM_POLICY_ALL, frozenset()
        if PLATFORM_POLICY_ALL in names:
            return PLATFORM_POLICY_INTERACTIVE, frozenset()
        if names:
            return "allowlist", names
    return PLATFORM_POLICY_INTERACTIVE, frozenset()


def detect_kanban_worker(environ: Mapping[str, str]) -> bool:
    """Return True when the process is a dispatcher-spawned Kanban worker."""
    value = environ.get("HERMES_KANBAN_TASK") if isinstance(environ, Mapping) else None
    return isinstance(value, str) and bool(value.strip())


def platform_decision(
    platform: Any,
    config: TwoStageConfig,
    *,
    kanban_worker: bool = False,
) -> tuple[bool, str]:
    """Return ``(route, reason_code)`` for one turn.

    ``interactive`` (default) skips ``NONINTERACTIVE_PLATFORMS`` and Kanban
    workers; unknown or empty platforms still route so older hosts keep the
    current behavior. ``all`` routes every turn. An explicit list routes only
    listed platforms; a Kanban worker routes only when ``kanban`` is listed.
    """
    name = _normalize_platform(platform)
    if config.platform_policy == PLATFORM_POLICY_ALL:
        return True, PLATFORM_REASON_ALLOWED
    if config.platform_policy == "allowlist":
        if kanban_worker and "kanban" not in config.platform_allowlist:
            return False, PLATFORM_REASON_KANBAN_WORKER
        if name in config.platform_allowlist:
            return True, PLATFORM_REASON_ALLOWED
        return False, PLATFORM_REASON_NOT_LISTED
    if kanban_worker:
        return False, PLATFORM_REASON_KANBAN_WORKER
    if name in NONINTERACTIVE_PLATFORMS:
        return False, PLATFORM_REASON_NONINTERACTIVE
    return True, PLATFORM_REASON_ALLOWED


# Planning ----------------------------------------------------------------------


@dataclass(frozen=True)
class TwoStagePlan:
    """Request layout computed before any network call.

    ``direct`` means the catalog fits one Choice request and detail is
    ``names``: the executor reuses ``routing._select_skill_small`` so the wire
    request is identical to the legacy small-catalog path.
    """

    direct: bool
    partitions: tuple[tuple[str, ...], ...]
    stage2_enabled: bool
    max_requests: int
    hosted_detail: str
    recheck_top_k: int

    @property
    def stage1_requests(self) -> int:
        return len(self.partitions)


def _names_only(candidates: Sequence[Any]) -> list[dict[str, Any]]:
    names: list[dict[str, Any]] = []
    for item in candidates:
        name = item if isinstance(item, str) else item.get("name") if isinstance(item, Mapping) else None
        if name == _SKILL_NONE:
            raise ValueError(f"candidate name {_SKILL_NONE!r} is reserved")
        names.append({"name": name})
    _criteria(names, "name", max_entries=None)
    return names


def _partition_parts(
    names: Sequence[str],
    *,
    task: str,
    total_count: int,
    partition: int,
    include_needs_skill: bool,
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build the exact stage-1 state and questions for one partition."""
    state, questions = _skill_request_parts(
        [{"name": name} for name in names],
        task=task,
        total_count=total_count,
        partition=partition,
        include_needs_skill=False,
    )
    if include_needs_skill:
        questions["needs_skill"] = dict(_EARLY_STOP_QUESTION)
    return state, questions


def _partition_names(names: list[str], task: str) -> tuple[tuple[str, ...], ...]:
    """Pack names into partitions that each fit exactly one provider request.

    Sizing uses the same builder as the wire request, including the longer
    catalog-level early-stop question on partition 0.
    """
    partitions: list[tuple[str, ...]] = []
    current: list[str] = []
    total = len(names)
    for name in names:
        while True:
            index = len(partitions)
            trial = current + [name]
            state, questions = _partition_parts(
                trial, task=task, total_count=total, partition=index, include_needs_skill=index == 0
            )
            criteria_bytes = len(
                json.dumps(questions[f"skill_chunk_{index}"]["criteria"], ensure_ascii=False).encode("utf-8")
            )
            too_large = _request_size(state, questions) > _REQUEST_LIMIT_BYTES
            if not current and too_large:
                raise ValueError("a single skill candidate exceeds the bounded serialized request budget")
            if current and (
                len(trial) > _SKILL_PARTITION_SIZE
                or criteria_bytes > _PARTITION_CRITERIA_BYTES
                or too_large
            ):
                partitions.append(tuple(current))
                current = []
                continue
            current = trial
            break
    if current:
        partitions.append(tuple(current))
    return tuple(partitions)


def plan_two_stage(task: str, candidates: Sequence[Any], config: TwoStageConfig) -> TwoStagePlan:
    """Plan partitions and the request bound without sending anything.

    A single partition with ``names`` detail needs no stage 2: its stage-1
    choice already covers every offered name.
    """
    if type(task) is not str:
        raise ValueError("task must be a string")
    if not isinstance(candidates, (list, tuple)) or not candidates:
        raise ValueError("candidates must contain at least 1 entry")
    names = _names_only(candidates)
    if (
        config.hosted_detail == HOSTED_DETAIL_NAMES
        and len(names) <= _CHOICE_MAX_OPTIONS
        and _skill_direct_request_fits(task, names)
    ):
        return TwoStagePlan(
            direct=True,
            partitions=(tuple(item["name"] for item in names),),
            stage2_enabled=False,
            max_requests=1,
            hosted_detail=config.hosted_detail,
            recheck_top_k=config.recheck_top_k,
        )
    partitions = _partition_names([item["name"] for item in names], task)
    stage2 = len(partitions) > 1 or config.hosted_detail != HOSTED_DETAIL_NAMES
    total = len(partitions) + (1 if stage2 else 0)
    if total > MAX_PLANNED_REQUESTS:
        raise ValueError("two-stage plan exceeds the bounded request budget")
    return TwoStagePlan(
        direct=False,
        partitions=partitions,
        stage2_enabled=stage2,
        max_requests=total,
        hosted_detail=config.hosted_detail,
        recheck_top_k=config.recheck_top_k,
    )


def _scan_reason(text: str) -> str | None:
    from .automatic import _local_scan_reason  # late import: automatic imports routing

    return _local_scan_reason(text, text)


def _bounded_detail(value: Any, limit: int) -> str | None:
    if type(value) is not str:
        return None
    text = " ".join(value.split())[:limit]
    return text or None


def skill_excerpt(content: Any, limit: int = MAX_DETAIL_EXCERPT_CHARS) -> str | None:
    """Return a bounded SKILL.md excerpt without YAML frontmatter.

    Use as the ``excerpt_loader`` body around Hermes' ``skill_view`` content.
    """
    if type(content) is not str:
        return None
    text = content.lstrip()
    if text.startswith("---"):
        end = text.find("\n---", 3)
        text = text[end + 4:] if end != -1 else ""
    return _bounded_detail(text, limit)


def build_stage2_skills(
    names: Sequence[str],
    candidates_by_name: Mapping[str, Mapping[str, Any]],
    detail: str,
    *,
    excerpt_loader: Callable[[str], Any] | None = None,
) -> tuple[list[dict[str, str]], int]:
    """Return stage-2 skill rows and the count of withheld detail fields.

    ``names`` returns exact names only. Opt-in detail is bounded and scanned;
    a field that fails the local scan, or a loader that raises, sends no detail.
    """
    rows: list[dict[str, str]] = []
    withheld = 0
    for name in names:
        row = {"name": name}
        if detail in (HOSTED_DETAIL_DESCRIPTIONS, HOSTED_DETAIL_EXCERPT):
            source = candidates_by_name.get(name) or {}
            description = _bounded_detail(source.get("description"), MAX_DETAIL_DESCRIPTION_CHARS)
            if description is not None and description != name:
                if _scan_reason(description) is None:
                    row["description"] = description
                else:
                    withheld += 1
        if detail == HOSTED_DETAIL_EXCERPT and excerpt_loader is not None:
            try:
                raw_excerpt = excerpt_loader(name)
            except Exception:  # noqa: BLE001 -- detail is optional; fail closed to names
                raw_excerpt = None
            excerpt = _bounded_detail(raw_excerpt, MAX_DETAIL_EXCERPT_CHARS)
            if excerpt is not None:
                if _scan_reason(excerpt) is None:
                    row["excerpt"] = excerpt
                else:
                    withheld += 1
        rows.append(row)
    return rows, withheld


def stage2_request_parts(
    task: str, rows: Sequence[Mapping[str, str]]
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Build the exact stage-2 state and questions.

    Detail stays in ``state``; criteria carry the description when present so
    the Choice question sees the same bounded text.
    """
    criteria: dict[str, str] = {}
    for row in rows:
        name = row["name"]
        if name == _SKILL_NONE or name in criteria:
            raise ValueError("stage-2 candidate names must be unique and not reserved")
        criteria[name] = row.get("description") or name
    criteria[_SKILL_NONE] = _STAGE2_NONE_TEXT
    questions = {
        "skill": {
            "type": "choice",
            "instructions": (
                "Re-check these finalists. Which offered skill best matches this task? Choose the "
                "none option when no finalist adds material value. The result is advisory."
            ),
            "criteria": criteria,
        },
        "needs_skill": dict(_NEEDS_SKILL_QUESTION),
    }
    state = {"task": task, "stage": 2, "skills": [dict(row) for row in rows]}
    return state, questions


def _fit_stage2(task: str, rows: list[dict[str, str]]) -> tuple[list[dict[str, str]], int]:
    """Drop excerpts, then descriptions, until the request fits the byte bound."""
    trimmed = 0
    for field in ("excerpt", "description"):
        state, questions = stage2_request_parts(task, rows)
        if _request_size(state, questions) <= _REQUEST_LIMIT_BYTES:
            return rows, trimmed
        for row in reversed(rows):
            if field in row:
                del row[field]
                trimmed += 1
                state, questions = stage2_request_parts(task, rows)
                if _request_size(state, questions) <= _REQUEST_LIMIT_BYTES:
                    return rows, trimmed
    state, questions = stage2_request_parts(task, rows)
    if _request_size(state, questions) > _REQUEST_LIMIT_BYTES:
        raise ValueError("stage-2 request exceeds the bounded serialized request budget")
    return rows, trimmed


def rank_stage1(
    partition_answers: Sequence[tuple[tuple[str, ...], Mapping[str, float]]],
    *,
    top_k: int,
    min_probability: float,
) -> list[str]:
    """Merge per-partition Choice probabilities into a global top-K list.

    Probabilities are normalized within each partition, so this is a ranking
    heuristic, not a calibrated cross-partition probability. Ties keep catalog
    order. The none option never enters the list.
    """
    scored: list[tuple[float, int, int, str]] = []
    for partition_index, (names, probabilities) in enumerate(partition_answers):
        for position, name in enumerate(names):
            probability = float(probabilities.get(name, 0.0))
            if probability >= min_probability and probability > 0.0:
                scored.append((probability, partition_index, position, name))
    scored.sort(key=lambda item: (-item[0], item[1], item[2]))
    return [item[3] for item in scored[:top_k]]


# Execution -----------------------------------------------------------------------


class _RequestLedger:
    """Thread-safe aggregate request bound.

    Client request budgets live in ContextVars. A worker thread runs in a copied
    context, so its decrements never reach the caller. This ledger enforces the
    planned bound across every thread and pooled client.
    """

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._used = 0
        self._closed = False
        self._lock = threading.Lock()

    def claim(self) -> None:
        with self._lock:
            if self._closed:
                raise DeadlineExceeded("two-stage routing already returned")
            if self._used >= self._limit:
                raise ValueError("Jev provider-request budget exceeded")
            self._used += 1

    def close(self) -> None:
        """Refuse every later claim, so no worker sends after the caller returns."""
        with self._lock:
            self._closed = True

    @property
    def used(self) -> int:
        with self._lock:
            return self._used


@dataclass(frozen=True)
class _PartitionResult:
    probabilities: dict[str, float]
    needs: float | None
    metadata: dict[str, Any]
    choice: str
    confidence: float


def _decide_partition(
    client: Any,
    *,
    ledger: _RequestLedger,
    task: str,
    names: tuple[str, ...],
    partition: int,
    total_count: int,
    include_needs_skill: bool,
) -> _PartitionResult:
    state, questions = _partition_parts(
        names,
        task=task,
        total_count=total_count,
        partition=partition,
        include_needs_skill=include_needs_skill,
    )
    key = f"skill_chunk_{partition}"
    operation_remaining_deadline()
    ledger.claim()
    # A pooled client has no outer scope in a worker thread; bound it to one
    # request so a client-side batch split cannot overspend. The ledger is the
    # authoritative aggregate bound.
    with request_budget_scope(client, 1):
        result = client.decide(state, questions, public_or_sanitized_data_ack=True)
    operation_remaining_deadline()
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict) or set(answers) != set(questions):
        raise ValueError("Jev partition answer keys do not exactly match the request")
    choice, confidence, probabilities = _choice_metrics(
        answers.get(key), questions[key]["criteria"], key
    )
    needs: float | None = None
    if include_needs_skill:
        needs_answer = answers.get("needs_skill")
        if not isinstance(needs_answer, dict):
            raise TypeError("Jev partition response is missing needs_skill")
        needs = _bounded_number(needs_answer.get("noul"), "needs_skill.noul")
    return _PartitionResult(probabilities, needs, _decision_metadata(result), choice, confidence)


def _is_budget_error(exc: BaseException) -> bool:
    return isinstance(exc, ValueError) and "budget exceeded" in str(exc)


def _run_parallel(
    jobs: list[Callable[[Any], Any]],
    clients: Sequence[Any],
    *,
    max_workers: int,
    prior: list[dict[str, Any]],
) -> list[Any]:
    """Run jobs concurrently inside the current operation deadline.

    Each job gets its own copied context, so the aggregate deadline and host
    cancel check apply in every worker. Jobs are spread across ``clients``;
    two jobs on one pooled client still serialize on that client's connection
    lock. Late results are discarded; running HTTP requests are not killed.
    """
    if not jobs:
        return []
    executor = ThreadPoolExecutor(
        max_workers=max(1, min(max_workers, len(jobs))),
        thread_name_prefix="jev-two-stage",
    )
    futures: list[Future] = []
    try:
        for index, job in enumerate(jobs):
            context = contextvars.copy_context()
            futures.append(executor.submit(context.run, job, clients[index % len(clients)]))
        pending = set(futures)
        while pending:
            remaining = operation_remaining_deadline()
            timeout = _POLL_SECONDS if remaining is None else min(_POLL_SECONDS, remaining)
            _done, pending = wait(pending, timeout=timeout, return_when=FIRST_COMPLETED)
            if any(future.exception() is not None for future in _done):
                break
        # Deadline or cancel between polls raises from operation_remaining_deadline.
        if pending:
            operation_remaining_deadline()
    except BaseException as exc:
        successes = [
            future.result().metadata
            for future in futures
            if future.done() and not future.cancelled() and future.exception() is None
        ]
        partial = prior + successes
        if partial and isinstance(exc, (DeadlineExceeded, TimeoutError)):
            raise PartialAccountingError(
                "Jev two-stage routing stopped after successful request(s)", partial=partial
            ) from exc
        raise
    finally:
        executor.shutdown(wait=False, cancel_futures=True)

    results: list[Any] = []
    failure: BaseException | None = None
    for future in futures:
        if future.cancelled() or not future.done():
            continue
        error = future.exception()
        if error is not None and failure is None:
            failure = error
        elif error is None:
            results.append(future.result())
    if failure is not None:
        if _is_budget_error(failure):
            raise failure
        partial = prior + [item.metadata for item in results]
        if isinstance(failure, PartialAccountingError):
            partial = partial + failure.partial
        if partial:
            raise PartialAccountingError(
                "Jev two-stage partition failed after successful request(s)", partial=partial
            ) from failure
        raise failure
    return results


def _thresholds(
    choice_confidence: float, needs_skill: float, winning_probability: float
) -> dict[str, float]:
    return {
        "choice_confidence": _bounded_number(choice_confidence, "choice_confidence_threshold"),
        "needs_skill": _bounded_number(needs_skill, "needs_skill_threshold"),
        "winning_probability": _bounded_number(winning_probability, "winning_probability_threshold"),
    }


def _gate(
    choice: str,
    confidence: float,
    probabilities: Mapping[str, float],
    needs: float | None,
    thresholds: Mapping[str, float],
) -> tuple[str | None, list[str]]:
    reasons: list[str] = []
    if choice == _SKILL_NONE:
        reasons.append("hosted_none_option")
    if confidence < thresholds["choice_confidence"]:
        reasons.append("choice_confidence_below_threshold")
    if needs is None or needs < thresholds["needs_skill"]:
        reasons.append("needs_skill_below_threshold")
    if probabilities.get(choice, 0.0) < thresholds["winning_probability"]:
        reasons.append("winning_probability_below_threshold")
    return (None if reasons else choice), reasons


def run_two_stage(
    *,
    task: str,
    candidates: Sequence[Any],
    client: Any,
    config: TwoStageConfig | None = None,
    client_pool: Sequence[Any] = (),
    excerpt_loader: Callable[[str], Any] | None = None,
    deadline_seconds: float | None = None,
    choice_confidence_threshold: float = DEFAULT_SKILL_CHOICE_CONFIDENCE_THRESHOLD,
    needs_skill_threshold: float = DEFAULT_SKILL_NEEDS_THRESHOLD,
    winning_probability_threshold: float = DEFAULT_SKILL_WINNING_PROBABILITY_THRESHOLD,
    public_or_sanitized_data_ack: bool = True,
) -> dict[str, Any]:
    """Select at most one advisory skill with the two-stage pattern.

    ``task`` must already be the caller-authorized hosted payload. ``candidates``
    are local rows (``name`` and optional ``description``); stage 1 sends names
    only. ``client_pool`` adds clients for true parallel stage-1 partitions.
    The return value is shaped like ``routing.select_skill`` so receipts and
    ``automatic.py`` can consume it unchanged.
    """
    if public_or_sanitized_data_ack is not True:
        raise PermissionError("public_or_sanitized_data_ack is false; this call was refused")
    config = config or TwoStageConfig()
    thresholds = _thresholds(
        choice_confidence_threshold, needs_skill_threshold, winning_probability_threshold
    )
    plan = plan_two_stage(task, candidates, config)
    candidates_by_name: dict[str, Mapping[str, Any]] = {
        (item if isinstance(item, str) else item["name"]): (
            {"name": item} if isinstance(item, str) else item
        )
        for item in candidates
    }
    clients = [client, *client_pool]
    started = time.perf_counter()
    total = len(candidates_by_name)
    ledger = _RequestLedger(plan.max_requests)

    def partition_job(index: int) -> Callable[[Any], _PartitionResult]:
        def job(worker_client: Any) -> _PartitionResult:
            return _decide_partition(
                worker_client,
                ledger=ledger,
                task=task,
                names=plan.partitions[index],
                partition=index,
                total_count=total,
                include_needs_skill=index == 0,
            )

        return job

    if plan.direct:
        with request_budget_scope(client, 1, deadline_seconds=deadline_seconds):
            with operation_deadline_scope(deadline_seconds):
                result = _select_skill_small(
                    task=task,
                    candidates=_names_only(candidates),
                    client=client,
                    choice_confidence_threshold=choice_confidence_threshold,
                    needs_skill_threshold=needs_skill_threshold,
                    winning_probability_threshold=winning_probability_threshold,
                    public_or_sanitized_data_ack=True,
                )
        result.update(
            {
                "shortlist_policy": SHORTLIST_POLICY_TWO_STAGE_SINGLE,
                "hosted_detail": plan.hosted_detail,
                "stage1_request_count": 1,
                "stage1_needs_skill_noul": result.get("needs_skill_noul"),
                "early_stop": False,
                "parallel_clients": len(clients),
                "request_rounds": 1,
                "wall_latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "request_count": 1,
                "total_latency_ms": result.get("latency_ms"),
                "total_usage": result.get("usage") or {},
            }
        )
        return result

    try:
        return _run_planned(
            plan=plan,
            task=task,
            client=client,
            clients=clients,
            config=config,
            ledger=ledger,
            partition_job=partition_job,
            candidates_by_name=candidates_by_name,
            excerpt_loader=excerpt_loader,
            deadline_seconds=deadline_seconds,
            thresholds=thresholds,
            started=started,
        )
    finally:
        # No worker may start a request after this call returns or raises.
        ledger.close()


def _run_planned(
    *,
    plan: TwoStagePlan,
    task: str,
    client: Any,
    clients: list[Any],
    config: TwoStageConfig,
    ledger: _RequestLedger,
    partition_job: Callable[[int], Callable[[Any], _PartitionResult]],
    candidates_by_name: Mapping[str, Mapping[str, Any]],
    excerpt_loader: Callable[[str], Any] | None,
    deadline_seconds: float | None,
    thresholds: Mapping[str, float],
    started: float,
) -> dict[str, Any]:
    calls: list[dict[str, Any]] = []
    total = len(candidates_by_name)
    with request_budget_scope(client, plan.max_requests, deadline_seconds=deadline_seconds):
        with operation_deadline_scope(deadline_seconds):
            stage1: list[tuple[tuple[str, ...], dict[str, float]]] = []
            first: _PartitionResult | None = None
            early_stopped = False
            rounds = 0
            if config.early_stop:
                first = partition_job(0)(client)
                calls.append(first.metadata)
                stage1.append((plan.partitions[0], first.probabilities))
                rounds += 1
                early_stopped = (
                    first.needs is not None and first.needs < config.early_stop_threshold
                )
                rest = [] if early_stopped else list(range(1, len(plan.partitions)))
            else:
                rest = list(range(len(plan.partitions)))
            if rest:
                results = _run_parallel(
                    [partition_job(index) for index in rest],
                    clients,
                    max_workers=config.parallel_requests,
                    prior=list(calls),
                )
                rounds += 1
                for index, outcome in zip(rest, results):
                    calls.append(outcome.metadata)
                    stage1.append((plan.partitions[index], outcome.probabilities))
                    if index == 0:
                        first = outcome
            if first is None:
                raise RuntimeError("two-stage partition 0 produced no result")
            needs_stage1 = first.needs

            base: dict[str, Any] = {
                "thresholds": thresholds,
                "candidate_count": total,
                "offered_count": total,
                "excluded_count": 0,
                "hosted_detail": plan.hosted_detail,
                "stage1_request_count": len(calls),
                "stage1_needs_skill_noul": needs_stage1,
                "early_stop": early_stopped,
                "parallel_clients": len(clients),
            }
            if early_stopped:
                return _finish(
                    base, calls, started, rounds,
                    selected=None, reasons=["needs_skill_early_stop"],
                    needs=needs_stage1, policy=SHORTLIST_POLICY_TWO_STAGE_EARLY_STOP,
                )

            if not plan.stage2_enabled:
                # Single names-only partition: stage 1 already saw every name.
                names = plan.partitions[0]
                selected, reasons = _gate(
                    first.choice, first.confidence, first.probabilities, needs_stage1, thresholds
                )
                return _finish(
                    base, calls, started, rounds,
                    selected=selected, reasons=reasons, needs=needs_stage1,
                    policy=SHORTLIST_POLICY_TWO_STAGE_SINGLE,
                    probabilities={name: first.probabilities[name] for name in names},
                    confidence=first.confidence,
                )

            finalists = rank_stage1(
                stage1,
                top_k=plan.recheck_top_k,
                min_probability=config.stage1_min_probability,
            )
            if not finalists:
                return _finish(
                    base, calls, started, rounds,
                    selected=None, reasons=["no_stage1_candidate"], needs=needs_stage1,
                    policy=SHORTLIST_POLICY_TWO_STAGE,
                )
            rows, withheld = build_stage2_skills(
                finalists, candidates_by_name, plan.hosted_detail, excerpt_loader=excerpt_loader
            )
            rows, trimmed = _fit_stage2(task, rows)
            state, questions = stage2_request_parts(task, rows)
            stage2_metadata: dict[str, Any] | None = None
            try:
                operation_remaining_deadline()
                ledger.claim()
                result = client.decide(state, questions, public_or_sanitized_data_ack=True)
                operation_remaining_deadline()
                answers = result.get("answers") if isinstance(result, dict) else None
                if not isinstance(answers, dict) or set(answers) != set(questions):
                    raise ValueError("Jev stage-2 answer keys do not exactly match the request")
                stage2_metadata = _decision_metadata(result)
                choice, confidence, probabilities = _choice_metrics(
                    answers.get("skill"), questions["skill"]["criteria"], "skill"
                )
                needs_answer = answers.get("needs_skill")
                if not isinstance(needs_answer, dict):
                    raise TypeError("Jev stage-2 response is missing needs_skill")
                needs = _bounded_number(needs_answer.get("noul"), "needs_skill.noul")
            except PartialAccountingError as exc:
                raise PartialAccountingError(
                    "Jev two-stage recheck failed after stage 1", partial=calls + exc.partial
                ) from exc
            except Exception as exc:
                if _is_budget_error(exc):
                    raise
                observed = calls + ([stage2_metadata] if stage2_metadata is not None else [])
                raise PartialAccountingError(
                    "Jev two-stage recheck failed after stage 1", partial=observed
                ) from exc
            rounds += 1
            calls.append(stage2_metadata)
            selected, reasons = _gate(choice, confidence, probabilities, needs, thresholds)
            base.update(
                {
                    "stage2_offered_count": len(rows),
                    "detail_withheld_count": withheld,
                    "detail_trimmed_count": trimmed,
                    "detail_sent_count": sum(
                        1 for row in rows if "description" in row or "excerpt" in row
                    ),
                }
            )
            return _finish(
                base, calls, started, rounds,
                selected=selected, reasons=reasons, needs=needs,
                policy=SHORTLIST_POLICY_TWO_STAGE,
                probabilities={name: value for name, value in probabilities.items() if name != _SKILL_NONE},
                confidence=confidence,
            )


def _finish(
    base: dict[str, Any],
    calls: list[dict[str, Any]],
    started: float,
    rounds: int,
    *,
    selected: str | None,
    reasons: list[str],
    needs: float | None,
    policy: str,
    probabilities: Mapping[str, float] | None = None,
    confidence: float = 0.0,
) -> dict[str, Any]:
    probabilities = dict(probabilities or {})
    aggregate = _aggregate_metadata(calls)
    last = calls[-1] if calls else {}
    return {
        "status": "selected" if selected is not None else "abstained",
        "selected": selected,
        "abstention_reason": None if selected is not None else ";".join(reasons),
        "needs_skill_noul": needs,
        "needs_skill_probability": needs,
        "confidence": confidence,
        "winning_probability": probabilities.get(selected, 0.0) if selected else 0.0,
        "probabilities": probabilities,
        "shortlist_policy": policy,
        "request_rounds": rounds,
        # Provider time summed across requests; wall time reflects parallelism.
        "wall_latency_ms": round((time.perf_counter() - started) * 1000, 1),
        "model": last.get("model"),
        "request_id": last.get("request_id"),
        "latency_ms": last.get("latency_ms"),
        "usage": last.get("usage") or {},
        **base,
        **aggregate,
    }
