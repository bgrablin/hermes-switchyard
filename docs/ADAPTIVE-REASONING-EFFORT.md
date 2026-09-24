# Adaptive reasoning effort

Switchyard can lower Hermes `reasoning_effort` for routine steps. Your `/reasoning` level is always the cap.

## Behavior

- **Your level is the cap.** Switchyard reads the level from each request (your `/reasoning` setting after Hermes' per-model clamp). In `auto` mode it asks Jev to pick a level from the ones at or below it for the provider. Jev never sees a higher candidate, so it can never send more than you chose.
- **No room, no call.** When no lower level exists for the route (for example `low` on OpenAI Codex models), Switchyard does not call Jev and sends your level unchanged.
- **Manual changes pin.** If you change `/reasoning` mid-session on the same model, the session switches to `pinned` and your level is sent unchanged until you run `/switchyard effort auto`.
- **Model switches re-baseline.** A model switch or fallback reads the new level and keeps the current mode.
- **Fail closed to your level.** On a Jev timeout, error, invalid choice, or missing data acknowledgement, your level is sent unchanged.
- **Fresh context per turn.** Tool outcomes and the stuck flag reset at each new user turn. The stuck flag is the latest tool call's result only.
- **Fewer calls.** Jev is asked only when the context changes (new turn, a tool outcome that changes the stuck flag, or a new cap). Same-context retries reuse the last choice.
- **No text leaves the host.** Jev receives the requested level, the candidates, a task length bucket, tool outcome status values, and the stuck flag. It never receives task or tool text.
- **Messages are never rewritten**, so the Hermes prompt cache stays valid. Only request-scoped effort fields change.

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

Every decision appends one closed-set record to `effort-history.jsonl` in the plugin data directory (last 2,000 records, 1 MiB): session and turn ID, model, mode, requested level, sent level, cap, reason code, whether Jev was called, Jev latency and confidence, and the stuck flag. No text is stored.

`hermes switchyard stats [--since 24h]` includes an `adaptive_reasoning_effort` section: levels requested and sent, how often effort was lowered, unchanged or raised, reasons, Jev calls per request, and Jev latency p50/p95.

Read the level that was actually sent from these records or a request dump. The TUI reasoning label shows your setting, not the value on the wire.

## Hermes seam

Requires Hermes 0.21.4 or later: `PluginContext.register_middleware("llm_request", ...)` plus `hermes_cli.middleware.apply_llm_request_middleware`. Hosts without that API record `noop_seam_unavailable` and do not change requests. The `/switchyard` command is registered only when the host exposes `register_command`.

Model routing (`jev_model_route`) stays advisory (`applied: false`).

## Receipts

`hermes switchyard status --json` includes `reasoning_effort_adapter` with the effective settings. `last_receipt()` in `hermes_switchyard.reasoning_effort_adapter` returns the last decision with `status`, `effort` (the level sent), `requested_effort`, `cap`, `mode`, `reason_code` and `applied`.

Reason codes: `jev_selected`, `cached`, `no_room`, `pinned`, `pinned_by_user_change`, `excluded_model`, `no_host_effort`, `reasoning_disabled`, `unsupported_route`, `disabled`, `kept_requested_on_jev_failure`, `kept_requested_ack_required`, `invalid_choice`, `cached_unchanged`.
