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

## Frozen end-to-end release evaluation

The signed `e2e_plan.json` fixes 32 held-out traces for A′ (the main model judges the optional progress questions while the scripted Jev handles base questions), B (v0.5.4), and C (this candidate). The separate live B/C run alternates on four public browser goals. Per-case results are in `offline.json`, `a_prime.jsonl`, and `live.jsonl`; `acceptance.json` contains the frozen rule verdicts. A′ used 41 gpt-6-sol calls (cap 197; 38 valid and 3 invalid replies counted as abstentions). The live browser run used 8 physical Jev requests (cap 24) and skipped no pair. No route fallback or result tuning was used.

| Held-out arm (32 traces) | Completion candidates | Stalls stopped early with suggestion (8 labelled) | Premature / false completions | Main input + cached input / output tokens | Jev tokens | Known cost (USD); null requests | Task p50 / p95 / total latency (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| A′ realistic judgment | 9 / 32 | 8 / 8 | 6 / 0 | 147,172 + 89,088 / 3,608 | not recorded | 0.35554465; 3 null | 24,808.789 / 41,977.754 / 712,030.096 |
| B v0.5.4 | 14 / 32 | 0 / 8 | 0 / 0 | 0 / 0 | not recorded | 0.00968885; 3 null | 1,251.042 / 1,751.786 / 37,534.288 |
| C candidate | 14 / 32 | 6 / 8 | 0 / 0 | 0 / 0 | not recorded | 0.00939425; 3 null | 1,003.142 / 1,752.888 / 33,574.221 |

The Jev exchange token counts are not captured by this harness; do not read “not recorded” as zero. A′'s main-model price is the frozen [gpt-6-sol standard list price](https://developers.openai.com/api/docs/models/gpt-6-sol) (retrieved 2026-09-27), including cached input and output; 21,857 auxiliary title-generation tokens are recorded but not priced. The held-out Jev costs are modeled from request bytes, not provider charges. All three arms have three null Jev costs caused by the frozen injected 429/529/outage faults. The frozen rule treats each null as a failure; it does not replace it with zero. These traces use scripted Jev answers and modeled request latency, not measured model quality or network latency.

| Live arm (4 public goals) | Completion candidates | Incomplete goals stopped early with suggestion | False completions | Main / Jev tokens | Provider cost (USD); null requests | Task p50 / p95 / total latency (ms) | Physical requests |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| B v0.5.4 | 2 / 4 | 0 / 2 | 0 | 0 / not recorded | 0.000399462; 0 null | 1,434.3 / 1,962.2 / 6,151.2 | 4 |
| C candidate | 2 / 4 | 0 / 2 | 0 | 0 / not recorded | 0.000342510; 0 null | 1,378.6 / 2,067.9 / 6,143.9 | 4 |

The live task times include real headless-browser setup and provider time. Both incomplete goals blocked at operation selection before the semantic-stall rule fired. `completion_candidate` is not independent goal verification. The p95 column is descriptive, not one of the frozen release gates.

| Frozen acceptance rule | Held-out verdict | Live verdict |
| --- | --- | --- |
| Outcome: C completes at least as many as each baseline, stops more labelled stalls early with a suggestion, and has zero premature stops and false completions | **FAIL**: C 6 early stops ≤ A′ 8 (C 14 completions, 0 premature/false) | **FAIL**: C 0 early stops ≤ B 0 (both 2 completion candidates) |
| Task p50 latency no worse than each baseline | **PASS**: C 1,003.142 ms ≤ B 1,251.042 ms and A′ 24,808.789 ms | **PASS**: C 1,378.6 ms ≤ B 1,434.3 ms |
| Task total latency no worse than each baseline | **PASS**: C 33,574.221 ms ≤ B 37,534.288 ms and A′ 712,030.096 ms | **PASS**: C 6,143.9 ms ≤ B 6,151.2 ms |
| Total cost no worse than each baseline, with no null cost | **FAIL**: three injected-fault nulls in each arm | **PASS**: C $0.000342510 ≤ B $0.000399462; no nulls |

**Release acceptance: FAIL.** Keep `browser_progress_mode` at its existing default `off`. The earlier offline design gate passed, but it was a scripted wiring gate and cannot override the frozen end-to-end rule failures.
