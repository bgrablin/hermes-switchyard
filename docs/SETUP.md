# Setup

Hermes Switchyard has two separate setup boundaries:

1. Network access to download the public GitHub repository.
2. TypeSafe or OpenRouter access for live Jev decisions.

A ChatGPT or Codex subscription is separate and does not pay Jev or OpenRouter request charges.

## Requirements

For skill selection, model routing, and `jev_assess`:

- Hermes Agent with the native plugin contract: `plugin.yaml`, a root `__init__.py`, and `register(ctx)`.
- Python 3.11 or newer for the repository's offline checks.
- Network access to `github.com/bgrablin/hermes-switchyard`.
- Either a TypeSafe API key in the active profile as `TYPESAFE_API_KEY`, or an OpenRouter API key as `OPENROUTER_API_KEY`.
- Enough account credit or current allowance for the selected route.

For `jev_computer_use`, Hermes must have the Cua Driver-backed `computer_use` tool available on Windows, macOS, or Linux and the target application must be installed. The plugin delegates desktop I/O to Hermes; it does not ship a second driver.

The supported endpoints are `https://api.typesafe.ai/v1/systemone` and `https://openrouter.ai/api/alpha/decisions`. `jev_provider: auto` prefers direct TypeSafe when its key exists.

## Install

Install and enable the plugin with the supported one-liner:

```text
hermes plugins install bgrablin/hermes-switchyard --enable
```

Both provider keys are optional alternatives, so installation does not prompt for either one. To install without enabling first:

```text
hermes plugins install bgrablin/hermes-switchyard --no-enable
hermes plugins list
hermes plugins enable jev-decision
```

Start a fresh Hermes session after installation or an update.

## Add a Jev key safely

Run exactly one provider-specific setup command and enter the key only in its masked prompt:

```text
hermes jev-decision setup --provider typesafe
# or
hermes jev-decision setup --provider openrouter
```

Do not put a key in `hermes auth add`, a command argument, URL, fixture, repository file, or issue report. Check plugin availability without displaying keys:

```text
hermes plugins list --enabled
```

Profiles do not share secrets automatically. After adding or changing a key, start a fresh Hermes session.

## Confirm the plugin

List enabled plugins:

```text
hermes plugins list --enabled
```

Run the native plugin check from the plugin root with a fresh temporary Hermes home when you do not want to change a live profile:

```text
HERMES_HOME="$(mktemp -d)" hermes plugins doctor . --ci
```

On Windows, set `HERMES_HOME` to a new temporary directory using the shell's normal environment-variable syntax. Plugin Doctor imports and registers plugin code in-process, so it checks the real loader but is not a sandbox. Use it only with reviewed code.

A successful native check proves discovery and registration, not model quality, GUI completion, or permission to send private data.

## Configure the plugin

The plugin settings are profile-scoped under `plugins.entries.jev-decision.settings`:

```text
hermes config set plugins.entries.jev-decision.settings.jev_provider auto
hermes config set plugins.entries.jev-decision.settings.computer_max_steps 100
```

Leave `jev_model` empty to use the provider default. Direct TypeSafe uses `jev-latest`; OpenRouter uses `typesafe/jev-1.13`.

If an earlier setup pinned the TypeSafe-only alias while using `auto`, remove it so provider-specific defaults work:

```text
hermes config unset plugins.entries.jev-decision.settings.jev_model
```

Some Windows text-entry actions use the host-owned Hermes text model. If Hermes has no configured model, choose one through the normal interactive command:

```text
hermes model
```

That model is separate from Jev. A Codex login can supply Hermes' host model when configured, but it does not supply a TypeSafe or OpenRouter account, key, credit, or Jev access.

## Privacy requirements

Before a Jev tool runs, the invoking code must set `public_or_sanitized_data_ack: true`. This confirms that the data was reviewed before it is sent. It is not a scan, a redaction guarantee, data-loss-prevention control, authorization to share, or permission to bypass another control.

For a Cua Driver request, the decision state can include the goal, target application, window title, safe controls, visible context, and recent actions. Text entry can also send selected field context to the configured Hermes text model. Do not send private, employer, regulated, credential, password, API-key, token, payment, or verification-code data.

## What is supported

- `jev_assess` validates public/sanitized Choice, Score, and Noul answers and batches large independent question maps into bounded requests.
- `jev_skill_select` searches the full supplied catalog through partitioned Choices. It does not load the skill.
- `jev_model_route` filters and ranks explicit candidate metadata. It does not change the active model or use a fallback provider.
- `jev_computer_use` runs bounded actions through Hermes' Cua Driver-backed tool on Windows, macOS, and Linux. It rechecks targets before acting and returns `verified: false` until Hermes independently checks the result.
- Jev may abstain. A confidence value is not a correctness guarantee.

## Missing-key symptoms and recovery

If neither `TYPESAFE_API_KEY` nor `OPENROUTER_API_KEY` is available, Hermes can disable the plugin during loading and the Jev tools will not appear as available. If a request reaches the plugin without a key, it fails closed with a generic request-validation error rather than exposing credential details.

Recover without changing code:

1. Run `hermes jev-decision setup --provider typesafe` or use `--provider openrouter` and enter the key only in the masked prompt.
2. Start a fresh Hermes session.
3. Run `hermes plugins list --enabled`.
4. Run `hermes plugins doctor . --ci` from the plugin root if discovery remains unclear.

A Codex login, a different `jev_model` value, or a missing direct key does not fix a missing secret. Choose the provider whose profile secret is present.

## Limits and future work

The release still does not load skills automatically, change the active Hermes model, use provider fallback, claim calibrated correctness, or certify GUI completion independently. Cua Driver remains the host-owned desktop executor; Switchyard adds the Jev decision layer and does not bypass its approval or platform boundaries.
