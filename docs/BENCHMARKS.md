# Hermes Switchyard benchmark results

Repository-owned with/without evidence for **0.5.0** tip `c8e6008`. Human-readable benefits first. Hashes and reproduce steps are under [Proof](#proof). This page does not claim whole-agent improvement.

## Per-feature scorecard

| Feature surface | Without | With | Latency / cost (with) | Fair A/B? |
| --- | ---: | ---: | --- | --- |
| `jev_skill_select` (top-1) | 7/12 | **12/12** | p50 **185 ms**, p95 **303 ms**; **~$0.000055**/decision | Yes — frozen 24-task lexical vs live Jev |
| Failed to pick a needed skill | 11/18 | **5/18** | (same run) | Yes |
| No-skill false positives | 0/6 | **0/6** | (same run) | Yes |
| Required-set via top-1 `select_skill` | **0/5** | **0/5** | (same run) | Yes — honest gap for top-1 API |
| `jev_skill_select_many` | top-1 **0/5** complete | **5/5** complete (mean coverage 1.0) | p50 **253 ms**, p95 **352 ms**; **$0.000347** for 5 | Yes — same 5 frozen required-set tasks |
| `jev_model_route` | 3/3 local cheapest-qualified | **3/3** recommend | p50 **164 ms**; **$0.000067** for 3 | Partial — local filter is code-owned metadata, not Hermes default picker |
| Model adapter receipt | n/a | `applied: **false**` | (adapter smoke) | Contract check on Hermes 0.19/0.21 |
| `jev_assess` | 2/3 first-option | **3/3** Choice | p50 **212 ms**; **$0.000040** for 3 | Weak baseline only (n=3 smoke) |
| Automatic routing (`local_only` vs `off`) | silent when off | 1/2 positives; no-fit stays silent | ~1–2 ms local | Yes for hook on/off; not whole-agent |
| `jev_computer_use` DOM | stock A/B **not run** | Felidae: 1 click, Jev **249 ms**, ~**$0.000213**, `goal_verified: false` | Switchyard-only labeled | No fair stock A/B on this pass |

## Skill select (frozen 24-task live value bench)

**Plain English:** Jev got **12 of 12** single-skill decisions right. The local word matcher got **7 of 12**. Across the 18 tasks that needed a skill, failures fell from **11 to 5**. Both arms avoided false recommendations on all **6** no-skill tasks.

| Metric | Local lexical | Live Switchyard | Paired change |
| --- | ---: | ---: | ---: |
| Strict single-skill top-1 | 7/12 (58.33%) | 12/12 (100.00%) | +41.67 pp |
| Failed to pick a needed skill | 11/18 (61.11%) | 5/18 (27.78%) | −33.33 pp |
| Positive abstentions | 7/18 (38.89%) | 3/18 (16.67%) | −22.22 pp |
| No-fit false positives | 0/6 (0.00%) | 0/6 (0.00%) | no change |
| Candidate coverage | 18/18 | 18/18 | no change |
| Ambiguous accepted | 0/1 | 1/1 | +1 case |
| Required-set completion (top-1 API) | 0/5 | 0/5 | no change |

Observed Switchyard provider timing/usage (claimable; every case has a live receipt):

- Provider p50: **185.0 ms** · p95: **303.3 ms**
- Total provider time: **5,195.0 ms** · wall: **5,204.376 ms**
- Tokens: **31,212** in / **3,966** out
- Jev PAYG: **$0.0013109** for 24 cases (~**$0.000055**/decision)

**Interpretation:** live Switchyard improves strict single-skill selection and reduces times a needed skill was missed versus the local lexical fallback without raising no-fit false positives. The top-1 API still cannot complete multi-skill required sets (**0/5**).

Dataset hash unchanged from the historical public freeze (`97a7702c…`). Plugin/collector hashes were refreshed on tip `c8e6008` because plugin source drifted; numbers were re-collected rather than reused from `c6d9b28`.

## Multi-skill (`jev_skill_select_many`)

Same **5** frozen required-set tasks as the selector bench.

| Arm | Required-set complete | Mean coverage | p50 latency | Total cost |
| --- | ---: | ---: | ---: | ---: |
| Without: `jev_skill_select` (top-1) | **0/5** | 0.10 | ~165–184 ms | $0.000273 |
| With: `jev_skill_select_many` | **5/5** | **1.00** | **253 ms** | $0.000347 |

**Interpretation:** multi-skill is no longer a hidden zero when callers use the dedicated API. The README’s older **0/5** figure remains true only for the **top-1** `select_skill` contract and must stay visible there.

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

### Switchyard DOM (measured on this metrics pass)

Public Cat → Felidae Wikipedia race via `run_browser_goal` on tip-main:

| Field | Value |
| --- | --- |
| status | `completion_candidate` |
| clicks / Jev requests | 1 / 1 |
| Jev latency | **249.2 ms** |
| operation elapsed | **2231.8 ms** (incl. ~1.4 s session setup) |
| Jev cost | ~**$0.000213** |
| `computer_use_dispatches` | **0** (DOM path; no Hermes `computer_use` between clicks) |
| `goal_verified` / `verified` | **false** / **false** (coordinator owns verification) |
| completion predicate | `url_contains: /wiki/Felidae` satisfied locally |

### Stock Hermes `computer_use` A/B

**Not run** on this pass. A fair without arm needs the same task, same model, and stock GUI/browser tool budget. Do not invent a latency/cost delta.

### Windows headed operator notes (qualitative + short-race receipt)

A concurrent Windows validation (Hermes 0.21.x) showed `jev_computer_use` callable when toolsets are pinned. Short Cat → Felidae race: **1 click**, final URL Felidae, status `completion_candidate`, **`goal_verified: false`**. Pinning only `computer_use` without `hermes_switchyard` produced `Unknown toolsets: hermes_switchyard`. A longer scenic Pizza → United Nations race was operator-observed to run many clicks then stall before UN; that long-race receipt was **not** retained on this metrics box, so it is **not** hash-bound here.

## Proof

### Live selector value report (tip `c8e6008`)

| Field | Value |
| --- | --- |
| Report | [`live-selector-c8e6008.json`](benchmarks/live-selector-c8e6008.json) |
| Report hash | `995db72de90ab2498dd157f48d3380ff326338d0a5d055662a28ac0810a6fba3` |
| Plugin source hash | `614b402d26be013d4290ce4f6e0ea57509eb71bccf575721ec8381fe3c7cf0d9` |
| Collector source hash | `0b31a15d1f7966a8f600ab825e7af1785cc6abfccf55e4284d7c79ca67afce34` |
| Dataset hash | `97a7702c0fa0474a8b13e8b018f4a1c86d4b9f84cbe5cba4c5d7122df73b6c28` |
| Catalog hash | `d16e9d6e2c6850b9624083f7102004bea3ba9f6b3c0db8dd51988b544df8c070` |
| Report source hash | `0a83cac98482aaded5ae87dc707bdb97236c99f8e3dabcbaf31d8721cc20f466` |
| Jev model | `typesafe/jev-1.13-20260917` via OpenRouter (fallback disabled) |
| Provider calls | 24 ok / 0 errors |

Historical report at the prior plugin hash: [`live-selector-c6d9b28.json`](benchmarks/live-selector-c6d9b28.json) (same dataset; do not mix plugin hashes when citing).

### Feature battery artifact

[`feature-battery-c8e6008.json`](benchmarks/feature-battery-c8e6008.json) holds the multi-skill, model-route, assess, automatic, and computer-use rows from this pass.

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
- `goal_verified` / `verified` remain false inside `jev_computer_use` until the coordinator checks.
- Results bind to the hashes above. Re-run when plugin source hash drifts.
