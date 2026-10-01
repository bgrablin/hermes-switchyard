# Request-planning performance and proposal disposition

This change removes repeated JSON serialization from three existing paths:
skill partition planning, multi-skill batch planning, and `DecisionClient.decide`
question batching. It preserves candidate order, partition boundaries, request
bodies, byte limits, sequential dispatch, and the existing accounting and egress
controls. A complete payload is still validated immediately before transport.
Requests that fit in one batch take a direct path. There is no new setting.

## Measured scope

The baseline is main `541649a3ae64eb666a3e85dff35a03642d00030a`.
The final candidate is reconstructed by applying the v1 patch and then the v2
patch in `frozen/source-records.tar.gz` to that baseline. Frozen source hashes
identify the measured Python files independently of the eventual PR commit.

The primary claim was frozen before each run: identical plans and payloads,
with at least 50% less median local planning time on the 600-entry cases.
This is a component optimization of existing behavior. It does **not** establish
the whole-conversation efficiency or capability gate in `docs/VALUE-EVALUATION.md`,
qualify a new automatic feature, or justify enabling an existing opt-in feature.

Silver, Python 3.11.16, 21 interleaved repetitions per arm and case:

| Component | Main median | Candidate median | Reduction |
| --- | ---: | ---: | ---: |
| Plan 25 skills | 0.794 ms | 0.138 ms | 82.6% |
| Plan 600 skills | 98.713 ms | 2.696 ms | 97.3% |
| Plan multi-selection over 600 skills | 112.034 ms | 3.786 ms | 96.6% |
| Decide 255 questions, synthetic transport | 17.180 ms | 0.604 ms | 96.5% |
| Decide 600 questions, small state, synthetic transport | 37.597 ms | 2.341 ms | 93.8% |
| Decide 600 questions, 50 KB state, synthetic transport | 81.286 ms | 2.713 ms | 96.7% |
| Decide one question, synthetic transport | 0.049 ms | 0.063 ms | 0.014 ms slower |

All eight workload variants produced identical plan or payload hashes, with no
failures across 336 final offline observations. The small absolute regression
on the one-question control is retained; this is not a claim that every path
became faster. JSON size is measured in UTF-8 bytes, including escaped content,
the first partition's extra question, and changing partition/question indices.

## Live OpenRouter check

The final run used `typesafe/jev-1.13-20260917`: four question workloads, three
repetitions, two interleaved arms, 24 observations and 48 logical provider
requests. Connections started cold in both arms. All requests succeeded,
resolved to the pinned model, and passed the client's typed-response checks.
The complete planned wire payloads matched across both arms for each workload.
Answers varied between repeated hosted calls; response equivalence is not
claimed. This checks transport compatibility, not model accuracy.

| Workload | Main median | Candidate median | Reduction |
| --- | ---: | ---: | ---: |
| One question | 255.0 ms | 205.7 ms | 19.3% |
| 255 questions | 327.0 ms | 276.2 ms | 15.5% |
| 600 questions, small state | 809.5 ms | 796.3 ms | 1.6% |
| 600 questions, 50 KB state | 1044.2 ms | 966.3 ms | 7.5% |

Three repetitions are insufficient for a network-latency qualification. The
earlier v1 live run was mixed, including regressions, and remains in the archive.
Do not select a favorable live subset or translate local CPU savings into a
whole-Hermes speedup. There were no native conversation evaluations in this study,
no plugin-disabled or release arms, and no main-model calls. Those comparisons
remain necessary before promoting a new routing or decision behavior.

Both versions' live observations are retained: 48 observations, 96 logical
provider requests, $0.035266392 reported cost, zero missing cost fields and no
reported failures. Logical request counts do not independently meter transport
retries. The v1 candidate was revised to avoid incremental planning on requests
that already fit one batch and to retain private-helper duplicate validation.
Each version has its own freeze and source reconstruction; results are not pooled.

Review added semantic checks of the reported accounting: an independent
full-serialization oracle checks each request count, and recorded per-case costs
and per-run/combined totals are verified. These are retrospective checks of
retained observations, not retroactive additions to the pre-call freezes. The v1
rows omitted resolved-model and wire-payload fields, so only v2 independently
verifies those fields; no missing historical fields are filled in.

## Disposition of the proposed ideas

This table records code and contract review, not unperformed live experiments.
Only request planning was implemented and benchmarked here.

| Proposal | Decision and reason |
| --- | --- |
| Coalesce skill and effort decisions | Continue in existing draft [#188](https://github.com/bgrablin/hermes-switchyard/pull/188), without a duplicate implementation. Its shared-decision pilot also changed effort choices, so its result does not isolate transport savings. Production qualification must retain scope, exact payload/authority binding, metadata-only separation, and all bypasses. |
| Parallel partitions and question batches | Do not add a thread pool to the current counters. A deterministic check using two copied contexts spent a one-request parent budget twice while leaving the parent counter unchanged. A supported implementation needs a synchronized operation ledger, bounded connection ownership, cancellation/deadline propagation, and complete accounting for concurrent successes and failures. Parallel speedup was not measured here. |
| Linear skill chunk planning | Implemented, with the same optimization applied to multi-skill and general question batches. No catalog-plan cache is needed, so there is no new invalidation or cross-task state. |
| Pre-warm TLS on load or a trivial turn | Excluded as proposed. It introduces network activity on paths whose existing behavior avoids hosted work. Warming synchronously after authorization does not hide that first handshake; any future background warm-up needs a separately justified lifecycle and bounded cleanup. Existing connection reuse remains. |
| Taxonomy routing | Unqualified. Current candidate input has no validated taxonomy; a wrong category can hide the correct skill. Two calls are not a size-independent bound when categories or leaves exceed Choice/request limits. Require complete coverage and measured recall before replacing full partition search. |
| Score instead of Choice for effort | Unqualified. Choice already returns a distribution, and ordinal Score does not itself establish useful thresholds or calibrated hysteresis. Preserve effort caps and stakes protection; compare outcomes and oscillation on an independent workload before changing the decision primitive. |
| Per-action risk Noul | Reject its proposed role as an approval gate. A model score cannot authorize an action or relax the native executor's controls. An advisory warning would need a separate utility evaluation. `reconcile_before_retry` must continue to reflect attempted actions and uncertain effects, not only a risk prediction. |
| DOM prompt-injection Noul | Unqualified as an added detector. Page text remains untrusted regardless of the score; detection is not a replacement for that boundary. A useful addition needs attack/benign coverage, false-negative and false-positive measurements, and end-to-end latency evidence. No detector or bypass is added. |
| Retry/replan/ask-user Choice | Unqualified. `post_tool_call` records outcomes; its returned Choice does not execute recovery. Safe retry also needs tool semantics, remaining budgets and reconciled side effects. A future advisory integration can use supported extension points, but must prove better recovery without duplicate effects. |
| Topic-change compaction | Excluded as proposed. Topic change does not establish which prior constraints can be discarded. The current plugin's pre-turn capture occurs after turn-start compression and supplies no trusted compaction-control contract. Do not modify host source to make this feature work. |
| Cloudflare endpoint | Availability verified in [Cloudflare's model documentation](https://developers.cloudflare.com/ai/models/typesafe/jev/). It is an account-scoped API with separate credentials and a distinct envelope. A future explicit provider would need route, credential and egress tests. It is not an automatic fallback and adds no demonstrated hot-path value to this PR. |

Jev's documented Score primitive returns a score, legend, probabilities and
confidence. [Vercel also documents Jev access](https://vercel.com/changelog/ai-gateway-now-supports-typesafe-clients-and-http-api-for-jev).
Availability alone is not a reason to change the configured provider.

## Reproduce and inspect

`frozen/measurements.tar.gz` contains the v1/v2 freezes, every raw observation,
and summaries. `frozen/source-records.tar.gz` contains the baseline revision and
both source patches. `frozen/sha256.json` binds the archives and visible summaries.
No credentials, user tasks, or private source text are included.

Run `python evaluation/hotpath/verify.py` to check retained evidence, completeness,
payload agreement and recomputed timing summaries. It also requires the complete
checked-out plugin Python source set and benchmark driver to match both v2 freezes.
Changed, missing or added source files invalidate these results for the current
tree; rerun the benchmark on changed source before claiming its performance.
Run the boundary tests with
`python -m unittest discover -s tests -p test_request_planning.py`.
Run `planning.py --baseline BASE --candidate CANDIDATE --output NEW_DIRECTORY`
for a new offline measurement. Live measurements additionally require `--live
--profile EVALUATION_PROFILE` in a configured Hermes environment. Existing output
directories are never overwritten. Keep the new freeze and every observation.

Historical unit verification uses `frozen/measured-source-v2.tar.gz`, a copy of
the exact measured source independently checked against the original freeze.
It preserves the original observations and does not certify later feature
branches. The default `verify.py` command still checks the current tree and
rejects source drift; passing the immutable measured source only audits the
historical study. Do not reuse its measurements as current-branch qualification.
