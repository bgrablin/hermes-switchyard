# Record triage demo (`jev_assess` library example)

**In short:** this is a worked example, not a feature you turn on. It shows how to use Jev's typed decisions safely in a real workflow: sorting a batch of bug reports into "qualified," "needs info," or "out of scope," and rating severity. Code decides what counts as a confident answer, acts only on those answers, writes the results to disk, and a separate check re-verifies everything. Use it as a template for your own `jev_assess` workflows.

`hermes_switchyard.record_triage` is a second bounded demonstration of the existing `jev_assess` primitive. It is a library module, not a registered tool: the tool surface and `plugin.yaml` are unchanged. It shows useful motion end to end: records go in, typed judgements are accepted or refused by code, a deterministic consumer acts, and a separate check re-verifies the result.

## What it does

Input is up to 64 public or synthetic bug-report records: `id`, `title`, `body`, `data_class` (`public` or `synthetic`), and an optional `component`. Anything else is rejected before any request.

1. **Local bypass.** Code decides two cases without a provider request: an empty body is `needs_info` (`empty_body`), and an exact normalized duplicate of an earlier record is `out_of_scope` (`exact_duplicate_of:<id>`). Those records are never sent.
2. **Bounded `jev_assess` batches.** The remaining records go out in batches of at most 8, and the workflow also sizes each batch to fit one provider request, because every request repeats the shared `state`. A batch therefore never relies on `decide` splitting it, which would discard the answers of requests that already completed if a later split failed, or refuse the whole batch before any request. Each batch is one `client.decide` call under the same request budget and aggregate deadline the registered tool uses (`build_assessment_request` returns the exact `state` and `questions` a `jev_assess` call takes). Per record there is one Choice over the code-defined alternatives `qualified`, `needs_info`, `out_of_scope`, and one ordered Score over `cosmetic`, `minor`, `major`, `critical`. The whole run is limited to 16 provider requests.
3. **Acceptance gate.** A disposition is accepted only when both its confidence and its winning probability reach `disposition_threshold` (default 0.80, an uncalibrated local policy value). Otherwise the record abstains. A severity below `severity_threshold` leaves a qualified record `unrated`.
4. **Deterministic consumer.** It acts only on accepted decisions, and only when local policy also allows it: `qualified` needs a `component` from the code-defined set, otherwise the consumer rejects it. Actions are `queue_qualified`, `request_info`, and `close_out_of_scope`, written as files plus a `manifest.json` with SHA-256 hashes and per-component priority queues.
5. **Independent check.** `verify_artifact(out_dir, records)` re-reads the artifact from disk and re-derives the local rules, thresholds, routability, hashes, queue order, and stray files. `run_record_triage` itself always returns `verified: false`.

## Severity rubric

The Score criteria describe observable impact, in the existing index order:

| Index / level | Evidence boundary | Priority |
| --- | --- | --- |
| 0 / cosmetic | Appearance or wording only; intended operations work and no data is lost | p3 |
| 1 / minor | Functional degradation while the intended task still completes, or a failed operation with an explicit usable workaround; no data loss or unauthorized access | p2 |
| 2 / major | Intended operation blocked with no usable workaround, without reported permanent loss or unauthorized access | p1 |
| 3 / critical | Permanent loss/corruption, unauthorized access, or confirmed exposed secrets | p0 |

These are this demonstration workflow's categories, not a universal incident-severity standard. Code selects the modal Score category from its probability vector, not the possibly fractional expected score. The four index mappings and all acceptance thresholds are unchanged.

The reviewed rubric was confirmed on 24 concrete synthetic reports, twice per arm: it produced 42 correctly rated priorities versus 18 on main, out of 48 observations per arm. The candidate accepted zero wrong priorities; main accepted two. All 24 work-queue artifacts passed on-disk verification. Mean batch latency was 232.169 ms versus 195.525 ms (+18.74%); the median was nearly unchanged, while the tail and reported cost increased. Six candidate records still abstained. In this demonstration taxonomy, a functional slowdown that still completes is minor regardless of magnitude; it is not a universal incident severity policy. The [complete evaluation](https://github.com/bgrablin/hermes-switchyard/tree/main/evaluation/decision_quality) retains the initial rubric's results, an explicitly disclosed pilot label correction, the new confirmation, and rejected candidates. These small synthetic workloads do not establish calibrated real-report accuracy.

## Four separate stages

Every record reports these separately: `attempt` (was a provider request made, did its batch complete), `decision` (accepted, abstained, or unassessed, and from `jev` or `local_rule`), `consumer` (acted, rejected, skipped, or failed), and, only after you call `verify_artifact`, the verified outcome.

## Fail-closed behaviour

- Invalid input, a false `public_or_sanitized_data_ack`, or a bad deadline raises before any provider request. A non-empty output directory is refused.
- A failed, malformed, or late batch leaves its records `unassessed` and held. Nothing is retried, no other model is tried, and no local guess replaces the missing judgement.
- One failed batch never stops the run. Every other batch is still attempted, whether the failure was the first, a middle, or the last batch, or several in a row, and each completed assessment is kept and acted on. Only an expired aggregate deadline or an exhausted request budget stops later batches, and a batch that cannot start is reported as not attempted with that reason.
- Unprocessed records are explicit evidence, not silence. Each carries `decision.status: unassessed` with a closed-set reason (`deadline_exceeded`, `provider_failed`, `invalid_response`, `malformed_answer`, `request_budget_exhausted`). The manifest records each record's `attempt` and an `unprocessed` map of id to reason, and `verify_artifact` rejects a manifest that hides, invents, or mislabels one.
- A fault inside one record's answer parsing or consumer step is contained to that record (`malformed_answer` or consumer `failed` with `consumer_error`). Interrupts such as Ctrl-C still propagate, so an interrupted run writes no manifest and is not verified.
- Malformed answers are rejected per record. Provider and transport text never enters a result or artifact; only stable reason codes (`deadline_exceeded`, `provider_failed`, `invalid_response`, `malformed_answer`, `request_budget_exhausted`).
- Cost is never invented. `total_cost` is a number only when every completed batch reported a cost and no attempted batch failed; otherwise it is `null` with `cost_known: false` and the reported part in `known_cost_subtotal`. A run that needed no provider request reports a known cost of 0.0.

## Limits

- The offline tests use a synthetic transport. They show plumbing and fail-closed behaviour; they do not show that Jev's judgements are correct, calibrated, or cheaper than another approach.
- `verify_artifact` checks that actions follow policy and match the recorded decisions. It cannot tell whether an accepted decision was right.
- The live rubric evidence is limited to synthetic public reports and the library workflow. It does not establish real-report accuracy, automatic normal-prompt integration, or GUI behavior.

## Use

```python
from hermes_switchyard.client import DecisionClient
from hermes_switchyard.record_triage import run_record_triage, verify_artifact

with DecisionClient(api_key=key) as client:  # key from your profile secret store
    result = run_record_triage(records, client=client, out_dir="triage-out")
report = verify_artifact("triage-out", records)
```
