"""Frozen native same-provider routing pilot; public synthetic tasks only."""
import argparse
import hashlib
import importlib.util
import io
import json
import math
import os
import random
import shutil
import statistics
import subprocess
import tarfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[1]
spec=importlib.util.spec_from_file_location("worker_driver",REPO/"evaluation/decision_quality/native_compare.py")
driver=importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)
driver.ROOT=ROOT
CASES=[
 {"id":"capital","prompt":"What is the capital of Italy? Return only the city.","expected":"Rome"},
 {"id":"extract","prompt":"Public sample: item=maple; serial=KQ-4821; colour=green. Return only the serial.","expected":"KQ-4821"},
 {"id":"sum","prompt":"Return only the sum of 148 and 267.","expected":"415"},
 {"id":"sort","prompt":"Sort 12, 3, 24, 7 numerically. Return comma-separated integers without spaces.","expected":"3,7,12,24"},
 {"id":"boolean","prompt":"Return YES if every member of [6,12,18,23] is divisible by 3, otherwise NO. Answer only YES or NO.","expected":"NO"},
 {"id":"scope","prompt":"Cedar item 4 is complete. Birch item 4 is pending. For Birch item 4 return COMPLETE or PENDING only.","expected":"PENDING"},
 {"id":"logic","prompt":"Exactly one of P and Q is true. P implies not R. R is true. Return the true member of P and Q only.","expected":"Q"},
 {"id":"trace","prompt":"Python: a=[2,4]; b=a; b.append(6); a=a+[8]. Return only len(b).","expected":"3"},
 {"id":"units","prompt":"Convert 2 hours and 17 minutes into minutes. Return the integer only.","expected":"137"},
 {"id":"product","prompt":"Return only the integer product of 47 and 59.","expected":"2773"},
 {"id":"missing","prompt":"Choose the newest version of a package whose name and version list have not been provided. Return UNKNOWN if this cannot be determined.","expected":"UNKNOWN"},
 {"id":"date","prompt":"A synthetic event starts on February 28, 2028. What date is two days later? Return YYYY-MM-DD only.","expected":"2028-03-01"}
]

def digest(p):
 return hashlib.sha256(p.read_bytes()).hexdigest()

def runtime_hashes():
 root=Path("<recorded-hermes-source-root>")
 names=subprocess.check_output(["git","ls-files","*.py"],cwd=root,text=True).splitlines()
 return {name:digest(root/name) for name in names if (root/name).is_file()}

def main():
 parser=argparse.ArgumentParser()
 parser.add_argument("--output",type=Path,required=True)
 args=parser.parse_args()
 out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
 revisions={}
 for arm,ref in [("release","v0.5.6"),("main","a0fd0ad670bec53a72d2fa2a6ef851382f5e46c0")]:
  revisions[arm]=subprocess.check_output(["git","rev-parse",ref],cwd=REPO,text=True).strip()
  data=subprocess.check_output(["git","archive",ref],cwd=REPO)
  dest=out/arm;dest.mkdir()
  with tarfile.open(fileobj=io.BytesIO(data)) as tar:tar.extractall(dest,filter="data")
 shutil.copytree(out/"main",out/"candidate")
 shutil.copy2(ROOT/"pilot.py",out/"candidate/hermes_switchyard/routing_pilot.py")
 (out/"candidate/__init__.py").write_text(
  'from .hermes_switchyard import register as _register\n'
  'from .hermes_switchyard.routing_pilot import ContextProxy\n'
  'def register(ctx):\n    return _register(ContextProxy(ctx))\n')
 before=runtime_hashes()
 (out/"runtime-before.json").write_text(json.dumps(before,sort_keys=True))
 freeze={"cases":CASES,"repeats":2,"arms":["off","release","main","candidate"],
  "revisions":revisions,"source_model":"gpt-6-sol","candidate_model":"gpt-6-luna",
  "provider":"openai-codex","jev":"typesafe/jev-1.13-20260917","jev_provider":"openrouter",
  "scope":"First-turn, short text-only requests; no tools, memory or previous context.",
  "acceptance":"Candidate correctness no lower than every control; median and total latency at least 5% below disabled and main; no unsupported provider mutation; a live routed wire request must occur. Not sufficient for general-release qualification.",
  "routing_deadline_ms":400,"concurrent_provider_calls":1,
  "files":{p.name:digest(p) for p in [Path(__file__),ROOT/"pilot.py",ROOT/"native_worker.py"]},
  "runtime_revision":subprocess.check_output(["git","rev-parse","HEAD"],cwd="<recorded-hermes-source-root>",text=True).strip()}
 (out/"freeze.json").write_text(json.dumps(freeze,indent=2))
 workers={};rows=[]
 try:
  for arm in freeze["arms"]:
   workers[arm]=driver.Worker(arm,out,out/("main" if arm=="off" else arm))
  jobs=[(r,c) for r in range(2) for c in CASES]
  rng=random.Random(873811);rng.shuffle(jobs)
  with (out/"raw.jsonl").open("x") as handle:
   for repeat,case in jobs:
    order=list(workers);rng.shuffle(order)
    for arm in order:
     row=workers[arm].ask({"id":case["id"]+"-"+str(repeat),"prompt":case["prompt"]})
     row["correct"]=str(row.get("final") or "").strip()==case["expected"]
     row["expected"]=case["expected"];row["repeat"]=repeat
     rows.append(row);handle.write(json.dumps(row)+"\n");handle.flush()
     print(json.dumps({"arm":arm,"id":row["id"],"correct":row["correct"],
      "wall_ms":row["wall_ms"],"wire":row["wire"],"routed":any(x.get("applied") for x in row.get("route",[]))}),flush=True)
 finally:
  for worker in workers.values():worker.close()
 after=runtime_hashes()
 (out/"runtime-after.json").write_text(json.dumps(after,sort_keys=True))
 summary={"runtime_unchanged":before==after,"arms":{}}
 for arm in freeze["arms"]:
  rs=[r for r in rows if r["arm"]==arm];values=sorted(r["wall_ms"] for r in rs)
  summary["arms"][arm]={"n":len(rs),"correct":sum(r["correct"] for r in rs),
   "median_ms":statistics.median(values),"total_ms":sum(values),
   "p95_ms":values[math.ceil(.95*len(values))-1],
   "main_dispatches":sum(len(r["wire"]) for r in rs),"jev_calls":sum(len(r["jev"]) for r in rs),
   "routed":sum(any(x.get("applied") for x in r.get("route",[])) for r in rs),
   "wire_models":sorted({w["model"] for r in rs for w in r["wire"]})}
 (out/"summary.json").write_text(json.dumps(summary,indent=2))
 print("SUMMARY "+json.dumps(summary),flush=True)
if __name__=="__main__":main()
