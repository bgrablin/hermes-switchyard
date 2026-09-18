"""Fixed-route OpenRouter Decisions API client for Jev."""
from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any


DEFAULT_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
ALLOWED_MODELS = frozenset({"typesafe/jev-1.13", "typesafe/jev-1.13-20260917"})
EXPECTED_MODEL = "typesafe/jev-1.13"


def _require_public_data_ack(acknowledged: bool) -> None:
    if acknowledged is not True:
        raise PermissionError(
            "public_or_sanitized_data_ack must be true: this is a caller attestation, not DLP; "
            "do not send private, employer, or regulated UI/data to a model"
        )


def _valid_model_slug(model: Any) -> bool:
    return type(model) is str and model in ALLOWED_MODELS


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
    """Call only the fixed OpenRouter Decisions endpoint and require exact model resolution."""

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
            raise ValueError("OpenRouter API key is required")
        if endpoint != DEFAULT_ENDPOINT:
            raise ValueError("Jev uses the fixed OpenRouter Decisions endpoint")
        if not _valid_model_slug(model):
            raise ValueError("Jev model is not an evidence-backed allowed alias")
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.api_key = api_key
        self.endpoint = endpoint
        self.model = model
        self.timeout = float(timeout)
        self.transport = transport

    def _post(self, payload: dict) -> dict:
        if self.transport is not None:
            try:
                result = self.transport(payload)
            except Exception as exc:  # noqa: BLE001 - never expose transport/payload details
                raise RuntimeError(f"Jev transport failed: {type(exc).__name__}") from None
            if not isinstance(result, dict):
                raise TypeError("Jev transport returned a non-object response")
            return result
        request = urllib.request.Request(
            self.endpoint,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        opener = urllib.request.build_opener(_NoRedirectHandler())
        try:
            with opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            exc.read()
            raise RuntimeError(f"Jev provider returned HTTP {exc.code}") from None
        except (OSError, TimeoutError) as exc:
            raise RuntimeError(f"Jev connection failed: {type(exc).__name__}") from None
        try:
            result = json.loads(raw)
        except (TypeError, ValueError):
            raise RuntimeError("Jev provider returned invalid JSON") from None
        if not isinstance(result, dict):
            raise TypeError("Jev provider returned a non-object response")
        return result

    def _validate_resolved_model(self, resolved: Any) -> str:
        if not _valid_model_slug(resolved):
            raise ValueError("Jev provider resolved a model outside the allowed aliases")
        # The base alias may resolve to the one evidence-backed concrete alias.
        # A request made with the concrete alias must resolve exactly to it.
        if self.model == EXPECTED_MODEL and resolved in ALLOWED_MODELS:
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

    def decide(
        self,
        state: Any,
        questions: dict,
        *,
        public_or_sanitized_data_ack: bool = False,
    ) -> dict:
        """Return a validated typed response; no request occurs without caller attestation."""
        _require_public_data_ack(public_or_sanitized_data_ack)
        if not isinstance(questions, dict) or not questions:
            raise ValueError("questions must be a non-empty object")
        started = time.perf_counter()
        result = self._post({
            "model": self.model,
            "state": state,
            "questions": questions,
            "provider": {"allow_fallbacks": False},
        })
        if not isinstance(result, dict):
            raise TypeError("Jev response must be an object")
        result["model"] = self._validate_resolved_model(result.get("model"))
        answers = result.get("answers")
        if not isinstance(answers, dict):
            raise TypeError("Jev response has no answers object")
        for name, question in questions.items():
            if not isinstance(question, dict):
                raise TypeError(f"Invalid question {name}")
            answer = answers.get(name)
            if not isinstance(answer, dict):
                raise TypeError(f"Jev response is missing answer {name}")
            question_type = question.get("type")
            if question_type == "choice":
                self._validate_choice(name, answer, question.get("criteria") or {})
            elif question_type == "noul":
                value = answer.get("noul")
                if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 1:
                    raise ValueError(f"Invalid noul answer {name}")
            else:
                raise ValueError(f"Unsupported Jev question type {question_type!r}")
        usage = self._validate_usage(result.get("usage", {}))
        result["usage"] = usage
        latency = result.get("latency_ms")
        if latency is None:
            latency = round((time.perf_counter() - started) * 1000, 1)
        elif type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError("Invalid Jev latency")
        result["latency_ms"] = float(latency)
        return result

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
