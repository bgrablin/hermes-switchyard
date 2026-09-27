#!/usr/bin/env python3
"""Measure the Jev request bytes per call for arm C, before and after.

For every fixture case, run the real ``jev_research_navigator`` handler with a
fake Jev client and record the UTF-8 byte size of the compact JSON ``state``
and ``questions`` it would send. Cases that make no Jev call record 0.

Usage:
  request_bytes.py --before-ref <git ref> --output request_bytes.json

The "before" tree is extracted with ``git archive`` so each side runs in its
own interpreter with its own package.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def _size(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def child() -> None:
    sys.path.insert(0, str(HERE))
    import evaluate as ev
    import hermes_switchyard

    book, _ = ev.load_book()
    rows = []
    for case in book["cases"]:
        captured: dict = {}

        class Capture(ev.FakeJev):
            def decide(self, state, questions, **kwargs):
                captured["state"], captured["questions"] = state, questions
                return super().decide(state, questions, **kwargs)

        context = ev.Context({"research_navigator_enabled": True, "jev_provider": "openrouter"})
        with mock.patch.object(hermes_switchyard, "_secret", return_value="fixture-key"), mock.patch.object(
            hermes_switchyard, "DecisionClient", return_value=Capture(case)
        ):
            hermes_switchyard.register(context)
            context.tools["jev_research_navigator"](
                {"goal": case["goal"], "claims": copy.deepcopy(case["claims"]), "windows": copy.deepcopy(case["windows"])}
            )
        state_bytes = _size(captured["state"]) if captured else 0
        question_bytes = _size(captured["questions"]) if captured else 0
        rows.append({"case": case["id"], "split": case["split"], "state_bytes": state_bytes, "question_bytes": question_bytes, "total_bytes": state_bytes + question_bytes})
    json.dump(rows, sys.stdout)


def measure(package_root: Path) -> list[dict]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(package_root), str(HERE)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else []))
    out = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child"], capture_output=True, text=True, env=env, check=True)
    return json.loads(out.stdout)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--before-ref", help="git ref for the 'before' package")
    parser.add_argument("--output", help="write JSON here")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        child()
        return 0
    if not args.before_ref:
        parser.error("--before-ref is required")
    before_sha = subprocess.run(["git", "-C", str(ROOT), "rev-parse", f"{args.before_ref}^{{commit}}"], capture_output=True, text=True, check=True).stdout.strip()
    with tempfile.TemporaryDirectory(prefix="rn-bytes-") as tmp:
        archive = subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", before_sha, "hermes_switchyard", "plugin.yaml"], capture_output=True, check=True).stdout
        tar_path = Path(tmp) / "src.tar"
        tar_path.write_bytes(archive)
        with tarfile.open(tar_path) as tar:
            tar.extractall(Path(tmp) / "src", filter="data")
        before = measure(Path(tmp) / "src")
    after = measure(ROOT)
    rows = []
    for old, new in zip(before, after):
        assert old["case"] == new["case"]
        rows.append({"case": old["case"], "split": old["split"], "before_bytes": old["total_bytes"], "after_bytes": new["total_bytes"],
                     "before_question_bytes": old["question_bytes"], "after_question_bytes": new["question_bytes"]})
    calling = [r for r in rows if r["before_bytes"] and r["after_bytes"]]
    report = {
        "schema_version": 1,
        "before_ref": before_sha,
        "measure": "UTF-8 bytes of compact JSON state + questions sent to Jev, per call (one call per case)",
        "cases_with_a_call": len(calling),
        "total_before_bytes": sum(r["before_bytes"] for r in calling),
        "total_after_bytes": sum(r["after_bytes"] for r in calling),
        "rows": rows,
    }
    report["reduction"] = round(1 - report["total_after_bytes"] / report["total_before_bytes"], 4) if report["total_before_bytes"] else None
    text = json.dumps(report, indent=1, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    print(json.dumps({k: report[k] for k in ("before_ref", "cases_with_a_call", "total_before_bytes", "total_after_bytes", "reduction")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
