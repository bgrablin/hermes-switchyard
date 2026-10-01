# Hermes Switchyard is installed. Two steps to finish

1. Save one Jev provider key. You type it into a masked prompt:

   hermes switchyard setup --provider typesafe
   or
   hermes switchyard setup --provider openrouter

   Never put a key in a command argument, URL, or chat. Setup also turns on
   the `computer_use` and `hermes_switchyard` toolsets for you (it runs
   `hermes switchyard ensure-toolsets`). If Hermes' coding focus mode is on,
   it overrides that list. Pin `-t computer_use,hermes_switchyard` instead,
   and confirm with `hermes switchyard status --json`.

2. Start a fresh Hermes session. If you use the messaging gateway, restart it:

   hermes gateway restart

That's it. No other configuration is needed.

## What is now on

- Adaptive reasoning effort: easy turns may go out at lower effort.
  Your `/reasoning` level is always the cap. On turns where Switchyard decides
  something, a short `Reasoning: …` line under the reply shows what was sent. This needs Hermes 0.21.4 or newer.
- Automatic skill routing: Switchyard may pick and load one matching skill
  per turn.
- Seven Jev tools the model can call, including `jev_computer_use`.

`jev_model_route` only recommends a model. It never switches it.

## What gets sent to Jev

Both automatic features may send a bounded, secret-scrubbed excerpt of your
current message (never history, memory, tool output, or files) to your Jev
provider. Only use them with public or sanitized content. Hermes, not this
plugin, is responsible for classifying your data.

Keep skill routing on your machine:

    hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_routing_mode local_only

Suggest skills without loading them:

    hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_consumer_mode advisory

Stop hosted skill routing only (tool calls are unaffected):

    hermes config set plugins.entries.hermes-switchyard.settings.automatic_skill_public_or_sanitized_data_ack false

Stop adaptive effort (this also stops sending message text for it):

    hermes config set plugins.entries.hermes-switchyard.settings.adaptive_reasoning_effort false

Refuse all Jev tool calls. This also stops adaptive effort from sending
message text. The setting `public_or_sanitized_data_ack` is on by default:

    hermes config set plugins.entries.hermes-switchyard.settings.public_or_sanitized_data_ack false

Even with acknowledgement on, automatic routing runs a local scan that keeps
restricted or marked content local when Hermes supplies no policy for the turn.
Text Hermes explicitly authorizes skips that scan. A host policy that denies
hosting, or that is malformed, always wins. Explicit Jev tools get no scan,
only the acknowledgement check, so their inputs must already be safe to send.

## Handy commands

    /switchyard effort status | pin | auto     inspect or change effort for this session
    hermes switchyard status --json            what a fresh session will see (no network)
    hermes switchyard status --json --toolsets "computer_use,terminal"
                                               the same, for an explicit toolset pin
    hermes switchyard ensure-toolsets          re-enable the two toolsets if tools are missing

## Good to know

- Plugin Doctor checks registration only. Use `status` to see whether a session
  can actually call the tools.
- On Windows PowerShell, quote toolset pins:
  hermes -t "computer_use,hermes_switchyard" chat
- `jev_computer_use` uses a local Chromium-family browser for public web goals.
  Desktop apps need Hermes' own `computer_use` (Cua Driver).
- If Hermes falls back to its native `computer_use`, that is not a Switchyard
  run. If `jev_computer_use` is missing from the session, fix the toolsets first.

Show this guide again anytime:

    hermes switchyard guide

Full documentation: README.md and docs/README.md in the plugin folder.
