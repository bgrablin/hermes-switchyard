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

## Native evaluation: original interrupted run

Native v1 froze 80 conversations: eight cases, two repetitions, and five
interleaved arms, with serial main-model requests. The arms were disabled,
release v0.5.6 (`552940b8`), main `a0fd0ad6`, candidate source with only multi-file
recognition disabled, and candidate source with multi-file recognition enabled.
The measured candidate was based on `9fda6b3`; later refusal guards were not part
of this run and must not inherit its timings.

The command channel failed during execution. Recovery found 15 completed rows
(three per arm), and no surviving driver or worker. Those observations remain a
separate interrupted run; they do not establish a performance win and are not
pooled with the replacement. Recovered native v1 scripts match all three frozen
hashes; the driver's original import line was reconstructed and verified against
its pre-run SHA-256. The earlier development screen and keyed scripts have
post-freeze formatting differences; those mismatches are disclosed in recovery
manifests. Their numbers remain development observations, not qualification.

## Native evaluation: complete replacement plan

Native v2 was separately frozen before dispatch on October 1, 2026. It repeats
all 80 conversations in the same randomized order. Its release arm is unchanged;
its main arm is `d50c724b31bb2cb945495637ba40a1ce6504ccd1`. Its candidate runtime
is `350ee5a6b8d50ba7b2b2364a4b339e6e94336800`, including source-finder dependency
`ddbdac67`. The same-source control disables only multi-file recognition. Source
files, fixtures, job order, runner scripts, and tracked Hermes runtime files are
hashed before dispatch. Runtime hashes are checked again at completion.

Both runs use native Hermes with Codex `gpt-6-sol`, requested high effort, six main
calls maximum, a 90-second conversation budget, isolated fixture cwd, and only
read/search file tools. Adaptive effort stays enabled in plugin arms; automatic
skill recommendation is disabled. Jev is pinned to `typesafe/jev-1.13-20260917`
through OpenRouter. No Sonnet is used.

The predeclared gate requires preserved correct completions, no new errors,
at least 10% lower median and total wall time than disabled and main, and no
more than 10% p95 regression. Release and same-source control results are also
reported. All fallback cases stay in the denominator. Every final answer is
checked for required facts, contradictions and source-verifiable quotations.
Strict JSON formatting is recorded separately from factual correctness. Both
nearest-rank and linearly interpolated p95 will be reported because the original
plan did not specify a percentile convention; a win must survive both.

Other evaluations were running on the same host during v2; interleaving limits
but does not eliminate this timing confound. The benchmark uses synthetic
fixtures and does not qualify default enablement or broad workload claims.

The checked-in runner subsequently made host paths configurable and restricted
its script snapshot to the three runner files. Frozen v2 copies preserve the
actual measured scripts, and the resolved paths and candidate runtime are
unchanged. These are harness portability changes, not a restarted or retuned run.

Native v2 is currently running. Full results, script provenance and the final
merge-or-close decision will be recorded after all arms are graded. No partial
performance verdict is used to promote the feature.
