#!/usr/bin/env python3
"""Check tracked repository files for portable, public-ready content."""
from __future__ import annotations

import argparse
import io
import json
import re
import subprocess
import sys
import tokenize
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SELF_RELATIVE = Path(__file__).resolve().relative_to(ROOT).as_posix()
TEXT_SUFFIXES = {
    ".json",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
ABSOLUTE_PATH = re.compile(r"(?<![A-Za-z0-9])(?:[A-Za-z]:[\\/]|/(?:home|Users|private|var|tmp)(?:/|\\\\))")
IPV4 = re.compile(r"(?<![0-9])(?:\d{1,3}\.){3}\d{1,3}(?![0-9])")
PRIVATE_HOSTNAME = re.compile(
    r"(?i)(?<![A-Za-z0-9_.-])(?:[A-Za-z0-9-]+\.)+(?:local|lan|internal|home|corp)(?![A-Za-z0-9_.-])"
)
CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?ix)(?:\b|_)(?:api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|password|passwd|private[_-]?key|secret)\b"
    r"\s*[:=]\s*(?:[\"'](?P<quoted>[^\"'\r\n]{8,})[\"']|(?P<bare>[A-Za-z0-9][A-Za-z0-9_./+=:-]{11,}))"
)
TOKEN_SHAPE = re.compile(
    r"(?i)\b(?:ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{16,}|sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{20,})\b"
)
STALE_BRANDING = re.compile(r"(?i)\bopenclaw\b")
AGENT_IDENTITY = re.compile(r"(?im)^\s*(?:author|authors|committer|signer)\s*:\s*(?:hermes|gpt|claude|codex|assistant)\b")
NON_ENGLISH_COMMENT_SCRIPT = re.compile(
    r"[\u0370-\u052f\u0590-\u08ff\u0900-\u0dff\u1100-\u11ff\u3040-\u30ff\u3130-\u318f\u4e00-\u9fff]"
)
TRACKED_OPERATIONAL_NAME = re.compile(
    r"(?i)(?:^|[-_.])(handoff|hand-off|transcript|session-log|evidence)(?:[-_.]|$)"
)
SYNTHETIC_CREDENTIAL_VALUES = frozenset(
    {
        "test-key",
        "fixture-key",
        "fixture-key-value",
        "fixture-key",
        "fixture-profile-a",
        "fixture-profile-b",
        "offline-only-placeholder",
        "not-a-release-value",
    }
)
CREDENTIAL_FILE_NAMES = frozenset({
    ".env",
    ".credentials",
    "auth.json",
    "credentials",
    "credentials.json",
    "secret.json",
    "secrets.json",
})
CREDENTIAL_FILE_SUFFIXES = frozenset({".key", ".pem", ".p12", ".pfx"})
REQUIRED_MANIFEST_TEXT = (
    "name: hermes-switchyard",
    "author: bgrablin",
    "jev_computer_use",
    "jev_skill_select",
    "jev_skill_select_many",
    "jev_model_route",
)


def _yaml_scalar(text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}:\s*([^#\r\n]+)", text)
    if match is None:
        return None
    value = match.group(1).strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value or None


def _tracked_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            capture_output=True,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("git tracked-file listing failed") from exc
    if result.returncode != 0:
        raise RuntimeError("git tracked-file listing failed")
    paths: list[Path] = []
    for raw in result.stdout.split(b"\0"):
        if not raw:
            continue
        relative = raw.decode("utf-8", errors="strict")
        path = root / relative
        if path.is_file():
            paths.append(path)
        else:
            raise RuntimeError(f"tracked file is missing: {relative}")
    return sorted(paths, key=lambda path: path.relative_to(root).as_posix())


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _tracked_path_failures(relative: str) -> list[str]:
    path = Path(relative)
    lower_name = path.name.casefold()
    failures: list[str] = []
    if path.suffix.casefold() in {".log", ".jsonl"}:
        failures.append(f"operational log file is tracked in {relative}")
    if any(part.casefold() in {"evidence", "local-evidence", "raw-runs", "handoffs"} for part in path.parts):
        failures.append(f"operational evidence path is tracked in {relative}")
    if TRACKED_OPERATIONAL_NAME.search(lower_name):
        failures.append(f"operational handoff or transcript name is tracked in {relative}")
    return failures


def _comment_failures(relative: str, text: str) -> list[str]:
    if not relative.casefold().endswith(".py"):
        return []
    failures: list[str] = []
    try:
        tokens = tokenize.generate_tokens(io.StringIO(text).readline)
        for token in tokens:
            if token.type == tokenize.COMMENT and NON_ENGLISH_COMMENT_SCRIPT.search(token.string):
                failures.append(f"non-English script in Python comment in {relative}")
                break
    except (tokenize.TokenError, IndentationError):
        # Compilation is a separate check; keep the hygiene scan focused on
        # files that can be tokenized without exposing parser text.
        pass
    return failures


def _content_failures(relative: str, text: str) -> list[str]:
    failures: list[str] = []
    if ABSOLUTE_PATH.search(text):
        failures.append(f"host-specific absolute path in {relative}")
    if IPV4.search(text):
        failures.append(f"IPv4 address in {relative}")
    if PRIVATE_HOSTNAME.search(text):
        failures.append(f"private hostname suffix in {relative}")
    if STALE_BRANDING.search(text):
        failures.append(f"stale branding in {relative}")
    if AGENT_IDENTITY.search(text):
        failures.append(f"agent identity attribution in {relative}")
    if TOKEN_SHAPE.search(text):
        failures.append(f"credential-shaped token in {relative}")
    for match in CREDENTIAL_ASSIGNMENT.finditer(text):
        value = (match.group("quoted") or match.group("bare") or "").casefold()
        if value not in SYNTHETIC_CREDENTIAL_VALUES:
            failures.append(f"credential-shaped assignment in {relative}")
            break
    failures.extend(_comment_failures(relative, text))
    return failures


def _credential_path_failures(relative: str) -> list[str]:
    name = Path(relative).name.casefold()
    if name in CREDENTIAL_FILE_NAMES or name.endswith(tuple(CREDENTIAL_FILE_SUFFIXES)):
        return [f"credential file is tracked in {relative}"]
    if name.startswith(".env.") and name != ".env.example":
        return [f"credential file is tracked in {relative}"]
    return []


def _text_from_bytes(data: bytes) -> str | None:
    """Return UTF-8 text, or None for binary content such as branding images."""
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def _manifest_failures(root: Path) -> list[str]:
    failures: list[str] = []
    manifest = root / "plugin.yaml"
    if not manifest.is_file():
        return ["missing root plugin.yaml"]
    text = manifest.read_text(encoding="utf-8", errors="replace")
    version = _yaml_scalar(text, "version")
    if version is None:
        failures.append("root manifest has no version")
    for required in REQUIRED_MANIFEST_TEXT:
        if required not in text:
            failures.append(f"root manifest missing {required!r}")

    readme = root / "README.md"
    if version is not None and readme.is_file():
        readme_text = readme.read_text(encoding="utf-8", errors="replace")
        readme_version = re.search(r"(?m)^Version:\s*([^\s]+)\s*$", readme_text)
        if readme_version is None or readme_version.group(1) != version:
            failures.append("README version does not match plugin.yaml")

    skill = root / "hermes_switchyard" / "skills" / "hermes-switchyard-operations" / "SKILL.md"
    if version is not None and skill.is_file():
        skill_version = _yaml_scalar(skill.read_text(encoding="utf-8", errors="replace"), "version")
        if skill_version != version:
            failures.append("skill version does not match plugin.yaml")
    return failures


def _fixture_failures(root: Path) -> list[str]:
    fixture = root / "evaluation" / "fixtures.json"
    if not fixture.is_file():
        return ["missing evaluation/fixtures.json"]
    try:
        book = json.loads(fixture.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ["evaluation fixtures are not valid JSON"]
    failures: list[str] = []
    if not isinstance(book, dict) or book.get("public_synthetic") is not True:
        failures.append("evaluation fixture book is not marked public_synthetic")
        return failures
    fixtures = book.get("fixtures")
    if not isinstance(fixtures, list) or not fixtures:
        failures.append("evaluation fixture book has no fixtures")
        return failures
    for index, item in enumerate(fixtures):
        if not isinstance(item, dict) or item.get("public_synthetic") is not True:
            failures.append(f"evaluation fixture {index} is not marked public_synthetic")
            continue
        expected = item.get("expected")
        if not isinstance(expected, dict):
            failures.append(f"evaluation fixture {index} has no expected result")
            continue
        network_expected = item.get("kind") == "skill" or expected.get("network") is True
        response_present = item.get("synthetic_response") is not None
        if network_expected != response_present:
            failures.append(f"evaluation fixture {index} network fixture boundary is inconsistent")
    return failures


def _history_commits(root: Path) -> list[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-list", "--all"],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("git history listing failed") from exc
    if result.returncode != 0:
        raise RuntimeError("git history listing failed")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _history_failures(root: Path, text_suffixes: set[str]) -> list[str]:
    failures: list[str] = []
    for commit in _history_commits(root):
        try:
            tree = subprocess.run(
                ["git", "-C", str(root), "ls-tree", "-r", "-z", "--name-only", commit],
                capture_output=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError("git history tree listing failed") from exc
        if tree.returncode != 0:
            raise RuntimeError("git history tree listing failed")
        for raw_relative in tree.stdout.split(b"\0"):
            if not raw_relative:
                continue
            try:
                relative = raw_relative.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise RuntimeError("git history tree contains a non-UTF-8 path") from exc
            if relative == SELF_RELATIVE:
                continue
            failures.extend(_tracked_path_failures(relative))
            failures.extend(_credential_path_failures(relative))
            try:
                shown = subprocess.run(
                    ["git", "-C", str(root), "show", f"{commit}:{relative}"],
                    capture_output=True,
                    check=False,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise RuntimeError("git history file read failed") from exc
            if shown.returncode != 0:
                raise RuntimeError(f"git history blob read failed: {relative}")
            text = _text_from_bytes(shown.stdout)
            if text is None:
                continue
            for failure in _content_failures(relative, text):
                failures.append(f"history {commit[:12]}: {failure}")
    return failures


def run_checks(root: Path, include_history: bool = False) -> list[str]:
    root = Path(root).resolve()
    failures = _manifest_failures(root)
    failures.extend(_fixture_failures(root))
    tracked = _tracked_files(root)
    for path in tracked:
        relative = _relative(root, path)
        failures.extend(_tracked_path_failures(relative))
        failures.extend(_credential_path_failures(relative))
        if relative != SELF_RELATIVE:
            text = _text_from_bytes(path.read_bytes())
        else:
            text = None
        if text is not None:
            failures.extend(_content_failures(relative, text))
    if include_history:
        failures.extend(_history_failures(root, TEXT_SUFFIXES))
    return failures


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    parser.add_argument("--history", action="store_true", help="scan committed history as well as the current tracked tree")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        failures = run_checks(args.root, include_history=args.history)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    print("portability and public-hygiene checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
