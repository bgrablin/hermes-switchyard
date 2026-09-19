"""Standalone per-turn egress contract for automatic hosted decisions.

Hermes core owns classification, sanitization, and authorization.  The plugin
accepts only the small typed envelope documented in
``docs/AUTOMATIC-INTEGRATION.md``.  A missing, malformed, unknown, denied, or
restricted envelope never authorizes hosted construction.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

TURN_EGRESS_POLICY_VERSION = 1
MAX_ALLOWED_PAYLOAD_CHARS = 4_000
ROUTING_MODES = frozenset({"off", "local_only", "hosted_sanitized"})
ALLOWED_DATA_CLASSES = frozenset({"public", "sanitized"})
RESTRICTED_DATA_CLASSES = frozenset(
    {
        "private",
        "employer",
        "regulated",
        "credential",
        "payment",
        "verification",
        "prompt_injection",
        "secret",
        "unknown",
    }
)
DATA_CLASSES = ALLOWED_DATA_CLASSES | RESTRICTED_DATA_CLASSES

_REASON_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_ALLOWED_PAYLOAD_CONTROL_CHARS = frozenset({"\n", "\r", "\t"})


@dataclass(frozen=True)
class TurnEgressEvaluation:
    """A redacted result of evaluating one host-owned turn envelope.

    ``allowed_payload`` is retained only for the immediate allowed call.  It is
    never included in :meth:`metadata` and callers must not persist it in a
    receipt or recommendation result.
    """

    allowed: bool
    decision: str
    data_class: str | None
    status: str
    reason_code: str
    allowed_payload: str | None = None
    version: int | None = None

    @property
    def metadata(self) -> dict[str, Any]:
        """Return status-only metadata safe for a plugin receipt."""
        return {
            "policy_status": self.status,
            "policy_reason": self.reason_code,
            "policy_data_class": self.data_class,
            "policy_version": self.version,
        }

    @property
    def cache_key(self) -> tuple[Any, ...]:
        """Return policy identity without retaining allowed payload text."""
        return (
            self.allowed,
            self.decision,
            self.data_class,
            self.status,
            self.reason_code,
            _payload_digest(self.allowed_payload),
            self.version,
        )


def _payload_digest(value: str | None) -> str | None:
    """Return a collision-resistant cache identity without retaining payload text."""
    if value is None:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _invalid(*, version: int | None = None, data_class: str | None = None) -> TurnEgressEvaluation:
    return TurnEgressEvaluation(
        allowed=False,
        decision="invalid",
        data_class=data_class,
        status="denied",
        reason_code="per_turn_policy_invalid",
        version=version,
    )


def _valid_reason_code(value: Any) -> bool:
    return value is None or (type(value) is str and bool(_REASON_CODE_RE.fullmatch(value)))


def _valid_payload(value: Any) -> bool:
    if type(value) is not str or not value or not value.strip() or len(value) > MAX_ALLOWED_PAYLOAD_CHARS:
        return False
    for char in value:
        codepoint = ord(char)
        if char in _ALLOWED_PAYLOAD_CONTROL_CHARS:
            continue
        if codepoint < 32 or 0x7F <= codepoint <= 0x9F:
            return False
    return True


def evaluate_turn_egress_policy(policy: Any) -> TurnEgressEvaluation:
    """Evaluate a host-owned per-turn envelope without performing I/O.

    The plugin deliberately does not classify or sanitize the original user
    message.  Only an explicit ``allow`` envelope for the ``public`` or
    ``sanitized`` data class, with a bounded ``allowed_payload``, can authorize
    hosted routing.
    """
    if policy is None:
        return TurnEgressEvaluation(
            allowed=False,
            decision="missing",
            data_class=None,
            status="unknown",
            reason_code="per_turn_policy_missing",
        )
    if not isinstance(policy, Mapping):
        return _invalid()

    version = policy.get("version")
    if type(version) is not int or version != TURN_EGRESS_POLICY_VERSION:
        return _invalid(version=version if type(version) is int else None)

    decision = policy.get("decision")
    data_class = policy.get("data_class")
    reason_code = policy.get("reason_code")
    if (
        type(decision) is not str
        or decision not in {"allow", "deny", "unknown"}
        or type(data_class) is not str
        or not data_class
        or len(data_class) > 64
        or data_class not in DATA_CLASSES
        or not _valid_reason_code(reason_code)
    ):
        return _invalid(version=version)

    if decision == "unknown":
        return TurnEgressEvaluation(
            allowed=False,
            decision=decision,
            data_class=data_class,
            status="unknown",
            reason_code="per_turn_policy_unknown",
            version=version,
        )

    if decision == "deny":
        reason = "restricted_data_class" if data_class in RESTRICTED_DATA_CLASSES else "per_turn_policy_denied"
        return TurnEgressEvaluation(
            allowed=False,
            decision=decision,
            data_class=data_class,
            status="denied",
            reason_code=reason,
            version=version,
        )

    if data_class not in ALLOWED_DATA_CLASSES:
        return TurnEgressEvaluation(
            allowed=False,
            decision=decision,
            data_class=data_class,
            status="denied",
            reason_code="restricted_data_class",
            version=version,
        )
    allowed_payload = policy.get("allowed_payload")
    if not _valid_payload(allowed_payload):
        return _invalid(version=version, data_class=data_class)
    if type(allowed_payload) is not str:  # keep the type explicit for callers and static checkers
        return _invalid(version=version, data_class=data_class)
    return TurnEgressEvaluation(
        allowed=True,
        decision=decision,
        data_class=data_class,
        status="allowed",
        reason_code="per_turn_policy_allowed",
        allowed_payload=allowed_payload,
        version=version,
    )


def is_routing_mode(value: Any) -> bool:
    """Return whether *value* is one of the explicit automatic modes."""
    return type(value) is str and value in ROUTING_MODES
