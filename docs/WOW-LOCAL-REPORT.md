# Local observation report (`wow`)

Switchyard can summarize **retained local records** without calling Jev or a provider. This is a read-only report, not a comparison with Switchyard off and not a measure of task quality or savings.

```text
hermes switchyard wow
hermes switchyard wow --days 14 --json
/switchyard wow
/switchyard wow --days 14 --json
```

The default window is the trailing 7 days. `--days N` accepts an integer from 1 to 3650. Both commands use the same report builder. JSON has `schema_version: 1` and top-level `plugin_state`, `window`, `coverage`, `sources`, and `metrics`. The `window` object gives the UTC start and end. Each metric gives a `count` (or `value` for latency), its denominator `n`, and `status`. Text shows the window and each metric's denominator and status.

| Metric | Observation and denominator (`n`) |
| --- | --- |
| `observed_turns` | Valid retained skill-routing turn receipts in the window; `n` is those receipts. |
| `skills_selected` | Retained routing receipts with a selected skill; `n` is routing receipts. A selection is not a load. |
| `skills_loaded` | Retained routing receipts whose consumer says `loaded` and whose skill load is verified; `n` is routing receipts. A load is not a successful task. |
| `below_cap_turns` | Distinct recorded effort `(session_id, turn_id)` pairs with `sent` below `cap` at least once; `n` is pairs with comparable levels. Missing IDs or levels are not counted as a known zero. |
| `light_turn_bypasses` | Routing receipts with the recorded `trivial_turn` or `light_no_skill` bypass reason; `n` is routing receipts. |
| `jev_calls` | Recorded routing request counts plus effort records with `jev_called: true`; `n` is routing receipts plus effort records. Explicit Jev tools and unrecorded calls are outside this figure. |
| `median_latency_ms` | Nearest-rank p50 of recorded one-request routing latencies and effort Jev latencies; `n` is eligible latency samples. Multi-request routing totals are not per-call samples. |
| `hosted_failures`, `hosted_abstentions` | Typed routing terminal states; `n` is routing receipts. |

The two sources report `available`, `partial`, or `unavailable`. `partial` means invalid records or a retention boundary prevent a complete view; counts then describe only valid retained observations. An unavailable source produces `null`/`unknown` for metrics that need it, not a guessed zero. A valid empty source can report zero observations with `n=0`, but this says nothing about turns the plugin did not observe. The routing writer retains at most 500 records or 1 MiB; the effort writer retains at most 2,000 records or 1 MiB. A longer `--days` window cannot recover rotated or expired rows.

`plugin_state: not_registered_here` means this CLI process has not registered the plugin. It does not prove the plugin is off in another session. An enabled plugin's `/switchyard wow` command reports registration in that session; a disabled plugin has no in-session command. The report never reads a session store and never emits prompt text, selected skill names, provider messages, paths, or raw receipt rows. It cannot establish complete turn coverage, avoided calls, dollars, saved time or tokens, auto-approvals, or an improvement caused by Switchyard.

The offline synthetic contract and its limits are in `evaluation/wow/PLAN.md` in the source repository; that evaluation file is not in the plugin archive. The comparative harness and any live outcome verdict remain separate and require their own authorization.
