"""Launch the native worker using Hermes' own runtime command."""

import argparse
import json
import os
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument(
    "--arm", choices=["baseline", "candidate", "features"], required=True
)
parser.add_argument("--template", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
source = Path(__file__).resolve().parents[2]
script = source / "evaluation/feature_expansion/native_probe.py"
prefix = json.loads(
    subprocess.check_output(["hermes", "--print-runtime-command"], text=True)
)
old = "runpy.run_module('hermes_cli.main', run_name='__main__', alter_sys=True)"
assert old in prefix[-1]
prefix[-1] = prefix[-1].replace(
    old,
    f"sys.argv=[{str(script)!r}]+sys.argv[1:];runpy.run_path({str(script)!r},run_name='__main__')",
)
env = os.environ.copy()
env.update(HERMES_HOME=str(args.template), HERMES_DISABLE_LAZY_INSTALLS="1")
raise SystemExit(
    subprocess.call(
        prefix
        + [
            "--arm",
            args.arm,
            "--source",
            str(source),
            "--home",
            str(args.output.with_suffix("")),
            "--output",
            str(args.output),
        ],
        env=env,
    )
)
