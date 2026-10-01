"""Fail closed when published observations, fixtures, or frozen provenance drift."""

import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess

BASE_SHA = "afee8afdec3967201ff6c24d29dc892e6311e6a4"
ARCHIVE_HEADER = (
    "# Archived pre-call source; not a runnable entry point.\n"
    "# ruff: noqa: E402,E701,E702,F401 -- preserve historical bytes below\n"
)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, reason):
    if not condition:
        raise ValueError("evaluation provenance refused: " + reason)


def validate(root: Path):
    manifest = json.loads((root / "provenance.json").read_text())
    raw = (root / "observations.json").read_bytes()
    require(
        digest(raw) == manifest["observations_sha256"], "observations hash mismatch"
    )
    book = json.loads(raw)
    require(
        set(book) == {"screen", "workflow", "native", "freezes"},
        "unexpected observation sections",
    )
    expected_files = {
        "screen-run": {"screen.py", "cases.json"},
        "workflow-run": {
            "workflow_compare.py",
            "workflow-cases.json",
            "cases.json",
            "record_triage.py",
        },
        "native-run": {"native_compare.py", "native_worker.py", "cases.json"},
    }
    require(
        set(manifest["frozen_sources"]) == set.union(*expected_files.values()),
        "incomplete frozen source set",
    )
    require(
        set(manifest["arm_sources"]) == {"main", "release", "candidate"},
        "incomplete source arm set",
    )
    frozen = {}
    for name, entry in manifest["frozen_sources"].items():
        path = root / entry["path"]
        require(
            path.resolve().is_relative_to(root.resolve()),
            "snapshot outside evaluation directory",
        )
        content = path.read_bytes()
        if name.endswith(".py"):
            prefix = ARCHIVE_HEADER.encode()
            require(content.startswith(prefix), "invalid archive header: " + name)
            content = content[len(prefix) :]
        require(digest(content) == entry["sha256"], "frozen source mismatch: " + name)
        frozen[name] = entry["sha256"]
    for name, freeze in book["freezes"].items():
        require(name in {"screen-run", "workflow-run", "native-run"}, "unknown freeze")
        require(
            set(freeze["files"]) == expected_files[name], "incomplete frozen file set"
        )
        for filename, sha in freeze["files"].items():
            require(
                frozen.get(filename) == sha, "pre-call source mismatch: " + filename
            )
    require(
        set(book["freezes"]) == {"screen-run", "workflow-run", "native-run"},
        "missing freeze",
    )
    native_freeze = book["freezes"]["native-run"]
    require(
        set(native_freeze["revisions"]) == {"main", "release"},
        "incomplete revision set",
    )
    require(native_freeze["revisions"]["main"] == BASE_SHA, "wrong main revision")
    require(
        native_freeze["model"] == "gpt-6-sol"
        and native_freeze["provider"] == "openai-codex",
        "wrong native provider/model",
    )
    require(
        book["freezes"]["screen-run"]["model"] == "typesafe/jev-1.13-20260917",
        "wrong screening model",
    )
    require(
        book["freezes"]["workflow-run"]["model"] == "typesafe/jev-1.13-20260917",
        "wrong workflow model",
    )
    repo = root.parents[1]
    for arm, revision in native_freeze["revisions"].items():
        require(arm in {"main", "release"}, "unknown source arm")
        tree = subprocess.check_output(
            ["git", "rev-parse", revision + "^{tree}"], cwd=repo, text=True
        ).strip()
        require(
            tree == manifest["arm_sources"][arm]["tree"], "source tree mismatch: " + arm
        )
        require(
            revision == manifest["arm_sources"][arm]["revision"],
            "source revision mismatch: " + arm,
        )
    baseline = subprocess.check_output(
        ["git", "show", BASE_SHA + ":hermes_switchyard/record_triage.py"], cwd=repo
    )
    require(
        digest(baseline) == frozen["record_triage.py"],
        "workflow source differs from pinned main",
    )
    require(
        digest(native_freeze["candidate_patch"].encode())
        == manifest["arm_sources"]["candidate"]["patch_sha256"],
        "candidate patch mismatch",
    )
    require(
        manifest["arm_sources"]["candidate"]["base"] == BASE_SHA, "wrong candidate base"
    )
    # The changed production rubric must remain exactly the independently evaluated criteria.
    cases = json.loads((root / "cases.json").read_text())
    workflow = json.loads((root / "workflow-cases.json").read_text())
    for name in ["cases.json", "workflow-cases.json"]:
        require(
            digest((root / name).read_bytes()) == frozen[name], "fixture drift: " + name
        )
    module = ast.parse((repo / "hermes_switchyard/record_triage.py").read_text())
    literals = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in module.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in {"SEVERITY_LEVELS", "SEVERITY_CRITERIA"}
    }
    require(
        literals["SEVERITY_LEVELS"] == ("cosmetic", "minor", "major", "critical"),
        "severity index drift",
    )
    require(
        [literals["SEVERITY_CRITERIA"][level] for level in literals["SEVERITY_LEVELS"]]
        == cases["severity_rubric"],
        "production rubric differs from evaluated candidate",
    )
    expected_screen = []
    for repeat in range(2):
        for offset in range(0, 32, 4):
            expected_screen += [
                ("rubrics", arm, f"batch-{offset}-{repeat}")
                for arm in ["existing", "descriptive"]
            ]
        expected_screen += [
            ("effort", "existing", f"{case['id']}-{repeat}") for case in cases["effort"]
        ]
        expected_screen += [("audit", "batched", str(repeat))]
        expected_screen += [
            ("serial_triage", "descriptive", f"{case['id']}-{repeat}")
            for case in cases["records"][:4]
        ]
    require(
        Counter((r["family"], r["arm"], r["id"]) for r in book["screen"])
        == Counter(expected_screen),
        "missing, duplicate, or extra screen rows",
    )
    expected_workflow = {
        (arm, f"{arm}-{offset}-{repeat}")
        for arm in ["main", "candidate"]
        for offset in range(0, 16, 4)
        for repeat in range(2)
    }
    require(
        Counter((r["arm"], r["id"]) for r in book["workflow"])
        == Counter(expected_workflow),
        "missing, duplicate, or extra workflow rows",
    )
    expected_native = {
        (arm, f"{case['id']}-{repeat}")
        for arm in ["off", "release", "main", "candidate"]
        for case in cases["effort"]
        for repeat in range(2)
    }
    require(
        Counter((r["arm"], r["id"]) for r in book["native"])
        == Counter(expected_native),
        "missing, duplicate, or extra native rows",
    )
    answers = {c["id"]: c["expected"] for c in cases["effort"]}
    for row in book["native"]:
        require(
            row["expected"] == answers[row["id"].rsplit("-", 1)[0]],
            "native label drift",
        )
        require(
            all(w["model"] == "gpt-6-sol" for w in row["wire"]), "native model drift"
        )
    for row in book["workflow"]:
        offset = int(row["id"].split("-")[1])
        ids = [r["id"] for r in workflow["records"][offset : offset + 4]]
        require(
            row["expected"] == {rid: workflow["expected"][rid] for rid in ids},
            "workflow label drift",
        )
        require(
            [r["id"] for r in row["result"]["records"]] == ids, "workflow record drift"
        )
    for row in book["screen"]:
        if row["family"] == "rubrics":
            offset = int(row["id"].split("-")[1])
            ids = [r["id"] for r in cases["records"][offset : offset + 4]]
            require(
                row["expected"] == {rid: cases["labels"][rid] for rid in ids},
                "screen label drift",
            )
        elif row["family"] == "serial_triage":
            rid = row["id"].rsplit("-", 1)[0]
            require(
                row["expected"] == {rid: cases["labels"][rid]}, "serial label drift"
            )
        elif row["family"] == "audit":
            require(
                row["expected"] == {s["id"]: s["expected"] for s in cases["audit"]},
                "audit label drift",
            )
    return book
