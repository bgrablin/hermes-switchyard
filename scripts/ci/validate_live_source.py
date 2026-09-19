#!/usr/bin/env python3
"""Fail-closed validation for manually authorized live-contract sources."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

TRUSTED_REPOSITORY = "bgrablin/hermes-switchyard"
TRUSTED_REFS = frozenset({"main", "bgrablin/release-packaging"})
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


class TrustedSourceError(ValueError):
    """Raised when a manual live-contract source is outside the allowlist."""


def validate_trusted_source(
    *,
    repository: str,
    ref: str,
    requested_sha: str,
    checked_out_sha: str,
) -> dict[str, str]:
    """Validate the exact repository, ref, and source SHA selected by an operator."""
    if repository != TRUSTED_REPOSITORY:
        raise TrustedSourceError("live contract is allowed only for the canonical repository")
    if ref not in TRUSTED_REFS:
        raise TrustedSourceError("ref is not in the explicit live-contract allowlist")
    if not _SHA_RE.fullmatch(requested_sha):
        raise TrustedSourceError("source SHA must be exact lowercase hexadecimal")
    if checked_out_sha != requested_sha:
        raise TrustedSourceError("checked-out source does not match the requested exact SHA")
    return {"repository": repository, "ref": ref, "source_sha": requested_sha}


def _checked_out_sha(repository: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--verify", "HEAD^{commit}"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TrustedSourceError("could not read the checked-out source SHA") from exc
    return result.stdout.strip()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--source-sha", required=True)
    parser.add_argument("--checkout", type=Path, default=Path.cwd())
    parser.add_argument(
        "--selector-only",
        action="store_true",
        help="validate the allowlisted selector before the conditional checkout",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        checked_out_sha = args.source_sha if args.selector_only else _checked_out_sha(args.checkout)
        validate_trusted_source(
            repository=args.repository,
            ref=args.ref,
            requested_sha=args.source_sha,
            checked_out_sha=checked_out_sha,
        )
    except TrustedSourceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("trusted live-contract source validated")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
