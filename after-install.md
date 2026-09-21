# Hermes Switchyard — next steps

The plugin is installed. Jev tool calls are on. Hermes owns data classification; this plugin does not.

1. Save exactly one Jev provider key with a masked prompt:
   `hermes switchyard setup --provider typesafe`
   or
   `hermes switchyard setup --provider openrouter`
   Do not put a key in a command argument, URL, or chat.

2. Start a fresh Hermes session. If you use the gateway, run:
   `hermes gateway restart`

`public_or_sanitized_data_ack` is on by default. Callers may omit it. Turn it off with:
`hermes config set plugins.entries.hermes-switchyard.settings.public_or_sanitized_data_ack false`

Automatic skill routing is local-only by default. To opt into hosted automatic routing, set the mode explicitly and acknowledge the bounded public/sanitized-data contract:
`hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode hosted_sanitized`
`hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack true`

Optional extras:

- `jev_computer_use` uses a local Chromium-family browser for public web goals. Desktop GUI still needs Hermes computer_use (Cua Driver).
- Plugin Doctor reports registration only. Per-session callable exposure is different: run
  `hermes switchyard status --json`
  and, for an explicit pin,
  `hermes switchyard status --json --toolsets "computer_use,terminal"`.
- `jev_computer_use` is callable only when the `computer_use` toolset is selected. Decision tools need `hermes_switchyard`. Ensure both without widening unrelated tools:
  `hermes switchyard ensure-toolsets`
  Setup also runs that ensure after saving a key. On Windows PowerShell, quote pins:
  `hermes -t "computer_use,hermes_switchyard" chat`
- Native `computer_use` fallback is not a Jev success. If `jev_computer_use` is missing from the session catalog, fix toolsets before treating the turn as Switchyard computer use.

Re-read this list anytime:

```text
hermes switchyard guide
```
