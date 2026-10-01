# Archived pre-call source; not a runnable entry point.
# ruff: noqa: E402,E701,E702,F401 -- preserve historical bytes below
"""Select the frozen baseline even when evaluating from the candidate checkout."""

import io
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

BASE_SHA = "afee8afdec3967201ff6c24d29dc892e6311e6a4"
_SNAPSHOT = None


def activate_baseline():
    global _SNAPSHOT
    if _SNAPSHOT is None:
        repo = Path(__file__).resolve().parents[2]
        archive = subprocess.check_output(
            ["git", "archive", BASE_SHA, "hermes_switchyard"], cwd=repo
        )
        _SNAPSHOT = tempfile.TemporaryDirectory(prefix="switchyard-baseline-")
        with tarfile.open(fileobj=io.BytesIO(archive)) as handle:
            handle.extractall(_SNAPSHOT.name, filter="data")
        sys.path.insert(0, _SNAPSHOT.name)
    return Path(_SNAPSHOT.name)
