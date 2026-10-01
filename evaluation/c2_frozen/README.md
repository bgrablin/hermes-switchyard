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

## Review follow-up

The follow-up after Copilot review incorporates a closed allowlist (`read_file`, `browser_snapshot`) and active-mutation/generation synchronization. All other tool names dispatch and invalidate all scopes. These are additional safeguards beyond the historical benchmark revision; no new live comparison is claimed for this combination.

Future receipts include the reviewed source-manifest hash and freeze digest. The worker verifies installed plugin bytes before each job, and both the campaign and analyzer reject missing or mismatched provenance. The original raw rows have global-manifest provenance only and remain unchanged in the evidence archive; use the archived analyzer for that historical run. Do not add invented per-row hashes to historical receipts. The updated workload uses eligible native read names; the historical benchmark used a trusted synthetic MCP callback.

The next review also requires the gate to prove it is last in the actual native execution-chain snapshot. If that private host seam is unavailable or another middleware follows it, the call dispatches and is not cached. Authoritative failed-call status invalidates all scopes even without evidence or scope metadata. The analyzer hashes the exact freeze bytes before accepting any receipt. These follow-ups are covered by offline regressions and are not new live benchmark results.

For a separately authorized future campaign, set `SWITCHYARD_C2_HERMES_ROOT` to the reviewed Hermes checkout and `SWITCHYARD_C2_PROFILE` to its isolated evaluation profile. Optionally set `SWITCHYARD_C2_PYTHON` to that runtime's interpreter; it defaults to the invoking interpreter. The runner refuses to overwrite an existing freeze. The archived original runner retains its exact historical bytes.
