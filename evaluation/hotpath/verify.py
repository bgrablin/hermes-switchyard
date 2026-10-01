"""Verify retained component evidence without credentials or network calls."""
import hashlib
import json
import math
import statistics
import tarfile
from pathlib import Path


def main():
    root = Path(__file__).parent / "frozen"
    for name, expected in json.loads((root / "sha256.json").read_text()).items():
        assert hashlib.sha256((root / name).read_bytes()).hexdigest() == expected, name
    with tarfile.open(root / "measurements.tar.gz") as archive:
        def read(name):
            return archive.extractfile(name).read().decode()

        for run in ("offline", "live", "offline-v2", "live-v2"):
            freeze = json.loads(read(run + "/freeze.json"))
            rows = [json.loads(line) for line in read(run + "/observations.jsonl").splitlines()]
            summary = json.loads(read(run + "/summary.json"))
            expected_jobs = [(rep, freeze["cases"][idx]["id"], freeze["cases"][idx]["kind"], arm) for rep, idx, arm in freeze["jobs"]]
            actual_jobs = [(r["rep"], r["case"], r["kind"], r["arm"]) for r in rows]
            assert actual_jobs == expected_jobs, (run, "job completeness/order")
            assert not any("error_type" in r for r in rows), (run, "failed observations")
            for case in summary:
                subset = [r for r in rows if r["case"] == case["case"] and r["kind"] == case["kind"]]
                for arm, saved in case["arms"].items():
                    times = sorted(r["wall_ms"] for r in subset if r["arm"] == arm)
                    assert saved == {"n": len(times), "failures": 0, "median_ms": statistics.median(times), "total_ms": sum(times), "p95_ms": times[math.ceil(.95 * len(times)) - 1]}, (run, case["case"], arm)
                reduction = 100 * (1 - case["arms"]["candidate"]["median_ms"] / case["arms"]["baseline"]["median_ms"])
                assert reduction == case["median_reduction_pct"]
                if run.startswith("offline") or run == "live-v2":
                    assert len({r["plan_or_payload_sha256"] for r in subset}) == 1
                if run.startswith("offline"):
                    assert case["identical_plan_or_payload"] is True
            if run.endswith("v2"):
                assert json.loads((root / (run + "-summary.json")).read_text()) == summary
            print(f"{run}: {len(rows)} complete observations; summaries verified")


if __name__ == "__main__":
    main()
