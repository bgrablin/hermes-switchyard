# Offline outcome labels (0.6.0 candidate)

Switchyard routing receipts record routing choices, not answer quality. This local-only tool creates four **evidence-limited** labels from the existing bounded receipt history. It does not run in a Hermes hook, call Jev, change routing or effort, create a runtime holdout, or send data to a provider. It is not in the 0.5.6 release.

## Inputs and use

The command requires three explicit local paths. It has no default profile path and never opens a remote service:

```text
python -m hermes_switchyard.outcome_labels --history-dir <private-plugin-data-copy> --evidence <ephemeral-local-evidence.json> --output <new-private-labels.json>
```

`--history-dir` holds a copy of the existing `receipt-history.jsonl`. The existing reader accepts only valid canonical retained records, skips malformed or partial lines, and reads its bounded tail. The denominator is the number of valid **retained** records, not the number of original turns. Do not run this against a real user store without master authorization. The output path must be new; the command writes it with private permissions and refuses to overwrite either input.

The separate evidence file is a JSON array keyed by `session_id` and `turn_id`, with optional `turn_completed: true|false`, `tool_trace_complete: true|false`, `tool_events: [{"session_id": "...", "turn_id": "...", "status": "ok|error"}]`, `assistant_message_id`, and `user_message: {"reply_to_message_id": "...", "text": "..."}` on the following turn. Optional `retried` and `undone` booleans censor ambiguous corrections; typed `reactions: [{"target_message_id": "...", "kind": "positive|negative"}]` are never labels. No collector or consent flow is included. Only the frozen **synthetic** fixtures in `evaluation/outcome-labels/` were run. Treat any evidence file with user text as sensitive ephemeral input: do not publish it, log it, or persist it as a label. Extra evidence fields such as chat IDs or raw error messages are rejected before output.

## Label meaning and unknowns

Each label object has only the split enum and four boolean-or-`UNKNOWN` fields:

| Field | Known true | Known false | `UNKNOWN` examples |
| --- | --- | --- | --- |
| `skill_loaded_after_selection` | Exact selected skill, canonical consumer `loaded`, verified load | Exact selected skill, canonical `load_failed` | No selection, advisory receipt, missing or mismatched load evidence |
| `tool_error_in_turn` | Same-turn closed-status tool error | Declared complete same-turn trace without error | Missing, incomplete without error, malformed or mismatched trace |
| `next_turn_user_correction` | Explicit correction marker in an attributable next-user reply | Attributable complete next-user reply without correction or ambiguous cue | No following retained turn, other split, wrong reply target, retry/undo, reaction alone, or correction-like wording outside the high-precision markers |
| `turn_completed` | Explicit true from matching evidence | Explicit false from matching evidence | No completion evidence |

The correction marker must begin with `No, I asked for`, `No, I asked you to`, `That's not what I asked for`, `That's not what I asked you to`, or `You misread/misunderstood my request` (case-insensitive). Other correction-like words, including `wrong`, `mistake`, `actually`, `I meant`, `retry`, or `undo`, are ambiguous and remain `UNKNOWN`, not a negative. A reply must target the current assistant message ID and be the next retained turn in that session, in the same offline split. Missing or duplicate turn identities censor **all** fields. A reaction does not prove satisfaction or correction. These labels measure explicit observable friction and load/trace evidence, not answer correctness, task success, or user satisfaction.

## Offline split and report

The holdout uses `sha256("switchyard-outcome-label/1|holdout-v1|" + turn_id)`, the first eight digest bytes as an unsigned big-endian integer, and `mod 5 == 0` for holdout (20%). The rest is train; missing/duplicate IDs are `UNKNOWN`. It assigns **already-retained data** for offline tuning only and never disables runtime skill or effort hooks. Cross-arm next-turn attribution is censored. The output never includes IDs, user text, chat IDs, prompts, error text, raw reactions, or a stable cross-session identifier. Its fixed report includes per-field `denominator`, `labeled`, and `unknown` counts, plus train/holdout/unknown counts. Rates or benefits are not inferred from coverage.

The frozen evaluation has 14 synthetic cases and 22 retained records. Its planted known counts are 2 load, 3 tool, 2 correction, and 10 completion; all other field values are explicitly unknown. It has zero known false-positive correction markers, zero disagreement on planted attributable labels, stable repeated splits, and no cross-arm correction attribution in these cases. This is **not** a comparative benefit result. A comparative SHIP verdict waits for the merged harness; no production benefit claim is allowed without at least 100 eligible attributable turns per arm and a predeclared paired analysis of error/correction rates, cost, denominators, and intervals. See the [frozen plan](../evaluation/outcome-labels/PLAN.md) for kill criteria.
