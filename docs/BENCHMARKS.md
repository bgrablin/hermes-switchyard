# Hermes Switchyard benchmark results

The selector value report below was collected against source commit `7dc77c8` in the 0.5.4 codebase. This report-refresh commit changes documentation and the release allowlist only, leaving the plugin and selector collector source unchanged. The other feature-battery rows below—multi-skill, model route, assess, automatic routing, and computer use—remain **0.5.0-only**: their feature-battery measurements were collected at `c8e6008c6314e182fd7b100a30efb384db542ee8`, and computer-use DOM was re-bound to tip-main commit `a8dae196b0b9892eeb627829d97363ec3d4bb9c9` after #57+#58. Those rows were **not re-collected for 0.5.1, 0.5.2, 0.5.3, or 0.5.4**. Comparison arms vary by row. Human-readable benefits first. Hashes and reproduce steps are under [Proof](#proof). This page does not claim whole-agent improvement.

## Per-feature scorecard

| Feature | What this row measures | Comparison arm | Measured arm | Latency / cost (measured) | Fair A/B? |
| --- | --- | ---: | ---: | --- | --- |
| Skill pick | One right specialist skill for a task (`jev_skill_select`; refreshed for 0.5.4) | Lexical: 7/12 | **12/12** | p50 **166.2 ms**, p95 **245.9 ms**; **~$0.000055**/decision | Yes — frozen 24-task lexical vs live Jev |
| Multi-skill pick | Finish a task that needs several skills (`jev_skill_select_many`) | One-skill API (`jev_skill_select`): 0/5 sets | **5/5** sets (mean coverage 1.0) | p50 **253 ms**, p95 **352 ms**; **$0.000347** for 5 | Yes — same 5 frozen multi-skill tasks; comparison arm is still a Switchyard API |
| Model route | Recommend a model + auditable receipt (`jev_model_route`); ships in 0.5.0 | no Switchyard recommendation | **3/3** recommend; `applied: false` (Hermes does not switch yet) | p50 **164 ms**; **$0.000067** for 3 | Partial — agrees with code-owned local filter; not a Hermes auto-picker |
| Assess | Small typed multiple-choice check (`jev_assess`) | First-option baseline: 2/3 | **3/3** | p50 **212 ms**; **$0.000040** for 3 | Weak baseline only (n=3 smoke) |
| Automatic skill routing | Pre-model skill hint, local match on vs off | silent when off | 1/2 needed; no-fit stays silent | ~1–2 ms local | Yes for hook on/off; not whole-agent |
| Computer use | Browser/desktop goal progress (`jev_computer_use` DOM) | stock A/B **pending** Session-1 GUI | Felidae: 1 click; local `url_contains` ok; **`goal_verified: false`** (dual-gate) | Jev **365 ms**; ~**$0.00021** | **Unavailable** — stock Session-1 GUI arm pending; do not treat Switchyard-only as A/B |

Rows kept off the install scorecard: **needed-skill failures 11→5** is reported within the refreshed selector benchmark; **false skill suggestions 0→0** is a no-delta safety check. The multi-skill, model-route, assess, automatic-routing, and computer-use measurements in the feature battery are historical **0.5.0-only** evidence. Hermes does not apply the model-route recommendation in that feature-battery measurement (`applied: false`).

## Skill select (frozen 24-task live value bench)

**Plain English:** Jev got **12 of 12** single-skill decisions right. The local word matcher got **7 of 12**. On positive-skill tasks, Jev missed **5/18** (lexical: **11/18**) and abstained **4/18** times (lexical: **7/18**). No-fit false positives were **0/6 for both arms**. Required-set and ambiguous cases remain separate measures; one-skill pick is not a multi-skill set-completion test.

| Metric | Local lexical | Live Switchyard | Paired change |
| --- | ---: | ---: | ---: |
| Strict single-skill top-1 | 7/12 (58.33%) | 12/12 (100.00%) | +41.67 pp |
| Failed to pick a needed skill | 11/18 (61.11%) | 5/18 (27.78%) | −33.33 pp |
| Positive abstentions | 7/18 (38.89%) | 4/18 (22.22%) | −16.67 pp |
| No-fit false positives | 0/6 (0.00%) | 0/6 (0.00%) | no change |
| Candidate coverage | 18/18 | 18/18 | no change |
| Ambiguous accepted | 0/1 | 1/1 | +1 case |

Observed Switchyard provider timing/usage (claimable; every case has a live receipt):

- Provider p50: **166.2 ms** · p95: **245.9 ms**
- Total provider time: **4,326.9 ms** · wall: **4,335.677 ms**
- Tokens: **31,212** in / **3,972** out
- Jev PAYG: **$0.0013109** for 24 cases (~**$0.000055**/decision)

**Interpretation:** live Switchyard improves strict single-skill selection and reduces times a needed skill was missed versus the local lexical fallback without raising no-fit false positives. Multi-skill tasks are measured under [`jev_skill_select_many`](#multi-skill-jev_skill_select_many), not by scoring one-skill pick on set completion.

The dataset hash is unchanged from the historical public freeze (`97a7702c…`). The plugin and collector hashes in [Proof](#proof) bind this refreshed selector measurement to source `7dc77c8`; the subsequent documentation/allowlist-only report-refresh commit leaves them unchanged.

## Multi-skill (`jev_skill_select_many`)

Same **5** frozen required-set tasks as the selector bench.

| Arm | Multi-skill sets complete | Mean coverage | p50 latency | Total cost |
| --- | ---: | ---: | ---: | ---: |
| Comparison: one-skill API (`jev_skill_select`, still Switchyard) | **0/5** | 0.10 | ~165–184 ms | $0.000273 |
| Measured: `jev_skill_select_many` | **5/5** | **1.00** | **253 ms** | $0.000347 |

**Interpretation:** when a task needs several skills together, `select_many` completes the set (**5/5**). One-skill pick is the wrong tool for that job (**0/5** on the same tasks) — that contrast is why this row exists, not a claim that one-skill pick will ever score set completion.

## Model pick (`jev_model_route`)

Three public-synthetic routing tasks (terminal / web / vision) with an explicit approved candidate set.

| Arm | Correct | Notes |
| --- | ---: | --- |
| Without: local cheapest-qualified filter | 3/3 | Code-owned metadata filter; ~0.01 ms; no provider call |
| With: `route_model` + Jev capability fit | 3/3 | p50 **164 ms**; **$0.000067** total |
| `recommend_model_route` adapter receipt | selected/abstain varies by registry shape | **`applied: false`** always on this Hermes generation |

**Caveat:** when candidate metadata already encodes cost and capabilities completely, the local filter and Jev agree. This microbench does **not** show Jev beating Hermes’ default model picker (that picker was not the without arm). The install value is an auditable recommendation that will not silently change the active model (`applied: false` until an apply seam exists).

## Assess (`jev_assess`)

Tiny typed Choice smoke (n=3: sky color, 2+2, Earth-is-planet).

| Arm | Correct | p50 latency | Total cost |
| --- | ---: | ---: | ---: |
| Without: first-criteria baseline | 2/3 | n/a | $0 |
| With: live Jev Choice | **3/3** | **212 ms** | $0.000040 |

**Caveat:** weak without baseline; shows the tool answers, not that assess beats every alternative model.

## Automatic skill routing

Install-default path: `AutomaticSkillRecommender` **`local_only`** vs **`off`** on three public tasks (docker, postgres, no-fit). No hosted Jev.

| Arm | Behavior |
| --- | --- |
| Without (`off`) | Always silent (3/3) — never invents a skill |
| With (`local_only`) | docker matched; postgres abstained; no-fit stayed silent (1/2 positives) |

**Caveat:** not a counterbalanced Hermes-session outcome study. Hosted `hosted_sanitized` + `load` adoption additionally needs acknowledgement and a host `turn_egress_policy` allow envelope.

## Computer use (`jev_computer_use`)

### Switchyard DOM (re-measured on tip-main dual-gate)

Public Cat → Felidae Wikipedia race via `run_browser_goal` on tip-main `a8dae19` (includes [PR #57](https://github.com/bgrablin/hermes-switchyard/pull/57) dual-gate + #58). Artifact: [`computer-use-goal-verified-felidae.json`](benchmarks/computer-use-goal-verified-felidae.json).

| Field | Value |
| --- | --- |
| tip | `a8dae19` (`origin/main`) |
| status | `completion_candidate` |
| clicks / Jev requests | 1 / 1 |
| Jev latency | **365.0 ms** |
| operation elapsed | **2232.0 ms** |
| Jev cost | ~**$0.00021** |
| `computer_use_dispatches` | **0** (DOM path; no Hermes `computer_use` between clicks) |
| `goal_verified` / `verified` | **false** / **false** |
| `verification_owner` | `coordinator` |
| `completion_source` | `local_predicate` |
| completion predicate | `url_contains: Felidae` satisfied locally |
| dual-gate note | Local-predicate early-stop does **not** self-certify. Dual-gate requires Hermes `DONE` (`provider_decision`) **and** a satisfied local condition → `verification_owner: hermes_and_url`. |

### Stock Hermes `computer_use` A/B

**Pending** a fair Session-1 interactive GUI run of stock Hermes `computer_use` on the same Cat→Felidae public goal. Do not invent a latency/cost delta. Fair A/B is **unavailable** until that stock arm runs. Switchyard DOM arm above is measured with **`goal_verified: false`** under dual-gate.

### Windows headed operator notes (qualitative + short-race receipt)

A concurrent Windows validation (Hermes 0.21.x) showed `jev_computer_use` callable when toolsets are pinned. Short Cat → Felidae race recorded in the feature battery: **1 click**, final URL Felidae, status `completion_candidate`, **`goal_verified: false`** / **`verified: false`** (do not conflate with the Linux tip-main DOM receipt above). Pinning only `computer_use` without `hermes_switchyard` produced `Unknown toolsets: hermes_switchyard`. A longer scenic Pizza → United Nations race was operator-observed to run many clicks then stall before UN; that long-race receipt was **not** retained on this metrics box, so it is **not** hash-bound here.

## Proof

### Live selector value report (source `7dc77c8`)

| Field | Value |
| --- | --- |
| Report | [`live-selector-7dc77c8.json`](benchmarks/live-selector-7dc77c8.json) |
| Report hash | `fd2caa911d4c9bc5766558776fa809b2c27564115c496713be58a67a94938ffc` |
| Plugin source hash | `94741a2b6762f32d01f8eb35cf221a9ea546dcc55d004e43fbdabc18266d96f4` |
| Collector source hash | `f9027bc10c214ba744a1aecaeeb388a00cef28c08e0c1ba410ade4c9d539d07b` |
| Dataset hash | `97a7702c0fa0474a8b13e8b018f4a1c86d4b9f84cbe5cba4c5d7122df73b6c28` |
| Catalog hash | `d16e9d6e2c6850b9624083f7102004bea3ba9f6b3c0db8dd51988b544df8c070` |
| Report source hash | `0a83cac98482aaded5ae87dc707bdb97236c99f8e3dabcbaf31d8721cc20f466` |
| Jev model | `typesafe/jev-1.13-20260917` via OpenRouter |
| Provider calls | 24; report status `ok` |

Earlier selector summaries remain available for historical comparison: [`live-selector-c8e6008.json`](benchmarks/live-selector-c8e6008.json) and [`live-selector-c6d9b28.json`](benchmarks/live-selector-c6d9b28.json). Each report is bound to its own plugin/collector hashes; do not mix hashes when citing results.

### Feature battery artifact

[`feature-battery-c8e6008.json`](benchmarks/feature-battery-c8e6008.json) holds the historical **0.5.0-only** multi-skill, model-route, assess, automatic, and computer-use rows. Its `plugin_tips` metadata records the selector/feature-microbench source tip as `c8e6008` and computer-use DOM tip as `a8dae19` (see the artifact). The older selector-only report at `c8e6008` is linked above; the refreshed selector-only evidence is the separate `7dc77c8` report.

### Reproduce selector value report

```text
python3 evaluation/benchmark/collect_switchyard.py \
  --live \
  --public-synthetic-ack \
  --max-requests 24 \
  --plugin-path . \
  --output switchyard.records.json

PYTHONPATH=evaluation/benchmark:. python3 evaluation/benchmark/value_report.py \
  --plugin-path . \
  --switchyard-input switchyard.records.json \
  --public-synthetic-ack \
  --max-requests 24 \
  --output value-report.json
```

The report refuses partial case sets, missing acknowledgement, dataset/catalog/source mismatches, failed provider rows, simulated rows, missing provider timing, or request-cap violations. Collectors resolve `OPENROUTER_API_KEY` through Hermes’ secret scope and never print it.

## Boundaries

- Selector and feature microbenches are not proof of whole-agent task improvement.
- The lexical / first-option / local-filter arms are deterministic local baselines, not competing hosted models (except where noted).
- Strict top-1, multi-skill, no-fit, ambiguity, model-route, assess, automatic, and computer-use metrics stay separate. No aggregate “awesomeness score.”
- Provider confidence is uncalibrated and is not correctness probability.
- Dual-gate: `goal_verified` / `verified` are **true** only when Hermes agreed `DONE` (`completion_source: provider_decision`) **and** a local completion condition is satisfied (`verification_owner: hermes_and_url`). A `local_predicate` early-stop may still be `completion_candidate` but keeps both flags **false**. Provider `DONE` without a satisfied condition stays unverified (`verification_owner: coordinator`).
- Results bind to the hashes above. Re-run when plugin source hash drifts.
