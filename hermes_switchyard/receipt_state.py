"""Source identity, receipt validation, and operator-facing receipt state."""
from __future__ import annotations

import json
import math
import os
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PLUGIN_NAME = "hermes-switchyard"
SOURCE_MANIFEST_NAME = "SOURCE-MANIFEST.json"
RECEIPT_SOURCE_SHA_UNAVAILABLE = "unavailable"
RECEIPT_VERSION_UNAVAILABLE = "unavailable"
SOURCE_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
VERSION_RE = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?$")
SAFE_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
SAFE_REASON_PART_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

RECEIPT_TERMINAL_STATES = frozenset(
    {
        "local_selection",
        "hosted_selection",
        "hosted_abstention",
        "hosted_failure",
        "hosted_failure_local_fallback",
        "hosted_skipped",
        "cache_hit",
    }
)
HOSTED_ERROR_CODES = frozenset(
    {
        "transport_or_execution_failure",
        "ack_required",
        "validation_failure",
        "typed_response_failure",
        "request_budget_exhausted",
        "plugin_error",
    }
)
HOSTED_SKIP_REASONS = frozenset(
    {
        "disabled",
        "ack_required",
        "local_scan_unknown_structured",
        "local_scan_unclassifiable",
        "local_scan_control_character",
        "local_scan_prompt_injection",
        "local_scan_payment_data",
        "local_scan_verification_data",
        "local_scan_contact_identifier",
        "local_scan_secret_like_value",
        "local_scan_restricted_data",
        "public_or_sanitized_data_ack_required",
        "client_unavailable",
        "local_confident",
        "cache_hit",
        "empty_task",
        "no_candidates",
        "routing_mode_off",
        "routing_mode_local_only",
        "diagnostic_value_unavailable",
    }
)
USAGE_NUMERIC_KEYS = frozenset(
    {
        "cost",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
    }
)
RECEIPT_FIELDS = frozenset(
    {
        "terminal_state",
        "source",
        "selected",
        "hosted_attempted",
        "hosted_succeeded",
        "hosted_error",
        "hosted_skip_reason",
        "abstention_reason",
        "jev_model",
        "request_id",
        "request_count",
        "latency_ms",
        "total_latency_ms",
        "total_usage",
        "candidate_count",
        "offered_count",
        "excluded_count",
        "shortlist_policy",
        "verified",
        "advisory_only",
        "plugin_identity",
        "source_sha",
    }
)
PLUGIN_IDENTITY_FIELDS = frozenset({"plugin", "version", "source_sha"})
# Fields a terminal automatic consumer adds to a receipt so the load outcome
# survives a persistence/readback round trip. `consumer_status` names the
# terminal load result; `loaded_skill`, `loaded_source`, and
# `skill_load_verified` capture the successful-load readback.
CONSUMER_RECEIPT_FIELDS = frozenset({
    "consumer_status",
    "loaded_skill",
    "loaded_source",
    "skill_load_verified",
})
# `advisory_only` means "no skill was loaded in this operation." A terminal
# consumer receipt records the load outcome instead, so `advisory_only` may
# be False only when the receipt carries a valid consumer record.
_CONSUMER_STATUSES = frozenset({"loaded", "load_failed", "explicit_override", "mandatory_conflict"})


def _plugin_root(repo_dir: Path | str | None = None) -> Path:
    if repo_dir is not None:
        return Path(repo_dir).resolve()
    return Path(__file__).resolve().parent.parent


def resolve_source_sha(repo_dir: Path | str | None = None) -> str:
    """Return the exact source SHA from a validated release manifest."""
    path = _plugin_root(repo_dir) / SOURCE_MANIFEST_NAME
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return RECEIPT_SOURCE_SHA_UNAVAILABLE
    if not isinstance(manifest, dict):
        return RECEIPT_SOURCE_SHA_UNAVAILABLE
    expected_keys = {"files", "format", "manifest_version", "plugin", "source_sha", "version"}
    if set(manifest) != expected_keys:
        return RECEIPT_SOURCE_SHA_UNAVAILABLE
    if (
        manifest.get("format") != 1
        or manifest.get("manifest_version") != 1
        or manifest.get("plugin") != PLUGIN_NAME
        or not isinstance(manifest.get("version"), str)
        or not VERSION_RE.fullmatch(manifest["version"])
        or not isinstance(manifest.get("files"), list)
    ):
        return RECEIPT_SOURCE_SHA_UNAVAILABLE
    source_sha = manifest.get("source_sha")
    if not isinstance(source_sha, str) or not SOURCE_SHA_RE.fullmatch(source_sha):
        return RECEIPT_SOURCE_SHA_UNAVAILABLE
    return source_sha


def resolve_plugin_version(repo_dir: Path | str | None = None) -> str:
    """Return the manifest version, or an explicit unavailable sentinel."""
    path = _plugin_root(repo_dir) / "plugin.yaml"
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return RECEIPT_VERSION_UNAVAILABLE
    match = re.search(r"(?m)^\s*version:\s*([^\s#]+)", text)
    if match is None:
        return RECEIPT_VERSION_UNAVAILABLE
    value = match.group(1).strip().strip("'\"")
    return value if VERSION_RE.fullmatch(value) else RECEIPT_VERSION_UNAVAILABLE


def plugin_identity(repo_dir: Path | str | None = None) -> dict[str, str]:
    """Return the stable plugin/version/source identity used in receipts."""
    return {
        "plugin": PLUGIN_NAME,
        "version": resolve_plugin_version(repo_dir),
        "source_sha": resolve_source_sha(repo_dir),
    }


def safe_identifier(value: Any, *, max_length: int = 128) -> str | None:
    """Keep only bounded ASCII identifiers suitable for diagnostic output."""
    if type(value) is not str or len(value) > max_length:
        return None
    return value if SAFE_IDENTIFIER_RE.fullmatch(value) else None


def safe_reason(value: Any, *, max_length: int = 256) -> str | None:
    """Keep a bounded closed-form reason without arbitrary provider text."""
    if type(value) is not str:
        return None
    value = value.strip()
    if not value or len(value) > max_length:
        return None
    parts = value.split(";")
    return value if all(SAFE_REASON_PART_RE.fullmatch(part) for part in parts) else None


def finite_nonnegative(value: Any, default: float = 0.0) -> float:
    """Convert a finite non-negative number without allowing overflow or NaN."""
    if type(value) not in (int, float):
        return default
    try:
        converted = float(value)
    except (OverflowError, ValueError):
        return default
    return converted if math.isfinite(converted) and converted >= 0 else default


def nonnegative_int(value: Any, default: int = 0) -> int:
    """Return an ordinary non-negative integer, otherwise a safe default."""
    return value if type(value) is int and value >= 0 else default


def safe_usage(value: Any) -> dict[str, float | None]:
    """Copy only bounded numeric usage fields into a receipt."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float | None] = {}
    for key, item in value.items():
        if key not in USAGE_NUMERIC_KEYS:
            continue
        if key == "cost" and item is None:
            result[key] = None
            continue
        if type(item) not in (int, float):
            continue
        numeric = finite_nonnegative(item, default=-1.0)
        if numeric >= 0:
            result[key] = numeric
    return result


def _hermes_home() -> Path | None:
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured).expanduser()
    try:
        from hermes_constants import get_hermes_home
    except (ImportError, AttributeError):
        return None
    try:
        return Path(get_hermes_home()).expanduser()
    except (OSError, TypeError, ValueError):
        return None


def _receipt_state_file() -> Path | None:
    """Return the plugin-owned state path when a Hermes home is available."""
    home = _hermes_home()
    if home is None:
        return None
    return home / "plugins" / PLUGIN_NAME / "receipt.json"


def canonicalize_receipt(receipt: Any) -> dict[str, Any] | None:
    """Return the exact record that validate, persist, and readback agree on."""
    normalized = normalize_receipt(receipt)
    if normalized is None or not validate_receipt(normalized):
        return None
    return normalized


def store_latest_receipt(receipt: dict[str, Any]) -> bool:
    """Atomically retain the latest valid receipt for the diagnostic command."""
    canonical = canonicalize_receipt(receipt)
    if canonical is None:
        return False
    path = _receipt_state_file()
    if path is None:
        return False
    temporary: Path | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_path = tempfile.mkstemp(prefix=".receipt-", suffix=".tmp", dir=path.parent)
        temporary = Path(raw_path)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(canonical, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
        return True
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass


def read_latest_receipt() -> dict[str, Any] | None:
    """Read the latest persisted receipt only when it matches the contract."""
    path = _receipt_state_file()
    if path is None:
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return None
    canonical = canonicalize_receipt(record)
    return canonical


def _finite(value: Any) -> bool:
    if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def normalize_receipt(receipt: Any) -> dict[str, Any] | None:
    """Return a receipt valid by construction, or None when malformed.

    Advisory-only receipts keep ``advisory_only=True`` and carry no consumer
    record. Terminal consumer receipts carry a valid consumer record;
    ``advisory_only`` is derived as ``consumer_status != 'loaded'`` so a
    load-mode record is valid by construction and an advisory record is never
    misread as a successful load. ``verified`` stays False so a load is never
    presented as task completion.
    """
    if not isinstance(receipt, dict):
        return None
    receipt = dict(receipt)
    consumer_status = receipt.get("consumer_status")
    if consumer_status is not None and (
        type(consumer_status) is not str or consumer_status not in _CONSUMER_STATUSES
    ):
        return None
    skill_load_verified = receipt.get("skill_load_verified")
    if skill_load_verified is not None and type(skill_load_verified) is not bool:
        return None
    loaded_skill = receipt.get("loaded_skill")
    if loaded_skill is not None and safe_identifier(loaded_skill) is None:
        return None
    loaded_source = receipt.get("loaded_source")
    if loaded_source is not None and safe_identifier(loaded_source) is None:
        return None
    if consumer_status is not None:
        receipt["consumer_status"] = consumer_status
        receipt["loaded_skill"] = loaded_skill
        receipt["loaded_source"] = loaded_source
        receipt["skill_load_verified"] = skill_load_verified
        receipt["advisory_only"] = bool(consumer_status != "loaded")
    receipt["verified"] = False
    return receipt


def validate_receipt(receipt: Any) -> bool:
    """Validate one canonical receipt exactly as supplied, nothing more."""
    if not isinstance(receipt, dict):
        return False
    allowed_fields = RECEIPT_FIELDS | CONSUMER_RECEIPT_FIELDS
    if set(receipt) - allowed_fields:
        # Undeclared fields are rejected (difference test, not superset).
        return False
    if not RECEIPT_FIELDS <= set(receipt):
        return False
    consumer_present = bool(CONSUMER_RECEIPT_FIELDS & set(receipt))
    if consumer_present and not CONSUMER_RECEIPT_FIELDS <= set(receipt):
        # Partial consumer records are rejected; they appear as a group.
        return False
    # A successful-load receipt requires the complete evidence group; a failed
    # or overridden load cannot claim a verified load or a loaded skill.
    if consumer_present:
        status = receipt["consumer_status"]
        if status == "loaded":
            if (
                safe_identifier(receipt.get("loaded_skill") or "") is None
                or receipt.get("loaded_source") not in {"local", "jev"}
                or receipt.get("skill_load_verified") is not True
                or receipt.get("advisory_only") is not False
            ):
                return False
        else:
            if receipt.get("loaded_skill") is not None or receipt.get("loaded_source") is not None:
                return False
            if status == "load_failed" and receipt.get("skill_load_verified") is not False:
                return False
    if receipt["terminal_state"] not in RECEIPT_TERMINAL_STATES:
        return False
    if receipt["source"] not in {"local", "jev", "none"}:
        return False
    selected = receipt["selected"]
    if selected is not None and safe_identifier(selected) is None:
        return False
    for key in ("hosted_attempted", "hosted_succeeded", "verified", "advisory_only"):
        if type(receipt[key]) is not bool:
            return False
    if receipt["verified"] is not False:
        return False
    # advisory_only is False only for a terminal consumer receipt that recorded
    # a load outcome; an advisory-only receipt keeps advisory_only True.
    if receipt["advisory_only"] is False and not consumer_present:
        return False
    if receipt["hosted_succeeded"] and not receipt["hosted_attempted"]:
        return False
    if receipt["hosted_error"] is not None and receipt["hosted_error"] not in HOSTED_ERROR_CODES:
        return False
    skip_reason = receipt["hosted_skip_reason"]
    if skip_reason is not None and skip_reason not in HOSTED_SKIP_REASONS:
        return False
    abstention_reason = receipt["abstention_reason"]
    if abstention_reason is not None and safe_reason(abstention_reason) is None:
        return False
    for key in ("jev_model", "request_id", "shortlist_policy"):
        if receipt[key] is not None and safe_identifier(receipt[key]) is None:
            return False
    for key in ("request_count", "candidate_count", "offered_count", "excluded_count"):
        if type(receipt[key]) is not int or receipt[key] < 0:
            return False
    for key in ("latency_ms", "total_latency_ms"):
        if not _finite(receipt[key]):
            return False
    usage = receipt["total_usage"]
    if not isinstance(usage, dict) or any(
        key not in USAGE_NUMERIC_KEYS
        # The single unknown-cost representation is `cost: None` (matching
        # safe_usage). Anything else must be a finite non-negative number;
        # a numeric cost is validated like every other usage field.
        or (key != "cost" and not _finite(value))
        or (key == "cost" and value is not None and not _finite(value))
        for key, value in usage.items()
    ):
        return False
    source_sha = receipt["source_sha"]
    if (
        type(source_sha) is not str
        or (source_sha != RECEIPT_SOURCE_SHA_UNAVAILABLE and not SOURCE_SHA_RE.fullmatch(source_sha))
    ):
        return False
    identity = receipt["plugin_identity"]
    if not isinstance(identity, dict) or set(identity) != PLUGIN_IDENTITY_FIELDS:
        return False
    if (
        identity["plugin"] != PLUGIN_NAME
        or safe_identifier(identity["version"], max_length=64) is None
        or type(identity["source_sha"]) is not str
    ):
        return False
    if identity["source_sha"] != source_sha:
        return False
    if receipt["terminal_state"] == "local_selection" and (receipt["source"] != "local" or not selected):
        return False
    if receipt["terminal_state"] == "hosted_selection" and (receipt["source"] != "jev" or not receipt["hosted_attempted"] or not selected):
        return False
    if receipt["terminal_state"] == "hosted_failure_local_fallback" and (
        receipt["source"] != "local" or not receipt["hosted_attempted"] or not receipt["hosted_error"] or not selected
    ):
        return False
    if receipt["terminal_state"] == "hosted_skipped" and (receipt["hosted_attempted"] or not skip_reason):
        return False
    if receipt["terminal_state"] == "cache_hit" and receipt["hosted_attempted"]:
        return False
    return True
