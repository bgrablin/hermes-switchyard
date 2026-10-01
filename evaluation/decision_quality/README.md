# Jev decision-quality evaluation

Source idea: [the supplied Polydao post](https://x.com/polydao/status/2104783226833186920). These are our independent synthetic measurements, not validation of the post's advertised results.

Only the descriptive severity rubric qualified for this PR. It is always used by the existing `record_triage` library workflow. This change does not register a new tool or enable a new routing path.

## Results

Measurements ran on 2026-10-01 UTC with OpenRouter `typesafe/jev-1.13-20260917`. Native Hermes controls used `openai-codex` / `gpt-6-sol`, a requested effort of high, no fallback, no tools or memory, isolated profiles, and at most two concurrent main requests. No Sonnet was used.

| Candidate | Evidence | Decision |
| --- | --- | --- |
| Descriptive severity rubric | Independent full workflow: 28/32 correctly rated and acted-on records versus 15/32 on main; zero wrong accepted priorities in both arms | Include |
| Confidence gate at 0.8 for effort reductions | 24/24 correct answers on main and 24/24 with the gate; high-effort requests increased from 1 to 4 | Exclude: no measured quality gain |
| Decision-audit classifier | 22/32 correct classifications; 8 wrong classifications passed both 0.8 gates | Exclude: confident errors |
| Batch qualification | Two four-record runs averaged 218 ms batched versus 884 ms serial | Already implemented; retain existing behavior, no new feature claim |

The full workflow used 16 independently written concrete bug reports, twice per arm, in four-record batches. Both disposition gates and the severity gate stayed at 0.8. Each arm made eight requests. The candidate produced 28 rated priorities, four abstentions, and no qualified-but-unrated records. Main produced 15 rated priorities, six abstentions, and 11 qualified-but-unrated records. Every one of the 16 artifacts passed the existing independent on-disk verifier. That verifier checks policy and integrity; semantic correctness comes from the frozen labels.

| Full workflow metric | Main | Descriptive rubric |
| --- | ---: | ---: |
| Correct rated priorities | 15/32 | 28/32 |
| Wrong accepted priorities | 0/32 | 0/32 |
| Mean batch latency | 207.461 ms | 227.902 ms |
| Median batch latency | 200.240 ms | 198.398 ms |
| Observed nearest-rank p95 (8 batches) | 240.777 ms | 385.653 ms |
| Reported total Jev cost | $0.000483000 | $0.000578424 |

Mean latency increased 9.85%; the small-sample tail was worse. The reported cost increased about 20%. The improvement is useful priority coverage, not a claim of cheaper inference. The paired records share prompts across repeats, so 32 observations are not 32 independent defects.

The earlier conditional-severity screen used 32 reports twice per arm, with 12 development and 20 holdout reports frozen before calls. On 40 holdout observations, main accepted 20 correct and two wrong severities; the descriptive rubric accepted 40 correct and zero wrong. One main batch failed response validation and remains in the denominator. Those reports had weak reproduction details: every disposition abstained, so this screen alone established no work-queue gain. The independent concrete-report workflow above was required before selection.

Score values can be fractional expected values. Final scoring selects the modal category from the probability vector, matching the production consumer, rather than truncating the score. An early ad hoc tally incorrectly truncated it; `summary.json` and all figures here use the corrected consumer-aligned calculation.

## Native effort control

There were 96 native conversations: 12 fixed-answer tasks, two repeats, four arms. Disabled, v0.5.6, frozen main, and confidence-gated main each answered 24/24 correctly. v0.5.6 appended its legacy receipt to all answers, so strict formatting scored 0/24; the offline summary separately removes only that known receipt and preserves the original score and final text. This formatting difference does not count as a confidence-gate benefit.

The frozen main revision is `afee8afdec3967201ff6c24d29dc892e6311e6a4`. Release and main SHAs, the candidate patch, ordered observations, provider request IDs, usage, failures, and file hashes are retained in `observations.json`. Native worker usage fields are preserved as returned by Hermes and are not summed into a cost-saving claim. The raw wire observations establish actual effort and physical main dispatches.

This is a deliberately narrow, synthetic test. The gate's lack of benefit here does not prove it can never help harder tasks. The audit failures do not justify an automatic optimizer. No C2 authorization, cache freshness, source, scope, or completeness behavior changed. Abstained records remain held for their existing caller to handle; this PR does not add an automatic main-model recovery stage.

## Reproduce and inspect

- `python evaluation/decision_quality/summarize.py` recomputes `summary.json` from the committed `observations.json` without a provider call.
- `provenance.json` binds the immutable observation export to frozen fixtures, original source snapshots, Git revisions/trees, and the candidate patch. The replay checks those hashes, exact row identities/counts, labels, and the production rubric before writing a summary; missing historical Git objects cause refusal. `frozen/` retains the original pre-call sources verbatim below two explicit archive-comment lines. Only historical formatting warnings are exempted in those archival copies; they are not runnable entry points. The maintained runners include post-run formatting cleanup and explicit baseline loading.
- Use the native Hermes Python environment and an authorized test profile with its configured OpenRouter credential to run `screen.py --output <fresh-directory>` and `workflow_compare.py --output <fresh-directory>`. The runners use the profile's secret scope; they never print credentials. Both pin baseline code with `git archive`; that commit must be available locally.
- `native_compare.py --output <fresh-directory>` uses `hermes --print-runtime-command`, snapshots the pinned release/main, constructs the evaluated confidence-gate candidate, and owns four isolated workers. Set `HERMES_HOME` to the authorized test profile before invoking it. This makes paid/live model calls.
- Every live output directory must be new. Local profiles, snapshots, and queue artifacts stay untracked. The committed observation export contains synthetic text and portable artifact references only.

The winning rubric is byte-for-byte the four criteria tested in the frozen cases. Severity names, score indices, priority mappings, workflow schema, confidence gates, and request budgets are preserved. Existing request-size planning accounts for the longer criteria automatically.
