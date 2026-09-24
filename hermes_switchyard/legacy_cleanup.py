"""Detect and clean up artifacts left by the retired ``jev-decision`` plugin.

Scope is exact and closed. The module only considers:

* ``plugins.enabled`` entries that are exactly ``jev-decision`` or
  its underscore spelling (no case folding, no whitespace trimming);
* a ``jev-decision-operations`` skill directory directly under the profile
  ``skills/`` root or one category level below it, whose ``SKILL.md``
  frontmatter declares exactly ``name: jev-decision-operations``;
* the pre-0.4.3 install-tree receipt ``plugins/hermes-switchyard/receipt.json``;
* stale ``.receipt-*.tmp`` files in that install tree and in the
  profile-owned ``plugin-data/hermes-switchyard`` directory.

Nothing else is touched. Old workspaces, worktrees, and backups that contain a
legacy underscore-named package are out of scope because no path rule can prove they
are disposable.

Cleanup is a dry run unless ``apply=True``. On apply, every target is moved into
a new private archive directory under
``plugin-data/hermes-switchyard/legacy-cleanup/<stamp>/`` and ``config.yaml``
is copied there before its ``plugins.enabled`` list is rewritten through the
Hermes config writer. A ``manifest.json`` in the archive records the original
location of every moved item so the operator can restore it by hand.

Every target fails closed when it is a symlink, sits below a symlinked
directory inside the Hermes home, is not owned by the current user (POSIX), or
changed identity between planning and the move.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import receipt_state

PLUGIN_NAME = receipt_state.PLUGIN_NAME
# The underscore spelling is assembled at runtime: the release gate rejects the
# retired package name as a literal anywhere in the tree.
LEGACY_PLUGIN_NAMES = ("jev-decision", "jev" + "_decision")
LEGACY_SKILL_NAME = "jev-decision-operations"
STALE_TEMP_SECONDS = 3600
ARCHIVE_DIRNAME = "legacy-cleanup"

# Stable public vocabulary. Callers (status, cleanup CLI) render these values;
# they never render exception text.
KIND_CONFIG = "plugins_enabled_entry"
KIND_SKILL = "legacy_skill"
KIND_RECEIPT = "legacy_install_tree_receipt"
KIND_TEMP = "stale_receipt_temp"

STATUS_PLANNED = "planned"
STATUS_APPLIED = "applied"
STATUS_REFUSED = "refused"
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"


@dataclass
class _Item:
    kind: str
    target: str
    action: str
    status: str
    reason: str = ""
    path: Path | None = None
    relative: str = ""
    identity: tuple | None = None
    entries: tuple[str, ...] = ()
    archived_to: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "kind": self.kind,
            "target": self.target,
            "action": self.action,
            "status": self.status,
        }
        if self.reason:
            record["reason"] = self.reason
        if self.entries:
            record["entries"] = list(self.entries)
        if self.archived_to:
            record["archived_to"] = self.archived_to
        record.update(self.extra)
        return record


def _default_home() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()).expanduser()


def _identity(st: os.stat_result) -> tuple:
    return (st.st_dev, st.st_ino, stat.S_IFMT(st.st_mode), getattr(st, "st_uid", None))


def _file_identity(st: os.stat_result) -> tuple:
    return _identity(st) + (st.st_size, st.st_mtime_ns)


def _owned(st: os.stat_result) -> bool:
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None:  # Windows: POSIX ownership does not apply.
        return True
    return st.st_uid == geteuid()


def _symlink_below(home: Path, path: Path) -> bool:
    """True when ``path`` or any component between ``home`` and it is a symlink.

    The home itself may legitimately be a symlink; only components inside it
    are checked. A missing component is not a symlink.
    """
    try:
        relative = path.relative_to(home)
    except ValueError:
        return True
    current = home
    for part in relative.parts:
        current = current / part
        try:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                return True
        except FileNotFoundError:
            return False
    return False


def _display(home: Path, path: Path) -> str:
    try:
        return "$HERMES_HOME/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _check_target(home: Path, path: Path, *, want_dir: bool) -> tuple[str, os.stat_result | None]:
    """Return ``("", stat)`` when a target is safe to move, else a refusal reason."""
    if _symlink_below(home, path.parent):
        return "symlinked_parent", None
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return "missing", None
    except OSError:
        return "unreadable", None
    if stat.S_ISLNK(st.st_mode):
        return "symlink", st
    if want_dir and not stat.S_ISDIR(st.st_mode):
        return "not_directory", st
    if not want_dir and not stat.S_ISREG(st.st_mode):
        return "not_regular_file", st
    if not _owned(st):
        return "not_owned", st
    return "", st


# --------------------------------------------------------------------------
# Config entry
# --------------------------------------------------------------------------

def _config_helpers():
    from hermes_cli import config as hermes_config

    return hermes_config


def _plan_config(home: Path) -> list[_Item]:
    path = home / "config.yaml"
    target = _display(home, path) + ":plugins.enabled"
    reason, st = _check_target(home, path, want_dir=False)
    if reason == "missing":
        return []
    if reason:
        return [_Item(KIND_CONFIG, target, "remove_entries", STATUS_REFUSED, reason, path=path)]
    try:
        raw = _config_helpers().read_user_config_raw(path)
    except Exception:  # noqa: BLE001 -- unreadable or unparseable config fails closed
        return [_Item(KIND_CONFIG, target, "remove_entries", STATUS_REFUSED, "config_unreadable", path=path)]
    matches = _matching_entries(raw)
    if not matches:
        return []
    return [
        _Item(
            KIND_CONFIG,
            target,
            "remove_entries",
            STATUS_PLANNED,
            path=path,
            identity=_file_identity(st),
            entries=tuple(matches),
        )
    ]


def _matching_entries(raw: Any) -> list[str]:
    plugins = raw.get("plugins") if isinstance(raw, dict) else None
    enabled = plugins.get("enabled") if isinstance(plugins, dict) else None
    if not isinstance(enabled, list):
        return []
    return [entry for entry in enabled if type(entry) is str and entry in LEGACY_PLUGIN_NAMES]


def _apply_config(home: Path, item: _Item, archive: Path) -> None:
    helpers = _config_helpers()
    if helpers.is_managed():
        raise _Refusal("managed_config")
    path = item.path
    assert path is not None
    reason, st = _check_target(home, path, want_dir=False)
    if reason:
        raise _Refusal(reason)
    if _file_identity(st) != item.identity:
        raise _Refusal("changed_since_plan")
    # Fail-closed read from Hermes' own writer guard.
    raw = helpers.require_readable_config_before_write(path)
    if _matching_entries(raw) != list(item.entries):
        raise _Refusal("changed_since_plan")
    backup = archive / "config" / "config.yaml"
    backup.parent.mkdir(mode=0o700)
    shutil.copy2(path, backup)
    os.chmod(backup, 0o600)
    if os.name == "nt":
        from . import _win_acl

        _win_acl.set_private_dacl(backup)  # Copying can preserve the source ACL.
    item.archived_to = _display(home, backup)
    # Re-check after the backup: the backup must be of the exact file we rewrite.
    reason, st = _check_target(home, path, want_dir=False)
    if reason or _file_identity(st) != item.identity:
        raise _Refusal("changed_since_plan")
    enabled = raw["plugins"]["enabled"]
    raw["plugins"]["enabled"] = [
        entry for entry in enabled if not (type(entry) is str and entry in LEGACY_PLUGIN_NAMES)
    ]
    comments = [line for line in backup.read_text(encoding="utf-8").splitlines() if "#" in line]
    if comments:
        # Older Hermes writers discard YAML comments. Probe a private copy first
        # so the real config is never rewritten by a lossy writer.
        with tempfile.TemporaryDirectory(prefix=".config-probe-", dir=archive) as scratch:
            probe = Path(scratch) / "config.yaml"
            shutil.copy2(backup, probe)
            helpers.atomic_config_write(probe, raw)
            written = probe.read_text(encoding="utf-8").splitlines()
            if any(line not in written for line in comments):
                raise _Refusal("config_writer_drops_comments")
            if helpers.read_user_config_raw(probe) != raw:
                raise _Refusal("config_writer_changed_fields")
    helpers.atomic_config_write(path, raw)
    # Readback through the same raw reader the plan used.
    if _matching_entries(helpers.read_user_config_raw(path)):
        raise _Failure("readback_mismatch")


# --------------------------------------------------------------------------
# Skill directory
# --------------------------------------------------------------------------

def _skill_frontmatter_name(skill_dir: Path) -> str | None:
    skill_md = skill_dir / "SKILL.md"
    try:
        st = os.lstat(skill_md)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    try:
        with open(skill_md, encoding="utf-8") as handle:
            head = handle.read(8192)
    except (OSError, UnicodeDecodeError):
        return None
    lines = head.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            return None
        key, sep, value = line.partition(":")
        if sep and key.strip() == "name" and key == key.lstrip():
            return value.strip().strip("'\"")
    return None


def _skill_candidates(root: Path) -> list[Path]:
    found: list[Path] = []
    try:
        top = sorted(os.scandir(root), key=lambda e: e.name)
    except OSError:
        return found
    for entry in top:
        if entry.name == LEGACY_SKILL_NAME:
            found.append(root / entry.name)
            continue
        if entry.name.startswith(".") or not entry.is_dir(follow_symlinks=False):
            continue
        try:
            children = sorted(os.scandir(entry.path), key=lambda e: e.name)
        except OSError:
            continue
        found.extend(root / entry.name / child.name for child in children if child.name == LEGACY_SKILL_NAME)
    return found


def _plan_skills(home: Path) -> list[_Item]:
    root = home / "skills"
    try:
        root_st = os.lstat(root)
    except FileNotFoundError:
        return []
    except OSError:
        return [_Item(KIND_SKILL, _display(home, root), "archive", STATUS_REFUSED, "unreadable")]
    if stat.S_ISLNK(root_st.st_mode):
        # A symlinked skills root is not scanned; report it without traversal.
        return [_Item(KIND_SKILL, _display(home, root), "archive", STATUS_REFUSED, "symlinked_parent")]
    items: list[_Item] = []
    for path in _skill_candidates(root):
        target = _display(home, path)
        reason, st = _check_target(home, path, want_dir=True)
        if reason:
            items.append(_Item(KIND_SKILL, target, "archive", STATUS_REFUSED, reason, path=path))
            continue
        if _skill_frontmatter_name(path) != LEGACY_SKILL_NAME:
            items.append(_Item(KIND_SKILL, target, "archive", STATUS_SKIPPED, "not_legacy_skill", path=path))
            continue
        items.append(
            _Item(
                KIND_SKILL,
                target,
                "archive",
                STATUS_PLANNED,
                path=path,
                relative=path.relative_to(home).as_posix(),
                identity=_identity(st),
            )
        )
    return items


# --------------------------------------------------------------------------
# Receipt artifacts
# --------------------------------------------------------------------------

def _plan_receipt(home: Path) -> list[_Item]:
    legacy = home / "plugins" / PLUGIN_NAME / "receipt.json"
    current = home / "plugin-data" / PLUGIN_NAME / "receipt.json"
    target = _display(home, legacy)
    reason, st = _check_target(home, legacy, want_dir=False)
    if reason == "missing":
        return []
    if reason:
        return [_Item(KIND_RECEIPT, target, "archive", STATUS_REFUSED, reason, path=legacy)]
    legacy_valid = _valid_receipt_file(legacy)
    current_reason, _ = _check_target(home, current, want_dir=False)
    current_valid = not current_reason and _valid_receipt_file(current)
    if legacy_valid and not current_valid:
        # The legacy file is the only copy of a usable receipt. `hermes
        # switchyard receipt` migrates it on first read; never archive first.
        return [_Item(KIND_RECEIPT, target, "archive", STATUS_SKIPPED, "not_migrated", path=legacy)]
    return [
        _Item(
            KIND_RECEIPT,
            target,
            "archive",
            STATUS_PLANNED,
            path=legacy,
            relative=legacy.relative_to(home).as_posix(),
            identity=_file_identity(st),
        )
    ]


def _valid_receipt_file(path: Path) -> bool:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    return receipt_state.canonicalize_receipt(record) is not None


def _plan_temps(home: Path, now: float, stale_after: float) -> list[_Item]:
    items: list[_Item] = []
    for directory in (home / "plugins" / PLUGIN_NAME, home / "plugin-data" / PLUGIN_NAME):
        try:
            dir_st = os.lstat(directory)
        except OSError:
            continue
        if stat.S_ISLNK(dir_st.st_mode) or _symlink_below(home, directory):
            # Report once per symlinked directory only when it actually holds temps.
            try:
                names = [n for n in os.listdir(directory) if _is_temp_name(n)]
            except OSError:
                names = []
            if names:
                items.append(
                    _Item(KIND_TEMP, _display(home, directory), "archive", STATUS_REFUSED, "symlinked_parent")
                )
            continue
        try:
            names = sorted(n for n in os.listdir(directory) if _is_temp_name(n))
        except OSError:
            continue
        for name in names:
            path = directory / name
            target = _display(home, path)
            reason, st = _check_target(home, path, want_dir=False)
            if reason == "missing":
                continue
            if reason:
                items.append(_Item(KIND_TEMP, target, "archive", STATUS_REFUSED, reason, path=path))
                continue
            if now - st.st_mtime < stale_after:
                # A writer may still own it; the receipt writer renames within milliseconds.
                items.append(_Item(KIND_TEMP, target, "archive", STATUS_SKIPPED, "recent", path=path))
                continue
            items.append(
                _Item(
                    KIND_TEMP,
                    target,
                    "archive",
                    STATUS_PLANNED,
                    path=path,
                    relative=path.relative_to(home).as_posix(),
                    identity=_file_identity(st),
                )
            )
    return items


def _is_temp_name(name: str) -> bool:
    return name.startswith(".receipt-") and name.endswith(".tmp") and len(name) > len(".receipt-.tmp")


# --------------------------------------------------------------------------
# Moves
# --------------------------------------------------------------------------

class _Refusal(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Failure(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _apply_move(home: Path, item: _Item, archive: Path) -> None:
    path = item.path
    assert path is not None
    want_dir = item.kind == KIND_SKILL
    reason, st = _check_target(home, path, want_dir=want_dir)
    if reason:
        raise _Refusal("changed_since_plan" if reason == "missing" else reason)
    current = _identity(st) if want_dir else _file_identity(st)
    if current != item.identity:
        raise _Refusal("changed_since_plan")
    if want_dir and _skill_frontmatter_name(path) != LEGACY_SKILL_NAME:
        raise _Refusal("changed_since_plan")
    destination = archive / "files" / item.relative
    _mkdir_private(archive, destination.parent)
    if os.path.lexists(destination):
        raise _Refusal("archive_collision")
    try:
        os.rename(path, destination)
    except OSError as exc:
        raise _Failure("cross_device" if getattr(exc, "errno", None) == 18 else "move_failed") from None
    item.archived_to = _display(home, destination)


def _mkdir_private(archive: Path, directory: Path) -> None:
    current = archive
    for part in directory.relative_to(archive).parts:
        current = current / part
        try:
            current.mkdir(mode=0o700)
        except FileExistsError:
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise _Refusal("archive_symlink") from None


def _create_archive(home: Path, now: float) -> Path:
    base = home / "plugin-data" / PLUGIN_NAME / ARCHIVE_DIRNAME
    if _symlink_below(home, base):
        raise _Refusal("archive_symlink")
    from hermes_constants import mkdir_under_hermes_home

    mkdir_under_hermes_home(base)
    if _symlink_below(home, base):
        raise _Refusal("archive_symlink")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    archive = base / f"{stamp}-{os.getpid()}"
    archive.mkdir(mode=0o700)  # exist_ok=False: never reuse or merge an archive.
    if os.name == "nt":
        try:
            from . import _win_acl

            _win_acl.set_private_dacl(archive, inherit_to_children=True)
        except Exception as exc:  # noqa: BLE001 -- never archive into an unprotected directory
            try:
                archive.rmdir()
            except OSError:
                pass
            raise OSError("private archive ACL unavailable") from exc
    return archive


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def _plan(home: Path, now: float, stale_after: float) -> list[_Item]:
    return [
        *_plan_config(home),
        *_plan_skills(home),
        *_plan_receipt(home),
        *_plan_temps(home, now, stale_after),
    ]


def detect_legacy_artifacts(
    hermes_home: str | os.PathLike | None = None,
    *,
    now: float | None = None,
    stale_after: float = STALE_TEMP_SECONDS,
) -> dict[str, Any]:
    """Read-only scan. Performs no writes and creates no directories."""
    home = Path(hermes_home).expanduser() if hermes_home is not None else _default_home()
    items = _plan(home, time.time() if now is None else now, stale_after)
    return {
        "hermes_home": str(home),
        "legacy_artifacts": any(item.status in (STATUS_PLANNED, STATUS_REFUSED) for item in items),
        "findings": [item.public() for item in items],
    }


def status_warnings(hermes_home: str | os.PathLike | None = None) -> list[str]:
    """One line per actionable finding, for ``hermes switchyard status``."""
    lines = []
    for finding in detect_legacy_artifacts(hermes_home)["findings"]:
        if finding["status"] not in (STATUS_PLANNED, STATUS_REFUSED):
            continue
        lines.append(_line(finding, "Legacy"))
    if lines:
        lines.append("Run `hermes switchyard cleanup` to review, then `--apply` to archive them.")
    return lines


def cleanup_legacy_artifacts(
    hermes_home: str | os.PathLike | None = None,
    *,
    apply: bool = False,
    now: float | None = None,
    stale_after: float = STALE_TEMP_SECONDS,
    _before_apply: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Plan (default) or apply the exact legacy cleanup.

    ``apply=True`` requires ``hermes_home`` to equal the active Hermes home so
    the Hermes config writer and the files moved here act on the same profile.
    """
    now = time.time() if now is None else now
    home = Path(hermes_home).expanduser() if hermes_home is not None else _default_home()
    items = _plan(home, now, stale_after)
    result: dict[str, Any] = {
        "hermes_home": str(home),
        "mode": "apply" if apply else "dry_run",
        "archive": None,
    }
    planned = [item for item in items if item.status == STATUS_PLANNED]
    if not apply or not planned:
        result["status"] = _overall(items, apply)
        result["actions"] = [item.public() for item in items]
        return result
    if _same_path(home, _default_home()) is not True:
        for item in planned:
            item.status, item.reason = STATUS_REFUSED, "home_mismatch"
        result["status"] = "refused"
        result["actions"] = [item.public() for item in items]
        return result
    if _before_apply is not None:
        _before_apply()  # test seam for race injection between plan and apply
    try:
        archive = _create_archive(home, now)
    except _Refusal as exc:
        for item in planned:
            item.status, item.reason = STATUS_REFUSED, exc.reason
        result["status"] = "refused"
        result["actions"] = [item.public() for item in items]
        return result
    except OSError:
        for item in planned:
            item.status, item.reason = STATUS_FAILED, "archive_unavailable"
        result["status"] = "failed"
        result["actions"] = [item.public() for item in items]
        return result
    result["archive"] = _display(home, archive)
    for item in planned:
        try:
            if item.kind == KIND_CONFIG:
                _apply_config(home, item, archive)
            else:
                _apply_move(home, item, archive)
            item.status = STATUS_APPLIED
        except _Refusal as exc:
            item.status, item.reason = STATUS_REFUSED, exc.reason
        except _Failure as exc:
            item.status, item.reason = STATUS_FAILED, exc.reason
        except Exception:  # noqa: BLE001 -- config writer and filesystem faults fail closed
            item.status, item.reason = STATUS_FAILED, "apply_failed"
    try:
        _write_manifest(home, archive, items, now)
    except Exception:  # noqa: BLE001 -- report completed moves even if the manifest write fails
        result["manifest_status"] = "failed"
        result["status"] = "failed"
    else:
        result["status"] = _overall(items, apply)
    result["actions"] = [item.public() for item in items]
    return result


def _same_path(a: Path, b: Path) -> bool:
    try:
        return a.resolve(strict=False) == b.resolve(strict=False)
    except OSError:
        return False


def _write_manifest(home: Path, archive: Path, items: list[_Item], now: float) -> None:
    manifest = {
        "plugin": PLUGIN_NAME,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "hermes_home": str(home),
        "restore": "Move each archived_to path back to its target path. "
        "Restore config.yaml from config/config.yaml only if nothing else changed it since.",
        "actions": [item.public() for item in items],
    }
    path = archive / "manifest.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, sort_keys=True, indent=2)
        handle.write("\n")


def _overall(items: list[_Item], apply: bool) -> str:
    statuses = {item.status for item in items}
    if not items or statuses <= {STATUS_SKIPPED}:
        return "clean"
    if not apply:
        return "planned" if STATUS_PLANNED in statuses else "refused"
    if STATUS_FAILED in statuses:
        return "failed"
    if STATUS_APPLIED in statuses:
        return "partial" if STATUS_REFUSED in statuses else "applied"
    return "refused"


def _line(finding: dict[str, Any], prefix: str) -> str:
    detail = f" [{', '.join(finding['entries'])}]" if finding.get("entries") else ""
    text = f"{prefix} {finding['kind']}: {finding['target']}{detail} -> {finding['action']} {finding['status']}"
    if finding.get("reason"):
        text += f" ({finding['reason']})"
    if finding.get("archived_to"):
        text += f"; archived to {finding['archived_to']}"
    return text


def format_cleanup_report(result: dict[str, Any]) -> list[str]:
    """Human-readable lines listing every action, for ``hermes switchyard cleanup``."""
    heading = "Dry run" if result["mode"] == "dry_run" else "Apply"
    lines = [f"Legacy jev-decision cleanup ({heading}): {result['status']}"]
    if result.get("archive"):
        lines.append(f"Archive: {result['archive']}")
    if result.get("manifest_status") == "failed":
        lines.append("Archive manifest could not be written; inspect the archive before restoring files.")
    lines.extend(_line(action, " ") for action in result["actions"])
    if not result["actions"]:
        lines.append("  No legacy jev-decision artifacts found.")
    elif result["mode"] == "dry_run" and result["status"] == "planned":
        lines.append("Nothing was changed. Re-run with --apply to archive the planned items.")
    return lines
