"""Code-owned approved model registry for advisory Hermes routing.

`jev_model_route` remains the documented Hermes tool routing point. Coordinators should prefer `hermes_switchyard.model_route_adapter.recommend_model_route`, which wraps this helper with auditable receipts and safe Hermes seam registration. Callers that
want a registry-backed recommendation use `route_model_from_registry` with an
explicit approved candidate list. Descriptions never confer approval. A
selected route is an auditable recommendation; this adapter never changes the
Hermes runtime model, provider, credentials, or fallback policy.

The shipped registry is empty on purpose. Operators supply a real approved
candidate list at the routing point rather than inferring policy from prose.
"""
from __future__ import annotations

from typing import Any, Sequence

from .client import DEFAULT_OPERATION_DEADLINE_SECONDS
from .routing import DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD, route_model

APPROVED_MODEL_REGISTRY: tuple[dict[str, Any], ...] = ()
REGISTRY_GENERATION = 1
_SELECTION_POLICY = (
    "cheapest qualified candidate; advisory only; runtime model is unchanged"
)


def route_model_from_registry(
    *,
    task: str,
    requirements: dict[str, Any] | None,
    client: Any,
    capability_fit_threshold: float = DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD,
    public_or_sanitized_data_ack: bool = True,
    deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
    registry: Sequence[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Recommend a model from a code-owned registry without changing runtime state."""
    candidates = list(APPROVED_MODEL_REGISTRY if registry is None else registry)
    if not candidates:
        return {
            "status": "abstained",
            "selected": None,
            "eligible_candidates": [],
            "excluded_candidates": [],
            "qualified_candidates": [],
            "capability_fit_scores": {},
            "capability_fit_threshold": capability_fit_threshold,
            "selection_policy": _SELECTION_POLICY,
            "model": None,
            "latency_ms": None,
            "usage": {},
            "abstention_reason": "empty_registry",
            "applied": False,
            "no_fallback": True,
            "source": "code_owned_registry",
        }
    parsed_requirements = dict(requirements or {})
    if "registry_generation" not in parsed_requirements:
        parsed_requirements["registry_generation"] = REGISTRY_GENERATION
    return route_model(
        task=task,
        candidates=candidates,
        requirements=parsed_requirements,
        client=client,
        capability_fit_threshold=capability_fit_threshold,
        public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        deadline_seconds=deadline_seconds,
    )
