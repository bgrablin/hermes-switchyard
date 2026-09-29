# Wow local-report evaluation plan (frozen before implementation)

This is an offline synthetic contract for `hermes switchyard wow` and `/switchyard wow`. It is not a live intervention comparison, a release verdict, or evidence of savings. The comparison harness and any live A′/B/C verdict wait for the evidence-harness integration and `LIVE_RUN_AUTHORIZED`.

## Sources and scope

- Read the profile-owned routing `receipt-history.jsonl` through the existing `receipt_history` reader and the effort `effort-history.jsonl` through the existing effort reader. No session store, provider, network, writer, routing, or turn hook changes. The slash command uses the same report builder as the CLI.
- Default window: trailing 7 days at the report clock; `--days N` selects a positive whole-day trailing window. Every displayed count has an explicit `n` observed records/turns and window. `--json` uses `schema_version: 1`; only aggregate fields from a closed allowlist can reach either output.
- Routing turns are valid, retained receipt records, not all Hermes turns. Selected and loaded are separate counts. Light-turn bypass is the recorded `trivial_turn` or `light_no_skill` bypass reason only. Hosted failures use typed failure receipts; abstention uses `hosted_abstention` only. No auto-approval figure, dollar/token/time saving, or population-coverage assertion.
- Effort below-cap counts distinct `(session_id, turn_id)` pairs with a known `cap` and `sent` level and at least one below-cap request; `n` is distinct pairs with comparable levels. Missing IDs/levels cannot silently become a zero. Jev request count combines routing `request_count` and effort records whose `jev_called` is true, but never calls from unobserved explicit tools. Median latency uses only one-request routing operations with a recorded latency and effort Jev records with a recorded latency; multi-request aggregate latency is not a per-call sample. Use nearest-rank p50 and report sample `n`.
- Both sources are independently classified as available, unavailable, or partial. Missing/unreadable/invalid records and retention boundaries are not complete coverage. If an entire source is unavailable, dependent totals and median become `null`/unknown, not zero; when partial, state the partial status and the retained `n`. Existing routing receipts retain at most 500 records / 1 MiB. The report never creates a new receipt or expands retention. A plugin that was not registered in the current process is `not_registered_here`; historical receipts do not prove it is on now. An installed command cannot speak from a disabled plugin session.

## Frozen fixtures and independent expected values

Use a fixed UTC clock and synthetic validated records, with literal assertions (do not compute expected values from the report implementation):

1. **Mixed window:** five routing turns: hosted selected+loaded (2 requests), light skipped (0), hosted failure (1), hosted abstention (1), local selected+loaded (0). Four effort turn identities: three distinct turns have at least one below-cap request, one stays at cap; two effort records have `jev_called=true`. Expected routing observed 5/5, selections 2/5, loads 2/5, light bypasses 1/5, hosted failures 1/5, abstentions 1/5, below-cap 3/4, Jev calls 6 from 5 routing and the recorded effort operations; one-call latency samples 21, 18, 30, 70 ms have nearest-rank p50 21 ms (n=4). An old record outside the seven-day window must change none of these values.
2. **Empty valid sources:** zero observed records (n=0), `null` median (n=0); no claim about unobserved turns.
3. **Unavailable/partial:** absent or unreadable source has `null` dependent counts; malformed, truncated, or unsupported lines make the source partial, without copying any raw field to output. A missing effort source must not become zero below-cap turns or zero adaptive Jev calls.
4. **Rotation/retention:** default writer drops the oldest of 501 routing turns and keeps exactly 500; exercise a small byte-bound separately. A report on retained data declares limited coverage, not a 501-turn population.
5. **Privacy:** embed a distinct text/path/credential-shaped marker in an effort record's extra content field and an invalid routing line. Neither text nor JSON contains it, its path, identifiers, selected skill names, provider messages, or arbitrary source keys.
6. **Plugin state and wiring:** fresh process without registration says `not_registered_here`; registration-backed slash and CLI display the same aggregate schema, with no command to mutate configuration.

## Gates and preserved failures

- Accuracy threshold: every reported count and denominator exactly equals the independent fixture oracle (zero tolerance). Any wrong count/denominator is a kill, not an acceptable approximation.
- Privacy threshold: no synthetic content marker in text or JSON. Any content-field leak is a kill.
- Cost/egress threshold: exactly zero physical provider requests, socket/network attempts, or session-store reads on either reporting path. Any nonzero call is a kill.
- Local latency threshold: p50 of repeated command invocations on the frozen local synthetic fixture is **strictly below 200 ms**; record sample count, clock, source shape, and raw durations. Do not call synthetic timing a live production latency.
- Claims threshold: no complete-population, savings, token/time/dollar, or automatic-approval claim. Unknowns and partial coverage must appear explicitly. Keep failing assertions/results in the run report; do not delete or silently reinterpret them.
