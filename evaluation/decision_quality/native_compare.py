"""Frozen, interleaved native control: disabled, release, main, confidence gate."""

import argparse
import concurrent.futures
import hashlib
import json
import os
import queue
import random
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parent


class Worker:
    def __init__(self, arm, out, source, *, hermes_root=None):
        prefix = json.loads(
            subprocess.check_output(["hermes", "--print-runtime-command"], text=True)
        )
        old = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
        assert old in prefix[-1]
        script = str(ROOT / "native_worker.py")
        prefix[-1] = prefix[-1].replace(
            old,
            f"sys.argv = [{script!r}] + sys.argv[1:]; runpy.run_path({script!r}, run_name='__main__')",
        )
        env = os.environ.copy()
        env["HERMES_DISABLE_LAZY_INSTALLS"] = "1"
        extra_args = (
            ["--hermes-root", str(hermes_root.resolve())]
            if hermes_root is not None
            else []
        )
        self.proc = subprocess.Popen(
            prefix
            + [
                "--arm",
                arm,
                "--home",
                str(out / ("home-" + arm)),
                "--source",
                str(source),
            ]
            + extra_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self.q = queue.Queue()

        def read():
            for line in self.proc.stdout:
                if line.startswith(("READY ", "ROW ", "WORKER_ERROR ")):
                    self.q.put(line.rstrip())
            self.q.put("WORKER_EXIT")

        threading.Thread(target=read, daemon=True).start()
        line = self.q.get(timeout=90)
        if not line.startswith("READY "):
            raise RuntimeError(line)
        self.ready = json.loads(line[6:])

    def ask(self, job):
        self.proc.stdin.write(json.dumps(job) + "\n")
        self.proc.stdin.flush()
        line = self.q.get(timeout=120)
        if not line.startswith("ROW "):
            raise RuntimeError(line)
        return json.loads(line[4:])

    def close(self):
        try:
            self.proc.stdin.write('{"stop":true}\n')
            self.proc.stdin.flush()
            self.proc.wait(timeout=10)
        except Exception:
            self.proc.terminate()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    out = a.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    repo = ROOT.parents[1]
    revisions = {}
    for name, ref in [
        ("release", "v0.5.6"),
        ("main", "afee8afdec3967201ff6c24d29dc892e6311e6a4"),
    ]:
        revisions[name] = subprocess.check_output(
            ["git", "rev-parse", ref], cwd=repo, text=True
        ).strip()
        archive = out / (name + ".tar")
        with archive.open("wb") as handle:
            subprocess.run(["git", "archive", ref], cwd=repo, stdout=handle, check=True)
        dest = out / name
        dest.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
        archive.unlink()
    shutil.copytree(out / "main", out / "candidate")
    adapter = out / "candidate/hermes_switchyard/reasoning_effort_adapter.py"
    text = adapter.read_text()
    old = "        elif lowered and stuck:"
    new = """        elif lowered and (confidence < 0.8 or probabilities[selected] < 0.8):
            effort = floor
            reason = "kept_requested_low_confidence"
        elif lowered and stuck:"""
    assert text.count(old) == 1
    adapter.write_text(text.replace(old, new))
    cases = json.loads((ROOT / "cases.json").read_text())["effort"]
    jobs = [(repeat, c) for repeat in range(2) for c in cases]
    random.Random(97842).shuffle(jobs)
    freeze = {
        "model": "gpt-6-sol",
        "provider": "openai-codex",
        "requested_effort": "high",
        "jev": "typesafe/jev-1.13-20260917",
        "revisions": revisions,
        "implementation_trees": {
            arm: subprocess.check_output(
                ["git", "rev-parse", revision + "^{tree}"], cwd=repo, text=True
            ).strip()
            for arm, revision in revisions.items()
        },
        "candidate_patch": new,
        "repeats": 2,
        "max_concurrent_main_requests": 2,
        "acceptance": "Correctness must improve on main with no new errors; a cost or latency change alone is not a winner. Preserve raw failures. No tools or memory.",
        "files": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), ROOT / "native_worker.py", ROOT / "cases.json"]
        },
    }
    (out / "freeze.json").write_text(json.dumps(freeze, indent=2))
    workers = {}
    try:
        for arm in ["off", "release", "main", "candidate"]:
            workers[arm] = Worker(arm, out, out / ("main" if arm == "off" else arm))
        rng = random.Random(749)
        with (
            (out / "raw.jsonl").open("x") as handle,
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
        ):
            for repeat, case in jobs:
                arms = list(workers)
                rng.shuffle(arms)
                for offset in range(0, len(arms), 2):
                    batch = arms[offset : offset + 2]
                    job = {
                        "id": case["id"] + "-" + str(repeat),
                        "prompt": case["prompt"],
                    }
                    futures = [pool.submit(workers[arm].ask, job) for arm in batch]
                    for arm, future in zip(batch, futures):
                        row = future.result()
                        row["expected"] = case["expected"]
                        row["correct"] = (row.get("final") or "").strip().strip(
                            "`"
                        ).strip().casefold() == case["expected"].casefold()
                        handle.write(json.dumps(row) + "\n")
                        handle.flush()
                        print(
                            json.dumps(
                                {
                                    k: row[k]
                                    for k in ["id", "arm", "correct", "wall_ms", "wire"]
                                }
                            ),
                            flush=True,
                        )
    finally:
        for worker in workers.values():
            worker.close()
    print("FINISHED " + str(out), flush=True)


if __name__ == "__main__":
    main()
