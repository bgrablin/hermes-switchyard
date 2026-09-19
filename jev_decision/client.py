"""Fixed-route OpenRouter Decisions API client for Jev."""
from __future__ import annotations

import http.client
import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import AbstractContextManager, contextmanager, nullcontext
from contextvars import ContextVar
from collections.abc import Callable
from typing import Any, cast


DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"
OPENROUTER_MODELS = frozenset({"typesafe/jev-1.13", "typesafe/jev-1.13-20260917"})
TYPESAFE_MODELS = frozenset({"jev-latest", "jev-1.13", "jev-1.13.0"})
ALLOWED_ENDPOINTS = frozenset({DEFAULT_ENDPOINT, TYPESAFE_ENDPOINT})
ALLOWED_MODELS = OPENROUTER_MODELS | TYPESAFE_MODELS
EXPECTED_MODEL = "typesafe/jev-1.13"
MAX_QUESTIONS_PER_REQUEST = 255
MAX_REQUEST_BYTES = 96_000
MAX_DECISION_REQUESTS = 64
MAX_TOTAL_QUESTIONS = MAX_QUESTIONS_PER_REQUEST * MAX_DECISION_REQUESTS
MAX_OPERATION_REQUESTS = 256


def request_budget_scope(
    client: Any, max_requests: int = MAX_DECISION_REQUESTS
) -> AbstractContextManager[None]:
    factory = getattr(client, "request_budget", None)
    return cast(AbstractContextManager[None], factory(max_requests)) if callable(factory) else nullcontext()


def _require_public_data_ack(acknowledged: bool) -> None:
    if acknowledged is not True:
        raise PermissionError(
            "public_or_sanitized_data_ack must be true: this is a caller attestation, not DLP; "
            "do not send private, employer, or regulated UI/data to a model"
        )


def _valid_model_slug(model: Any, endpoint: str | None = None) -> bool:
    if type(model) is not str:
        return False
    if endpoint == DEFAULT_ENDPOINT:
        return model in OPENROUTER_MODELS
    if endpoint == TYPESAFE_ENDPOINT:
        return model in TYPESAFE_MODELS
    return model in ALLOWED_MODELS


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Reject all redirects so a bearer token cannot be forwarded elsewhere."""

    @staticmethod
    def _reject(request, _fp, code, _message, headers):
        raise urllib.error.HTTPError(request.full_url, code, "redirects disabled", headers, None)

    http_error_301 = _reject
    http_error_302 = _reject
    http_error_303 = _reject
    http_error_307 = _reject
    http_error_308 = _reject


class DecisionClient:
    """Call one of the fixed Jev endpoints and require exact model resolution."""

    def __init__(
        self,
        *,
        api_key: str,
        endpoint: str = DEFAULT_ENDPOINT,
        model: str = EXPECTED_MODEL,
        timeout: float = 25,
        transport: Callable[[dict], dict] | None = None,
    ):
        if type(api_key) is not str or not api_key.strip():
            raise ValueError("Jev API key is required")
        if endpoint not in ALLOWED_ENDPOINTS:
            raise ValueError("Jev endpoint is not an evidence-backed fixed endpoint")
        if not _valid_model_slug(model, endpoint):
            raise ValueError("Jev model is not an evidence-backed allowed alias for this endpoint")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.timeout = float(timeout)
        self.transport = transport
        self._url = urllib.parse.urlsplit(endpoint)
        self._connection: http.client.HTTPSConnection | None = None
        self._connection_lock = threading.RLock()
        self._request_budget: ContextVar[int | None] = ContextVar(
            f"jev_request_budget_{id(self)}", default=None
        )

    @contextmanager
    def request_budget(self, max_requests: int = MAX_DECISION_REQUESTS):
        if type(max_requests) is not int or not 1 <= max_requests <= MAX_OPERATION_REQUESTS:
            raise ValueError("max_requests is outside the bounded operation budget")
        current = self._request_budget.get()
        if current is not None:
            yield
            return
        token = self._request_budget.set(max_requests)
        try:
            yield
        finally:
            self._request_budget.reset(token)

    def _post(self, payload: dict) -> dict:
        if self.transport is not None:
            try:
                result = self.transport(payload)
            except Exception as exc:  # noqa: BLE001 - never expose transport/payload details
                raise RuntimeError(f"Jev transport failed: {type(exc).__name__}") from None
            if not isinstance(result, dict):
                raise TypeError("Jev transport returned a non-object response")
            return result
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        path = self._url.path or "/"
        if self._url.query:
            path += f"?{self._url.query}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        with self._connection_lock:
            try:
                if self._connection is None:
                    host = self._url.hostname
                    if not host:
                        raise RuntimeError("Jev endpoint has no host")
                    self._connection = http.client.HTTPSConnection(
                        host,
                        self._url.port,
                        timeout=self.timeout,
                    )
                self._connection.request("POST", path, body=body, headers=headers)
                response = self._connection.getresponse()
                raw = response.read()
                status = response.status
                if response.will_close and self._connection is not None:
                    self._connection.close()
                    self._connection = None
                if 300 <= status < 400:
                    raise RuntimeError("Jev provider returned an unexpected redirect")
                if status >= 400:
                    raise RuntimeError(f"Jev provider returned HTTP {status}")
            except RuntimeError:
                raise
            except (OSError, TimeoutError, http.client.HTTPException) as exc:
                if self._connection is not None:
                    self._connection.close()
                self._connection = None
                raise RuntimeError(f"Jev connection failed: {type(exc).__name__}") from None
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            raise RuntimeError("Jev provider returned invalid JSON") from None
        if not isinstance(result, dict):
            raise TypeError("Jev provider returned a non-object response")
        return result

    def _validate_resolved_model(self, resolved: Any) -> str:
        if not _valid_model_slug(resolved, self.endpoint):
            raise ValueError("Jev provider resolved a model outside the allowed aliases")
        # Aliases may resolve to a dated/concrete release, but an explicitly
        # pinned model must resolve exactly to that model.
        if self.endpoint == DEFAULT_ENDPOINT and self.model == EXPECTED_MODEL and resolved in OPENROUTER_MODELS:
            return resolved
        if self.endpoint == TYPESAFE_ENDPOINT and self.model == "jev-latest" and resolved in TYPESAFE_MODELS:
            return resolved
        if resolved != self.model:
            raise ValueError("Jev provider resolved a different allowed alias")
        return resolved

    @staticmethod
    def _validate_usage(usage: Any) -> dict[str, Any]:
        if not isinstance(usage, dict):
            raise TypeError("Jev response usage must be an object")
        validated = dict(usage)
        if "cost" in validated:
            cost = validated["cost"]
            if type(cost) not in (int, float) or not math.isfinite(cost) or cost < 0:
                raise ValueError("Invalid Jev usage cost")
            validated["cost"] = float(cost)
        return validated

    @staticmethod
    def _validate_questions(questions: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(questions, dict) or not questions:
            raise ValueError("questions must be a non-empty object")
        if len(questions) > MAX_TOTAL_QUESTIONS:
            raise ValueError("question count exceeds the bounded provider-request budget")
        validated: dict[str, dict[str, Any]] = {}
        for name, question in questions.items():
            if type(name) is not str or not name or name != name.strip():
                raise ValueError("question names must be non-empty exact strings")
            if not isinstance(question, dict) or set(question) - {"type", "instructions", "criteria"}:
                raise ValueError(f"Invalid question {name}")
            question_type = question.get("type")
            instructions = question.get("instructions")
            criteria = question.get("criteria")
            if type(instructions) is not str or not instructions.strip():
                raise ValueError(f"Invalid question instructions {name}")
            if question_type == "choice":
                if not isinstance(criteria, dict) or not 1 <= len(criteria) <= 255:
                    raise ValueError(f"Invalid choice criteria {name}")
                if any(type(key) is not str or not key or type(value) is not str for key, value in criteria.items()):
                    raise ValueError(f"Invalid choice criteria {name}")
            elif question_type == "score":
                if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10 or any(type(item) is not str for item in criteria):
                    raise ValueError(f"Invalid score criteria {name}")
            elif question_type == "noul":
                if criteria is not None and (
                    not isinstance(criteria, dict)
                    or any(type(key) is not str or type(value) is not str for key, value in criteria.items())
                ):
                    raise ValueError(f"Invalid noul criteria {name}")
            else:
                raise ValueError(f"Unsupported Jev question type {question_type!r}")
            validated[name] = dict(question)
        return validated

    @staticmethod
    def _merge_usage(total: dict[str, Any], usage: dict[str, Any]) -> None:
        for key, value in usage.items():
            if type(value) in (int, float) and math.isfinite(value):
                total[key] = float(total.get(key, 0.0)) + float(value)
            elif key not in total:
                total[key] = value

    def _payload(self, state: Any, questions: dict[str, Any]) -> dict[str, Any]:
        payload = {"model": self.model, "state": state, "questions": questions}
        if self.endpoint == DEFAULT_ENDPOINT:
            payload["provider"] = {"allow_fallbacks": False}
        if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("Jev request exceeds the bounded serialized request budget")
        return payload

    def _decide_single(self, state: Any, questions: dict[str, dict[str, Any]]) -> dict:
        started = time.perf_counter()
        result = self._post(self._payload(state, questions))
        if not isinstance(result, dict):
            raise TypeError("Jev response must be an object")
        result["model"] = self._validate_resolved_model(result.get("model"))
        answers = result.get("answers")
        if not isinstance(answers, dict):
            raise TypeError("Jev response has no answers object")
        for name, question in questions.items():
            answer = answers.get(name)
            if not isinstance(answer, dict):
                raise TypeError(f"Jev response is missing answer {name}")
            question_type = question["type"]
            if question_type == "choice":
                self._validate_choice(name, answer, question["criteria"])
            elif question_type == "score":
                self._validate_score(name, answer, question["criteria"])
            else:
                value = answer.get("noul")
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"Invalid noul answer {name}")
        result["usage"] = self._validate_usage(result.get("usage", {}))
        latency = result.get("latency_ms")
        if latency is None:
            latency = round((time.perf_counter() - started) * 1000, 1)
        elif type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError("Invalid Jev latency")
        result["latency_ms"] = float(latency)
        return result

    def decide(
        self,
        state: Any,
        questions: dict,
        *,
        public_or_sanitized_data_ack: bool = False,
    ) -> dict:
        """Return a validated typed response; no request occurs without caller attestation."""
        _require_public_data_ack(public_or_sanitized_data_ack)
        validated = self._validate_questions(questions)
        batches: list[dict[str, dict[str, Any]]] = []
        current: dict[str, dict[str, Any]] = {}
        for name, question in validated.items():
            trial = {**current, name: question}
            try:
                self._payload(state, trial)
            except ValueError:
                if not current:
                    raise
                batches.append(current)
                current = {name: question}
                self._payload(state, current)
            else:
                if len(trial) > MAX_QUESTIONS_PER_REQUEST:
                    batches.append(current)
                    current = {name: question}
                else:
                    current = trial
        if current:
            batches.append(current)
        remaining = self._request_budget.get()
        allowed = MAX_DECISION_REQUESTS if remaining is None else remaining
        if len(batches) > allowed:
            raise ValueError("Jev provider-request budget exceeded")
        if remaining is not None:
            self._request_budget.set(remaining - len(batches))
        calls = [self._decide_single(state, batch) for batch in batches]
        if len(calls) == 1:
            calls[0]["request_count"] = 1
            calls[0]["total_latency_ms"] = calls[0]["latency_ms"]
            calls[0]["total_usage"] = dict(calls[0]["usage"])
            return calls[0]
        answers: dict[str, Any] = {}
        usage: dict[str, Any] = {}
        latency = 0.0
        for call in calls:
            answers.update(call["answers"])
            self._merge_usage(usage, call["usage"])
            latency += call["latency_ms"]
        return {
            "model": calls[-1]["model"],
            "answers": answers,
            "usage": usage,
            "latency_ms": latency,
            "request_count": len(calls),
            "total_latency_ms": latency,
            "total_usage": usage,
        }

    @staticmethod
    def _validate_choice(name: str, answer: dict, criteria: dict) -> None:
        if not isinstance(criteria, dict) or not criteria:
            raise ValueError(f"Jev choice {name} has no offered criteria")
        choice = answer.get("choice")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        numbers = [] if not isinstance(probabilities, dict) else [*probabilities.values(), confidence]
        valid = (
            choice in criteria
            and isinstance(probabilities, dict)
            and set(probabilities) == set(criteria)
            and all(type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 1 for value in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[choice] >= max(probabilities.values()) - 1e-6
        )
        if not valid:
            raise ValueError(f"Jev choice {name} is outside the offered criteria")

    @staticmethod
    def _validate_score(name: str, answer: dict, criteria: list) -> None:
        if not isinstance(criteria, list) or not 2 <= len(criteria) <= 10:
            raise ValueError(f"Jev score {name} has invalid ordered criteria")
        score = answer.get("score")
        legend = answer.get("legend")
        probabilities = answer.get("probabilities")
        confidence = answer.get("confidence")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(float(score)):
            raise ValueError(f"Invalid Jev score answer {name}")
        score_value = float(score)
        if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not math.isfinite(float(confidence)):
            raise ValueError(f"Invalid Jev score confidence {name}")
        confidence_value = float(confidence)
        expected_keys = {str(index) for index in range(len(criteria))}
        if not (
            0 <= score_value <= len(criteria) - 1
            and isinstance(legend, dict)
            and set(legend) == expected_keys
            and isinstance(probabilities, dict)
            and set(probabilities) == expected_keys
            and all(type(value) in (int, float) and math.isfinite(float(value)) and 0 <= float(value) <= 1 for value in probabilities.values())
            and 0 <= confidence_value <= 1
            and abs(sum(float(value) for value in probabilities.values()) - 1) < 0.02
            and all(legend[str(index)] == criterion for index, criterion in enumerate(criteria))
        ):
            raise ValueError(f"Jev score {name} is outside the offered criteria")
        weighted = sum(index * float(probabilities[str(index)]) for index in range(len(criteria)))
        if abs(score_value - weighted) > 0.02:
            raise ValueError(f"Jev score {name} is inconsistent with its probability distribution")

    def close(self) -> None:
        """Close the pooled HTTP connection without affecting synthetic transports."""
        with self._connection_lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
