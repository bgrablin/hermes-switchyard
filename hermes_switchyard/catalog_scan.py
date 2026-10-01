"""Read-only, bounded package review. Findings are indicators, never certification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .retrieved_screen import screen_text

MAX_FILES = 512
MAX_ENTRIES = 1024
MAX_FINDINGS = 4096
MAX_FILE_BYTES = 256_000
MAX_TOTAL_BYTES = 4_000_000
TEXT_SUFFIXES = frozenset(
    {
        ".py",
        ".sh",
        ".bash",
        ".js",
        ".ts",
        ".json",
        ".yaml",
        ".yml",
        ".md",
        ".toml",
        ".txt",
    }
)
SKIP_DIRS = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__"})
CODE_SUFFIXES = frozenset({".py", ".sh", ".bash", ".js", ".ts"})
RULES = (
    (
        "download_execute",
        re.compile(
            r"\b(?:curl|wget)\b[^\n]{0,400}\|\s*(?:(?:sudo|doas)\s+)?(?:sh|bash|zsh)\b"
        ),
    ),
    (
        "dynamic_execution",
        re.compile(
            r"\b(?:eval|exec)\s*\(|\b(?:os\.system|subprocess\.(?:run|Popen|call))\s*\("
        ),
    ),
    (
        "credential_access",
        re.compile(
            r"(?:\.ssh[/\\]|\.aws[/\\]credentials|\.env\b|os\.environ|process\.env)"
        ),
    ),
    (
        "network_access",
        re.compile(
            r"\b(?:requests\.(?:get|post|put)|urlopen|fetch)\s*\(|\b(?:curl|wget|scp|rsync)\b"
        ),
    ),
)


def inspect_text(name: str, text: str) -> list[dict[str, Any]]:
    """Evidence contains locations and rule IDs only; no source text or secrets."""
    findings: list[dict[str, Any]] = []
    suffix = Path(name).suffix.lower()
    for line_number, line in enumerate(text.splitlines(), 1):
        for reason in screen_text(line):
            findings.append({"path": name, "line": line_number, "rule": reason})
        if suffix in CODE_SUFFIXES:
            for rule, pattern in RULES:
                if pattern.search(line):
                    findings.append({"path": name, "line": line_number, "rule": rule})
    if suffix == ".json":
        try:
            value = json.loads(text)
        except (ValueError, TypeError, RecursionError):
            return findings + [{"path": name, "line": None, "rule": "invalid_json"}]
        servers = (
            value.get("mcpServers", value.get("mcp_servers", {}))
            if isinstance(value, dict)
            else {}
        )
        if not isinstance(servers, dict):
            findings.append({"path": name, "line": None, "rule": "invalid_mcp_config"})
        if isinstance(servers, dict):
            for server in servers.values():
                if not isinstance(server, dict):
                    findings.append(
                        {"path": name, "line": None, "rule": "invalid_mcp_entry"}
                    )
                    continue
                command = server.get("command")
                args = server.get("args", [])
                if (
                    (command is not None and not isinstance(command, str))
                    or not isinstance(args, list)
                    or not all(isinstance(arg, str) for arg in args)
                    or ("url" in server and not isinstance(server["url"], str))
                ):
                    findings.append(
                        {"path": name, "line": None, "rule": "invalid_mcp_entry"}
                    )
                    continue
                if command in {"npx", "uvx"}:
                    packages = [
                        a for a in args if isinstance(a, str) and not a.startswith("-")
                    ]
                    if not packages or not all(
                        re.search(r"(?:@\d|==\d)", p) for p in packages
                    ):
                        findings.append(
                            {
                                "path": name,
                                "line": None,
                                "rule": "unpinned_mcp_launcher",
                            }
                        )
                url = server.get("url", "")
                if isinstance(url, str) and url.startswith("http://"):
                    findings.append(
                        {"path": name, "line": None, "rule": "unencrypted_mcp_endpoint"}
                    )
    return findings[: MAX_FINDINGS + 1]


class _ScanBudget(Exception):
    pass


def _walk(fd, directory, skipped, entries, depth=0):
    # scandir consumes directory entries lazily, before the global limit. fwalk
    # materializes the entire directory before yielding and cannot enforce it.
    names = []
    with os.scandir(fd) as iterator:
        for entry in iterator:
            entries[0] += 1
            if entries[0] > MAX_ENTRIES:
                raise _ScanBudget()
            names.append(entry.name)
    files, directories = [], []
    for name in sorted(names):
        rel = str(Path(directory, name))
        try:
            mode = os.stat(name, dir_fd=fd, follow_symlinks=False).st_mode
        except OSError:
            skipped.append({"path": rel, "reason": "entry_changed"})
            continue
        if stat.S_ISLNK(mode):
            skipped.append({"path": rel, "reason": "symlink"})
        elif stat.S_ISDIR(mode):
            if name in SKIP_DIRS or depth >= 32:
                skipped.append(
                    {
                        "path": rel,
                        "reason": "excluded_directory"
                        if name in SKIP_DIRS
                        else "directory_depth",
                    }
                )
            else:
                directories.append(name)
        else:
            files.append(name)
    yield directory, files, fd
    for name in directories:
        rel = str(Path(directory, name))
        try:
            child = os.open(
                name, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY, dir_fd=fd
            )
        except OSError:
            skipped.append({"path": rel, "reason": "directory_changed"})
            continue
        try:
            yield from _walk(child, rel, skipped, entries, depth + 1)
        finally:
            os.close(child)


def scan_catalog(root: str | Path) -> dict[str, Any]:
    """Use descriptor-relative no-follow reads. Unavailable safety => no scan."""
    result: dict[str, Any] = {
        "schema": "switchyard.catalog_scan.v1",
        "status": "incomplete",
        "network": False,
        "executed": False,
        "coverage_complete": False,
        "files": [],
        "findings": [],
        "skipped": [],
        "bytes_read": 0,
        "content_sha256": None,
    }
    if (
        os.scandir not in os.supports_fd
        or os.open not in os.supports_dir_fd
        or not getattr(os, "O_NOFOLLOW", 0)
    ):
        result["skipped"].append({"path": ".", "reason": "safe_scan_unavailable"})
        return result
    root = Path(root)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY
    try:
        root_fd = os.open(root, flags)
    except OSError:
        result["skipped"].append({"path": ".", "reason": "root_unavailable"})
        return result
    exhausted = False
    visited = 0
    try:
        for directory, files, dirfd in _walk(root_fd, ".", result["skipped"], [0]):
            for name in files:
                visited += 1
                rel = str(Path(directory, name))
                if visited > MAX_FILES:
                    exhausted = True
                    break
                if (
                    Path(name).suffix.lower() not in TEXT_SUFFIXES
                    or name == ".env"
                    or name.startswith(".env.")
                ):
                    result["skipped"].append(
                        {"path": rel, "reason": "outside_text_scope"}
                    )
                    continue
                fd = None
                try:
                    fd = os.open(
                        name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dirfd
                    )
                    before = os.fstat(fd)
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or before.st_size > MAX_FILE_BYTES
                    ):
                        raise ValueError("file_scope")
                    with os.fdopen(fd, "rb") as stream:
                        fd = None
                        data = stream.read(MAX_FILE_BYTES + 1)
                        after = os.fstat(stream.fileno())
                    if len(data) > MAX_FILE_BYTES or (
                        before.st_size,
                        before.st_mtime_ns,
                    ) != (after.st_size, after.st_mtime_ns):
                        raise ValueError("file_changed_or_large")
                    if result["bytes_read"] + len(data) > MAX_TOTAL_BYTES:
                        exhausted = True
                        break
                    text = data.decode("utf-8")
                    if "\x00" in text:
                        raise ValueError("binary")
                    result["bytes_read"] += len(data)
                    result["files"].append(
                        {
                            "path": rel,
                            "sha256": hashlib.sha256(data).hexdigest(),
                            "bytes": len(data),
                        }
                    )
                    findings = inspect_text(rel, text)
                    remaining = MAX_FINDINGS - len(result["findings"])
                    result["findings"].extend(findings[:remaining])
                    if len(findings) > remaining:
                        result["skipped"].append(
                            {"path": rel, "reason": "finding_budget"}
                        )
                        exhausted = True
                        break
                except (OSError, ValueError, UnicodeError):
                    result["skipped"].append(
                        {"path": rel, "reason": "unreadable_changed_or_unsupported"}
                    )
                finally:
                    if fd is not None:
                        os.close(fd)
            if exhausted:
                result["skipped"].append({"path": ".", "reason": "scan_budget"})
                break
    except _ScanBudget:
        result["skipped"].append({"path": ".", "reason": "scan_budget"})
    except OSError:
        result["skipped"].append({"path": ".", "reason": "directory_changed"})
    finally:
        os.close(root_fd)
    result["content_sha256"] = hashlib.sha256(
        json.dumps(result["files"], sort_keys=True).encode()
    ).hexdigest()
    result["coverage_complete"] = not result["skipped"]
    result["status"] = (
        "review_required" if result["findings"] else "no_indicators_in_scanned_files"
    )
    return result
