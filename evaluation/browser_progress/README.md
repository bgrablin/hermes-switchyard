# DOM Progress and Recovery evaluation (F2)

This directory holds the offline evaluation for the opt-in DOM Progress and
Recovery feature (`browser_progress_mode: advisory_stop`). The evaluation checks
wiring and policy with scripted Jev answers. It does not measure model accuracy
or end-to-end utility.

## Files

| File | Content |
| --- | --- |
| `evaluate.py` | The harness. It runs arms A, B, C, and C_off and applies the thresholds. |
| `fixtures.json` | 40 synthetic public DOM traces: 8 development cases and 32 held-out cases. |
| `fixtures.lock.json` | The SHA-256 hashes of `fixtures.json` and `live.json`, frozen before any run. |
| `live.json` | Four public goals for an optional capped live run. It holds no provider output. |

The harness refuses to run if a fixture hash does not match the lock.

## Arms

- **A**: the plugin is not used. The fixed action plan of each case replays on
  the fake browser with no Jev request. A is a safety control only.
- **B**: `run_browser_goal` from the `v0.5.4` tag (from `git archive`).
- **C**: `run_browser_goal` from this tree with `progress_mode="advisory_stop"`.
- **C_off**: this tree with the default `off` mode.

B, C, and C_off use a real `DecisionClient`. Only its HTTP exchange is replaced
by a scripted fake. Request building, response validation, 429/529 retries, and
accounting are the shipped code. Each arm runs in its own interpreter.

## Run

Offline (default, no network):

    python3 evaluation/browser_progress/evaluate.py --output /path/report.json

Use `--baseline-root <tree>` to compare against a different baseline source
tree. The exit code is 0 only when all of the held-out thresholds pass and
C_off sends no feature question.

Do not run `--live` without separate approval. It uses the configured Jev
route and a real browser, with a cap of 24 physical requests.

## Predeclared thresholds

These values were frozen before any run. A failed gate keeps the feature off.
The values do not change after a run.

1. Zero false completions and zero destination bypasses in C.
2. Zero semantic stops on productive held-out traces.
3. C completes at least every case that B completes.
4. C stops early, with fewer dispatched actions than B, on at least 4 of the 8
   held-out semantic stalls.
5. Zero added physical Jev requests on valid steps (each step with no injected
   fault uses one request).
6. Paired step latency: C p50 is at most B p50 plus 15%, and C p95 is at most
   B p95 plus 300 ms.
7. Paired known cost: C is at most 1.15 times B.
8. Zero caller-value leaks in C request bodies.
9. C_off sends no feature question.

Latency is the measured local loop time plus a modeled 250 ms per physical
request. Cost is a synthetic price that follows request bytes (bytes / 4 x
2e-7 USD).

## Offline result

Fixtures `f4ceb2728f713d0c4988c8abcf19546c37f8e7f612c423385038b764691ce299`
(v1, frozen 2026-09-27T14:48:56Z). Baseline B is `v0.5.4` at `5e2b878`.

| Check | Development | Held-out |
| --- | --- | --- |
| False completions or bypasses | 0 | 0 |
| Premature stops on productive traces | 0 | 0 |
| Completion regressions compared with B | 0 | 0 |
| Early stops on labelled stalls | 2 of 2 | 6 of 8 (gate: 4) |
| Added physical requests on valid steps | 0 | 0 |
| Paired p50 step latency, B / C (ms) | 250.28 / 250.42 | 250.26 / 250.32 |
| Paired p95 step latency, B / C (ms) | 250.42 / 251.54 | 250.31 / 251.39 |
| Paired known cost, C / B | 1.140 | 1.129 |
| Caller-value leaks in C | 0 | 0 |
| C_off feature questions | 0 | 0 |

Held-out totals: B dispatched 159 actions with 146 physical requests. C
dispatched 135 actions with 130 physical requests. C_off sent the same request
bytes as the pre-feature tree on all 40 cases.

## Limits

- The Jev answers are scripted. The result shows that the loop acts correctly
  on the given answers. It does not show that Jev gives those answers.
- Latency does not include provider time that depends on tokens.
- Cost is modeled from request size. Null cost is not counted as zero.
- The two held-out stalls without an early stop (`ho-stall-7` and
  `ho-stall-8`) have scripted answers that are not confident. The loop then
  keeps the baseline behavior, as designed.
- A live run is separate. It needs its own approval and its own report.
