# Hermes Switchyard skill-selection benchmark

This directory (`evaluation/benchmark/`) is an offline-first selector microbenchmark intended to remain usable before and after repository publication. It does not modify Hermes core, the Switchyard plugin, configuration, live profiles, Windows components, or the source repository.

The heldout set is 24 public synthetic cases: six clear positives, six no-fit requests, six near-misses, and six multi-skill/ambiguous requests. The catalog has 12 generic public skill descriptions. Labels are frozen in `fixtures.json` before any provider-backed execution. Four development fixtures are separate and are never included in the heldout report.

Arms

A. `lexical`: deterministic token-overlap ranking with fixed constants in `benchmark.py` (`LEXICAL_MIN=0.20`, `LEXICAL_MARGIN=0.05`) and explicit abstention. These constants are implementation defaults, not tuned on heldout cases.

B. `luna`: direct `openai-codex/gpt-5.6-luna-900k` selection at `max`, using the same task and complete candidate catalog. Offline mode uses labelled simulation records only. `collect_luna.py` prepares one supported Hermes one-shot request per case and records the official `--usage-file` receipt. It does not access auth directly or fall back to a paid provider.

C. `switchyard`: the actual `hermes_switchyard.routing.select_skill` imported from the exact reviewed plugin path. Offline mode supplies a synthetic typed client response, so it exercises the current selector contract without a model request. `collect_switchyard.py` uses the real `DecisionClient`; it never injects a response.

C is not an integrated per-turn hook. Current Switchyard requires an explicit tool invocation. The harness records one selector invocation plus the Jev provider call for C and reports that coordination overhead separately. This is a selector microbenchmark only. It is not evidence of whole-agent improvement. A separate Hermes end-to-end without-advisor/with-advisor comparison is required before making that claim.

Run the offline harness

    python3 evaluation/benchmark/benchmark.py --mode offline --plugin-path <reviewed-plugin-path>

The default is offline and makes no network or model calls. It imports the actual candidate package and checks its source hashes. Use `--output path.json` only when a local report artifact is useful; generated runs should not be committed.

The harness refuses a comparative report when an arm is missing a case, has duplicate/extra cases, or has inconsistent dataset, catalog, request, fixture, or source hashes. Source hashes are checked against their exact target: benchmark code for lexical, the frozen prompt template for Luna, and the reviewed plugin source for Switchyard. Live Luna/Switchyard rows must be non-simulated successful provider observations with positive provider-call counts and structured measurement provenance. Failed observations are retained by collectors but cannot enter a comparative report.

Metrics and honesty rules

- Strict single-skill cases have top-1 exact-match metrics; a Luna multi-selection is not a top-1 hit.
- No-fit cases use a separate false-positive rate. Suppressing a no-fit request is not a positive hit.
- Positive abstention and positive miss are counted only on positive cases.
- Required-set cases report multi-skill capability coverage. One selected skill is not silently scored as the complete set.
- Ambiguous cases use their own accepted-set metric; they are not folded into multi-skill capability or strict top-1 accuracy.
- Candidate coverage reports whether every labelled target is present in the offered catalog.
- Each record carries dataset, fixture, request, candidate-catalog, and arm source hashes.
- Usage fields are null when unavailable, never zero by assumption. Included Codex quota tokens and Jev PAYG dollars are separate fields. The report never turns those into a “cheaper” claim.
- Wall time and provider-call time are distinct. p50/p95 use nearest-rank: sort n values, use rank `ceil(p*n)`, and clamp the rank to at least one. Offline timing is harness/simulation timing and is explicitly non-claimable. Batch timing is never treated as individual latency. Provider totals and percentiles are null unless every case has an observed provider-time value; the report includes observed/total coverage counts.
- The initial design is one case per provider request. Repeats, if later added, must be a separate variability analysis and must not overwrite the first-run result.

Direct Luna baseline

`baseline_prompt.md` is the stable prompt template. `benchmark.render_luna_prompt()` renders only the public task and candidate catalog. Expected labels and offline simulations never enter a provider prompt. The current supported Hermes one-shot route is:

    hermes --safe-mode --ignore-user-config --ignore-rules -t context_engine \
      -m openai-codex/gpt-5.6-luna-900k --provider openai-codex --reasoning max \
      --usage-file CASE_USAGE.json -z "$(cat CASE_PROMPT.txt)"

Use the route’s official JSON response and usage report. Do not scrape hidden token counts, infer dollar cost, or put credentials in a prompt or command. `collect_luna.py` uses this route with the empty `context_engine` toolset, one sequential subprocess per case, and a 900-second per-case bound. Its `wall_ms` is end-to-end Hermes process time; `provider_call_ms` stays null because the supported CLI usage file does not expose provider-request timing. A bounded `hermes --version` probe records the runtime identity required for live ingestion and resume; its duration is recorded separately and is not relabelled as provider latency.

A batch decision can reduce startup overhead, but its wall time is batch latency, not per-case latency. Do not divide it by 24 or compare it with individual-request p50/p95. The first live run should use one request per case. Live ingestion requires both an explicit `--max-requests` from 1 through 60 and `--public-synthetic-ack`:

    python3 evaluation/benchmark/benchmark.py --mode live \
      --plugin-path <reviewed-plugin-path> \
      --luna-input luna.records.json --switchyard-input switchyard.records.json \
      --max-requests 48 --public-synthetic-ack

The input files must use the normalized schema shown below. This harness does not execute live mode. The parent coordinator owns live execution, grading, review, and claims. The prepared collectors are deliberately not run by this implementation.

Switchyard collection command:

    python3 evaluation/benchmark/collect_switchyard.py --live --public-synthetic-ack --max-requests 24 \
      --plugin-path <reviewed-plugin-path> --output switchyard.records.json

Luna collection command:

    python3 evaluation/benchmark/collect_luna.py --live --public-synthetic-ack --max-requests 24 \
      --output luna.records.json

Both collectors append a complete normalized receipt after every case. A provider or parsing failure is written as `measurement_status: "failed"` with a bounded error type and is never scored as a selection. The Switchyard collector resolves `OPENROUTER_API_KEY` only through Hermes' supported runtime secret scope and never prints it. The Luna collector delegates credential handling entirely to Hermes.

The Switchyard command must run with the Python environment that provides Hermes' `agent.secret_scope`; otherwise it refuses before any provider call. Do not replace that scope with a command-line key or a hand-read credential.

Normalized arm input field map

The following JSON is an abbreviated, non-loadable field map. Actual receipts must contain the complete provenance and request identities emitted by the collector; do not use this excerpt as input to the harness.

    {
      "schema_version": 1,
      "measurement_schema_version": 1,
      "arm": "luna",
      "collection_mode": "live",
      "hermes_runtime_identity": "Hermes Agent <bounded version identifier>",
      "dataset_hash": "from offline report",
      "candidate_catalog_hash": "from offline report",
      "public_synthetic_ack": true,
      "records": [{
        "case_id": "...",
        "dataset_hash": "...", "fixture_hash": "...", "request_hash": "...",
        "request_identity": {"case_id": "...", "task_hash": "...", "candidate_catalog_hash": "...", "template_hash": "...", "request_hash": "..."},
        "candidate_catalog_hash": "...", "source_hash": "...", "collector_source_hash": "...",
        "status": "selected" or "abstained",
        "selected": "exact-name" or null,
        "selected_skills": ["exact-name"],
        "model": "openai-codex/gpt-5.6-luna-900k",
        "provider": "openai-codex", "reasoning": "max",
        "usage": {
          "input_tokens": null, "output_tokens": null, "total_tokens": null,
          "included_codex_quota_tokens": null, "jev_payg_dollars": null,
          "reported_dollar_cost": null
        },
        "wall_ms": null, "provider_call_ms": null,
        "timing_scope": "case",
        "selector_invocation_count": 0, "provider_call_count": 1,
        "coordination_call_count": 0,
        "public_synthetic_ack": true,
        "actual_call": true, "simulated": false,
        "measurement_schema_version": 1, "measurement_status": "ok",
        "measurement_provenance": {"kind": "actual_provider_observation", "collector": "luna_hermes_oneshot", "measurement_scope": "case", "observed_case_count": 1, "observed_case_ids": ["..."], "provider_response_observed": true, "wall_time_observed": true, "provider_time_observed": false, "usage_observed": true}
      }]
    }

For `switchyard`, the resolved model must be one of the actual Jev aliases and the provider-call count must include the explicit selector call’s provider request. Switchyard request identities are arm-specific and have `template_hash: null`; the Luna baseline alone uses the prompt template hash. Every provider row also carries the exact collector source hash. For every arm, use the exact hashes emitted by the harness. Do not hand-edit labels or result hashes. A failed measurement may have `status: "failed"`, empty selection fields, and a non-empty bounded `error`; it cannot produce a comparative report.

Scope boundary

Live collection is operator-initiated and is not run by import, offline tests, or the normal CI workflow. Retained live evidence must be inspected at its exact source and request hashes; failed measurements remain failed and cannot enter a comparative report. Luna `max` is a user-selected baseline setting, not Hermes' quickest default. No numerical improvement is claimed. A whole-agent study must use fresh Hermes runs with and without the advisor, the same Luna model/reasoning/toolset, the same synthetic tasks, and independently verified outcomes. This directory enables that evidence collection; it is not proof of whole-agent gains.
