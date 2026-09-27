# Adaptive reasoning effort

Switchyard can lower Hermes `reasoning_effort` for routine steps. Your `/reasoning` level is the cap by default; `adaptive_reasoning_effort_allow_raise` explicitly permits one higher level after a failed tool call.

## Behavior

- **Your level is the default cap.** Switchyard reads the level from each request (your `/reasoning` setting after Hermes' per-model clamp). In `auto` mode it asks Jev to pick a level from the ones at or below it for the provider. When `adaptive_reasoning_effort_allow_raise` is enabled and the latest tool call failed, it may offer one higher wire level.
- **No room, no call.** When no lower level exists for the route (for example `low` on OpenAI Codex models), Switchyard does not call Jev and sends your level unchanged.
- **Manual changes pin.** If you change `/reasoning` mid-session on the same model, the session switches to `pinned` and your level is sent unchanged until you run `/switchyard effort auto`.
- **Model switches re-baseline.** A model switch or fallback reads the new level and keeps the current mode.
- **Fail closed to your level.** On a Jev timeout, error, invalid or missing answer, or missing data acknowledgement, your level is sent unchanged.
- **The current message decides.** Jev answers two questions about your current message: an effort choice and a `stakes` score from 0 to 1. Code, not Jev, applies the result. A stakes score of 0.5 or more never lowers your level. After a failed tool call, the level is not lowered below yours. The 0.5 threshold is provisional; it is not a measured accuracy guarantee.
- **Fresh context per turn.** Tool outcomes and the stuck flag reset at each new user turn. The stuck flag is the latest tool call's result only.
- **Fewer calls.** Jev is asked only when the context changes (new turn, a tool outcome that changes the stuck flag, or a new cap). Same-context retries reuse the last choice.
- **Messages are never rewritten**, so the Hermes prompt cache stays valid. Only request-scoped effort fields change.
- **Delegated tasks keep their own state.** A subagent or background fork that shares the session ID but runs under a different task ID gets its own level, model, mode, cached choice, and tool outcomes. It never changes the foreground cap or mode, and `/switchyard effort` does not read or change it.
- **A pending mode survives a compression rotation.** If compression moves the session to a new ID before the first request of a turn, the turn keeps its original task ID. A `pin` or `auto` that you set before that message is still applied, because the controller also accepts the task ID that holds the pending mode. If you change the mode more than once before that request, only the last command applies, even when the commands were stored under IDs from before and after the rotation. Limit: without a pending mode or prior foreground request, a fresh controller cannot identify that first rotated request as foreground, even when a session key is bound. It keeps separate state under the normal cap and `allow_raise` rules; `/switchyard effort status` may show no model request until a later foreground turn.

## Data sent to Jev

When adaptive effort is on and `public_or_sanitized_data_ack` is `true`, Switchyard sends **bounded text from your current message** to the configured Jev provider. This is how Jev tells a greeting from a short but consequential request.

What Jev receives:

- `current_request`: your current message as Hermes gives it to the `pre_llm_call` hook, before memory and plugin context are added. Only text is sent. For a message with parts, only parts of type `text` or `input_text` are sent; image, file, document, and tool-result parts are never sent, even when they carry a `text` field. Messages longer than 1,200 characters are cut to the first and last parts. Messages longer than 16,000 characters stay local: Switchyard does not scan, cut, or send them, and keeps your level.
- `turn_phase`, `recent_tool_statuses` (`ok`, `error`, `failed`, or `unknown`), and `latest_tool_failed`.
- The candidate levels.

What Jev never receives: conversation history, assistant replies, the system prompt, memory context, plugin context, loaded skills, tool names, tool arguments, tool results, file paths, and the provider request. Switchyard never reads task text from the provider request, because that request can contain memory context and tool results.

Switchyard keeps your level and makes **no** Jev call when:

- it did not capture your current message for this turn (for example, a delegated subagent turn, a background turn, or a host without `pre_llm_call`);
- the message is empty, has no text part, has an unknown shape, or is longer than 16,000 characters;
- a local scan finds a secret-like value, payment or verification data, a contact identifier, a restricted marking (for example `proprietary` or `company confidential`), control characters, prompt injection, or a structured payload. The whole message is scanned before it is cut, and the cut text is scanned again.

Secret-like values include a value assigned to a secret-like name (for example `DB_PASSWORD=…`, `OPENAI_API_KEY: …`, `"api_key": "…"`, or `--api-key …`), a password in a URL (`scheme://user:…@host`), an `Authorization:` header value, and known token prefixes. A name alone, a placeholder such as `<your-password>` or `$OPENAI_API_KEY`, and a plain number such as `MAX_TOKENS=4096` are not values. The scan is conservative, so some public text that looks like an assignment (for example `token: required`) also stays local.

The local scan is **not** data loss prevention. It checks values and markings, not topics, so a public question about password hashing is still sent. It cannot detect unmarked private or employer text. Do not enter that text in a session with adaptive effort on.

To send no message text: set `adaptive_reasoning_effort` to `false` (this is the privacy opt-out for this feature), or set `public_or_sanitized_data_ack` to `false`. Adaptive effort then sends your level unchanged. Disabling adaptive effort does not change the separate automatic skill feature.

Switchyard keeps the captured text only in memory for the current turn and clears it when the turn ends. It is never written to receipts, the history file, or logs.

## Command

```text
/switchyard effort status   show mode, your level, last level sent, and why
/switchyard effort pin      send your /reasoning level unchanged in this session
/switchyard effort auto     let Switchyard lower effort for routine steps again
```

`auto` uses your current level as the new cap. The command acts on the session it runs in. Before the session's first model request, it applies from the first message.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `adaptive_reasoning_effort` | `true` | Set `false` to disable the adapter. |
| `adaptive_reasoning_effort_mode` | `auto` | Start mode for new sessions: `auto` or `pinned`. |
| `adaptive_reasoning_effort_exclude_models` | `[]` | Model name patterns (fnmatch, case-insensitive) the adapter never touches, for example `["*astra*"]`. No Jev calls for these models. |
| `adaptive_reasoning_effort_allow_raise` | `false` | When `true`, `auto` may go one level above your level while the latest tool call failed. It drops back after the next successful tool call or new turn. |
| `adaptive_reasoning_effort_deadline_seconds` | `1.5` | Jev budget per choice. On timeout your level is sent. |
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

## Receipts

`hermes switchyard status --json` includes `reasoning_effort_adapter` with the effective settings. `last_receipt()` in `hermes_switchyard.reasoning_effort_adapter` returns the last decision with `status`, `effort` (the level sent), `requested_effort`, `cap`, `mode`, `reason_code` and `applied`.

Reason codes: `jev_selected`, `cached`, `no_room`, `pinned`, `pinned_by_user_change`, `excluded_model`, `no_host_effort`, `reasoning_disabled`, `unsupported_route`, `disabled`, `kept_requested_on_jev_failure`, `kept_requested_ack_required`, `invalid_choice`, `cached_unchanged`.
