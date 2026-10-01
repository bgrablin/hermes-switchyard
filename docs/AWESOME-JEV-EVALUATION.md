# Awesome Jev implementation experiments

Status: multi-file evidence is a promising component, held as an opt-in draft
pending the full native-conversation evaluation. No default-on or whole-turn
performance claim is made. This work extends the source-prefetch pilot in #183.

## Scope and sources

The [awesome-jev collection](https://github.com/yibie/awesome-jev) supplied four
plausible additions to the current Switchyard implementation:

| Family | Inspiration | Experiment / disposition |
| --- | --- | --- |
| Multi-file evidence | [jev-retrieval](https://github.com/romeromarcelo/jev-retrieval), [jselect](https://github.com/keltokhy/jselect) | Keyed pointwise decisions and exact duplicate handling; implemented as an opt-in extension. |
| Output pruning | [jev-pruner](https://github.com/tamaratran/jev-pruner) | Initial candidate lost required facts; keyed follow-up improved it, but automatic pruning remains unqualified. |
| Batch triage | [jev-logtriage](https://github.com/jyatesdotdev/jev-logtriage) | Tested current Switchyard triage; abstention limited coverage. No new runtime feature justified. |
| Completion checking | [jev-belay](https://github.com/valentynkit/jev-belay) | Missed unsupported completion claims; no additional completion hook. |

This is a bounded investigation of these four additions, not an exhaustive test
of every project in the collection or every possible implementation. Existing
skill routing, model routing and typed assessment already cover other overlaps.

## Observed development results

All fixtures were public synthetic data. Jev was pinned to
`typesafe/jev-1.13-20260917` through OpenRouter. Case labels and thresholds were
written before each stage. The follow-up reused development fixtures and is
explicitly not an independent confirmation.

| Screen | Observed result |
| --- | --- |
| Lexical overlap retrieval control | 5 of 30 required evidence items; this is a simple token-overlap control, not BM25 or native Hermes. |
| Global Choice retrieval, two repeats | 58 of 60 required items; two unrelated selections. |
| Positional Noul retrieval, two repeats | 52 of 60 required items; no unrelated selections. |
| Positional Noul with diversity, two repeats | 57 of 60 required items; no unrelated selections. |
| Positional output pruning, two repeats | Only 8 of 32 required items survived; reject. |
| Completion checker, two repeats | 20 of 24 verdicts correct; four missed nudges, no false nudges. |
| Existing record triage, two repeats | 7 accepted correct labels, 9 abstentions, zero accepted wrong labels. |
| Explicit keyed retrieval follow-up | 60 of 60 required items, zero unrelated selections across 26 runs. |
| Explicit keyed output follow-up | 32 of 32 required items retained across 16 runs; 24 unnecessary items also retained. |

The first stage made 96 provider requests, the keyed follow-up 42, and the
multi-source confirmation 16. Zero provider failures were observed in the initial
96-request screen. Valid typed output alone did not establish semantic correctness:
the positional pruning answers were valid but selected the wrong content.

## New multi-source confirmation

Eight new case families were run twice: batch flushing, lease renewal, queue
overload with contradictory documentation, page continuation, invalid text,
shutdown draining, explicit absence, and unrelated files. The selected source
bundle used stable passage keys, independent relevance questions, exact text
deduplication, and native fallback on uncertainty or budget overflow.

Ten of 16 operations returned evidence. Every accepted result contained all
required passages and no unrelated passage. Page continuation, explicit absence,
and unrelated-source cases each deferred twice. These are component observations;
they do not prove final-answer accuracy or useful end-to-end latency. In particular,
all deferred cases must stay in the native evaluation denominator.

## Native evaluation and interruption

An 80-conversation plan was frozen before dispatch: eight cases, two repetitions,
and five interleaved arms, with serial main-model requests:

- Hermes with Switchyard disabled;
- release v0.5.6;
- main `a0fd0ad670bec53a72d2fa2a6ef851382f5e46c0`;
- candidate source with only multi-file recognition disabled;
- candidate source with multi-file recognition enabled.

The candidate was built on source-finder commit `9fda6b3`; the PR was subsequently
recovered onto `149e16ce332c68c039fc18d0b6432dfc2a02a01e`, retaining that branch's
newer scoped-refusal guards. The dependency was subsequently merged through
`424ed3ab107282e91887bcca37d6ed37ebc5a855`, which strengthens whole-turn refusal
and mutating-action checks. Do not attribute measured timings to these later bases.

Main inference used native Hermes with Codex `gpt-6-sol`, requested high effort,
six main calls maximum, a 90-second conversation budget, isolated fixture cwd,
and only read/search file tools. The normal adaptive-effort setting stayed enabled
in plugin arms; automatic skill recommendation was disabled in all arms. The
same-source control isolates the multi-file change. No Sonnet was used.

The predeclared efficiency gate requires preserved correct completions, no new
errors, at least 10% lower median and total wall time than disabled and main,
and no greater than 10% p95 regression. Release and toggle results must also be
reported. Final answers require factual coverage and source-verifiable quotations.

The remote command channel stopped responding during the run. The complete
observations, frozen scripts, hashes and final unit-suite output remain on the
evaluation host and have not been retrieved into this PR. The full performance
verdict is therefore **pending**, not passed. Partial rows are not used to claim
a win. Do not promote this feature or merge this draft based on the component
screen alone.

Before promotion, recover the original raw observations and verify their script
hashes; retain any failed/interrupted runs; grade every arm including fallbacks;
publish the full totals and decision. If the run was interrupted, use a separately
frozen replacement run rather than filling missing rows selectively. Source tests
pass in the recovered checkout; CI is the full-suite gate for the published head.
