# Adaptive reasoning effort

Switchyard can lower Hermes `reasoning_effort` for routine steps. Your `/reasoning` level is the cap by default; `adaptive_reasoning_effort_allow_raise` explicitly permits one higher level after a failed tool call.

## Behavior

- **Your level is the default cap.** Switchyard reads the level from each request (your `/reasoning` setting after Hermes' per-model clamp). In `auto` mode it asks Jev to pick a level from the ones at or below it for the provider. When `adaptive_reasoning_effort_allow_raise` is enabled and the latest tool call failed, it may offer one higher wire level.
- **Trivial turns stay local.** When your current message is only a greeting, thanks, or acknowledgement (for example `hi`, `thanks`, `ok thanks!`, or `👍`), Switchyard sends the lowest allowed level for the route with no Jev call and no network (`local_trivial`). It uses the closed word list that automatic skill routing uses (`hermes_switchyard/trivial_turn.py`): at most 6 words, each on the list, or up to 8 characters with no letters or digits. A task word (`hi, delete the prod backups`, `thanks, now deploy`), a code block, a URL, or a file path makes the message non-trivial, so Jev decides. The cap and pin rules do not change: the level never goes above your cap, and a pinned session sends your level. After a tool call in the same turn, Switchyard asks Jev again. Delegated tasks never use this bypass, because they have no user message of their own.
- **No room, no call.** When no lower level exists for the route (for example `low` on OpenAI Codex models), Switchyard does not call Jev and sends your level unchanged.
- **A `/reasoning` change sets a new cap.** If you change `/reasoning` mid-session on the same model, the new level becomes the cap and the session stays in `auto`. Switchyard asks Jev again for the next request. For example, after `/reasoning max`, a reply to `thanks!` can still go out below `max`. To send your level unchanged, run `/switchyard effort pin`; run `/switchyard effort auto` to resume adaptation. Open TUI windows started before an install keep their old plugin code until restarted.
- **Model switches re-baseline.** A model switch or fallback reads the new level and keeps the current mode.
- **Fail closed to your level.** On a Jev timeout, error, invalid or missing answer, or missing data acknowledgement, your level is sent unchanged.
- **The current message decides.** Jev answers two questions about your current message: an effort choice and a `stakes` score from 0 to 1. Code, not Jev, applies the result. A stakes score of 0.5 or more never lowers your level. After a failed tool call, the level is not lowered below yours. The 0.5 threshold is provisional; it is not a measured accuracy guarantee.
- **Fresh context per turn.** Tool outcomes and the stuck flag reset at each new user turn. The stuck flag is the latest tool call's result only.
- **Fewer calls.** Jev is asked only when the context changes (new turn, a tool outcome that changes the stuck flag, a new cap, or a step-level ask). Same-context retries reuse the last choice.
- **Step-level adaptation (bounded).** In a long turn, Switchyard can ask Jev again after routine read-only tool rounds. All of these must be true: the request does not ask for a change (for example, no `fix`, `edit`, `write`, `delete`, or `deploy`), the turn has no high-stakes signal, no tool call failed, and no write tool ran in this turn. A step ask also needs at least 2 routine read rounds in a row and at least 3 rounds since the last ask. There are at most 4 step asks per turn, and none after two decisions agree. A step ask offers only your level and one level below it. After a write tool call, the next request goes back to your level without a Jev call (`kept_requested_after_write`). Set `adaptive_reasoning_effort_step_adaptation` to `false` for one decision per turn.
- **Messages are never rewritten**, so the Hermes prompt cache stays valid. Only request-scoped effort fields change.
- **Delegated tasks keep their own state.** A subagent or background fork that shares the session ID but runs under a different task ID gets its own level, model, mode, cached choice, and tool outcomes. It never changes the foreground cap or mode, and `/switchyard effort` does not read or change it.
- **A pending mode survives a compression rotation.** If compression moves the session to a new ID before the first request of a turn, the turn keeps its original task ID. A `pin` or `auto` that you set before that message is still applied, because the controller also accepts the task ID that holds the pending mode. If you change the mode more than once before that request, only the last command applies, even when the commands were stored under IDs from before and after the rotation.
- **The first request after a rotation is foreground.** Hermes calls `pre_llm_call` once per user turn with an empty `parent_session_id` for a foreground turn. Switchyard uses that as evidence, so a fresh controller binds the first rotated request to the foreground state and `/switchyard effort status` shows it. A delegated child always has a parent ID and stays isolated. If the host does not send `parent_session_id`, Switchyard does not guess: the task keeps separate state under the normal cap.

## Data sent to Jev

When adaptive effort is on and `public_or_sanitized_data_ack` is `true`, Switchyard sends **bounded text from your current message** to the configured Jev provider. This is how Jev tells a greeting from a short but consequential request.

What Jev receives:

- `current_request`: your current message as Hermes gives it to the `pre_llm_call` hook, before memory and plugin context are added. Only text is sent. For a message with parts, only parts of type `text` or `input_text` are sent; image, file, document, and tool-result parts are never sent, even when they carry a `text` field. Messages longer than 1,200 characters are cut to the first and last parts. Messages longer than 16,000 characters stay local: Switchyard does not scan, cut, or send them, and keeps your level.
- `turn_phase`, `recent_tool_statuses` (`ok`, `error`, `failed`, or `unknown`), and `latest_tool_failed`.
- For a step-level ask only: `recent_tool_kinds` (closed set: `read`, `write`, `exec`, `other`) and `routine_success_streak` (a count). Tool names are mapped to a kind locally and are not sent.
- The candidate levels.

What Jev never receives: conversation history, assistant replies, the system prompt, memory context, plugin context, loaded skills, tool names, tool arguments, tool results, file paths, and the provider request. Switchyard never reads task text from the provider request, because that request can contain memory context and tool results.

Switchyard keeps your level and makes **no** Jev call when:

- it did not capture your current message for this turn (for example, a delegated subagent turn, a background turn, or a host without `pre_llm_call`);
- the message is empty, has no text part, has an unknown shape, or is longer than 16,000 characters;
- a local scan finds a restricted document marking (for example `CUI`, `SECRET//`, `FOUO`, `ITAR`, `proprietary`).

### Metadata only (older Hermes without an egress redactor)

When Hermes has no egress redactor (`agent.redact.redact_for_egress` is missing, raises an error, or returns its unavailable marker), Switchyard sends **no message text**. Jev still gets one ask per turn, with closed-set metadata that Switchyard computes locally:

- `request_shape`: `chars` (bucket `1-16`, `17-64`, `65-256`, `257-1024`, or `1025+`), `lines` (bucket `1`, `2-3`, `4-10`, or `11+`), and the booleans `has_code_fence`, `has_url`, `has_file_path`, and `has_question_mark`;
- `turn_index`: the count of user turns in this session;
- the tool statuses and, for step-level asks, the tool kinds and read streak listed above.

Jev receives no excerpt and no part of the message. Two local checks run on the full text and are never sent. A change request (for example `fix`, `edit`, `delete`, or `deploy`) keeps your level with no Jev call (`kept_requested_metadata_change_request`). A high-stakes word (for example `prod`, `delete`, `password`, `token`, `billing`, or `security`) also keeps your level with no Jev call (`kept_requested_metadata_high_stakes`). The decision receipt (`last_receipt()`) shows `scan_reason` `metadata_only`, and the receipt line ends with `metadata only`. Restricted markings and oversized messages still stay local with no Jev call.

Other text is redacted with the Hermes egress redactor before it is cut and sent, so secret-like values are masked in the text Jev receives. Emails and phone numbers are not treated as sensitive.

Secret-like values include a value assigned to a secret-like name (for example `DB_PASSWORD=…`, `OPENAI_API_KEY: …`, `"api_key": "…"`, or `--api-key …`), a password in a URL (`scheme://user:…@host`), an `Authorization:` header value, and known token prefixes. A name alone, a placeholder such as `<your-password>` or `$OPENAI_API_KEY`, and a plain number such as `MAX_TOKENS=4096` are not values. The scan is conservative, so some public text that looks like an assignment (for example `token: required`) also stays local.

The local scan is **not** data loss prevention. It checks values and markings, not topics, so a public question about password hashing is still sent. It cannot detect unmarked private, employer, or DoD text. Do not enter that text in a session with adaptive effort on.

To send no message text: set `adaptive_reasoning_effort` to `false` (this is the privacy opt-out for this feature), or set `public_or_sanitized_data_ack` to `false`. Adaptive effort then sends your level unchanged. Disabling adaptive effort does not change the separate automatic skill feature.

Switchyard keeps the captured text only in memory for the current turn and clears it when the turn ends. It is never written to receipts, the history file, or logs.

## Command

```text
/switchyard effort status        show mode, your level, last level sent, why, the last 5 decisions, and the session summary
/switchyard effort summary       show the session summary only
/switchyard effort pin           send your /reasoning level unchanged in this session
/switchyard effort auto          let Switchyard lower effort for routine steps again
/switchyard effort receipt on    add the receipt line to replies where Switchyard did work (default)
/switchyard effort receipt off   remove the receipt line
```

`auto` uses your current level as the new cap. The command acts on the session it runs in. Before the session's first model request, it applies from the first message.

`receipt on|off` changes the setting for this process until restart. The receipt line uses the Hermes `transform_llm_output` hook and is **on by default**. It is added to each foreground reply where Switchyard did work: it changed the effort, called Jev, or reused a cached decision. Examples:

```text
switchyard: effort high→low · Jev 180 ms
switchyard: effort high→low · local (no Jev call)
switchyard: effort high→low · Jev 180 ms · ~1.2k reasoning tokens saved (est.)
switchyard: effort high (kept: consequential) · Jev 210 ms
switchyard: effort high→low · Jev 190 ms · 2 cached
switchyard: effort high→low · Jev 170 ms · metadata only
```

`kept:` names why your level was kept: `consequential` (high stakes), `tool failed`, `after write`, `change request`, `Jev unavailable`, `invalid Jev answer`, or `Jev choice` (Jev picked your level). A pinned session, an excluded model, or a request with no room below your level gets no line, because Switchyard did no work.

The line is added to the reply you see. It is not stored in the conversation history, so the model does not see it on later turns. Delegated and background turns never get the line. If the line fails, the reply is sent unchanged.

### Saved-token estimate

The `saved` figure is an estimate from **measured** token counts only. Switchyard reads `usage` from the Hermes `post_api_request` hook (no text, only token counts) for each foreground request it saw:

1. A request sent at your level adds one baseline sample for that model and level (last 20 samples per session).
2. For a lowered request, when the session has at least 3 baseline samples for the same model and level, the estimate is `median(baseline) − measured`. It uses reasoning tokens when every baseline sample reports them; otherwise it uses output tokens, and the line says which.
3. The turn shows the figure only when every lowered request in the turn was measured. A negative or zero result shows `no reasoning tokens saved (est.)`.

With fewer than 3 baseline samples, or no usage from the host, Switchyard shows no figure. It never invents a number. The figure is an estimate: a routine message sent at your level could have used fewer tokens than the median baseline.

### Session summary

`/switchyard effort summary` (also part of `status`) is computed from in-memory counters only. It makes no network call and no Jev call:

```text
Switchyard effort summary (this session)
  turns: 4
  requests: lowered 1, kept 3, raised 0, not adapted 0
  Jev calls: 3, p50 190 ms, p95 240 ms
  local decisions (no Jev call): 1
  cached reuses: 0
  estimated tokens saved: ~500 output (est., 1 of 1 lowered requests measured)
```

`local decisions` counts trivial turns decided locally (`local_trivial`); they are not Jev calls and add no latency sample. `not adapted` counts requests Switchyard passed through (pinned, excluded model, no room, no host effort). Without a measured baseline the last line reads `estimated tokens saved: unknown (no measured baseline yet)`.

Example `status` lines for the last decisions (up to 5 are kept):

```text
  last 2 decisions (cap -> sent, reason, Jev latency):
    high -> low, jev_selected, 238 ms
    high -> high, kept_requested_after_write, no call
```

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `adaptive_reasoning_effort` | `true` | Set `false` to disable the adapter. |
| `adaptive_reasoning_effort_mode` | `auto` | Start mode for new sessions: `auto` or `pinned`. |
| `adaptive_reasoning_effort_exclude_models` | `[]` | Model name patterns (fnmatch, case-insensitive) the adapter never touches, for example `["*astra*"]`. No Jev calls for these models. |
| `adaptive_reasoning_effort_allow_raise` | `false` | When `true`, `auto` may go one level above your level while the latest tool call failed. It drops back after the next successful tool call or new turn. |
| `adaptive_reasoning_effort_deadline_seconds` | `0.4` | Jev budget per choice, configurable from 0.1 to 1.5 s. On timeout your level is sent unchanged. |
| `adaptive_reasoning_effort_step_adaptation` | `true` | Allow bounded step-level asks after routine read-only tool rounds (at most one level below your level). Set `false` for one decision per turn. |
| `adaptive_reasoning_effort_receipt_line` | `true` | Add one receipt line to each foreground reply where Switchyard did work. Set `false` to turn it off. |
| `adaptive_reasoning_effort_status_bar` | `true` | In the Hermes TUI, show the level sent next to your level in the status bar (`high→low`). Set `false` to turn it off. |
| `adaptive_reasoning_effort_default` | `medium` | Deprecated and unused since 0.5.4. |

Example:

```text
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_exclude_models '["*astra*"]'
hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort_mode pinned
```

Settings are read when the plugin loads. Start a new session (and restart the gateway for messaging platforms) after a change.

## Records and statistics

Every decision appends one closed-set record to `effort-history.jsonl` in the plugin data directory (last 2,000 records, 1 MiB): session and turn ID, model, mode, requested level, sent level, cap, reason code, whether Jev was called, Jev latency, confidence, and stakes score, and the stuck flag. No message text is stored.

`hermes switchyard stats [--since 24h]` includes a `reasoning_effort` section: levels requested and sent, how often effort was lowered, unchanged or raised, reasons, Jev calls per request, and Jev latency p50/p95.

Read the level that was actually sent from these records or a request dump. The TUI reasoning label shows your setting, not the value on the wire.

## Hermes seam

Requires Hermes 0.21.4 or later: `PluginContext.register_middleware("llm_request", ...)` plus `hermes_cli.middleware.apply_llm_request_middleware`. Hosts without that API record `noop_seam_unavailable` and do not change requests. The `/switchyard` command is registered only when the host exposes `register_command`.

Model routing (`jev_model_route`) stays advisory (`applied: false`).

## TUI status bar

The Hermes TUI status bar shows the model and a reasoning label. Hermes builds the label from two session fields: `reasoning_effort` (your `/reasoning` level) and `reasoning_effort_wire` (the level sent). When they differ, the label reads `high→low`.

Switchyard changes only the level in each request, so Hermes alone would always show your level. With `adaptive_reasoning_effort_status_bar` on (default), Switchyard re-emits the TUI session information with `reasoning_effort_wire` set to the level it sent:

- `hi` or `thanks` at `/reasoning high`: the label reads `high→low`.
- A consequential request: the label reads `high`.
- The label changes at the first model request of a turn and stays until the next change.

Rules:

- Foreground session only. A delegated child never changes the label.
- Your `/reasoning` setting and `agent.reasoning_config` never change (#118).
- It acts only when the Hermes TUI or Desktop gateway module is already loaded, and never imports it. The CLI and messaging platforms show only the receipt line.
- It uses private Hermes gateway names (`_sessions`, `_session_info`, `_emit`). If a future Hermes renames them, the label shows your level again and requests are not affected.
- It runs off the request thread and ignores every error.

## Receipts

`hermes switchyard status --json` includes `reasoning_effort_adapter` with the effective settings. `last_receipt()` in `hermes_switchyard.reasoning_effort_adapter` returns the last decision with `status`, `effort` (the level sent), `requested_effort`, `cap`, `mode`, `reason_code` and `applied`.

Reason codes: `jev_selected`, `jev_step_selected`, `local_trivial` (with `jev_called: false`), `cached`, `cached_unchanged`, `no_room`, `pinned`, `excluded_model`, `no_host_effort`, `reasoning_disabled`, `unsupported_route`, `disabled`, `invalid_choice`, `kept_requested_on_jev_failure`, `kept_requested_ack_required`, `kept_requested_no_task_text`, `kept_requested_restricted_text`, `kept_requested_high_stakes`, `kept_requested_after_tool_failure`, `kept_requested_after_write`, `kept_requested_metadata_change_request`, `kept_requested_metadata_high_stakes`. A metadata-only decision also has `scan_reason` `metadata_only`.

Since 0.5.5, `pinned_by_user_change` is not used: a `/reasoning` change sets a new cap and keeps `auto`.
