# Prospective Switchyard value evaluation

**In short:** this page holds the rules a new feature has to pass before it can ship or become a default.

- **Pick one goal up front:** *faster* (same results, at least 10% quicker) or *more capable* (better results, at most 15% slower).
- **Freeze the test plan** before any live call.
- **Measure whole conversations** against Hermes without the plugin and against current `main`.
- **No cherry-picking afterwards.**

This policy applies to new, frozen evaluations after its adoption. It does not
reclassify old studies, erase failed candidates, or declare existing work proven.
Cost remains reported with unknown values explicit; feature selection prioritizes
outcomes, latency, reliability, and automatic usefulness.

## Declare one primary claim before live calls

- Efficiency: preserve correct completions and avoid new incorrect or unsafe actions;
  reduce both median and total end-to-end time by at least 10% versus plugin-disabled
  Hermes and current main. Report current release too. Nearest-rank p95 may not regress
  by more than 10%. Small samples remain pilot evidence, not broad performance proof.
- Capability: improve a predeclared correctness/completion metric versus both controls;
  mean and p95 latency may increase by at most 15%. A different budget requires an
  explicit prospective justification. Never infer quality from model confidence alone.

Record the primary claim, exact source versions, models/providers and reasoning caps,
public/synthetic casebook, checker, repetitions, arm order, budgets, exclusion rules,
and all script hashes before any measured call. Freeze each new plan separately.
No post-hoc subset can turn a failed whole-workload acceptance into a pass.

## Whole-conversation evidence

Use native Hermes dispatch and the real configured provider, including Jev through
OpenRouter. Keep operator credentials in their authorized scope. Use separate temporary
profiles and fixture directories outside Git repositories. Verify cwd and tool paths;
refuse access outside fixtures. Count all model requests, retries, failures, tool rounds,
fallback, and unknown costs. Preserve raw records and independent grading evidence.

Compare disabled Hermes, current release, and current main. For an individual feature,
include a same-source control with only that feature toggled. Do not attribute another
feature's effect to the tested feature. Record unsupported or ineligible work as part
of the workload, and measure needless calls on no-benefit turns. Publish repeated-case
results and limitations; user-task-derived fixtures must first be deliberately sanitized.

## Promotion

Passing a small synthetic pilot supports further qualification. Default-on promotion
requires useful coverage, stable outcomes and latency across representative tasks, and
existing supported Hermes extension points. No feature may depend on a pending upstream
source change. Keep failed or inconclusive paths out of the automatic defaults.
