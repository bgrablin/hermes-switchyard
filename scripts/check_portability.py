#!/usr/bin/env python3
"""Fail closed on host-specific paths and stale standalone-plugin layout."""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF = Path(__file__).resolve()
TEXT_SUFFIXES = {".md", ".py", ".json", ".yaml", ".yml", ".txt", ".toml", ".xml"}
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/(?:home|Users|private|var|tmp)(?:/|\\\\))")
IPV4 = re.compile(r"(?<![0-9])(?:\d{1,3}\.){3}\d{1,3}(?![0-9])")
STALE_VERSION = ".".join(("0", "3", "1"))
FORBIDDEN_AUTHOR = "author: " + "Hermes"
PUBLIC_FIXTURE_PRIVATE_TERMS = re.compile(r"\b(?:private|employer|regulated|credential|payment|verification)\b", re.IGNORECASE)


def _files() -> list[Path]:
    ignored_names = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
    result = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.resolve() == SELF:
            continue
        if any(part in ignored_names for part in path.parts):
            continue
        if path.name.startswith("results") and path.parent.name == "evaluation":
            continue
        if path.suffix.lower() in TEXT_SUFFIXES:
            result.append(path)
    return result


def main() -> int:
    failures: list[str] = []
    manifest = ROOT / "plugin.yaml"
    entrypoint = ROOT / "__init__.py"
    nested_manifest = ROOT / "jev_decision" / "plugin.yaml"
    if not manifest.is_file():
        failures.append("missing root plugin.yaml")
    if not entrypoint.is_file():
        failures.append("missing root __init__.py entrypoint")
    if nested_manifest.exists():
        failures.append("nested jev_decision/plugin.yaml would create duplicate plugin layout")

    manifest_text = manifest.read_text(encoding="utf-8") if manifest.is_file() else ""
    for required in ("version: 0.3.2", "author: bgrablin", "jev_computer_use", "jev_skill_select", "jev_model_route"):
        if required not in manifest_text:
            failures.append(f"root manifest missing {required!r}")

    for path in _files():
        text = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(ROOT).as_posix()
        if ABSOLUTE_PATH.search(text):
            failures.append(f"host-specific absolute path in {rel}")
        if IPV4.search(text):
            failures.append(f"IPv4 address in {rel}")
        if STALE_VERSION in text:
            failures.append(f"stale source version in {rel}")
        if FORBIDDEN_AUTHOR.casefold() in text.casefold():
            failures.append(f"stale plugin author in {rel}")

    fixture = ROOT / "evaluation" / "fixtures.json"
    if fixture.is_file() and PUBLIC_FIXTURE_PRIVATE_TERMS.search(fixture.read_text(encoding="utf-8")):
        failures.append("public synthetic fixtures contain a private-data term")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    print("portability checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
