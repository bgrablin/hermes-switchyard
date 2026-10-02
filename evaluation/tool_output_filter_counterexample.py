"""Offline #151 falsification probe. Exit 1 means the essential-span gate failed.

This is a deterministic development counterexample, not an end-to-end benchmark.
It does not call a provider or treat unknown latency/usage/cost as zero.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_switchyard.output_pruning import prune_terminal_result  # noqa: E402
from hermes_switchyard.tool_output_filter import filter_tool_result_text  # noqa: E402


def main() -> int:
    # Frozen fixture and oracle: report this digest from successful build output.
    # No full-dump cue or failure/security marker is needed to make the fact essential.
    essential = "artifact digest: sha256:REQUIRED_SYNTHETIC_VALUE"
    pad = "build checkpoint completed\n" * 300
    raw = json.dumps({"output": pad + essential + "\n" + pad, "exit_code": 0, "stderr": ""})
    kwargs = {"tool_name": "terminal", "result": raw, "status": "ok"}
    candidate = filter_tool_result_text(
        enabled=True, user_text="Report the artifact digest.", **kwargs,
    ) or raw
    alternative = prune_terminal_result(**kwargs) or raw
    arms = {"plugin_disabled": raw, "current_release_filter_off": raw,
            "candidate_filter_on": candidate, "existing_lossless_alternative": alternative}
    results = {name: {"chars": len(value), "valid_json": isinstance(json.loads(value), dict),
                      "essential_span_retained": essential in value}
               for name, value in arms.items()}
    passed = all(result["essential_span_retained"] for result in results.values())
    print(json.dumps({
        "probe": "synthetic_development_counterexample", "native_task_benchmark": False,
        "fixture_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "source_sha256": hashlib.sha256(
            (Path(__file__).resolve().parents[1] / "hermes_switchyard/tool_output_filter.py").read_bytes()
        ).hexdigest(),
        "request_budget": 0, "physical_requests": 0, "arms": results,
        "essential_span_gate_passed": passed, "downstream_latency_ms": None,
        "billed_tokens": None, "total_cost": None,
    }, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
