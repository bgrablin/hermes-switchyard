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
    validate_implementations(
        root,
        manifest["run_implementations"],
        {"screen-run", "workflow-run", "native-run"},
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
    frozen_module = ast.parse((root / "frozen/record_triage.py").read_text())
    grading_names = {
        "_parse_answers",
        "_bounded_distribution",
        "_unit",
        "_decision",
        "_consume",
        "_priority",
        "_routable",
        "_canonical_bytes",
    }
    current_functions = {
        n.name: ast.dump(n)
        for n in module.body
        if isinstance(n, ast.FunctionDef) and n.name in grading_names
    }
    frozen_functions = {
        n.name: ast.dump(n)
        for n in frozen_module.body
        if isinstance(n, ast.FunctionDef) and n.name in grading_names
    }
    require(
        set(current_functions) == grading_names
        and current_functions == frozen_functions,
        "decision replay implementation drift",
    )
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
        == json.loads((root / "confirmation-cases.json").read_text())[
            "severity_rubric"
        ],
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
    for row in book["workflow"]:
        reconcile_workflow(row, workflow["records"])
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
    book["confirmation"] = validate_confirmation(root)
    return book


def validate_confirmation(root):
    """Check the reviewed rubric's new confirmation and retain the imperfect pilot."""
    manifest = json.loads((root / "confirmation-provenance.json").read_text())
    validate_implementations(
        root, manifest["run_implementations"], {"pilot", "confirmation"}
    )
    raw = (root / "confirmation-observations.json").read_bytes()
    require(
        digest(raw) == manifest["observations_sha256"], "confirmation observation drift"
    )
    require(
        digest((root / "followup-pilot.json").read_bytes()) == manifest["pilot_sha256"],
        "pilot observation drift",
    )
    require(manifest["main_revision"] == BASE_SHA, "confirmation source revision drift")
    book = json.loads(raw)
    require(set(book) == {"freeze", "rows"}, "unexpected confirmation sections")
    names = {
        "confirmation_compare.py",
        "confirmation-cases.json",
        "cases.json",
        "record_triage.py",
    }
    require(
        set(book["freeze"]["files"]) == names and set(manifest["files"]) == names,
        "incomplete confirmation source set",
    )
    require(
        book["freeze"]["model"] == "typesafe/jev-1.13-20260917",
        "confirmation model drift",
    )
    require(
        book["freeze"]["repeats"] == 2
        and book["freeze"]["thresholds"] == {"disposition": 0.8, "severity": 0.8},
        "confirmation policy drift",
    )
    for name in names:
        data = (root / "frozen" / name).read_bytes()
        if name.endswith(".py"):
            require(
                data.startswith(ARCHIVE_HEADER.encode()),
                "confirmation archive header drift",
            )
            data = data[len(ARCHIVE_HEADER.encode()) :]
        require(
            digest(data) == manifest["files"][name] == book["freeze"]["files"][name],
            "confirmation source drift: " + name,
        )
    cases = json.loads((root / "confirmation-cases.json").read_text())
    require(
        digest((root / "confirmation-cases.json").read_bytes())
        == manifest["files"]["confirmation-cases.json"],
        "confirmation fixture drift",
    )
    require(
        len(cases["records"]) == 24 and len(cases["expected"]) == 24,
        "confirmation case count drift",
    )
    expected_rows = {
        (arm, f"{arm}-{offset}-{repeat}")
        for arm in ["main", "candidate"]
        for offset in range(0, 24, 4)
        for repeat in range(2)
    }
    require(
        Counter((r["arm"], r["id"]) for r in book["rows"]) == Counter(expected_rows),
        "missing, duplicate, or extra confirmation rows",
    )
    for row in book["rows"]:
        offset = int(row["id"].split("-")[1])
        ids = [r["id"] for r in cases["records"][offset : offset + 4]]
        require(
            row["expected"] == {rid: cases["expected"][rid] for rid in ids},
            "confirmation label drift",
        )
        require(
            [r["id"] for r in row["result"]["records"]] == ids,
            "confirmation record drift",
        )
    for row in book["rows"]:
        reconcile_workflow(row, cases["records"])
    return book["rows"]


def validate_implementations(root, bindings, expected_runs):
    """Bind every run to the complete plugin implementation, not selected files."""
    require(set(bindings) == expected_runs, "incomplete run implementation set")
    for run, identity in bindings.items():
        require(
            set(identity) == {"revision", "tree"},
            "incomplete implementation identity: " + run,
        )
        require(
            identity["revision"] == BASE_SHA, "implementation revision drift: " + run
        )
        tree = subprocess.check_output(
            ["git", "rev-parse", identity["revision"] + "^{tree}"],
            cwd=root.parents[1],
            text=True,
        ).strip()
        require(tree == identity["tree"], "implementation tree drift: " + run)


def reconcile_workflow(row, records):
    """Re-derive accepted decisions and consumer payload hashes from provider answers."""
    from hermes_switchyard.record_triage import (
        _parse_answers,
        _consume,
        _canonical_bytes,
    )

    require(len(row["calls"]) == 1, "unexpected workflow provider call count")
    call = row["calls"][0]
    require(
        call["model"] == "typesafe/jev-1.13-20260917", "workflow provider model drift"
    )
    require(call["request_count"] == 1, "workflow provider request count drift")
    inputs = {record["id"]: record for record in records}
    entries = row["result"]["records"]
    names = {
        prefix + entry["id"]
        for entry in entries
        for prefix in ["disposition__", "severity__"]
    }
    require(set(call["answers"]) == names, "workflow provider answer set drift")
    for entry in entries:
        rid = entry["id"]
        decision = _parse_answers(rid, call["answers"], 0.8, 0.8)
        require(
            decision == entry["decision"],
            "workflow decision disagrees with provider answers",
        )
        consumer, payload = _consume(inputs[rid], decision)
        if payload is not None:
            consumer["file"] = f"actions/{rid}.json"
            consumer["sha256"] = digest(_canonical_bytes(payload))
        require(
            consumer == entry["consumer"],
            "workflow consumer disagrees with provider answers",
        )
    accounting = row["result"]["accounting"]
    require(
        accounting["requests_completed"] == call["request_count"],
        "workflow accounting request drift",
    )
    require(
        accounting["total_cost"] == call["usage"]["cost"],
        "workflow accounting cost drift",
    )
    require(
        row["verification"]
        == {"verified": True, "errors": [], "checked_records": len(entries)},
        "workflow artifact verification drift",
    )
