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

Hosted automatic skill routing is on by default. Turn it off with:
`hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack false`

Optional extras:

- `jev_computer_use` uses a local Chromium-family browser for public web goals. Desktop GUI still needs Hermes computer_use (Cua Driver).

Re-read this list anytime:

```text
hermes switchyard guide
```
