#!/usr/bin/env python3
"""Run the Hermes plugin install scanner on the exact tree a user installs.

`hermes plugins install` clones the repository and runs `tools.plugin_guard.scan_plugin`
on the clone. A `caution` verdict blocks a normal install, and a `dangerous` verdict
blocks it even with `--force`. This check scans a `git archive` export of HEAD (the
same files as a clone) with the pinned Hermes scanner and fails unless the verdict is
`safe`. It does not change or relax any scanner rule.
"""
from __future__ import annotations

import argparse
import collections
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path


def _export_head(repo: Path, dest: Path) -> str:
    sha = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--verify", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    archive = dest / "head.tar"
    subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", "-o", str(archive), sha],
        check=True,
    )
    tree = dest / "hermes-switchyard"
    tree.mkdir()
    with tarfile.open(archive) as handle:
        handle.extractall(tree, filter="data")
    archive.unlink()
    return sha


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-root", type=Path, default=Path.cwd())
    parser.add_argument("--upstream-root", type=Path, help="Hermes source tree that provides tools.plugin_guard")
    parser.add_argument("--report", type=Path, help="write a JSON report here")
    args = parser.parse_args(argv)

    if args.upstream_root:
        sys.path.insert(0, str(args.upstream_root.resolve()))
    try:
        from tools.plugin_guard import PLUGIN_SCANNER_VERSION, scan_plugin
    except ImportError as exc:  # pragma: no cover - environment error
        print(f"ERROR: cannot import the Hermes plugin scanner: {exc}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="switchyard-install-scan-") as tmp:
        sha = _export_head(args.plugin_root.resolve(), Path(tmp))
        result = scan_plugin(Path(tmp) / "hermes-switchyard", source="bgrablin/hermes-switchyard")

    blocking = [f for f in result.findings if f.severity in {"high", "critical"}]
    report = {
        "source_sha": sha,
        "scanner_version": PLUGIN_SCANNER_VERSION,
        "verdict": result.verdict,
        "finding_count": len(result.findings),
        "severity_counts": dict(collections.Counter(f.severity for f in result.findings)),
        "blocking_findings": [
            {"severity": f.severity, "pattern_id": f.pattern_id, "category": f.category,
             "file": f.file, "line": f.line, "description": f.description}
            for f in blocking
        ],
    }
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"install scan: {result.verdict} ({len(result.findings)} findings, scanner {PLUGIN_SCANNER_VERSION}) at {sha}")
    for item in report["blocking_findings"]:
        print(f"  {item['severity'].upper():8} {item['pattern_id']:28} {item['file']}:{item['line']}")
    if result.verdict != "safe":
        print("ERROR: a user install would be blocked. Fix the findings above; do not suppress them.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
