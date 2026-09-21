"""Policy-owned Hermes model-routing adapter.

``jev_model_route`` / ``jev_model_route_approved`` remain the documented tool
routing points. This module makes the approved-registry path first-class for
coordinators and for any future Hermes model-selection seam:

1. ``recommend_model_route`` runs code-owned registry filtering + Jev fit scoring
   through ``route_model_from_registry`` and returns a typed receipt.
2. ``recommend_model_route_from_profile`` does the same for the profile-owned
   approved registry via ``recommend_approved_model``.
3. ``register_model_route_adapter`` probes Hermes for a supported model-selection
   hook/API and registers against it when present; on Hermes 0.19 hosts that
   lack such a seam it records a safe no-op registration.
4. ``accept_model_route`` never mutates the active Hermes model unless an
   explicit, supported apply callback is supplied by the host — silent swaps
   are refused.

Receipts always carry ``applied: false`` until a real Hermes apply seam accepts
the recommendation. Empty and stale registries fail closed with distinct
``abstention_reason`` values and no provider egress.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, MutableMapping, Sequence

from .client import DEFAULT_OPERATION_DEADLINE_SECONDS
from .model_policy import recommend_approved_model
from .model_registry import route_model_from_registry
from .routing import DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD

# Hermes 0.19 PluginContext exposes register_hook / register_auxiliary_task but
# no model-selection apply seam. These names are the contract we will bind when
# a future host ships one; probing them keeps registration forward-compatible.
_MODEL_SELECTION_CTX_METHODS: tuple[str, ...] = (
    "register_model_router",
    "register_model_selection_policy",
    "register_model_route",
)
_MODEL_SELECTION_HOOK_NAMES: tuple[str, ...] = (
    "model_select",
    "pre_model_select",
    "select_model",
    "model_route",
)

_SELECTION_POLICY = (
    "cheapest qualified candidate; advisory only; runtime model is unchanged"
)
_NO_FALLBACK_POLICY = "no automatic provider or model fallback"
_ACCOUNT_BOUNDARY = (
    "recommendation grants no new provider, model, account, credential, "
    "or paid-fallback authority"
)

# Last registration result for status / tests. Process-local only.
_LAST_REGISTRATION: dict[str, Any] = {
    "registered": False,
    "mode": "uninitialized",
    "hermes_seam": None,
    "reason": "register_model_route_adapter_not_called",
}


def last_registration() -> dict[str, Any]:
    """Return a copy of the most recent adapter registration receipt."""
    return dict(_LAST_REGISTRATION)


def probe_model_selection_seam(ctx: Any) -> dict[str, Any]:
    """Probe a PluginContext-like object for a supported model-selection seam.

    Returns a structured probe result. A missing seam is not an error — Hermes
    0.19 simply does not expose one — and callers must treat recommendations as
    advisory until an apply seam exists.
    """
    for method_name in _MODEL_SELECTION_CTX_METHODS:
        method = getattr(ctx, method_name, None)
        if callable(method):
            return {
                "available": True,
                "kind": "ctx_method",
                "name": method_name,
                "can_apply": True,
            }
    register_hook = getattr(ctx, "register_hook", None)
    if callable(register_hook):
        # Host can register hooks, but only a known model-selection hook name
        # counts as a supported apply seam. pre_llm_call is advisory context
        # injection only and must not be treated as model apply.
        known = set(_MODEL_SELECTION_HOOK_NAMES)
        for attr in ("supported_hooks", "valid_hooks", "allowed_hooks", "VALID_HOOKS"):
            advertised = getattr(ctx, attr, None)
            if isinstance(advertised, (set, frozenset, list, tuple)):
                overlap = known.intersection(str(item) for item in advertised)
                if overlap:
                    name = sorted(overlap)[0]
                    return {
                        "available": True,
                        "kind": "hook",
                        "name": name,
                        # Hook observation alone cannot apply a model change.
                        "can_apply": False,
                    }
    return {
        "available": False,
        "kind": None,
        "name": None,
        "can_apply": False,
        "reason": "hermes_model_selection_seam_unavailable",
    }


def _as_receipt(
    route: Mapping[str, Any],
    *,
    source: str,
    seam: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Attach adapter metadata without rewriting route policy outcomes."""
    receipt = dict(route)
    receipt.setdefault("selection_policy", _SELECTION_POLICY)
    receipt["applied"] = False
    receipt["source"] = source
    receipt["no_fallback"] = True
    receipt["no_fallback_policy"] = _NO_FALLBACK_POLICY
    receipt["account_boundary"] = _ACCOUNT_BOUNDARY
    receipt["hermes_seam"] = (
        dict(seam)
        if seam is not None
        else {
            "available": False,
            "kind": None,
            "name": None,
            "can_apply": False,
            "reason": "not_probed",
        }
    )
    receipt["integration_point"] = (
        "hermes_switchyard.model_route_adapter.recommend_model_route"
        if source == "code_owned_registry"
        else "hermes_switchyard.model_route_adapter.recommend_model_route_from_profile"
    )
    return receipt


def recommend_model_route(
    *,
    task: str,
    requirements: dict[str, Any] | None,
    client: Any,
    capability_fit_threshold: float = DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD,
    public_or_sanitized_data_ack: bool = True,
    deadline_seconds: float = DEFAULT_OPERATION_DEADLINE_SECONDS,
    registry: Sequence[dict[str, Any]] | None = None,
    seam: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """First-class approved-registry recommendation with an auditable receipt.

    Callers pass task + requirements (+ optional explicit registry). Policy
    metadata stays code-owned; descriptions never confer approval. The active
    Hermes model is never changed.
    """
    routed = route_model_from_registry(
        task=task,
        requirements=requirements,
        client=client,
        capability_fit_threshold=capability_fit_threshold,
        public_or_sanitized_data_ack=public_or_sanitized_data_ack,
        deadline_seconds=deadline_seconds,
        registry=registry,
    )
    return _as_receipt(routed, source="code_owned_registry", seam=seam)


def recommend_model_route_from_profile(
    *,
    task: str,
    requirements: dict[str, Any],
    registry: Any,
    registry_version: Any,
    valid_until: Any,
    client: Any,
    public_or_sanitized_data_ack: bool = False,
    capability_fit_threshold: float = DEFAULT_MODEL_CAPABILITY_FIT_THRESHOLD,
    now: Any = None,
    seam: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Profile-owned approved-registry recommendation with an auditable receipt."""
    kwargs: dict[str, Any] = {
        "task": task,
        "requirements": requirements,
        "registry": registry,
        "registry_version": registry_version,
        "valid_until": valid_until,
        "client": client,
        "public_or_sanitized_data_ack": public_or_sanitized_data_ack,
        "capability_fit_threshold": capability_fit_threshold,
    }
    if now is not None:
        kwargs["now"] = now
    routed = recommend_approved_model(**kwargs)
    return _as_receipt(routed, source="profile_owned_registry", seam=seam)


def accept_model_route(
    receipt: Mapping[str, Any],
    *,
    apply_callback: Callable[[Mapping[str, Any]], Any] | None = None,
) -> dict[str, Any]:
    """Explicitly accept a recommendation through a host-supplied apply callback.

    Refuses when the receipt is not selected or when no apply callback is
    provided (Hermes 0.19 default — no silent swap). This function never writes
    Hermes provider/model/account configuration itself.
    """
    result: dict[str, Any] = dict(receipt)
    result["applied"] = False
    if receipt.get("status") != "selected" or not receipt.get("selected"):
        result["accept_status"] = "refused"
        result["accept_reason"] = "recommendation_not_selected"
        return result
    if apply_callback is None:
        result["accept_status"] = "refused"
        result["accept_reason"] = "hermes_apply_seam_unavailable"
        return result
    try:
        applied = apply_callback(receipt)
    except Exception as exc:  # noqa: BLE001 -- surface as typed refusal
        result["accept_status"] = "refused"
        result["accept_reason"] = "apply_callback_failed"
        result["accept_error_type"] = type(exc).__name__
        return result
    result["accept_status"] = "accepted"
    result["accept_reason"] = None
    result["apply_result"] = applied
    # Only mark applied when the host callback reports success explicitly.
    if isinstance(applied, Mapping) and applied.get("applied") is True:
        result["applied"] = True
    else:
        result["applied"] = False
        result["accept_status"] = "accepted_pending_host"
    return result


def _advisory_model_route_hook(**_kwargs: Any) -> None:
    """No-op hook body for hosts that accept model-selection hook names.

    Must never change the active model, inject credentials, or expand account
    authority. The tools and ``recommend_model_route`` remain the routing points.
    """
    return None


def register_model_route_adapter(ctx: Any) -> dict[str, Any]:
    """Register the policy adapter at a Hermes seam, or record a safe no-op.

    On Hermes 0.19 (no model-selection apply API) this returns a receipt with
    ``mode="noop_seam_unavailable"`` and does not alter runtime model state.
    When a future host exposes a supported method, the adapter registers a
    callback that still only *recommends* — apply remains explicit via
    ``accept_model_route``.
    """
    global _LAST_REGISTRATION
    seam = probe_model_selection_seam(ctx)
    if not seam.get("available"):
        receipt = {
            "registered": True,
            "mode": "noop_seam_unavailable",
            "hermes_seam": seam,
            "reason": "hermes_0_19_has_no_model_selection_apply_seam",
            "integration_point": (
                "hermes_switchyard.model_route_adapter.recommend_model_route"
            ),
            "applied_by_default": False,
        }
        _LAST_REGISTRATION = dict(receipt)
        return receipt

    kind = seam.get("kind")
    name = seam.get("name")
    if kind == "ctx_method" and isinstance(name, str):
        method = getattr(ctx, name)

        def _recommend_only(
            payload: Mapping[str, Any] | None = None, **kwargs: Any
        ) -> dict[str, Any]:
            body: MutableMapping[str, Any] = dict(payload or {})
            body.update(kwargs)
            return {
                "status": "adapter_registered",
                "mode": "recommend_only",
                "applied": False,
                "selection_policy": _SELECTION_POLICY,
                "request_keys": sorted(body),
            }

        method(_recommend_only)
        receipt = {
            "registered": True,
            "mode": "recommend_only_ctx_method",
            "hermes_seam": seam,
            "reason": None,
            "integration_point": (
                "hermes_switchyard.model_route_adapter.recommend_model_route"
            ),
            "applied_by_default": False,
        }
        _LAST_REGISTRATION = dict(receipt)
        return receipt

    if kind == "hook" and isinstance(name, str):
        register_hook = getattr(ctx, "register_hook", None)
        if callable(register_hook):
            register_hook(name, _advisory_model_route_hook)
            receipt = {
                "registered": True,
                "mode": "advisory_hook",
                "hermes_seam": seam,
                "reason": None,
                "integration_point": (
                    "hermes_switchyard.model_route_adapter.recommend_model_route"
                ),
                "applied_by_default": False,
            }
            _LAST_REGISTRATION = dict(receipt)
            return receipt

    receipt = {
        "registered": True,
        "mode": "noop_seam_unusable",
        "hermes_seam": seam,
        "reason": "seam_probe_positive_but_registration_unsupported",
        "integration_point": (
            "hermes_switchyard.model_route_adapter.recommend_model_route"
        ),
        "applied_by_default": False,
    }
    _LAST_REGISTRATION = dict(receipt)
    return receipt
