# Jev transfer pilots

These are evaluation adapters, not registered Switchyard features. They test two ideas: combine compatible per-turn decisions, and route a request to another model through the existing Hermes request middleware.

No Hermes source patch, production configuration change, new default, release, or deployment is included. The existing severity-rubric improvement is already on main and is not claimed again here. Plan-once computer use remains outside these pilots.

## Implemented candidates

**Same-provider routing:** an isolated adapter qualifies a short, first-turn, tool-free Codex request with Jev, then changes the actual wire model from Sol to Luna when qualified. It composes model and effort changes in one middleware callback because separate callbacks receive the original payload. Existing provider credentials remain with Hermes. Failed, late, unsupported, or ineligible decisions keep Sol.

**Combined decisions:** an isolated adapter adds effort questions to the existing first-stage skill request, retains skill verification, and consumes a result only for the matching session, task, turn, sanitized text, provider, effort cap, deadline, and candidate ladder. The early hook does not expose the final effort cap, so the prototype asks separate medium-cap and high-cap questions. Other cases use the existing effort path.

Both adapters are installed only into the harness's temporary plugin copy. They use private Switchyard implementation details and are intentionally not production integration code.

## Results

| Trial / arm | Correct completed | Median turn | Total elapsed | Jev calls |
| --- | ---: | ---: | ---: | ---: |
| Routing / off | 24/24 | 6.940 s | 148.387 s | 0 |
| Routing / release | 24/24 | 2.094 s | 57.201 s | 46 |
| Routing / main | 24/24 | 2.199 s | 58.343 s | 47 |
| Routing / candidate | 24/24 | 1.856 s | 47.523 s | 70 |
| Consolidation / off | 24/24 | 9.713 s | 264.378 s | 0 |
| Consolidation / release | 24/24 | 5.534 s | 130.303 s | 47 |
| Consolidation / main | 24/24 | 5.612 s | 142.362 s | 47 |
| Consolidation / candidate | 24/24 | 4.539 s | 115.130 s | 24 |

Routing improved median latency by 15.6% and total elapsed time by 18.5% versus frozen main, switching 22/24 turns to Luna. Consolidation improved median latency by 19.1% and total elapsed time by 19.1%, consuming a shared decision in 23/24 turns and reducing Jev calls from 47 to 24. Both passed the frozen feasibility gate; neither is release-qualified.

Each complete native trial has 12 synthetic tasks, two repeats, and four arms: plugin disabled, v0.5.6, frozen main, and the candidate. All main-model requests use the configured Codex provider; Jev uses OpenRouter with the pinned model typesafe/jev-1.13-20260917. Conversations are interleaved in a frozen randomized order and dispatched serially within each trial. This does not establish exclusive use of Silver or the providers by other sessions.

The routing workload contains short fixed-answer text tasks and has no tools. Consolidation uses ten hidden-fact tasks whose answers exist in skill bodies and two exact-copy tasks. Only skills_list and skill_view are available. The profile contains 25 fixture skills; enabled plugin arms also expose Switchyard's operations skill. Memory, user profile, context files, conversation history, fallback models, and background review are disabled. Workspaces are beneath the trial directory, not physically separated machines.

The frozen pilot gate is no worse correctness than every control, at least 5% lower median and total elapsed time than disabled and frozen main, actual candidate use, and matching pre/post fingerprints of tracked Hermes Python files during the trial. Passing it is feasibility evidence, not general-release qualification. Repeated prompts are not independent real-world tasks, and small-sample tails are descriptive.

## Interpretation and limits

- Sharing a request changes the information available to Jev and can change its answers. Consolidation's native latency is a combined effect of request sharing and different reasoning-effort decisions. It is not a measurement of transport savings alone.
- The initial decision screen matched its skill labels 41/48 times with separate calls and 33/48 with either combined form. Several labels demanded skills for self-contained tasks, so abstention is not necessarily a task failure. A follow-up using existing hidden-fact fixtures produced 38/48 matches with separate calls and 41/48 with either combined form. The first screen failed its original skill-label gate. This mixed evidence motivated native testing; the failed screen was not discarded or relabeled. The native consolidation tasks repeat a subset of the follow-up fixtures, so they are not an untouched holdout.
- Native routing's v0.5.6 control appended a legacy receipt to all 24 answers. Its original exact-format score was 0/24; semantic scoring removes only that exact terminal receipt and yields 24/24. Original final text and scores are retained. The consolidation worker disables both legacy and current receipt settings.
- The first consolidation run was interrupted after 79/96 rows when remote access failed. It has no post-run runtime fingerprint and is excluded from qualification. The entire comparison was repeated. Hermes changed between those runs; each freeze records its own runtime revision. No rows are stitched across them.
- Native runtime fingerprints cover tracked Python files, not all dependencies, configuration, operating-system state, or provider behavior. Model routing and consolidation ran on different host revisions and different tasks; their speedups must not be compared directly.
- The original decision-screen summaries used a floor-index tail statistic. The published replay consistently uses nearest-rank p95. It re-derives task scores from the retained final answers or parsed decision fields, rather than trusting the original aggregate scores.
- Jev usage and known costs are retained with missing-cost counts. Hermes main-model usage fields remain as returned and are not summed into a savings claim. Cost was not a feature-selection gate.

Default enablement needs a production implementation and broader qualification. Routing still needs explicit model-pin semantics, user-visible routing receipts, tool/history/context compatibility, provider capability checks, and failure/retry handling across supported routes. Its no-tools eligibility excludes ordinary tool-enabled Hermes sessions. Consolidation still needs a clean shared-result interface, correct receipt/cost ownership, timeout and cancellation behavior, concurrent-session tests, and larger catalogs and cap/provider coverage. The synthetic harness's acknowledged-public inputs do not establish a general production egress policy.

## Inspect and reproduce

Run the offline replay and tests from the repository root:

```sh
python3 evaluation/jev_transfer/summarize.py
python3 -m unittest discover -s tests -p 'test_jev_transfer*.py' -v
```

observations.json contains the unmodified per-run freezes, ordered raw rows, and deduplicated aggregate runtime fingerprints with file counts. frozen/ retains the evaluated Python sources before formatting cleanup; two runner copies redact only the host-specific runtime path. source-snapshots.json records public hashes and, for those copies, distinct original hashes. provenance.json binds those exports. The detailed runtime path/hash inventories remain with the local trial outputs; public replay checks aggregate-fingerprint equality and counts. The confirmation wrapper and shared native worker launcher were not included in the original pre-call hashes; their archived bytes are explicitly retrospective bindings, not a claim of a complete pre-call dependency freeze.

The maintained live runners are evaluation/model_routing/compare.py and evaluation/turn_consolidation/native_compare.py. Set HERMES_HOME to an authorized evaluation profile with working Codex and OpenRouter credentials, then pass --output with a new directory. They invoke hermes --print-runtime-command, use pinned Git snapshots, and make live provider calls. Pass --hermes-root with the installed Hermes source directory; the maintained runners do not assume a host-specific path. The smaller screen.py and confirmation.py require the installed Hermes Python environment.

The checked-in code has formatting/import cleanup after the runs; the original hashes match the verbatim files under frozen/, except the two declared portable copies. Original-byte verification of those two runners requires the local originals; public replay verifies the portable copies and their declared original-hash bindings. Local profile directories, plugin snapshots, databases, logs, and credential sources are excluded from the publication. The release archive's existing allowlist excludes evaluation code and data.

Review hardened the maintained runners without rerunning or relabeling historical observations: non-string and non-text routing inputs are rejected; shared receipts must bind to a real call and wire effort; new native freezes include the shared launcher, and consolidation freezes include task definitions and each copied fixture tree. The original pre-call omissions remain disclosed for the recorded studies.
