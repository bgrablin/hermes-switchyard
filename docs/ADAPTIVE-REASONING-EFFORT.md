# Adaptive reasoning effort

**In short:** you set `/reasoning` to the most effort you're willing to pay for. On each turn, Switchyard asks whether that much is really needed. Saying "thanks!" doesn't need `high` effort, but "deploy to prod" does. When a lower level is enough, Switchyard sends the lower level, which saves tokens and time. A short line under the reply tells you what happened:

```text
Reasoning: high→low · 180 ms
```

The rules:

- **Your level is the cap.** Switchyard can go lower, never higher, unless you explicitly allow one step up after a failed tool call (`adaptive_reasoning_effort_allow_raise`).
- **Easy turns are decided locally.** Greetings and thanks need no Jev call at all.
- **When in doubt, your level wins.** On a timeout, error, invalid answer, or privacy refusal, your level goes out unchanged.
- **It's on by default.** Turn it off with `adaptive_reasoning_effort: false`, or pin a session with `/switchyard effort pin`.

It needs Hermes 0.21.4 or newer. New to these terms? See the [Concepts primer](CONCEPTS.md).

## Everyday use

### Commands

```text
/switchyard effort status            what was sent last, why, recent decisions, and the session summary
/switchyard effort summary           just the session summary
/switchyard effort pin               always send your /reasoning level in this session
/switchyard effort auto              let Switchyard lower effort again
/switchyard effort receipt auto      show the receipt line when Switchyard decided something (default)
/switchyard effort receipt always    also show it on pinned or pass-through turns
/switchyard effort receipt off       never show it
```

`auto` uses your current level as the new cap. Commands act on the session where you run them. If you run one before the session's first model request, it applies from the first message.

### Cap vs. sent

Two numbers matter, and they show up in different places:

| Where | What it shows |
| --- | --- |
| Status bar and `/reasoning` | Your chosen level, the **cap**. It doesn't change when effort is lowered. |
| Receipt line and `/switchyard effort status` | The level actually **sent** on that turn, with a plain-language reason. |

This release doesn't sync the TUI status chip to the sent level. The status bar always shows the cap.

### Reading the receipt line

```text
Reasoning: high→low · 180 ms                                 Jev lowered effort; the call took 180 ms
Reasoning: high→low · local decision                         lowered on your machine, no Jev call
Reasoning: high→low · 180 ms · ~1.2k reasoning tokens saved (est.)
Reasoning: kept at high — consequential request · 210 ms     Jev judged this one important
Reasoning: high→low · 190 ms · 2 cached                      later requests in the turn reused the choice
Reasoning: high→low · 170 ms · shape only (message text not sent)
Reasoning: high · pinned
Reasoning: not adapted — host sent no effort · no Jev call
Reasoning: not adapted — this level cannot be adapted on this route · no Jev call
```

"Kept" lines give a plain reason:

- consequential request
- after a tool failure
- after a write
- change request
- cloud unavailable
- invalid cloud answer
- cloud over budget
- cloud decision (Jev picked your level)

### When the line appears

| Mode | Shows the line when… |
| --- | --- |
| `auto` (default) | Switchyard changed effort, or made or reused a decision (cloud, local, or cached). Also once per turn when every request passed through because the host sent no effort or the level can't be adapted on this route. |
| `always` | All of the above, plus pinned and other pass-through turns where the sent level is known. Unsupported routes stay quiet. |
| `off` | Never. |

A route-only turn (`no_host_effort` or `no_room`) shows one `not adapted` line in `auto` and `always`. Switchyard doesn't invent a level or call Jev for it, and if both reasons apply, the line names both. A turn that included a user-selected bypass isn't route-only.

Pinned sessions (including missing host effort), excluded models, disabled adaptation, and unsupported routes are quiet in `auto` and show the known level in `always`.

The receipt command **saves** your choice to `adaptive_reasoning_effort_receipt_mode`, so it survives restarts. Legacy `receipt work` and `receipt on` mean `auto`. The line is produced by Hermes' `transform_llm_output` hook.

The line becomes part of the reply you see, and current Hermes stores it, so `/resume` shows it too. Before each request, Switchyard strips these lines from earlier replies, so the model never sees them. That covers both the current `Reasoning: …` format and the older `switchyard: effort …` format; other text is untouched. Delegated and background turns never get a line. If adding the line fails, the reply goes out unchanged.

### Session summary

`/switchyard effort summary`, also part of `status`, uses only in-memory counters. It makes no network or Jev call.

```text
Switchyard effort summary (this session)
  Cap high · last sent low · auto · why: cloud decision
  turns: 4
  requests: lowered 1, kept 3, raised 0, not adapted 0
  cloud decisions: 3, p50 190 ms, p95 240 ms
  local decisions: 1
  cached reuses: 0
  estimated tokens saved: ~500 output (est., 1 of 1 lowered requests measured)
```

- **`local decisions`** counts trivial turns decided on your machine (`local_trivial`). They aren't cloud calls and add no latency sample.
- **`not adapted`** counts pass-through requests: pinned, excluded model, no room, or no host effort.
- **Without a measured baseline**, the last line reads `estimated tokens saved: unknown (no measured baseline yet)`.

The human-readable output avoids raw reason codes. The codes are still in `--json` and the history file. `status` also lists up to five recent decisions:

```text
  Cap high · last sent low · auto · why: cloud decision
  last 2 decisions (cap -> sent, why, latency):
    high -> low, cloud decision, 238 ms
    high -> high, after a write, no call
```

### The "tokens saved" estimate

The figure is built only from **measured** token counts. Switchyard reads `usage` from Hermes' `post_api_request` hook. That's token counts only, never text.

1. Each request sent at your level adds a baseline sample for that model and level. The last 20 per session are kept.
2. For a lowered request, if there are at least 3 baseline samples for the same model and level, the estimate is `median(baseline) − measured`. It uses reasoning tokens when every baseline sample reports them; otherwise it uses output tokens, and the line says which.
3. The figure appears only when every lowered request in the turn was measured. A zero or negative result shows `no reasoning tokens saved (est.)`.

With fewer than 3 samples, or no usage data from the host, no figure is shown. Switchyard never makes up a number. It's still an estimate: a routine message sent at full effort might have used fewer tokens than the median.

## What Jev sees (and doesn't)

With adaptive effort on and `public_or_sanitized_data_ack` set to `true`, Switchyard sends **bounded text from your current message** to your Jev provider. That's how Jev tells "thanks!" from a short but serious request.

**Jev receives:**

- **`current_request`:** your current message as Hermes passes it to the `pre_llm_call` hook, before memory and plugin context are added.
  - Only text is sent. For multi-part messages, only `text` or `input_text` parts count. Image, file, document, and tool-result parts are never sent, even when they carry a `text` field.
  - Over 1,200 characters, Switchyard sends the first and last parts.
  - Over 16,000 characters, the message stays local: it's not scanned, cut, or sent, and your level is kept.
- **Status flags:** `turn_phase`, `recent_tool_statuses` (`ok`, `error`, `failed`, or `unknown`), and `latest_tool_failed`.
- **For step-level asks only:** `recent_tool_kinds` (one of `read`, `write`, `exec`, `other`) and `routine_success_streak` (a count). Tool names are mapped to kinds locally and are never sent.
- The candidate effort levels.

**Jev never receives:**

- conversation history or assistant replies
- the system prompt, memory, or plugin context
- loaded skills
- tool names, arguments, or results
- file paths
- the provider request itself

Switchyard never reads task text from the provider request, because that request can contain memory and tool results.

**No Jev call at all, and your level is kept, when:**

- no current message was captured for this turn (a delegated subagent turn, a background turn, or a host without `pre_llm_call`);
- the message is empty, has no text part, has an unknown shape, or is longer than 16,000 characters;
- a local scan finds a restricted document marking such as `proprietary` or `company confidential`.

### Secret scrubbing

Text is passed through Hermes' egress redactor before it's cut and sent, so secret-looking values are masked. Emails and phone numbers are not treated as sensitive.

**Masked as secret values:**

- a value assigned to a secret-like name (`DB_PASSWORD=…`, `OPENAI_API_KEY: …`, `"api_key": "…"`, `--api-key …`)
- a password in a URL (`scheme://user:…@host`)
- an `Authorization:` header value
- known token prefixes

**Not treated as values:**

- a name on its own
- placeholders like `<your-password>` or `$OPENAI_API_KEY`
- plain numbers like `MAX_TOKENS=4096`

The scrubber is conservative, so some public text that looks like an assignment (such as `token: required`) may also be masked. The rest of the message is still sent.

**This is not data loss prevention.** It checks values and markings, not topics, so a public question about password hashing is still sent. It can't detect unmarked private or employer text. Don't enter that kind of text in a session with adaptive effort on.

### Metadata-only mode (older Hermes)

If Hermes has no egress redactor (`agent.redact.redact_for_egress` is missing, raises an error, or returns its "unavailable" marker), Switchyard sends **no message text at all**. Jev still gets one question per turn, using simple facts computed locally:

- **`request_shape`:**
  - `chars`, one of `1-16`, `17-64`, `65-256`, `257-1024`, or `1025+`
  - `lines`, one of `1`, `2-3`, `4-10`, or `11+`
  - true/false flags `has_code_fence`, `has_url`, `has_file_path`, and `has_question_mark`
- **`turn_index`:** how many user turns this session has had.
- The tool statuses, and for step-level asks, the tool kinds and read streak.

Two checks run on the full text locally and are never sent:

- A change request (`fix`, `edit`, `delete`, `deploy`, …) keeps your level with no Jev call (`kept_requested_metadata_change_request`).
- A high-stakes word (`prod`, `delete`, `password`, `token`, `billing`, `security`, …) does the same (`kept_requested_metadata_high_stakes`).

Receipts show `scan_reason` `metadata_only`, and the line ends with `shape only (message text not sent)`. Restricted markings and oversized messages still stay local.

### Turning off text sending

- Set `adaptive_reasoning_effort` to `false`. This is the privacy opt-out for this feature.
- Or set `public_or_sanitized_data_ack` to `false`.

Either way, your level is always sent unchanged. This doesn't affect automatic skill routing, which has its own settings.

The captured text lives only in memory for the current turn and is cleared when the turn ends. It's never written to receipts, the history file, or logs.

## How decisions are made (detailed rules)

**Reading the cap**

- Switchyard reads the level from each request: your `/reasoning` setting after Hermes' per-model clamp.
- In `auto` mode, it asks Jev to choose among the levels at or below that cap for the provider.
- With `adaptive_reasoning_effort_allow_raise` on and the latest tool call failed, it may offer one level higher.

**Trivial turns stay local.** When your current message is a closed-list greeting, thanks, or acknowledgement, Switchyard sends the lowest allowed level with no Jev call and no network (`local_trivial`).

- Examples: `hi`, `thanks`, `ok thanks!`, `👍`.
- A greeting-only instruction also counts, such as `Reply with exactly one short greeting sentence. Do not use tools.`
- Detection lives in `hermes_switchyard/trivial_turn.py`. Acknowledgements are at most 6 words from the list, or up to 8 characters with no letters or digits. Greeting prompts must ask for a greeting *as the reply* and name no second deliverable, so `Write a hello world program in Python.` is not trivial.
- A task word (`hi, delete the prod backups`, `thanks, now deploy`), a code block, a URL, or a file path makes the message non-trivial, and Jev decides.
- The cap and pin rules still apply. After a tool call in the same turn, Switchyard asks Jev again.
- Delegated tasks never use this shortcut, because they have no user message of their own.

**No room, no call.** If the requested level is outside the route's ladder, or there's no lower level within the cap (for example `low` on OpenAI Codex models), there's no Jev call and your level is sent.

**A `/reasoning` change sets a new cap.**

- Changing `/reasoning` mid-session on the same model makes the new level the cap. The session stays in `auto`, and Jev is asked again next time. For example, after `/reasoning max`, a reply to `thanks!` can still go out below `max`.
- Use `/switchyard effort pin` to send your level unchanged.
- TUI windows opened before an install keep their old plugin code until restarted.

**Model switches re-baseline.** A model switch or fallback reads the new level and keeps the current mode.

**Fail closed to your level.** A Jev timeout, error, invalid or missing answer, or missing data acknowledgement all mean your level is sent unchanged.

**The current message decides.** Jev answers two questions about your current message: an effort choice, and a `stakes` score from 0 to 1. Code, not Jev, applies the result:

- A stakes score of 0.5 or more never lowers your level. The 0.5 threshold is provisional, not a measured accuracy guarantee.
- After a failed tool call, the level is never lowered below yours.

**Fresh context per turn.** Tool outcomes and the "stuck" flag reset at each new user turn. The stuck flag reflects only the latest tool call.

**Fewer calls.** Jev is asked only when the context changes: a new turn, a tool outcome that flips the stuck flag, a new cap, or a step-level ask. Retries in the same context reuse the last choice.

**Step-level adaptation, bounded.** In a long turn, Switchyard can ask again after routine read-only tool rounds. All of these must hold:

- the request isn't asking for a change (no `fix`, `edit`, `write`, `delete`, `deploy`, …);
- the turn has no high-stakes signal;
- no tool call failed;
- no write tool ran this turn.

Limits on step asks:

- at least 2 routine read rounds in a row, and at least 3 rounds since the last ask;
- at most 4 per turn, and none after two decisions agree;
- each one offers only your level and one level below.

After a write tool, the next request goes back to your level with no Jev call (`kept_requested_after_write`). Set `adaptive_reasoning_effort_step_adaptation` to `false` for one decision per turn.

**Messages are never rewritten,** so Hermes' prompt cache stays valid. Only request-scoped effort fields change.

**Delegated tasks keep their own state.** A subagent or background fork that shares the session ID but runs under a different task ID gets its own level, model, mode, cached choice, and tool outcomes. It never changes the foreground cap or mode, and `/switchyard effort` neither reads nor changes it.

**Compression rotation.** When compression moves the session to a new ID before the first request of a turn:

- The turn keeps its original task ID. A `pin` or `auto` you set before that message still applies, because the controller also accepts the task ID that holds the pending mode.
- If you change the mode more than once before that request, only the last command applies, even if the commands were stored under IDs from before and after the rotation.
- Hermes calls `pre_llm_call` once per user turn with an empty `parent_session_id` for foreground turns. Switchyard uses that to bind the first rotated request to the foreground state, so `/switchyard effort status` shows it.
- A delegated child always has a parent ID and stays isolated.
- If the host doesn't send `parent_session_id`, Switchyard doesn't guess: the task keeps separate state under the normal cap.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `adaptive_reasoning_effort` | `true` | `false` turns the feature off. |
| `adaptive_reasoning_effort_mode` | `auto` | Starting mode for new sessions: `auto` or `pinned`. |
| `adaptive_reasoning_effort_exclude_models` | `[]` | Model name patterns (case-insensitive wildcards, such as `["*astra*"]`) that are never touched and never trigger a Jev call. |
| `adaptive_reasoning_effort_allow_raise` | `false` | When `true`, `auto` may go one level above your level while the latest tool call has failed. It drops back after the next successful tool call or a new turn. |
| `adaptive_reasoning_effort_deadline_seconds` | `0.4` | How long to wait for Jev, from 0.1 to 1.5 s. On timeout, your level is sent unchanged. |
| `adaptive_reasoning_effort_step_adaptation` | `true` | Allow bounded step-level asks after routine read-only rounds (at most one level below yours). `false` means one decision per turn. |
| `adaptive_reasoning_effort_receipt_mode` | `auto` | Receipt line: `auto`, `always`, or `off`. Changed by `/switchyard effort receipt …` and saved. Legacy `work`/`on` mean `auto`. |
| `adaptive_reasoning_effort_receipt_line` | `true` | Legacy on/off (`true` → `auto`, `false` → `off`). Prefer `adaptive_reasoning_effort_receipt_mode`. |
| `adaptive_reasoning_effort_default` | `medium` | Deprecated and unused since 0.5.4. |

Example:

```text
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_exclude_models '["*astra*"]'
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_mode pinned
```

Settings are read when the plugin loads. Start a new session, and restart the gateway for messaging platforms, after a change.

## Records and statistics

Every decision appends one record to `effort-history.jsonl` in the plugin data folder. The file keeps the last 2,000 records, up to 1 MiB. Each record holds:

- session and turn ID, model, and mode
- requested level, sent level, and cap
- reason code
- whether Jev was called, plus its latency, confidence, and stakes score
- the stuck flag

No message text is stored.

`hermes switchyard stats [--since 24h]` includes a `reasoning_effort` section:

- levels requested and sent
- how often effort was lowered, unchanged, or raised
- reasons
- Jev calls per request
- Jev latency p50/p95

To find the level actually sent, use the receipt line, `/switchyard effort status`, these records, or a request dump. The status bar shows your cap, not the per-turn level.

## Hermes integration point

This feature requires Hermes 0.21.4 or later: `PluginContext.register_middleware("llm_request", ...)` plus `hermes_cli.middleware.apply_llm_request_middleware`. Hosts without that API record `noop_seam_unavailable` and change nothing. The `/switchyard` command is registered only when the host provides `register_command`.

Model routing (`jev_model_route`) stays advisory (`applied: false`). This feature is the one that actually changes a request.

## Receipt fields and reason codes

`hermes switchyard status --json` includes `reasoning_effort_adapter` with the effective settings. `last_receipt()` in `hermes_switchyard.reasoning_effort_adapter` returns the last decision with `status`, `effort` (the level sent), `requested_effort`, `cap`, `mode`, `reason_code`, and `applied`.

| Group | Reason codes |
| --- | --- |
| Decided | `jev_selected`, `jev_step_selected`, `local_trivial` (with `jev_called: false`), `cached`, `cached_unchanged` |
| Passed through | `no_room`, `pinned`, `excluded_model`, `no_host_effort`, `reasoning_disabled`, `unsupported_route`, `disabled` |
| Kept your level | `invalid_choice`, `kept_requested_on_jev_failure`, `kept_requested_ack_required`, `kept_requested_no_task_text`, `kept_requested_restricted_text`, `kept_requested_high_stakes`, `kept_requested_after_tool_failure`, `kept_requested_after_write`, `kept_requested_metadata_change_request`, `kept_requested_metadata_high_stakes` |

A metadata-only decision also has `scan_reason` `metadata_only`.

Since 0.5.5, `pinned_by_user_change` is no longer used: a `/reasoning` change sets a new cap and keeps `auto`.
