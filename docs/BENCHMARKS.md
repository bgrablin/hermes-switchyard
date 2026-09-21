# Hermes Switchyard benchmark results

This page reports repository-owned evidence for Hermes Switchyard. It separates live provider observations from deterministic local baselines and does not claim whole-agent improvement.

## Plain-English result

Jev got all **12 of 12 single-skill decisions right**. The local word matcher got **7 of 12** right. Across the 18 tasks that needed a skill, Jev reduced failures from **11 to 5**, while both approaches avoided false recommendations on all 6 tasks that needed no skill.

The live run cost about **$0.000055 per decision** at a typical latency of **0.20 seconds**. The result shows clear value for choosing one specialist skill. It does not show value for multi-skill planning; that remains a known gap.

## Live selector value benchmark

**Candidate:** `c6d9b286566cf36696e746d64600c041cf40b838`  
**Dataset:** 24 frozen public-synthetic heldout cases  
**Jev model:** `typesafe/jev-1.13-20260917` through OpenRouter with provider fallback disabled  
**Provider calls:** 24 successful calls, 0 errors  
**Report:** [`live-selector-c6d9b28.json`](benchmarks/live-selector-c6d9b28.json)  
**Report hash:** `959899acf6ad792e0e5622357444d07d03baea427c603ddd5e0ca75f741cf3ca`

The comparison uses the same frozen task text, candidate catalog, and expected labels for both arms:

- **Local lexical baseline:** deterministic token-overlap policy; no provider call.
- **Switchyard:** real `DecisionClient` calls and actual Jev responses from the exact candidate source.

### Results

| Metric | Local lexical | Live Switchyard | Paired change |
| --- | ---: | ---: | ---: |
| Strict single-skill top-1 | 7/12 (58.33%) | 12/12 (100.00%) | +41.67 percentage points |
| Positive misses | 11/18 (61.11%) | 5/18 (27.78%) | -33.33 percentage points |
| Positive abstentions | 7/18 (38.89%) | 4/18 (22.22%) | -16.67 percentage points |
| No-fit false positives | 0/6 (0.00%) | 0/6 (0.00%) | no change |
| Candidate coverage | 18/18 (100.00%) | 18/18 (100.00%) | no change |
| Ambiguous accepted | 0/1 | 1/1 | +1 case |
| Required-set completion | 0/5 | 0/5 | no change |

### Observed latency and usage

These values are claimable only for the Switchyard arm because every case has a complete live provider receipt.

- Provider p50: **198.9 ms**
- Provider p95: **324.4 ms**
- Total provider time: **5,168.3 ms**
- Total wall time: **5,173.204 ms**
- Input tokens reported: **31,212**
- Output tokens reported: **3,966**
- Jev PAYG cost reported: **$0.0013109** total for 24 cases

### Interpretation

This run supports a narrow claim: **on this frozen selector dataset, live Switchyard materially improved strict single-skill selection and reduced positive misses versus the local lexical fallback without increasing no-fit false positives.**

It also exposes a real limitation: Switchyard is strict top-1, so it completed **0/5** multi-skill required sets. The plugin can recommend one skill well; it does not yet provide multi-skill planning. That limitation must remain visible rather than being averaged into a headline score.

## Reproduce

Collect live Switchyard receipts with an explicit public-synthetic acknowledgement and request cap:

```text
python3 evaluation/benchmark/collect_switchyard.py \
  --live \
  --public-synthetic-ack \
  --max-requests 24 \
  --plugin-path . \
  --output switchyard.records.json
```

Build the claim-gated value report:

```text
PYTHONPATH=evaluation/benchmark:. python3 evaluation/benchmark/value_report.py \
  --plugin-path . \
  --switchyard-input switchyard.records.json \
  --public-synthetic-ack \
  --max-requests 24 \
  --output value-report.json
```

The report refuses partial case sets, missing acknowledgement, dataset/catalog/source mismatches, failed provider rows, simulated rows, missing provider timing, or request-cap violations.

## Boundaries

- This is a selector benchmark, not proof of whole-agent task improvement.
- The lexical arm is a deterministic local baseline, not another hosted model.
- Strict top-1, no-fit, required-set, and ambiguity metrics remain separate. No aggregate score is emitted.
- Provider confidence is uncalibrated and is not reported as correctness probability.
- Results bind to the exact plugin, collector, report code, dataset, catalog, and resolved provider model hashes in the JSON report.
- A separate counterbalanced Hermes-session benchmark is required before claiming that automatic recommendations improve end-to-end agent outcomes.
