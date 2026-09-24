"""Source identity, receipt validation, and operator-facing receipt state."""
from __future__ import annotations

import json
import logging
import math
import os
import re
import stat
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

PLUGIN_NAME = "hermes-switchyard"
SOURCE_MANIFEST_NAME = "SOURCE-MANIFEST.json"
RECEIPT_TEMPORARY_PREFIX = ".receipt-"
RECEIPT_TEMPORARY_SUFFIX = ".tmp"
# A receipt write finishes in milliseconds. A temporary file older than this
# was left by a crashed or failed write and is safe to remove.
STALE_TEMPORARY_SECONDS = 3600
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
        "deadline_exceeded",
        "host_cancelled",
        "late_result_discarded",
        "plugin_error",
    }
)
HOSTED_ERROR_DETAILS = frozenset({
    "stale_connection", "connect_failed", "timeout", "http_401", "http_403",
    "http_404", "http_429", "http_529", "http_4xx", "http_5xx",
    "redirect", "invalid_response", "transport_failed", "validation_failure",
    "request_budget_exhausted", "retry_budget_exhausted", "deadline_exceeded",
    "host_cancelled", "late_result_discarded", "ack_required", "unknown",
})
# Optional so receipts written by older versions remain readable.
OPTIONAL_RECEIPT_FIELDS = frozenset({"hosted_error_detail"})
HOSTED_SKIP_REASONS = frozenset(
    {
        "disabled",
        "ack_required",
        "local_scan_unknown_structured",
        "local_scan_unclassifiable",
        "local_scan_unclassified",
        "per_turn_policy_missing",
        "per_turn_policy_unknown",
        "per_turn_policy_invalid",
        "per_turn_policy_denied",
        "restricted_data_class",
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
        "explicit_override",
        "consumer_contract_unmet",
        "diagnostic_value_unavailable",
        "noninteractive_platform",
        "platform_not_listed",
        "kanban_worker",
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
# Delivery / adoption / outcome stay separate. The plugin records outcome as
# unverified so delivery alone is never reported as improvement.
CONSUMPTION_CONTRACT_FIELDS = frozenset({
    "delivery_status",
    "adoption_status",
    "outcome_status",
})
DELIVERY_STATUSES = frozenset({"delivered", "not_delivered", "skipped"})
ADOPTION_STATUSES = frozenset({"adopted", "not_adopted", "suppressed", "not_applicable"})
OUTCOME_STATUSES = frozenset({"unverified"})
# Exact valid (delivery_status, adoption_status) pairs. Contradictory mixes
# such as skipped+not_adopted are rejected in normalize and validate.
VALID_DELIVERY_ADOPTION_PAIRS = frozenset({
    ("delivered", "adopted"),
    ("delivered", "not_adopted"),
    ("skipped", "suppressed"),
    ("not_delivered", "not_applicable"),
})
# `advisory_only` means "no skill was loaded in this operation." A terminal
# consumer receipt records the load outcome instead, so `advisory_only` may
# be False only when the receipt carries a valid consumer record.
_CONSUMER_STATUSES = frozenset({"loaded", "load_failed", "explicit_override", "mandatory_conflict"})
# When both a consumer record and a consumption contract are present, the
# pair must match the consumer outcome exactly.
_CONSUMER_CONTRACT_EXPECTATIONS = {
    "loaded": ("delivered", "adopted"),
    "load_failed": ("delivered", "not_adopted"),
    "explicit_override": ("skipped", "suppressed"),
    "mandatory_conflict": ("skipped", "suppressed"),
}


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
    """Return the stable plugin/version/source identity used in receipts.

    The source SHA prefers a validated release manifest and falls back to the
    checked-out git commit read from ``.git`` files with no subprocess, so a
    git install reports an exact SHA instead of ``unavailable``. The
    no-argument path is cached per process so a later ``hermes plugins
    update`` cannot retroactively re-stamp receipts produced by older code.
    """
    if repo_dir is None:
        # Imported lazily: receipt_history builds on this module at import time.
        from .receipt_history import process_source_sha

        source_sha = process_source_sha()
    else:
        from .receipt_history import resolve_receipt_source_sha

        source_sha = resolve_receipt_source_sha(repo_dir)
    return {
        "plugin": PLUGIN_NAME,
        "version": resolve_plugin_version(repo_dir),
        "source_sha": source_sha,
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


def merge_usage(total: dict[str, float | None], usage: Any) -> None:
    """Add allowlisted usage fields. A missing cost stays unknown, never zero."""
    parsed = safe_usage(usage)
    if "cost" not in parsed:
        total["cost"] = None
    for key, value in parsed.items():
        if value is None:
            total[key] = None
            continue
        if key in total and total[key] is None:
            continue
        total[key] = float(total.get(key) or 0.0) + float(value)


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


def _plugin_data_dir() -> Path | None:
    """Return Hermes' profile-scoped mutable directory for this plugin.

    Recent Hermes versions expose ``plugins.plugin_storage.plugin_data_dir``
    specifically so runtime state is not mixed with an installed plugin
    checkout. The fallback keeps the standalone plugin usable with older
    Hermes hosts without ever falling back to the install directory.
    """
    try:
        from plugins.plugin_storage import plugin_data_dir
    except (ImportError, AttributeError):
        home = _hermes_home()
        return home / "plugin-data" / PLUGIN_NAME if home is not None else None
    try:
        return Path(plugin_data_dir(PLUGIN_NAME))
    except (ImportError, AttributeError, OSError, TypeError, ValueError):
        home = _hermes_home()
        return home / "plugin-data" / PLUGIN_NAME if home is not None else None


def _receipt_state_file() -> Path | None:
    """Return the profile-owned receipt path, never the installed source tree."""
    data_dir = _plugin_data_dir()
    return data_dir / "receipt.json" if data_dir is not None else None


def _legacy_receipt_state_file() -> Path | None:
    """Return the pre-0.4.3 install-tree path for one-way migration/readback."""
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


def _apply_private_permissions(path: Path) -> None:
    """Restrict a receipt to its owner where the platform supports it.

    POSIX: 0600. Windows: replace the inherited DACL with a protected DACL
    granting the current user, SYSTEM, and Administrators full control,
    matching Hermes's own Windows permission contract applied per file
    without spawning ``icacls``. Raises ``OSError`` on failure so the caller
    can fail closed (an unprotected receipt must never be published).
    """
    if os.name == "nt":
        from . import _win_acl

        _win_acl.set_private_dacl(path)
        return
    os.chmod(path, 0o600)


def _write_canonical_receipt(path: Path, canonical: dict[str, Any], *, no_clobber: bool = False) -> bool:
    """Atomically write one validated receipt with private permissions.

    ``no_clobber=True`` publishes with an atomic no-replace link so a
    destination created concurrently is never overwritten; the caller keeps
    whatever landed first.

    Best-effort: every failure returns ``False`` and the temporary file is
    closed before it is removed, so a failed write leaves no open handle and
    no ``.receipt-*.tmp`` file behind (an open handle blocks the unlink on
    Windows).
    """
    temporary: Path | None = None
    descriptor: int | None = None
    handle: Any = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_path = tempfile.mkstemp(
            prefix=RECEIPT_TEMPORARY_PREFIX, suffix=RECEIPT_TEMPORARY_SUFFIX, dir=path.parent
        )
        temporary = Path(raw_path)
        handle = os.fdopen(descriptor, "w", encoding="utf-8")
        # The file object now owns the descriptor and closes it.
        descriptor = None
        # Protect the file before any content is written to it.
        _apply_private_permissions(temporary)
        json.dump(canonical, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        if no_clobber:
            try:
                os.link(temporary, path)
            except FileExistsError:
                return False
            try:
                temporary.unlink()
            except OSError:
                pass
            temporary = None
            return True
        os.replace(temporary, path)
        temporary = None
        return True
    except Exception as exc:  # receipt I/O must never break skill routing
        _log_persist_failure(exc)
        return False
    finally:
        # Close before unlink on every path: an open handle blocks the
        # unlink on Windows and leaks a descriptor everywhere.
        _close_quietly(handle, descriptor)
        if temporary is not None:
            try:
                temporary.unlink()
            except Exception:  # noqa: BLE001 -- cleanup is best-effort
                pass


def _close_quietly(handle: Any, descriptor: int | None) -> None:
    if handle is not None:
        try:
            handle.close()
        except Exception:  # noqa: BLE001 -- cleanup is best-effort
            pass
    elif descriptor is not None:
        try:
            os.close(descriptor)
        except Exception:  # noqa: BLE001 -- cleanup is best-effort
            pass


def _log_persist_failure(exc: BaseException) -> None:
    # Log the exception class only: messages can carry local paths.
    logger.warning(
        "Switchyard routing receipt was not saved (%s); skill routing continues.",
        type(exc).__name__,
    )


_WINDOWS_REPARSE_POINT = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)


def _is_stale_temporary(entry: os.DirEntry, cutoff: float) -> bool:
    name = entry.name
    if not (
        name.startswith(RECEIPT_TEMPORARY_PREFIX)
        and name.endswith(RECEIPT_TEMPORARY_SUFFIX)
        and len(name) > len(RECEIPT_TEMPORARY_PREFIX) + len(RECEIPT_TEMPORARY_SUFFIX)
    ):
        return False
    if entry.is_symlink():
        return False
    status = entry.stat(follow_symlinks=False)
    if not stat.S_ISREG(status.st_mode):
        return False
    if getattr(status, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT:
        return False
    return status.st_mtime < cutoff


def _sweep_stale_temporaries(directory: Path | None) -> None:
    """Remove temporary receipt files left by earlier failed writes.

    Only regular files that match the receipt temporary name and are older
    than ``STALE_TEMPORARY_SECONDS`` are removed. Symlinks, reparse points,
    directories, and other names are never touched, even when their name
    matches. Never raises and never logs a path.
    """
    if directory is None:
        return
    try:
        cutoff = time.time() - STALE_TEMPORARY_SECONDS
        with os.scandir(directory) as entries:
            candidates = list(entries)
    except Exception:  # noqa: BLE001 -- cleanup must never break routing
        return
    for entry in candidates:
        try:
            if not _is_stale_temporary(entry, cutoff):
                continue
            # Re-check right before removal; the entry may have been
            # replaced by a symlink since the directory scan.
            current = os.lstat(entry.path)
            if stat.S_ISREG(current.st_mode) and not (
                getattr(current, "st_file_attributes", 0) & _WINDOWS_REPARSE_POINT
            ):
                os.unlink(entry.path)
        except Exception:  # noqa: BLE001 -- cleanup must never break routing
            continue


def store_latest_receipt(receipt: dict[str, Any]) -> bool:
    """Atomically retain the latest valid receipt for the diagnostic command.

    Best-effort: this function never raises. It returns ``True`` only when
    this receipt is now the saved receipt. Any other outcome (invalid
    receipt, no profile directory, or a write error of any type) returns
    ``False``; the caller's recommendation and skill load continue
    unchanged. Stale temporary files from earlier failed writes are swept
    on every call where the profile directory is known.
    """
    path: Path | None = None
    stored = False
    try:
        canonical = canonicalize_receipt(receipt)
        if canonical is not None:
            path = _receipt_state_file()
            if path is not None:
                stored = _write_canonical_receipt(path, canonical)
    except Exception as exc:  # receipt I/O must never break skill routing
        _log_persist_failure(exc)
        stored = False
    try:
        _sweep_stale_temporaries(path.parent if path is not None else None)
    except Exception:  # noqa: BLE001 -- cleanup must never change the result
        pass
    return stored


def _retire_legacy_receipt(legacy_path: Path, current_path: Path, expected: dict[str, Any] | None = None) -> None:
    """Remove only a regular legacy record after a valid new-file readback."""
    try:
        previous = legacy_path.lstat()
        if not stat.S_ISREG(previous.st_mode):
            return  # never unlink a symlink or directory
        if not stat.S_ISREG(current_path.lstat().st_mode):
            return  # a symlink is not a verified profile-owned migration target
        current = canonicalize_receipt(json.loads(current_path.read_text(encoding="utf-8")))
        if current is None or (expected is not None and current != expected):
            return
        legacy = canonicalize_receipt(json.loads(legacy_path.read_text(encoding="utf-8")))
        if legacy != current:
            return  # a distinct or invalid legacy record was not migrated
        now = legacy_path.lstat()
        def identity(item):
            return (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns)

        if stat.S_ISREG(now.st_mode) and identity(now) == identity(previous):
            legacy_path.unlink()
    except (OSError, TypeError, ValueError):
        # A failed cleanup must not discard the valid profile-owned record.
        return


def read_latest_receipt() -> dict[str, Any] | None:
    """Read the profile-owned receipt and migrate one valid legacy record."""
    path = _receipt_state_file()
    if path is not None:
        try:
            _sweep_stale_temporaries(path.parent)
        except Exception:  # noqa: BLE001 -- cleanup must never break readback
            pass
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, TypeError, ValueError):
            record = None
        canonical = canonicalize_receipt(record)
        if canonical is not None:
            legacy_path = _legacy_receipt_state_file()
            if legacy_path is not None:
                _retire_legacy_receipt(legacy_path, path, canonical)
            return canonical

    legacy_path = _legacy_receipt_state_file()
    if legacy_path is None or (path is not None and path.exists()):
        return None
    try:
        record = json.loads(legacy_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        # Another reader may have migrated and retired the legacy file after
        # our first new-file check. Return its verified profile-owned record.
        if path is not None:
            try:
                return canonicalize_receipt(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, TypeError, ValueError):
                pass
        return None
    canonical = canonicalize_receipt(record)
    if canonical is None:
        return None

    # Never replace an unrelated or malformed new file. Publish with atomic
    # no-clobber semantics; delete the regular legacy artifact only after the
    # new profile-owned record is read back and matches this migration.
    if path is not None and not path.exists():
        if _write_canonical_receipt(path, canonical, no_clobber=True):
            _retire_legacy_receipt(legacy_path, path, canonical)
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
    delivery_status = receipt.get("delivery_status")
    adoption_status = receipt.get("adoption_status")
    outcome_status = receipt.get("outcome_status")
    contract_present = any(
        value is not None for value in (delivery_status, adoption_status, outcome_status)
    )
    if contract_present:
        if (
            delivery_status not in DELIVERY_STATUSES
            or adoption_status not in ADOPTION_STATUSES
            or outcome_status not in OUTCOME_STATUSES
        ):
            return None
        # Exact valid pairs only; contradictory mixes are dropped.
        if (delivery_status, adoption_status) not in VALID_DELIVERY_ADOPTION_PAIRS:
            return None
        if consumer_status is not None:
            expected = _CONSUMER_CONTRACT_EXPECTATIONS.get(consumer_status)
            if expected is None or (delivery_status, adoption_status) != expected:
                return None
        receipt["delivery_status"] = delivery_status
        receipt["adoption_status"] = adoption_status
        receipt["outcome_status"] = "unverified"
    receipt["verified"] = False
    return receipt


def validate_receipt(receipt: Any) -> bool:
    """Validate one canonical receipt exactly as supplied, nothing more."""
    if not isinstance(receipt, dict):
        return False
    allowed_fields = RECEIPT_FIELDS | OPTIONAL_RECEIPT_FIELDS | CONSUMER_RECEIPT_FIELDS | CONSUMPTION_CONTRACT_FIELDS
    if set(receipt) - allowed_fields:
        # Undeclared fields are rejected (difference test, not superset).
        return False
    if not RECEIPT_FIELDS <= set(receipt):
        return False
    consumer_present = bool(CONSUMER_RECEIPT_FIELDS & set(receipt))
    if consumer_present and not CONSUMER_RECEIPT_FIELDS <= set(receipt):
        # Partial consumer records are rejected; they appear as a group.
        return False
    contract_present = bool(CONSUMPTION_CONTRACT_FIELDS & set(receipt))
    if contract_present and not CONSUMPTION_CONTRACT_FIELDS <= set(receipt):
        return False
    if contract_present:
        if (
            receipt.get("delivery_status") not in DELIVERY_STATUSES
            or receipt.get("adoption_status") not in ADOPTION_STATUSES
            or receipt.get("outcome_status") not in OUTCOME_STATUSES
        ):
            return False
        if (
            receipt["delivery_status"],
            receipt["adoption_status"],
        ) not in VALID_DELIVERY_ADOPTION_PAIRS:
            return False
        # Never accept a claimed outcome improvement on a plugin receipt.
        if receipt["outcome_status"] != "unverified":
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
        if contract_present:
            expected = _CONSUMER_CONTRACT_EXPECTATIONS.get(status)
            if expected is None or (
                receipt["delivery_status"],
                receipt["adoption_status"],
            ) != expected:
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
    if "hosted_error_detail" in receipt:
        detail = receipt["hosted_error_detail"]
        if detail is not None and (
            type(detail) is not str or detail not in HOSTED_ERROR_DETAILS or receipt["hosted_error"] is None
        ):
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
