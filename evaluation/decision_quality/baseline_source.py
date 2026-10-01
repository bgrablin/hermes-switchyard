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


def source_identity():
    """The archive loader imports all plugin modules from this immutable tree."""
    repo = Path(__file__).resolve().parents[2]
    tree = subprocess.check_output(
        ["git", "rev-parse", BASE_SHA + "^{tree}"], cwd=repo, text=True
    ).strip()
    return {"revision": BASE_SHA, "tree": tree}
