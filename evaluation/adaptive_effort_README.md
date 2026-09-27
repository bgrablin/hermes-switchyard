# Adaptive effort evaluation (issue #121)

This harness measures what the adaptive reasoning-effort adapter sends to the
provider. It reads the effort from the provider SDK wire payload after the
Hermes `llm_request` middleware. It does not read TUI labels or trust receipts
for the sent level.

The harness does not copy the adapter policy. It compares the wire effort with
the requested cap and with independent fixture labels.

## Files

| File | Purpose |
| --- | --- |
| `evaluation/adaptive_effort_eval.py` | Harness and command-line tool. |
| `evaluation/adaptive_effort_fixtures.json` | Public synthetic development corpus with a frozen dev/holdout split. |
| `tests/test_adaptive_effort_eval_metrics.py` | Offline tests: fixture freeze, metrics, exact Jev wire fields. No Hermes needed. |
| `tests/test_adaptive_effort_eval_hermes.py` | Runs the harness through real Hermes middleware, including the contrast test. |

This corpus is for development. The coordinator keeps a separate held-out
corpus. Do not tune the adapter against the `holdout` split in this file.

## Command

Use the Hermes Python so that `hermes_cli`, `agent`, `anthropic`, `openai`,
and `httpx` import:

```sh
"$HERMES_PYTHON" evaluation/adaptive_effort_eval.py --split dev \
  --output evaluation/adaptive_effort_results.json
"$HERMES_PYTHON" -m unittest discover -s tests -p 'test_adaptive_effort_eval_*.py'
```

Options:

- `--split dev|holdout|all` (default `dev`).
- `--responder synthetic_lowest|recorded|live` (default `synthetic_lowest`).
  - `synthetic_lowest`: offline. Each Jev question gets its lowest option. It
    tests plumbing and guards. It is not a model of Jev accuracy.
  - `recorded`: offline. Needs `--recordings FILE`. A missing recording fails
    closed and is reported as `jev_call_failed`.
  - `live`: hosted Jev. Needs `--allow-network`, `--max-jev-calls 1..400`, and
    a Jev credential in the environment. Only a live run on `holdout` sets
    `accuracy_claim_allowed: true`.
- `--record-to FILE`: write the Jev responses seen in this run for later replay.
- `--fixtures FILE`: use another fixture book with the same schema (for
  example the coordinator held-out book).
- `--plugin-parent DIR`: candidate plugin root (default: this checkout).
- `--require-green`: exit 1 unless every gate is `GREEN`.

Exit codes: `0` run completed, `1` `--require-green` failed, `2` usage,
fixture, or child error.

The harness copies the candidate into a temporary Hermes home, writes a
minimal `config.yaml` there, and runs a child process with a small allow-listed
environment. Credentials pass to the child only in `live` mode.

## Fixture book schema (v1)

```json
{
  "schema_version": 1,
  "public_synthetic": true,
  "frozen_split_sha256": "<split_digest(fixtures)>",
  "routes": [{"name": "...", "provider": "...", "model": "...",
              "api_mode": "anthropic_messages | codex_responses"}],
  "fixtures": [{
    "id": "c01-routine", "slice": "short_routine", "split": "dev | holdout",
    "user_message": "...", "requested_effort": "low..ultra",
    "phase": "new_turn | after_tool", "tool_status": "ok | error | null",
    "labels": {"<labeler>": "lower_ok | keep_cap | ambiguous"},
    "contrast_group": "c01 (optional)", "restricted": false,
    "public_synthetic": true
  }]
}
```

Rules that validation enforces:

- `frozen_split_sha256` must equal the digest of every item's id, split, text,
  and labels. A change to a label, split, or text breaks the freeze.
- Members of a contrast group have the same text length, the same split, and
  both `lower_ok` and `keep_cap` consensus labels.
- Labelers that disagree give the consensus label `ambiguous`. Ambiguous items
  are not scored as false or successful lowering.

## Report schema (`switchyard.adaptive_effort_eval.v1`)

Top level: `status`, `responder`, `split`, `fixture_book`,
`candidate_source_sha256`, `jev_total_calls`, `jev_cap`, `jev_cap_reached`,
`summary`, `contrasts`, `acceptance`, `fixtures`.

Each `fixtures[]` record:

- `requested_wire`, `sent_wire`, `cap`: effort levels from the wire payload
  before and after middleware. `cap` comes from the receipt and falls back to
  `requested_wire`.
- `outcome`: `lowered`, `kept`, `kept_no_jev_call`, `abstained`, `raised`, or
  `unmeasured`.
- `score`: `false_lower`, `successful_lower`, `missed_lower`, `abstained`,
  `raised_above_cap`.
- `requests[]`: one entry per LLM request (two for `after_tool`), each with
  `prompt_bytes_identical` and a closed-set `receipt` copy.
- `jev.calls[]`: `model`, `request_id`, `usage`, `wire_latency_ms`,
  `client_latency_ms`, `answers` (the semantic answer), `question_types`,
  `provider_routing`, `response_source`.
- `jev.first_state_sha256`, `jev.state_keys`, `jev.user_text_in_state`.
- `leaks`: sentinel names found in any Jev state (system prompt, memory,
  plugin context, tool body, assistant text).
- `missing_evidence`: for example `responder_not_live_no_accuracy_claim`,
  `single_labeler`, `no_jev_call`, `jev_usage_missing`,
  `jev_wire_latency_missing`, `jev_request_id_missing`, `jev_call_failed`,
  `wire_effort_missing`, `receipt_missing`. The harness never fills a missing
  value.

`acceptance.gates` values are `GREEN`, `RED`, or `NOT_MEASURED`:
`contrast_pairs_distinguishable`, `no_sidecar_or_tool_leak`,
`restricted_text_not_sent`, `never_above_cap`, `prompt_bytes_unchanged`,
`holdout_safety`, `holdout_benefit`. The two holdout gates are measured only
for a live holdout run. `holdout_safety` needs a false-lower Wilson 95% upper
bound of at most 0.05 and no lowered short consequential item.
`holdout_benefit` needs at least 70% of `lower_ok` items lowered.

## Contrast state

On base `ebe1409` the `contrast_pairs_distinguishable` gate was `RED`:
same-length routine and consequential tasks reached Jev as the same state.
With the issue #121 adapter, Jev receives the bounded current request, so the
gate is `GREEN` and `tests/test_adaptive_effort_eval_hermes.py` asserts it
directly. A `GREEN` contrast gate shows that Jev can tell the pairs apart. It
does not show that Jev decides correctly.
