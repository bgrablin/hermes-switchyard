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
        "public_or_sanitized_data_ack_required",
        "client_unavailable",
        "local_confident",
        "cache_hit",
        "empty_task",
        "no_candidates",
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


def safe_usage(value: Any) -> dict[str, float]:
    """Copy only bounded numeric usage fields into a receipt."""
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, float] = {}
    for key, item in value.items():
        if key not in USAGE_NUMERIC_KEYS or type(item) not in (int, float):
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


def store_latest_receipt(receipt: dict[str, Any]) -> bool:
    """Atomically retain the latest valid receipt for the diagnostic command."""
    if not validate_receipt(receipt):
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
            json.dump(receipt, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
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
    return record if validate_receipt(record) else None


def _finite(value: Any) -> bool:
    if type(value) not in (int, float) or isinstance(value, bool) or value < 0:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def validate_receipt(receipt: Any) -> bool:
    """Validate the serialized, privacy-safe operator diagnostic contract."""
    if not isinstance(receipt, dict) or set(receipt) != RECEIPT_FIELDS:
        return False
    terminal = receipt["terminal_state"]
    if terminal not in RECEIPT_TERMINAL_STATES:
        return False
    if receipt["source"] not in {"local", "jev", "none"}:
        return False
    selected = receipt["selected"]
    if selected is not None and safe_identifier(selected) is None:
        return False
    for key in ("hosted_attempted", "hosted_succeeded", "verified", "advisory_only"):
        if type(receipt[key]) is not bool:
            return False
    if receipt["verified"] is not False or receipt["advisory_only"] is not True:
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
        key not in USAGE_NUMERIC_KEYS or not _finite(value) for key, value in usage.items()
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
    if terminal == "local_selection" and (receipt["source"] != "local" or not selected):
        return False
    if terminal == "hosted_selection" and (receipt["source"] != "jev" or not receipt["hosted_attempted"] or not selected):
        return False
    if terminal == "hosted_failure_local_fallback" and (
        receipt["source"] != "local" or not receipt["hosted_attempted"] or not receipt["hosted_error"] or not selected
    ):
        return False
    if terminal == "hosted_skipped" and (receipt["hosted_attempted"] or not skip_reason):
        return False
    if terminal == "cache_hit" and receipt["hosted_attempted"]:
        return False
    return True
