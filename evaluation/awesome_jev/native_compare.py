"""Native randomized paired efficiency confirmation, including all fallback cases."""

import hashlib
import datetime
import json
import os
import queue
import random
import shutil
import subprocess
import tarfile
import threading
from pathlib import Path
from holdout_cases import cases

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parents[1]
OUT = Path(os.environ.get("SWITCHYARD_EVAL_OUTPUT", str(Path.home() / ".hermes-eval/awesome-jev-native-v2")))
TEMPLATE = Path(os.environ.get("SWITCHYARD_EVAL_TEMPLATE", str(Path.home() / ".hermes-eval/switchyard-abc")))
HERMES_SOURCE = Path(os.environ.get("HERMES_SOURCE", str(Path.home() / ".hermes/hermes-agent")))


def runtime_fingerprint():
    runtime = HERMES_SOURCE
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=runtime).decode().split("\0")
    return {
        name: hashlib.sha256((runtime / name).read_bytes()).hexdigest()
        for name in names if name and (runtime / name).is_file()
    }


class Worker:
    def __init__(self, arm, source):
        prefix = json.loads(
            subprocess.check_output(["hermes", "--print-runtime-command"], text=True)
        )
        old = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
        assert old in prefix[-1]
        script = str(ROOT / "native_worker.py")
        prefix[-1] = prefix[-1].replace(
            old,
            f"sys.argv=[{script!r}]+sys.argv[1:];runpy.run_path({script!r},run_name='__main__')",
        )
        env = os.environ.copy()
        env.update(
            HERMES_HOME=str(TEMPLATE),
            HERMES_DISABLE_LAZY_INSTALLS="1",
            SWITCHYARD_EVAL_HOME=str(OUT / ("home-" + arm)),
        )
        self.proc = subprocess.Popen(
            prefix + ["--arm", arm, "--name", arm, "--source", str(source)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=env,
        )
        self.q = queue.Queue()

        def read():
            with (OUT / (arm + ".log")).open("x") as log:
                for line in self.proc.stdout:
                    log.write(line)
                    log.flush()
                    if line.startswith(("READY ", "ROW ", "WORKER_ERROR ")):
                        self.q.put(line.rstrip())
            self.q.put("WORKER_EXIT")

        threading.Thread(target=read, daemon=True).start()
        line = self.q.get(timeout=90)
        if not line.startswith("READY "):
            raise RuntimeError(line)

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
    OUT.mkdir(parents=True, exist_ok=False)
    versions = {}
    for arm, ref in [
        ("release", "v0.5.6"),
        ("main", "d50c724b31bb2cb945495637ba40a1ce6504ccd1"),
    ]:
        versions[arm] = subprocess.check_output(
            ["git", "rev-parse", ref], cwd=REPO, text=True
        ).strip()
        archive = OUT / (arm + ".tar")
        with archive.open("wb") as f:
            subprocess.run(["git", "archive", ref], cwd=REPO, stdout=f, check=True)
        dest = OUT / arm
        dest.mkdir()
        with tarfile.open(archive) as tar:
            tar.extractall(dest, filter="data")
        archive.unlink()
    candidate = OUT / "candidate"
    candidate.mkdir()
    for file in ["__init__.py", "plugin.yaml"]:
        shutil.copy2(REPO / file, candidate / file)
    shutil.copytree(
        REPO / "hermes_switchyard",
        candidate / "hermes_switchyard",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    book = cases()
    (OUT / "cases.json").write_text(json.dumps(book, indent=2))
    rng = random.Random(791002)
    jobs = []
    for rep in range(2):
        ordered = list(book)
        rng.shuffle(ordered)
        for case in ordered:
            arms = ["off", "release", "main", "toggle", "candidate"]
            rng.shuffle(arms)
            for arm in arms:
                jobs.append((rep, case, arm))
    frozen_scripts = OUT / "scripts"
    frozen_scripts.mkdir()
    for name in ("native_compare.py", "native_worker.py", "holdout_cases.py"):
        shutil.copy2(ROOT / name, frozen_scripts / name)
    runtime_before = runtime_fingerprint()
    freeze = {
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "replaces_interrupted_run": "awesome-jev-native-v1 (15/80 completed; not pooled)",
        "runtime_file_hashes": runtime_before,
        "primary_claim": "efficiency",
        "threshold": "Preserve all correct completions and no new errors; reduce median and total wall time >=10% versus off and main; p95 no more than 10% worse; release and same-source toggle reported. Whole workload including fallback.",
        "versions": versions,
        "candidate_base": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=REPO, text=True
        ).strip(),
        "model": "gpt-6-sol",
        "provider": "openai-codex",
        "jev": "typesafe/jev-1.13-20260917",
        "requested_effort": "high",
        "repeats": 2,
        "concurrency": 1,
        "main_call_cap": 6,
        "seconds_per_conversation": 90,
        "grading": "Required facts and contradictions from gold; exact quoted evidence must occur in source. Inspect every final answer; no provider judge or confidence-as-correctness. Record unsupported facts or missing evidence as failure, and all timeouts.",
        "order": [(r, c["id"], a) for r, c, a in jobs],
        "hashes": {
            str(p.relative_to(OUT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in candidate.rglob("*")
            if p.is_file()
        },
        "scripts": {
            p.name: hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [
                Path(__file__),
                ROOT / "native_worker.py",
                ROOT / "holdout_cases.py",
            ]
        },
        "hermes_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            cwd=HERMES_SOURCE,
            text=True,
        ).strip(),
    }
    (OUT / "freeze.json").write_text(json.dumps(freeze, indent=2))
    workers = {}
    try:
        for arm in ["off", "release", "main", "toggle", "candidate"]:
            workers[arm] = Worker(
                arm,
                OUT
                / ("main" if arm == "off" else "candidate" if arm == "toggle" else arm),
            )
        with (OUT / "raw.jsonl").open("x") as handle:
            for rep, case, arm in jobs:
                job = {**case, "id": case["id"] + f"-r{rep}"}
                row = workers[arm].ask(job)
                row.update(case_id=case["id"], repeat=rep)
                handle.write(json.dumps(row) + "\n")
                handle.flush()
                print(
                    json.dumps(
                        {k: row[k] for k in ["id", "arm", "wall_ms", "completed"]}
                        | {"calls": len(row["wire"]), "jev": len(row["jev"])}
                    ),
                    flush=True,
                )
    finally:
        for worker in workers.values():
            worker.close()
        runtime_after = runtime_fingerprint()
        (OUT / "runtime-after.json").write_text(json.dumps({"hashes": runtime_after, "unchanged": runtime_after == runtime_before}, indent=2))


if __name__ == "__main__":
    main()
