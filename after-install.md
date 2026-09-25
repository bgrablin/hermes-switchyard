# Hermes Switchyard — next steps

The plugin is installed. Automatic hosted skill routing (`hosted_sanitized` + `load` + standing acknowledgement) and Jev tool calls are on by default. Hermes owns data classification; this plugin does not.

1. Save exactly one Jev provider key with a masked prompt:
   `hermes switchyard setup --provider typesafe`
   or
   `hermes switchyard setup --provider openrouter`
   Do not put a key in a command argument, URL, or chat.
   Setup also runs `hermes switchyard ensure-toolsets` so `computer_use` and `hermes_switchyard` are selectable without a separate toolsets step.

2. Start a fresh Hermes session. If you use the gateway, run:
   `hermes gateway restart`

That is the happy path. No further `hermes config set` commands are required for automatic features.

## Opt out / privacy

To keep recommendations local-only (no hosted Jev for automatic routing):

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only
```

To deliver advisory context without auto-loading a skill:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode advisory
```

To refuse hosted automatic routing while leaving tool-call acknowledgement alone:

```text
hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack false
```

Tool-call `public_or_sanitized_data_ack` is also on by default. Callers may omit it. Turn it off with:

```text
hermes config set plugins.entries.hermes-switchyard.settings.public_or_sanitized_data_ack false
```

Hosted automatic routing uses standing acknowledgement when the host does not forward a `turn_egress_policy` allow envelope (`egress_authority: standing_ack`) and the local restricted-pattern scan is clean. Explicit deny, unknown, malformed, or restricted envelopes, and restricted local scans, still fail closed.

## Optional extras / troubleshooting

- `jev_computer_use` uses a local Chromium-family browser for public web goals. Desktop GUI still needs Hermes computer_use (Cua Driver).
- Plugin Doctor reports registration only. Per-session callable exposure is different: run
  `hermes switchyard status --json`
  and, for an explicit pin,
  `hermes switchyard status --json --toolsets "computer_use,terminal"`.
- If tools are missing from a session catalog after setup, re-run
  `hermes switchyard ensure-toolsets`
  On Windows PowerShell, quote pins:
  `hermes -t "computer_use,hermes_switchyard" chat`
- Native `computer_use` fallback is not a Jev success. If `jev_computer_use` is missing from the session catalog, fix toolsets before treating the turn as Switchyard computer use.

Re-read this list anytime:

```text
hermes switchyard guide
```

Adaptive reasoning effort is on after install (Hermes ≥ 0.21 `llm_request` middleware).
Your `/reasoning` level is the cap by default. Jev may lower it for routine steps; a failed Jev call keeps your level. Use `/switchyard effort status|pin|auto` to inspect or change the session mode. Only an explicit `adaptive_reasoning_effort_allow_raise: true` allows one higher level after a failed tool call.
Disable: `hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false`.
`jev_model_route` stays advisory — it does not switch models.

