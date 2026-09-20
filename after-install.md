# Hermes Switchyard — next steps

The plugin is installed. Full function still needs these operator steps; they are not discovered later.

1. Save exactly one Jev provider key with a masked prompt:
   `hermes switchyard setup --provider typesafe`
   or
   `hermes switchyard setup --provider openrouter`
   Do not put a key in a command argument, URL, or chat.

2. Start a fresh Hermes session. If you use the gateway, run:
   `hermes gateway restart`

3. Every Jev tool call must set `public_or_sanitized_data_ack: true` after you review the data. That flag is never set automatically.

Optional, only if you want those extras:

- Hosted automatic skill routing stays off until `automatic_skill_public_or_sanitized_data_ack` is true.
- `jev_computer_use` also needs Hermes computer_use (Cua Driver) on Windows, macOS, or Linux.

Re-read this list anytime:

```text
hermes switchyard guide
```
