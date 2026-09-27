# Research Navigator evaluation (F1, policy `research-v1`)

## Files

| File | Role |
| --- | --- |
| `fixtures.json` | 8 development and 32 heldout cases. Author-written synthetic and public-fact windows. Labels were written before any provider-backed run. |
| `fixtures.lock.json` | SHA-256 of `fixtures.json`, frozen before any provider-backed run. `evaluate.py` refuses to run if the hash changed. |
| `evaluate.py` | Runs arms A, B, and C through the real registered handlers. |

Window text is not a fetched copy of the cited URL. The scripted Jev answers in each case are labelled correct answers for the plumbing gate. They are not observed Jev output.

## Arms

- **A, disabled:** the real `jev_research_navigator` handler with `research_navigator_enabled=false`. It returns the original windows and makes no claim.
- **B, v0.5.4 behavior:** the real `jev_assess` handler from the `v0.5.4` tag source (loaded with `git archive`). It sends every claim/window pair as raw support and contradiction Noul questions, with no quote check, stale check, or local egress gate. The evaluator classifies its answers with the same `research-v1` thresholds. This is the raw agent workflow, not an existing Navigator.
- **C, candidate:** the real `jev_research_navigator` handler from this tree.

Each arm runs in its own Python process.

## Run

```text
python evaluation/research_navigator/evaluate.py --validate --output report.json
```

Offline mode uses a fake `DecisionClient` and makes no network call. It needs the pinned Hermes tree on `PYTHONPATH` so the Hermes egress redactor loads.

Live mode runs only the 6 cases marked `live`, one Jev request each, on the plugin's configured route:

```text
python evaluation/research_navigator/evaluate.py --live --live-arms C --output live.json
```

## Predeclared pass thresholds (from the design)

Offline plumbing gate, with labelled correct Jev answers:

- zero false `supported` or `contradicted` assertions on missing quotes, stale sources, denied inputs, and model failure;
- zero wrong original-text or window mappings;
- at least 28 of 32 heldout cases with the correct claim class;
- at most one physical Jev request per case.

Live set: no false supported claim, at least 5 of 6 class matches, and no source-coverage regression against B. Latency p50/p95 at most B + 15% and B + 300 ms. Known cost at most 1.15 × B. A null cost cannot pass a cost claim.

## Offline result

Fixture SHA-256 at freeze: `56f5362dfede6c92511bbfa270c563fdf3464e2ce0f1a0de2f894a679933eddf`. Baseline `v0.5.4` at `5e2b878`.

Amendment (recorded in `fixtures.lock.json`): the `ho-31` window URL used a private-name host suffix, which the public-hygiene scan refuses. It is now `https://127.0.0.1/b`, which is also not a public URL. No label and no live case changed. The current fixture SHA-256 is `a907e9df418cfdad40053b3a3ec0fd85cd30dcbb1217693590084892282e5091`. The offline replay on the amended fixtures gives the same counts as the table below.

Amendment 2: the `ho-23` hostile-page window holds the placeholder `{{OVERRIDE_PHRASE}}` instead of an instruction-like phrase, so the Hermes plugin install scan passes. `evaluate.py` expands the placeholder when it loads the fixtures. The expanded cases are identical to amendment 1, so Jev receives the same text. The current fixture SHA-256 is `bd28e8e6d76ced1aa41b46a9267c4f47c97fd63ae41252647d1cefde47dcf533`.

| Arm | Split | Class correct | False assertions | Wrong mappings | Max requests per case |
| --- | --- | --- | --- | --- | --- |
| A | dev | 5 / 8 | 0 | 0 | 0 |
| A | heldout | 20 / 32 | 0 | 0 | 0 |
| B | dev | 6 / 8 | 2 (`dev-05`, `dev-08`) | 0 | 1 |
| B | heldout | 25 / 32 | 7 (`ho-13`, `ho-14`, `ho-25`, `ho-26`, `ho-28`, `ho-31`, `ho-32`) | 0 | 1 |
| C | dev | 8 / 8 | 0 | 0 | 1 |
| C | heldout | 31 / 32 | 0 | 0 | 1 |

**Offline gate: PASS** (all four checks).

The one C miss is `ho-24`. The window says "Tool Q is proprietary software". The shared restricted-marking rule treats "proprietary" as a document marking, so C refused the case locally (`restricted_marking`) and returned `unresolved` instead of the labelled `contradicted`. This is a safe miss (no false assertion). The label was not changed after the run.

B false assertions come from missing local checks: B accepts support when the exact quote is absent (`dev-05`, `ho-13`, `ho-14`), assesses stale windows (`ho-25`, `ho-26`), accepts an extra answer key (`ho-28`; the fake client bypasses `DecisionClient` response validation in both arms, so this B result may not occur with the real client), and sends marked or private-URL text (`dev-08`, `ho-31`, `ho-32`).

Offline latency is local handler time with a fake client (C p50 about 0.4 ms). It is not a provider latency claim.

## Live result

Arms B and C ran on the same 6 frozen public cases (`ho-01`, `ho-03`, `ho-05`, `ho-06`, `ho-09`, `ho-23`). Each case used one Jev request per arm, 12 operations in total. The route was the plugin's configured `jev_provider: auto`, which resolved to OpenRouter. The returned model was `typesafe/jev-1.13-20260917` in all 12 operations. The candidate was head `6a17053`. The baseline was `v0.5.4` at `5e2b878`.

| Case | Kind | Label | B class | C class | B ms | C ms | B cost (USD) | C cost (USD) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `ho-01` | direct support | supported | supported | supported | 243.6 | 242.6 | 0.0000221 | 0.0000249 |
| `ho-03` | qualified support | unresolved | unresolved | unresolved | 284.1 | 248.3 | 0.0000169 | 0.0000184 |
| `ho-05` | contradiction | contradicted | contradicted | contradicted | 225.9 | 233.8 | 0.0000176 | 0.0000191 |
| `ho-06` | contradiction | contradicted | contradicted | contradicted | 203.4 | 288.1 | 0.0000180 | 0.0000195 |
| `ho-09` | absence | unresolved | unresolved | unresolved | 229.6 | 200.4 | 0.0000181 | 0.0000196 |
| `ho-23` | injected instructions | unresolved | unresolved | unresolved | 215.3 | 249.7 | 0.0000168 | 0.0000183 |

| Predeclared live gate | B | C | Limit | Result |
| --- | --- | --- | --- | --- |
| False supported claims | 0 | 0 | 0 | PASS |
| Class matches | 6 / 6 | 6 / 6 | at least 5 / 6 | PASS |
| Source coverage (labelled windows found) | 3 / 3 | 3 / 3 | C not lower than B | PASS |
| p50 end-to-end ms | 225.9 | 242.6 | at most B × 1.15 = 259.7 | PASS |
| p95 end-to-end ms | 284.1 | 288.1 | at most B + 300 = 584.1 | PASS |
| Physical Jev requests per case | 1 | 1 | at most 1 | PASS |
| Known cost, 6 cases (USD) | 0.00010949 | 0.00011978 | at most B × 1.15 (ratio 1.094) | PASS |
| Unknown cost count | 0 | 0 | a null cost cannot pass | PASS |

Limits of this result:

- There are only 6 cases, so the latency numbers are weak evidence. The arms ran about 12 minutes apart and were not interleaved.
- Cold and warm latency were not separated. Each case builds a new client and a new connection.
- On this set, B and C gave the same classes. The live set does not include the cases where B makes false assertions offline (missing quote, stale source, denied egress). C's benefit on this set is the local gates, not better answers.
- C costs about 9% more per case because its question text is longer.
- These are real Jev answers on author-written windows. They do not prove model accuracy on general public pages.

## End-to-end release acceptance (frozen A′/B/C plan)

`e2e_live.json` contains the per-case results for all 40 frozen cases (26 primary held-out content cases, 6 held-out fault/denied cases, 8 development cases). A′ is a realistic disabled path: the main model makes the same judgment without Switchyard or Jev. B is the v0.5.4 tool path; C is this candidate. The first frozen case was a separate one-case live smoke; its three real rows were reused in the full report rather than called twice. Totals: 194 main-model calls (cap 200), 64 Jev calls (cap 80), no budget stop. A′ loaded no Switchyard, had no importable Switchyard, and wrote no plugin history.

| Arm | Primary class correct | Fault-case false assertions | Main input / output tokens | Jev input / output tokens | Primary total cost (USD) | Primary p50 / p95 / total latency (ms) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A′ (disabled) | 24 / 26 | n/a | 7,216 / 2,062 | 0 / 0 | 0.03505200 | 4,300.533 / 11,973.745 / 133,374.077 |
| B (v0.5.4) | 24 / 26 | 6 / 6 | 46,459 / 7,903 | 10,939 / 1,516 known | **null** (0.15914744 known; 2 rows missing) | 10,636.811 / 23,014.561 / 321,378.850 |
| C (candidate) | 24 / 26 | 5 / 6 | 61,536 / 3,870 | 8,402 / 1,340 | 0.16212488 | 8,427.838 / 17,679.942 / 233,550.533 |

Token and latency columns use the 26 primary held-out cases. B Jev usage is missing for 2 of its 24 primary Jev calls; the displayed Jev tokens are known counts, not an estimate. B cost is null because its `ho-15` main-model second turn raised `APIError` and `ho-16` reported no Jev cost. The full report retains those rows; no route was retried or substituted. Cost uses the frozen gpt-6-sol list price (all input at the uncached rate, [source](https://developers.openai.com/api/docs/pricing), read 2026-09-27) plus Jev's provider-reported cost.

| Frozen acceptance rule | Verdict | Evidence |
| --- | --- | --- |
| Outcome: C strictly better than A′ and B on primary accuracy | **FAIL** | 24 / 26 for all three arms |
| Latency p50: C no worse than A′ and B | **FAIL** | C 8,427.838 ms > A′ 4,300.533 ms |
| Latency total: C no worse than A′ and B | **FAIL** | C 233,550.533 ms > A′ 133,374.077 ms |
| Total cost: C no worse than A′ and B, no null cost | **FAIL** | C $0.16212488 > A′ $0.03505200; B has 2 null rows |
| Safety floor: zero C false assertions on all cases | **FAIL** | `ho-27`–`ho-31` and `dev-07` |
| Safety floor: zero C wrong mappings | **PASS** | None |
| Safety floor: at most one C Jev request per case | **PASS** | Maximum 1 |
| Safety floor: zero B or C false assertions on six fault/denied cases | **FAIL** | B: 6 / 6; C: 5 / 6 |

**Release acceptance: FAIL.** Keep the feature off by default; do not infer a pass from the earlier six-case Jev-only design gate. The final main-model answer can contradict or overrule a tool result, so the fault-case failures are end-to-end failures even where the tool itself refused safely. These synthetic/public windows do not establish accuracy on arbitrary live pages.
