"""Offline preservation regression for the rejected #151 head/tail prototype.

Checks operator utility using a synthetic fixture, not an end-to-end benchmark.
No provider is called; unmeasured downstream usage, latency, and cost stay null.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_switchyard.output_pruning import OutputCompactionGuard, prune_terminal_result  # noqa: E402


def main() -> int:
    essential = "artifact digest: sha256:REQUIRED_SYNTHETIC_VALUE"
    pad = "build checkpoint completed\n" * 300
    raw = json.dumps({"output": pad + essential + "\n" + pad, "exit_code": 0, "stderr": ""})
    kwargs = {"tool_name": "terminal", "result": raw, "status": "ok"}
    scope = {"session_id": "synthetic", "task_id": "build", "turn_id": "turn"}
    guard = OutputCompactionGuard()
    guard.capture(user_message="Report the artifact digest.", **scope)
    compact = guard.prune(**scope, **kwargs) or raw
    guard.capture(user_message="Show the full output verbatim.", **scope)
    full = guard.prune(**scope, **kwargs) or raw
    # The existing reducer had no access to the request, and thus compacted
    # identical lines even when the user asked for the original output.
    current = prune_terminal_result(**kwargs) or raw
    arms = {"plugin_disabled": raw, "current_compaction_digest_request": current,
            "candidate_compaction_digest_request": compact,
            "current_compaction_full_output_request": current,
            "candidate_compaction_full_output_request": full}
    results = {name: {"chars": len(value), "valid_json": isinstance(json.loads(value), dict),
                      "essential_span_retained": essential in value,
                      "original_bytes_retained": value == raw}
               for name, value in arms.items()}
    passed = (all(item["essential_span_retained"] for item in results.values())
              and full == raw and current != raw and compact == current)
    source = Path(__file__).resolve().parents[1] / "hermes_switchyard/output_pruning.py"
    print(json.dumps({
        "probe": "synthetic_preservation_regression", "native_task_benchmark": False,
        "fixture_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "request_budget": 0, "physical_requests": 0, "arms": results,
        "preservation_and_utility_gate_passed": passed,
        "downstream_latency_ms": None, "billed_tokens": None, "total_cost": None,
    }, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
