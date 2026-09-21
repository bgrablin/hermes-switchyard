# Bounded record triage on `jev_assess`

`hermes_switchyard.record_triage` is a second bounded demonstration of the existing `jev_assess` primitive. It is a library module, not a registered tool: the tool surface and `plugin.yaml` are unchanged. It shows useful motion end to end: records go in, typed judgements are accepted or refused by code, a deterministic consumer acts, and a separate check re-verifies the result.

## What it does

Input is up to 64 public or synthetic bug-report records: `id`, `title`, `body`, `data_class` (`public` or `synthetic`), and an optional `component`. Anything else is rejected before any request.

1. **Local bypass.** Code decides two cases without a provider request: an empty body is `needs_info` (`empty_body`), and an exact normalized duplicate of an earlier record is `out_of_scope` (`exact_duplicate_of:<id>`). Those records are never sent.
2. **Bounded `jev_assess` batches.** The remaining records go out in batches of at most 8, and the workflow also sizes each batch to fit one provider request, because every request repeats the shared `state`. A batch therefore never relies on `decide` splitting it, which would discard the answers of requests that already completed if a later split failed, or refuse the whole batch before any request. Each batch is one `client.decide` call under the same request budget and aggregate deadline the registered tool uses (`build_assessment_request` returns the exact `state` and `questions` a `jev_assess` call takes). Per record there is one Choice over the code-defined alternatives `qualified`, `needs_info`, `out_of_scope`, and one ordered Score over `cosmetic`, `minor`, `major`, `critical`. The whole run is limited to 16 provider requests.
3. **Acceptance gate.** A disposition is accepted only when both its confidence and its winning probability reach `disposition_threshold` (default 0.80, an uncalibrated local policy value). Otherwise the record abstains. A severity below `severity_threshold` leaves a qualified record `unrated`.
4. **Deterministic consumer.** It acts only on accepted decisions, and only when local policy also allows it: `qualified` needs a `component` from the code-defined set, otherwise the consumer rejects it. Actions are `queue_qualified`, `request_info`, and `close_out_of_scope`, written as files plus a `manifest.json` with SHA-256 hashes and per-component priority queues.
5. **Independent check.** `verify_artifact(out_dir, records)` re-reads the artifact from disk and re-derives the local rules, thresholds, routability, hashes, queue order, and stray files. `run_record_triage` itself always returns `verified: false`.

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
- No live provider, native Hermes runtime, or GUI evidence is claimed here.

## Use

```python
from hermes_switchyard.client import DecisionClient
from hermes_switchyard.record_triage import run_record_triage, verify_artifact

with DecisionClient(api_key=key) as client:  # key from your profile secret store
    result = run_record_triage(records, client=client, out_dir="triage-out")
report = verify_artifact("triage-out", records)
```
