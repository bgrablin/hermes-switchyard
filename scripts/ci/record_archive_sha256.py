#!/usr/bin/env python3
"""Record the SHA-256 of the verified release archive for the release receipt."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--source-sha", default=os.environ.get("SWITCHYARD_SOURCE_SHA", ""))
    args = parser.parse_args(argv)
    if not args.source_sha:
        parser.error("--source-sha or SWITCHYARD_SOURCE_SHA is required")
    digest = hashlib.sha256(args.archive.read_bytes()).hexdigest()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps({"source_sha": args.source_sha, "archive_sha256": digest}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"{digest}  {args.archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
