# C2 safeguards and frozen evaluation

Tested production revision: `073c91e9bf75a5b89491f112de7ad7c8f2c54923`.
Both session and task IDs, full source identity, trusted per-call freshness verification, and a complete result digest are mandatory. Stock Hermes lacks the verifier and dispatches. C2 remains off by default.

One frozen run: 60 native answer turns, 240 source-read requests. All seven prerequisites and 18 targeted regressions passed first. Receipt normalization was frozen before calls.

| Arm | Correct | Median | Total | Reuse | Invalid reuse |
|---|---:|---:|---:|---:|---:|
| Disabled | 20/20 | 2.063 s | 48.141 s | 0/80 | 0 |
| v0.5.6 | 20/20 | 7.473 s | 146.511 s | 0/80 | 0 |
| Candidate | 20/20 | 2.372 s | 58.934 s | 26/80 | 0 |

The original strict latency gate failed against disabled. After the run, the user accepted a modest slowdown if the feature provides useful behavior. Keep C2 as an opt-in candidate; real native source integration and user utility remain unproven. The harness supplies four real reads and a trusted local-file adapter, then native Hermes answers from actual read results. This is not an autonomous tool-loop benchmark.

Candidate performed 120 freshness-verification reads plus 54 actual tool reads. Baselines each performed 80 reads. All 60 main HTTP attempts reconcile with token ledgers. Each enabled arm made 20 Jev calls costing $0.000630924; C2 itself makes no Jev requests. Main subscription quota cannot be converted to reliable economic cost, so no cost-superiority claim passes.

Freeze SHA-256: `5560e08d431bf2b5a781650c147831f83f97fca048c5c7b02d892df94d7133cb`.
Raw evidence and source snapshots are retained alongside these scripts on the evaluation host. No additional live campaign was run.

PR162 advanced concurrently to `2deaa25034d6dcd8002d020731186de02c07c4cf`. Five additional offline checks still reproduced unsafe reuse for missing task/session, unversioned catalogs, caller-only snapshot IDs, and explicitly truncated results. This isolated branch does not modify that PR or deploy C2.
