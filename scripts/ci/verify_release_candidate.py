#!/usr/bin/env python3
"""Verify a source-bound archive, its packaged links, and the native loader."""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.build_release import ReleaseError, verify_archive
from scripts.ci.check_native_hermes import NativeCompatibilityError, inspect_native_plugin


class ReleaseCandidateError(RuntimeError):
    """Raised when a release candidate is not source-verified and loadable."""


def verify_candidate(
    *,
    archive: Path,
    source_root: Path,
    source_sha: str,
    upstream_root: Path,
    upstream_sha: str,
) -> dict:
    try:
        archive_result = verify_archive(
            archive,
            source_root=source_root,
            expected_source_sha=source_sha,
        )
    except ReleaseError as exc:
        raise ReleaseCandidateError("source-bound archive verification failed") from exc
    if archive_result.get("verification") != "source-verified":
        raise ReleaseCandidateError("archive did not receive source-verified status")

    with tempfile.TemporaryDirectory(prefix="switchyard-release-extract-") as scratch:
        extracted = Path(scratch)
        try:
            with zipfile.ZipFile(archive) as opened:
                opened.extractall(extracted)
        except (OSError, zipfile.BadZipFile) as exc:
            raise ReleaseCandidateError("candidate archive could not be extracted") from exc
        required_files = (
            "README.md",
            "docs/SETUP.md",
            "docs/assets/hermes-switchyard-branding.png",
            "plugin.yaml",
            "__init__.py",
        )
        missing = [relative for relative in required_files if not (extracted / relative).is_file()]
        if missing:
            raise ReleaseCandidateError("candidate archive is missing packaged release assets")
        readme = (extracted / "README.md").read_text(encoding="utf-8")
        for link in ("docs/SETUP.md", "docs/assets/hermes-switchyard-branding.png"):
            if f"]({link})" not in readme:
                raise ReleaseCandidateError(f"README does not reference packaged asset {link}")
        try:
            native = inspect_native_plugin(
                extracted,
                upstream_root=upstream_root,
                upstream_sha=upstream_sha,
            )
        except NativeCompatibilityError as exc:
            raise ReleaseCandidateError("extracted candidate failed the native Hermes loader") from exc

    return {
        "ok": True,
        "archive": archive.name,
        "source_sha": source_sha,
        "verification": archive_result["verification"],
        "members": archive_result["members"],
        "plugin": archive_result["plugin"],
        "version": archive_result["version"],
        "readme_links_verified": ["docs/SETUP.md", "docs/assets/hermes-switchyard-branding.png"],
        "native_loader": {
            "plugin": native["plugin"],
            "registered_tools": native["registered_tools"],
            "hermes_source_sha": native["hermes_source_sha"],
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--upstream-root", type=Path, required=True)
    parser.add_argument("--upstream-sha", required=True)
    parser.add_argument("--report", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        report = verify_candidate(
            archive=args.archive,
            source_root=args.source_root,
            source_sha=args.source_sha,
            upstream_root=args.upstream_root,
            upstream_sha=args.upstream_sha,
        )
    except ReleaseCandidateError as exc:
        report = {"ok": False, "error": str(exc)}
        print(f"ERROR: {exc}", file=sys.stderr)
        exit_code = 1
    else:
        print(json.dumps(report, indent=2, sort_keys=True))
        exit_code = 0
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
